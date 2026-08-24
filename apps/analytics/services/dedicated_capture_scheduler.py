"""V3.2 forward-validation dedicated capture scheduler (2026-08-24).

Application-owned scheduler for `capture_v3_2_validation`. Spawned
from `AnalyticsConfig.ready()` when the process is a web/WSGI runtime
(gated to skip tests, migrations, and one-off management commands).

WHY THIS EXISTS (constraints from CLAUDE.md + repo inspection)

  * No railway.toml / Procfile / Dockerfile / Nixpacks config in the
    repo. Railway's start command is a UI-configured single gunicorn
    invocation — the repository has NO deployment-config knob for
    cron scheduling.
  * Railway Cron Jobs are provisioned via the Railway UI, not from
    code. Nothing in the repo can add or edit them.
  * refresh_data currently fires ~every 6 hours in production; that
    cadence cannot support the 30-min canonical capture window.
  * No existing background scheduler framework (no APScheduler,
    Celery, Redis). Introducing one for a single 10-min tick would
    add a major dependency for a small need.

  Therefore the correct implementation is a lightweight background
  daemon thread inside the Django process, coordinated across gunicorn
  workers via PostgreSQL advisory locks.

SAFETY DESIGN

  * `pg_try_advisory_lock(<magic_id>)` — non-blocking. Only ONE
    worker succeeds per tick even when N workers race. Released in
    a `try/finally` so a mid-tick crash never leaks the lock.
  * Interval check happens INSIDE the lock so no race can double-fire.
  * `capture_v3_2_validation`'s existing `(mlb_game, engine_version)`
    unique constraint guarantees per-snapshot idempotence regardless
    of any scheduler race we might miss.
  * Daemon thread — dies with the process on shutdown. No cleanup
    required.
  * Polls every POLL_SECONDS (60s); actual capture fires at
    INTERVAL_SECONDS (600s = 10 min) cadence.
  * Broad exception handling in the loop — one bad tick never kills
    the scheduler thread.

GATING

  Spawned only when we're in a "long-running" context (gunicorn or
  runserver). Skipped for:
    * `manage.py test` / migrate / makemigrations / collectstatic /
      shell / dbshell / ensure_seed / ensure_superuser / etc.
    * Env var `DISABLE_V3_2_SCHEDULER=true` (test/CI override).
    * Non-web sys.argv[0] patterns.

  Rationale: a `manage.py migrate` invocation lasts a second or two —
  spawning a scheduler thread there is pointless and could log
  spurious CronRunLog rows that break test isolation.

SQLITE DEV FALLBACK

  SQLite doesn't support advisory locks. Development uses a single
  gunicorn worker (or runserver, which is single-threaded), so the
  interval check alone is safe. On PostgreSQL production the lock
  adds the multi-worker guarantee.

OBSERVABILITY

  Every capture the scheduler fires produces a CronRunLog row keyed
  on `capture_v3_2_validation` (via the existing cron_run_log
  wrapper in the command's handler). The forward-health report's
  DEDICATED cadence block reads those rows.
"""
from __future__ import annotations

import logging
import os
import sys
import threading
import time
from typing import Optional

logger = logging.getLogger(__name__)


# --- Scheduler config (pre-registered, do NOT tune) ---
POLL_SECONDS = 60          # how often the loop wakes to check
INTERVAL_SECONDS = 600     # 10 min — target capture cadence
STARTUP_DELAY_SECONDS = 30 # let the app finish booting before first tick
# PostgreSQL advisory lock id. Arbitrary 32-bit int unique to this
# scheduler. NEVER share this id with another lock caller in the app.
PG_ADVISORY_LOCK_ID = 901102401

_shutdown_event = threading.Event()
_scheduler_thread: Optional[threading.Thread] = None


# ---------------------------------------------------------------------------
# Gating


_MANAGE_COMMANDS_TO_SKIP = frozenset({
    'test', 'migrate', 'makemigrations', 'showmigrations', 'sqlmigrate',
    'squashmigrations', 'collectstatic', 'shell', 'dbshell', 'check',
    'dumpdata', 'loaddata', 'createsuperuser', 'changepassword',
    'ensure_superuser', 'ensure_seed',
    # Any one-off ingest/backfill commands the operator runs manually
    # should not double as scheduler starts.
    'capture_v3_2_validation', 'refresh_data', 'ingest_odds',
    'ingest_schedule', 'ingest_injuries', 'ingest_pitcher_stats',
    'ingest_team_records', 'ingest_lineups', 'ingest_team_batting',
    'ingest_bullpen_snapshots', 'ingest_reliever_appearances',
    'backfill_bullpen_snapshots', 'bullpen_daily_refresh',
    'capture_snapshots', 'resolve_outcomes', 'settle_mockbets',
    'update_elo_ratings', 'prune_old_raw_snapshots',
    'diagnose_mlb_odds_gaps',
})


