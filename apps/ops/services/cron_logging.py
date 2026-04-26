"""Cron run logging — context manager that wraps a management command run.

Used by both Railway-triggered crons (refresh_data, refresh_scores_and_settle)
and the manual-trigger views in apps.ops.views. The context manager enforces
the running → success | failure | partial state machine and guarantees a row
is closed out even on exceptions.

Why a context manager:
  - Mirrors the natural shape of "I'm starting a thing, then I finish it"
  - Auto-closes on exception with full traceback in error_message
  - Atomic from the caller's POV — the wrapping code can never forget to
    write the completion row

Usage:
    with cron_run_log('refresh_data', trigger='cron') as log:
        ... # do the work
        log.summary = 'refreshed 4 sports'  # optional
        # log.mark_partial('odds failed for golf')  # optional partial path
"""
import logging
import time
import traceback
from contextlib import contextmanager

from django.utils import timezone

logger = logging.getLogger(__name__)

STDOUT_TAIL_LINES = 50  # last N lines of captured stdout to persist


class _CronLogHandle:
    """Mutable handle yielded from cron_run_log(). The wrapped block can set
    .summary, append to .stdout_tail, or call mark_partial() to override the
    default 'success' status that the context manager would otherwise apply.

    Behavior on exceptions: the context manager catches, sets status='failure'
    with the traceback, saves, and re-raises. Manual triggers depend on this
    so the UI can surface the failure.
    """

    def __init__(self, log_row):
        self._row = log_row
        self.summary = ''
        self.stdout_tail = ''
        self._forced_status = None
        self._forced_error = ''

    @property
    def id(self):
        return self._row.id

    @property
    def row(self):
        return self._row

    def mark_partial(self, message=''):
        """Override default success → partial. Use when some sub-tasks failed
        but the run wasn't a total wash (e.g., 3 of 4 sports refreshed)."""
        self._forced_status = 'partial'
        if message:
            self._forced_error = message

    def mark_failure(self, message=''):
        """Force-fail without raising. Useful when a sub-task signals failure
        through a return value rather than an exception."""
        self._forced_status = 'failure'
        if message:
            self._forced_error = message


@contextmanager
def cron_run_log(command, trigger='cron', triggered_by_user=None):
    """Open a CronRunLog row, yield a handle, and close the row on exit.

    Always closes the row, even on exception. Re-raises exceptions after
    recording them so callers see normal error flow.
    """
    # Local import: this module is loaded by management commands at startup
    # and Django apps may not be ready yet at import time.
    from apps.ops.models import CronRunLog

    row = CronRunLog.objects.create(
        command=command,
        trigger=trigger,
        triggered_by_user=triggered_by_user,
        status='running',
    )
    handle = _CronLogHandle(row)
    start = time.time()
    try:
        yield handle
    except Exception:  # noqa: BLE001 — we re-raise after logging
        duration = time.time() - start
        row.status = 'failure'
        row.completed_at = timezone.now()
        row.duration_seconds = round(duration, 3)
        row.error_message = traceback.format_exc()[:8000]
        row.summary = handle.summary or ''
        row.stdout_tail = _tail(handle.stdout_tail)
        row.save()
        raise
    else:
        duration = time.time() - start
        row.status = handle._forced_status or 'success'
        row.completed_at = timezone.now()
        row.duration_seconds = round(duration, 3)
        row.summary = handle.summary or ''
        row.error_message = handle._forced_error or ''
        row.stdout_tail = _tail(handle.stdout_tail)
        row.save()


def _tail(text):
    """Keep only the last STDOUT_TAIL_LINES lines so the row stays bounded."""
    if not text:
        return ''
    lines = text.splitlines()
    if len(lines) <= STDOUT_TAIL_LINES:
        return text[:8000]
    return '\n'.join(lines[-STDOUT_TAIL_LINES:])[:8000]


def is_command_running(command, max_age_seconds=600):
    """True iff there's a recent CronRunLog for `command` still in 'running'.

    The "max_age_seconds" guard is the workaround for crashed runs: a row
    stuck in 'running' for >10 minutes almost certainly means the worker
    died, and we don't want to permanently block manual re-triggering.
    """
    from datetime import timedelta

    from apps.ops.models import CronRunLog

    cutoff = timezone.now() - timedelta(seconds=max_age_seconds)
    return CronRunLog.objects.filter(
        command=command,
        status='running',
        started_at__gte=cutoff,
    ).exists()
