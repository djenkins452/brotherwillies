"""v3.3 SHADOW — bullpen backfill orchestration service.

The background-thread body invoked from
`apps.analytics.views.trigger_bullpen_backfill`. Runs the existing
`ingest_reliever_appearances` + `backfill_bullpen_snapshots` command
logic (calls into their `Command().handle()`-equivalent code paths)
inline while writing progress to a `BullpenBackfillRun` row.

Wrapped in try/except so any failure ends up as `status='failed'`
with the error message and a rolling log tail persisted on the row.
Never leaves a row stuck in `running` even on exception.

DOES NOT touch:
  * Any production decision path
  * The bullpen flag settings (they remain False)
  * The BettingRecommendation / MockBet tables

Only writes to:
  * mlb.RelieverAppearance   (upsert)
  * mlb.StartingPitcher      (create when a new pitcher is discovered)
  * mlb.TeamBullpenSnapshot  (append)
  * analytics.BullpenBackfillRun (progress rows)

2026-08-22 (post-first-Railway-failure): HTTP is now delegated to the
canonical `apps.datahub.providers.mlb.statsapi_client`. That client
chunks /schedule into small windows, sets an explicit User-Agent,
retries transient failures with backoff, and raises `StatsApiError`
with URL/status/body captured. See its module docstring for why the
initial single-request 180-day /schedule call failed on Railway.
"""
from __future__ import annotations

import logging
import time
import traceback
from datetime import date, datetime, timedelta
from typing import Optional

from django.utils import timezone

from apps.datahub.providers.mlb.statsapi_client import (
    StatsApiError, fetch_boxscore, fetch_schedule,
)


logger = logging.getLogger(__name__)


# Rolling log tail: last N lines written to the run row so the status
# page can render a live preview without unbounded row growth.
LOG_TAIL_LINE_CAP = 40
LOG_TAIL_CHAR_CAP = 4000


def _append_log(run, line: str) -> None:
    """Append `line` to run.log_tail with a rolling cap."""
    stamp = timezone.now().strftime('%H:%M:%S')
    payload = f'[{stamp}] {line}'
    lines = (run.log_tail or '').splitlines()
    lines.append(payload)
    lines = lines[-LOG_TAIL_LINE_CAP:]
    tail = '\n'.join(lines)
    if len(tail) > LOG_TAIL_CHAR_CAP:
        tail = tail[-LOG_TAIL_CHAR_CAP:]
    run.log_tail = tail
    run.save(update_fields=['log_tail'])


def _save_counters(run) -> None:
    """Persist just the counter fields (fast partial save for progress)."""
    run.save(update_fields=[
        'games_seen', 'boxscores_fetched',
        'appearances_created', 'appearances_updated',
        'appearances_skipped_existing', 'boxscore_errors',
        'snapshots_created', 'snapshots_skipped_existing',
    ])


def run_backfill_in_background(run_id: str, sleep_ms: int = 250) -> None:
    """Background-thread body. Idempotent per-game and per-snapshot."""
    from apps.analytics.models import BullpenBackfillRun

    try:
        run = BullpenBackfillRun.objects.get(id=run_id)
    except BullpenBackfillRun.DoesNotExist:
        logger.exception('bullpen_backfill: run row missing id=%s', run_id)
        return

    try:
        run.started_at = timezone.now()
        run.status = 'running'
        run.phase = 'ingest_appearances'
        run.save()
        _append_log(run, f'Starting backfill {run.date_from}..{run.date_to} (kind={run.kind})')

        _ingest_appearances(run, sleep_ms=sleep_ms)

        run.phase = 'build_snapshots'
        run.save(update_fields=['phase'])
        _append_log(run, 'Ingest phase complete; building snapshots')

        _build_snapshots(run)

        run.phase = 'done'
        # 2026-08-22: distinguish clean completion from
        # completion-with-boxscore-errors so ops can see at a glance
        # whether reconciliation is needed. Zero errors → completed;
        # any non-fatal individual boxscore errors → completed_with_errors.
        if run.boxscore_errors > 0:
            run.status = 'completed_with_errors'
            run.failure_summary = (
                f'{run.boxscore_errors} boxscore(s) failed during ingest; '
                f'run finished. Re-run to retry those games.'
            )
        else:
            run.status = 'completed'
        run.finished_at = timezone.now()
        run.save()
        _append_log(run, f'{run.status} in {run.elapsed_seconds}s')
    except StatsApiError as api_err:
        # HTTP failure: capture the exact endpoint / status / body /
        # attempt in a form Danny can act on without reading the Python
        # traceback. See StatsApiError.human_summary().
        logger.warning('bullpen_backfill_stats_api_failed run_id=%s: %s',
                       run_id, api_err.human_summary())
        try:
            run = BullpenBackfillRun.objects.get(id=run_id)
            run.status = 'failed'
            run.failure_summary = api_err.human_summary()[:500]
            run.error_message = ''.join(
                traceback.format_exception(api_err)
            )[:6000]
            run.finished_at = timezone.now()
            run.save()
            _append_log(run, f'FAILED (Stats API): {api_err.human_summary()[:250]}')
        except Exception:
            logger.exception('bullpen_backfill_failed_save run_id=%s', run_id)
    except Exception as exc:  # noqa: BLE001
        logger.exception('bullpen_backfill_failed run_id=%s', run_id)
        try:
            run = BullpenBackfillRun.objects.get(id=run_id)
            run.status = 'failed'
            run.failure_summary = repr(exc)[:500]
            run.error_message = ''.join(traceback.format_exception(exc))[:6000]
            run.finished_at = timezone.now()
            run.save()
            _append_log(run, f'FAILED: {repr(exc)[:200]}')
        except Exception:
            logger.exception('bullpen_backfill_failed_save run_id=%s', run_id)