def should_start_scheduler() -> bool:
    """Return True iff we're in a long-running WSGI/dev-server context.

    Env override: `DISABLE_V3_2_SCHEDULER=true` disables regardless.
    """
    if os.environ.get('DISABLE_V3_2_SCHEDULER', '').lower() in (
            'true', '1', 'yes'):
        return False
    argv = sys.argv or []
    if len(argv) == 0:
        return False
    argv0 = os.path.basename(argv[0] or '')
    # gunicorn boot: argv[0] typically ends with 'gunicorn'.
    if 'gunicorn' in argv0:
        return True
    # manage.py runserver — the only management command we run as
    # a long-lived process.
    if argv0 == 'manage.py':
        cmd = argv[1] if len(argv) > 1 else ''
        if cmd == 'runserver':
            return True
        if cmd in _MANAGE_COMMANDS_TO_SKIP:
            return False
        # Unknown management command: err on the side of skipping.
        return False
    # Pytest / other test runners.
    if 'pytest' in argv0 or 'py.test' in argv0:
        return False
    # Default: only start when we recognize a web runtime.
    return False


# ---------------------------------------------------------------------------
# Tick — the single decision point.


def _lock_and_run_pg() -> bool:
    """Acquire pg_try_advisory_lock, check interval, run capture.

    Returns True iff a capture actually fired this tick."""
    from django.db import connection
    lock_acquired = False
    try:
        with connection.cursor() as cur:
            cur.execute('SELECT pg_try_advisory_lock(%s)',
                        [PG_ADVISORY_LOCK_ID])
            row = cur.fetchone()
            lock_acquired = bool(row and row[0])
        if not lock_acquired:
            return False
        # Interval check happens INSIDE the lock so no race.
        if not _interval_elapsed():
            return False
        _invoke_capture()
        return True
    finally:
        if lock_acquired:
            try:
                with connection.cursor() as cur:
                    cur.execute('SELECT pg_advisory_unlock(%s)',
                                [PG_ADVISORY_LOCK_ID])
            except Exception:
                logger.exception('v3_2 scheduler: failed to release '
                                 'advisory lock')


def _run_no_lock() -> bool:
    """SQLite dev path — no advisory lock. Safe because dev uses a
    single worker."""
    if not _interval_elapsed():
        return False
    _invoke_capture()
    return True


def _interval_elapsed() -> bool:
    """True iff no capture_v3_2_validation CronRunLog row has started
    within the last INTERVAL_SECONDS."""
    from django.utils import timezone
    from datetime import timedelta
    from apps.ops.models import CronRunLog
    cutoff = timezone.now() - timedelta(seconds=INTERVAL_SECONDS)
    exists_recent = CronRunLog.objects.filter(
        command='capture_v3_2_validation',
        started_at__gte=cutoff,
    ).exists()
    return not exists_recent


def _invoke_capture() -> None:
    """Fire the management command via call_command so all existing
    logging + cron_run_log wrapping runs identically to a Railway
    cron invocation."""
    from django.core.management import call_command
    from io import StringIO
    buf = StringIO()
    try:
        call_command('capture_v3_2_validation', trigger='cron', stdout=buf)
        logger.info('v3_2 scheduler tick: %s',
                    buf.getvalue().strip()[:400])
    except Exception:
        logger.exception('v3_2 scheduler tick: capture_v3_2_validation '
                         'raised')


def scheduler_tick() -> bool:
    """One tick of the scheduler. Returns True iff capture fired.
    Broad exception handling — one bad tick never propagates out."""
    try:
        from django.db import connection
        if connection.vendor == 'postgresql':
            return _lock_and_run_pg()
        return _run_no_lock()
    except Exception:
        logger.exception('v3_2 scheduler tick failed')
        return False


# ---------------------------------------------------------------------------
# Thread body + startup


def _scheduler_loop() -> None:
    """Long-running loop body for the daemon thread."""
    logger.info('v3_2 dedicated capture scheduler starting; '
                'poll=%ds interval=%ds', POLL_SECONDS, INTERVAL_SECONDS)
    # Startup delay so the app finishes booting before we hit the DB.
    _shutdown_event.wait(STARTUP_DELAY_SECONDS)
    while not _shutdown_event.is_set():
        scheduler_tick()
        _shutdown_event.wait(POLL_SECONDS)
    logger.info('v3_2 dedicated capture scheduler stopping')


def start_scheduler_if_appropriate() -> Optional[threading.Thread]:
    """Called from AnalyticsConfig.ready(). Idempotent — a second call
    returns the existing thread reference without starting a duplicate."""
    global _scheduler_thread
    if _scheduler_thread is not None and _scheduler_thread.is_alive():
        return _scheduler_thread
    if not should_start_scheduler():
        logger.info('v3_2 scheduler: not starting in this context '
                    '(argv0=%r)', sys.argv[0] if sys.argv else None)
        return None
    _shutdown_event.clear()
    t = threading.Thread(
        target=_scheduler_loop,
        name='v3_2_capture_scheduler',
        daemon=True,
    )
    t.start()
    _scheduler_thread = t
    return t


def stop_scheduler() -> None:
    """Signal the scheduler to exit. Only used by tests — daemon
    threads die with the process in production."""
    _shutdown_event.set()
