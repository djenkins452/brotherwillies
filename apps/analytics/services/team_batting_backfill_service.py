"""v3.4 team-offense PHASE 2 — historical team hitting backfill.

Background-thread body for `TeamBattingBackfillRun` rows. Mirrors
`bullpen_backfill_service.py` — same async pattern, same status
lifecycle, same log tail, same failure summary handling.

DATA SOURCE
  MLB Stats API `/v1/teams/{id}/stats?stats=byDateRange&group=hitting`
  called via the canonical `apps.datahub.providers.mlb.statsapi_client`
  (User-Agent + retry + rich StatsApiError).

STRATEGY
  For each date D in [date_from, date_to]:
    For each MLB team with external_id set:
      Fetch season-to-date hitting through D (startDate=season start,
        endDate=D). Upsert TeamBattingSnapshot(team, as_of_date=D).

  Idempotent: re-running over the same date range updates in place
  via update_or_create.

  API cost estimate: ~30 teams × N dates. For a 180-day season:
    30 × 180 = 5,400 calls. Chunked into per-day-per-team fetches
    (~1KB each), well within any rate ceiling.

DOES NOT touch:
  * Any production decision path
  * USE_TEAM_OFFENSE flag (remains false)
  * The BettingRecommendation / MockBet tables

Writes only to:
  * mlb.TeamBattingSnapshot         (upsert)
  * analytics.TeamBattingBackfillRun (progress rows)
"""
from __future__ import annotations

import logging
import time
import traceback
from datetime import date, timedelta
from typing import Optional

from django.utils import timezone

from apps.datahub.providers.mlb.statsapi_client import (
    StatsApiError, fetch_team_hitting_range,
)


logger = logging.getLogger(__name__)


LOG_TAIL_LINE_CAP = 40
LOG_TAIL_CHAR_CAP = 4000


def _append_log(run, line: str) -> None:
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
    run.save(update_fields=[
        'fetches_attempted', 'fetches_succeeded', 'fetches_empty',
        'fetches_errored', 'snapshots_created', 'snapshots_updated',
        'teams_seen',
    ])


def _season_start_for(d: date) -> date:
    """MLB regular season anchor. March 1 is safe — captures all
    spring-training-adjacent play and every real regular season
    game. The API silently trims to actual season boundaries."""
    return date(d.year, 3, 1)


def _to_int(v) -> int:
    try:
        return int(v) if v is not None else 0
    except (TypeError, ValueError):
        return 0


def _to_float_rate(v):
    """API rate stats come as strings like '.320'. Handle empty."""
    if v is None or v == '':
        return None
    try:
        s = str(v).strip()
        if s.startswith('.') or s.startswith('-'):
            return float(s if s != '-' else '0')
        return float(s)
    except (TypeError, ValueError):
        return None