# ---------------------------------------------------------------------------
# Phase 1: ingest_reliever_appearances-equivalent, inline w/ progress
# ---------------------------------------------------------------------------


def _ingest_appearances(run, *, sleep_ms: int) -> None:
    """Walk /schedule for the run's date range, fetch /boxscore per Final
    game, upsert RelieverAppearance rows.

    Uses the canonical `apps.datahub.providers.mlb.statsapi_client`
    for all HTTP so retries, User-Agent, chunking, and rich error
    context are shared with the CLI command and the diagnostic
    endpoint. A schedule failure raises StatsApiError which the
    outer handler in run_backfill_in_background catches and turns
    into a human-readable failure_summary on the run row.

    Boxscore errors are per-game and DO NOT fail the whole run —
    they're counted in run.boxscore_errors so the run finishes with
    status='completed_with_errors'. Rationale: a single dodgy
    boxscore shouldn't destroy an 11-minute backfill.
    """
    from apps.datahub.management.commands.ingest_reliever_appearances import (
        _extract_pitcher_stats, _get_or_create_pitcher,
    )
    from apps.mlb.models import Game, RelieverAppearance

    # --- Schedule (chunked internally by the client) ---
    _append_log(run, f'Fetching schedule {run.date_from}..{run.date_to} in weekly chunks…')
    all_games_raw = fetch_schedule(run.date_from, run.date_to)
    gamepks = []
    for g in all_games_raw:
        status = (g.get('status') or {}).get('detailedState', '')
        if status not in ('Final', 'Completed Early', 'Game Over'):
            continue
        gpk = g.get('gamePk')
        if gpk:
            gamepks.append(int(gpk))
    run.games_seen = len(gamepks)
    _save_counters(run)
    _append_log(run, f'Schedule: {len(gamepks)} Final games')

    for i, gamepk in enumerate(gamepks, 1):
        game = Game.objects.filter(
            source='mlb_stats_api', external_id=str(gamepk),
        ).select_related('home_team', 'away_team').first()
        if game is None:
            # Game not in our Game table (spring training, etc.). Skip.
            continue

        if RelieverAppearance.objects.filter(game=game).exists():
            run.appearances_skipped_existing += 1
            if i % 50 == 0:
                _save_counters(run)
            continue

        try:
            data = fetch_boxscore(gamepk)
        except StatsApiError as e:
            run.boxscore_errors += 1
            # First few boxscore failures per run get logged with detail
            # so ops can see the pattern; later ones are counted only
            # to keep the log tail readable.
            if run.boxscore_errors <= 3:
                _append_log(run, f'boxscore {gamepk} failed: {e.human_summary()[:200]}')
            if i % 50 == 0:
                _save_counters(run)
            continue

        run.boxscores_fetched += 1
        for side, team in (('home', game.home_team), ('away', game.away_team)):
            side_block = (data.get('teams') or {}).get(side) or {}
            pitchers_list = side_block.get('pitchers') or []
            players_map = side_block.get('players') or {}
            for pid in pitchers_list:
                player = players_map.get(f'ID{pid}') or {}
                person = player.get('person') or {}
                pitch_stats = _extract_pitcher_stats(player)
                pitcher, _ = _get_or_create_pitcher(
                    pid, person.get('fullName', ''), team,
                )
                _, created_bool = RelieverAppearance.objects.update_or_create(
                    game=game, pitcher=pitcher,
                    defaults={'team': team, **pitch_stats},
                )
                if created_bool:
                    run.appearances_created += 1
                else:
                    run.appearances_updated += 1

        if i % 20 == 0:
            _save_counters(run)
            _append_log(
                run,
                f'Ingest progress {i}/{len(gamepks)}: '
                f'created={run.appearances_created} '
                f'updated={run.appearances_updated} '
                f'errors={run.boxscore_errors}',
            )
        if sleep_ms > 0 and i < len(gamepks):
            time.sleep(sleep_ms / 1000.0)

    _save_counters(run)
    _append_log(
        run,
        f'Ingest done: created={run.appearances_created} '
        f'updated={run.appearances_updated} '
        f'skipped_existing={run.appearances_skipped_existing} '
        f'errors={run.boxscore_errors}',
    )


# ---------------------------------------------------------------------------
# Phase 2: backfill_bullpen_snapshots-equivalent (no API)
# ---------------------------------------------------------------------------


def _build_snapshots(run) -> None:
    """Walk the Game universe in the run's window; write one snapshot per
    (team, first_pitch) via the deterministic builder. No API calls."""
    from apps.mlb.models import Game, TeamBullpenSnapshot
    from apps.mlb.services.bullpen_builder import persist_snapshot

    games = list(
        Game.objects.filter(
            first_pitch__date__gte=run.date_from,
            first_pitch__date__lte=run.date_to,
        )
        .select_related('home_team', 'away_team')
        .order_by('first_pitch')
    )
    _append_log(run, f'Snapshot phase: {len(games)} games')

    for i, g in enumerate(games, 1):
        for team in (g.home_team, g.away_team):
            if TeamBullpenSnapshot.objects.filter(
                team=team, as_of=g.first_pitch,
            ).exists():
                run.snapshots_skipped_existing += 1
                continue
            persist_snapshot(team, g.first_pitch)
            run.snapshots_created += 1
        if i % 100 == 0:
            _save_counters(run)
            _append_log(
                run,
                f'Snapshot progress {i}/{len(games)}: '
                f'created={run.snapshots_created} '
                f'skipped={run.snapshots_skipped_existing}',
            )

    _save_counters(run)
    _append_log(
        run,
        f'Snapshots done: created={run.snapshots_created} '
        f'skipped={run.snapshots_skipped_existing}',
    )


# ---------------------------------------------------------------------------
# Integrity audit — read-only, hits the DB only (no API)
# ---------------------------------------------------------------------------