def run_team_batting_backfill(run_id: str, *, sleep_ms: int = 100) -> None:
    """Background-thread body. Idempotent per (team, as_of_date)."""
    from apps.analytics.models import TeamBattingBackfillRun
    from apps.mlb.models import Team, TeamBattingSnapshot

    try:
        run = TeamBattingBackfillRun.objects.get(id=run_id)
    except TeamBattingBackfillRun.DoesNotExist:
        logger.exception('team_batting_backfill: run row missing id=%s', run_id)
        return

    try:
        run.started_at = timezone.now()
        run.status = 'running'
        run.save()
        _append_log(
            run,
            f'Starting team-batting backfill '
            f'{run.date_from}..{run.date_to} (kind={run.kind})',
        )

        teams = list(
            Team.objects
            .filter(source='mlb_stats_api')
            .exclude(external_id='')
            .order_by('name')
        )
        run.teams_seen = len(teams)
        run.save(update_fields=['teams_seen'])
        _append_log(run, f'Teams to backfill: {len(teams)}')

        if not teams:
            run.status = 'completed'
            run.finished_at = timezone.now()
            run.save()
            _append_log(run, 'No MLB teams with external_id — nothing to do')
            return

        # Enumerate every date in the run window. One fetch per
        # (team, date).
        dates = []
        d = run.date_from
        while d <= run.date_to:
            dates.append(d)
            d += timedelta(days=1)

        total_calls = len(teams) * len(dates)
        _append_log(
            run,
            f'{len(dates)} dates × {len(teams)} teams = '
            f'{total_calls} fetches',
        )

        # Iterate dates outer, teams inner — makes progress reads
        # naturally chronological in the log tail.
        for date_i, target_date in enumerate(dates, 1):
            season = target_date.year
            season_start = _season_start_for(target_date)
            for team in teams:
                run.fetches_attempted += 1
                try:
                    stat = fetch_team_hitting_range(
                        team_mlb_id=int(team.external_id),
                        start_date=season_start,
                        end_date=target_date,
                        season=season,
                    )
                except StatsApiError as e:
                    run.fetches_errored += 1
                    if run.fetches_errored <= 5:
                        _append_log(
                            run,
                            f'{target_date} {team.abbreviation or team.slug}: '
                            f'{e.human_summary()[:180]}',
                        )
                    if sleep_ms > 0:
                        time.sleep(sleep_ms / 1000.0)
                    continue

                if not stat:
                    run.fetches_empty += 1
                    if sleep_ms > 0:
                        time.sleep(sleep_ms / 1000.0)
                    continue

                run.fetches_succeeded += 1
                defaults = {
                    'season': season,
                    'plate_appearances': _to_int(stat.get('plateAppearances')),
                    'at_bats': _to_int(stat.get('atBats')),
                    'hits': _to_int(stat.get('hits')),
                    'doubles': _to_int(stat.get('doubles')),
                    'triples': _to_int(stat.get('triples')),
                    'home_runs': _to_int(stat.get('homeRuns')),
                    'walks': _to_int(stat.get('baseOnBalls')),
                    'hit_by_pitch': _to_int(stat.get('hitByPitch')),
                    'sac_flies': _to_int(stat.get('sacFlies')),
                    'strikeouts': _to_int(stat.get('strikeOuts')),
                    'runs': _to_int(stat.get('runs')),
                    'games_played': _to_int(stat.get('gamesPlayed')),
                    'obp_reported': _to_float_rate(stat.get('obp')),
                    'slg_reported': _to_float_rate(stat.get('slg')),
                    'ops_reported': _to_float_rate(stat.get('ops')),
                    'source': 'mlb_stats_api',
                }
                _, created = TeamBattingSnapshot.objects.update_or_create(
                    team=team, as_of_date=target_date,
                    defaults=defaults,
                )
                if created:
                    run.snapshots_created += 1
                else:
                    run.snapshots_updated += 1

                if sleep_ms > 0:
                    time.sleep(sleep_ms / 1000.0)

            if date_i % 3 == 0 or date_i == len(dates):
                _save_counters(run)
                _append_log(
                    run,
                    f'Date {date_i}/{len(dates)} ({target_date}) '
                    f'done: created={run.snapshots_created} '
                    f'updated={run.snapshots_updated} '
                    f'empty={run.fetches_empty} '
                    f'errors={run.fetches_errored}',
                )

        _save_counters(run)
        if run.fetches_errored > 0:
            run.status = 'completed_with_errors'
            run.failure_summary = (
                f'{run.fetches_errored} fetch error(s) during backfill; '
                f'{run.snapshots_created + run.snapshots_updated} rows written.'
            )
        else:
            run.status = 'completed'
        run.finished_at = timezone.now()
        run.save()
        _append_log(run, f'{run.status} in {run.elapsed_seconds}s')

    except StatsApiError as api_err:
        logger.warning('team_batting_backfill_stats_api_failed run_id=%s: %s',
                       run_id, api_err.human_summary())
        try:
            run = TeamBattingBackfillRun.objects.get(id=run_id)
            run.status = 'failed'
            run.failure_summary = api_err.human_summary()[:500]
            run.error_message = ''.join(
                traceback.format_exception(api_err),
            )[:6000]
            run.finished_at = timezone.now()
            run.save()
            _append_log(run, f'FAILED (Stats API): {api_err.human_summary()[:250]}')
        except Exception:
            logger.exception('team_batting_backfill_failed_save run_id=%s', run_id)
    except Exception as exc:  # noqa: BLE001
        logger.exception('team_batting_backfill_failed run_id=%s', run_id)
        try:
            run = TeamBattingBackfillRun.objects.get(id=run_id)
            run.status = 'failed'
            run.failure_summary = repr(exc)[:500]
            run.error_message = ''.join(
                traceback.format_exception(exc),
            )[:6000]
            run.finished_at = timezone.now()
            run.save()
            _append_log(run, f'FAILED: {repr(exc)[:200]}')
        except Exception:
            logger.exception('team_batting_backfill_failed_save run_id=%s', run_id)