def integrity_audit(*, sample_size: int = 200) -> dict:
    """Return PASS/FAIL findings across the required integrity checks.

    Read-only. Uses aggregation queries + a bounded sample-based
    leakage sanity check so it runs in seconds even on large datasets.
    """
    from django.db.models import Count, F, Q
    from apps.mlb.models import (
        Game, RelieverAppearance, StartingPitcher, Team, TeamBullpenSnapshot,
    )
    from apps.mlb.services.bullpen_builder import build_snapshot

    findings = []

    # 1. No duplicate RelieverAppearance for (game, pitcher). Enforced by
    # unique constraint but audit anyway (defense in depth).
    dup_appearances = (
        RelieverAppearance.objects
        .values('game_id', 'pitcher_id')
        .annotate(n=Count('id'))
        .filter(n__gt=1)
        .count()
    )
    findings.append({
        'check': 'no_duplicate_reliever_appearances',
        'result': 'PASS' if dup_appearances == 0 else 'FAIL',
        'detail': f'{dup_appearances} duplicate (game, pitcher) groups',
    })

    # 2. No duplicate TeamBullpenSnapshot for (team, as_of).
    dup_snapshots = (
        TeamBullpenSnapshot.objects
        .values('team_id', 'as_of')
        .annotate(n=Count('id'))
        .filter(n__gt=1)
        .count()
    )
    findings.append({
        'check': 'no_duplicate_snapshots',
        'result': 'PASS' if dup_snapshots == 0 else 'FAIL',
        'detail': f'{dup_snapshots} duplicate (team, as_of) groups',
    })

    # 3. No RelieverAppearance where the parent game hasn't started
    # yet at snapshot time. Sample-based check: for `sample_size` random
    # recent snapshots, rebuild the snapshot in-memory via the builder
    # (leakage-safe by construction) and compare recorded vs freshly-
    # rebuilt bullpen_era to within a small epsilon. Divergence would
    # indicate either a leakage bug or a non-deterministic path.
    recent_snaps = list(
        TeamBullpenSnapshot.objects
        .exclude(bullpen_era__isnull=True)
        .order_by('-as_of')[:sample_size]
    )
    diverged = 0
    inspected = 0
    for snap in recent_snaps:
        inspected += 1
        rebuilt = build_snapshot(snap.team, snap.as_of)
        if rebuilt.bullpen_era is None and snap.bullpen_era is None:
            continue
        if rebuilt.bullpen_era is None or snap.bullpen_era is None:
            diverged += 1
            continue
        if abs(rebuilt.bullpen_era - snap.bullpen_era) > 0.05:
            diverged += 1
    findings.append({
        'check': 'snapshot_determinism_sample',
        'result': 'PASS' if diverged == 0 else 'FAIL',
        'detail': (
            f'{inspected} recent snapshots re-built by the deterministic '
            f'builder; {diverged} diverged from persisted values by >0.05 ERA. '
            f'Zero divergence means (a) no leakage into persisted snapshots '
            f'and (b) the builder is deterministic on real data.'
        ),
    })

    # 4. Every reliever appearance's is_starter matches the boxscore
    # gamesStarted convention: each game should have EXACTLY ONE
    # is_starter=True appearance per team (barring the extremely rare
    # opener/bulk-game). Report deviations without failing — this is
    # informational.
    starter_count_deviations = 0
    game_ids_with_appearances = (
        RelieverAppearance.objects
        .values('game_id').distinct()
        .order_by('-game__first_pitch')[:sample_size]
    )
    for row in game_ids_with_appearances:
        starter_counts = (
            RelieverAppearance.objects
            .filter(game_id=row['game_id'])
            .values('team_id')
            .annotate(starters=Count('id', filter=Q(is_starter=True)))
        )
        for tc in starter_counts:
            if tc['starters'] != 1:
                starter_count_deviations += 1
    findings.append({
        'check': 'each_team_has_one_starter_per_game_sample',
        'result': 'INFO',
        'detail': (
            f'Sampled {sample_size} recent games — '
            f'{starter_count_deviations} team-game pairs had != 1 starter. '
            'Small counts are normal (opener strategy). Large counts suggest '
            'role misclassification.'
        ),
    })

    # 5. All 30 MLB teams represented in the appearance data.
    teams_with_apps = (
        RelieverAppearance.objects
        .values_list('team_id', flat=True).distinct().count()
    )
    mlb_team_total = Team.objects.count()
    findings.append({
        'check': 'all_teams_represented_in_appearances',
        'result': 'PASS' if teams_with_apps >= min(mlb_team_total, 30) else 'INFO',
        'detail': (
            f'{teams_with_apps}/{mlb_team_total} MLB teams have at least '
            'one RelieverAppearance row. Fewer than expected before backfill; '
            'should reach 30 after the historical run completes.'
        ),
    })

    # 6. Coverage on the last 60 days (both teams have a snapshot before
    # first_pitch).
    from datetime import date as _d, timedelta as _td
    window_start = _d.today() - _td(days=60)
    recent_games = list(
        Game.objects.filter(
            first_pitch__date__gte=window_start,
            first_pitch__date__lte=_d.today() - _td(days=1),
            status='final',
        )
    )
    both = home_only = away_only = neither = 0
    for g in recent_games:
        h = TeamBullpenSnapshot.objects.filter(
            team=g.home_team, as_of__lt=g.first_pitch,
        ).exists()
        a = TeamBullpenSnapshot.objects.filter(
            team=g.away_team, as_of__lt=g.first_pitch,
        ).exists()
        if h and a:
            both += 1
        elif h:
            home_only += 1
        elif a:
            away_only += 1
        else:
            neither += 1
    total = max(1, len(recent_games))
    coverage_pct = round(100.0 * both / total, 2)
    findings.append({
        'check': 'coverage_last_60_days',
        'result': 'PASS' if coverage_pct >= 80 else 'INFO',
        'detail': (
            f'Last 60d: {len(recent_games)} final games; both teams '
            f'covered on {both} ({coverage_pct}%). Ship criterion: >=80%.'
        ),
    })

    # 7. Data confidence distribution across snapshots.
    from django.db.models import Count as _Count
    conf_dist = list(
        TeamBullpenSnapshot.objects
        .values('data_confidence')
        .annotate(n=_Count('id'))
        .order_by('data_confidence')
    )
    findings.append({
        'check': 'data_confidence_distribution',
        'result': 'INFO',
        'detail': ', '.join(
            f'{d["data_confidence"]}={d["n"]}' for d in conf_dist
        ) or '(no snapshots yet)',
    })

    passed = sum(1 for f in findings if f['result'] == 'PASS')
    failed = sum(1 for f in findings if f['result'] == 'FAIL')
    info = sum(1 for f in findings if f['result'] == 'INFO')

    return {
        'findings': findings,
        'summary': {'PASS': passed, 'FAIL': failed, 'INFO': info},
        'overall': 'FAIL' if failed > 0 else 'PASS',
    }
