"""v3.3 SHADOW — MLB Stats API connectivity diagnostic.

Small read-only probe that exercises the three endpoints the bullpen
backfill depends on and reports PASS/FAIL for each. Uses the same
`statsapi_client` the production code uses, so if Danny sees FAIL on
this diagnostic on Railway, the actual backfill will also fail — and
the diagnostic tells us why (status code, response body preview,
attempt number) without needing to launch and abort an 11-minute run.

Never writes any data. Safe to hit as often as desired.
"""
from __future__ import annotations

import time
from datetime import date, timedelta
from typing import List

from apps.datahub.providers.mlb.statsapi_client import (
    StatsApiError, fetch_boxscore, fetch_schedule, fetch_teams,
)


# A boxscore known to exist from the v3.3 development investigation —
# TOR vs BOS on 2026-08-10, gamePk 822780. If this ever 404s upstream
# it means the API's historical horizon has shifted and we need to
# update the diagnostic; the shipping backfill is not affected.
KNOWN_HISTORICAL_GAMEPK = 822780


def run_api_check() -> dict:
    """Run the 3-step probe. Returns a dict shaped for the plaintext
    renderer + programmatic callers."""
    steps: List[dict] = []
    overall_pass = True

    steps.append(_check_teams())
    steps.append(_check_schedule_short_window())
    steps.append(_check_known_boxscore())

    for s in steps:
        if s['result'] != 'PASS':
            overall_pass = False

    return {
        'overall': 'PASS' if overall_pass else 'FAIL',
        'steps': steps,
    }


def _time(fn):
    t0 = time.monotonic()
    try:
        result = fn()
        return result, (time.monotonic() - t0) * 1000.0, None
    except StatsApiError as e:
        return None, (time.monotonic() - t0) * 1000.0, e


def _check_teams() -> dict:
    """Step 1 — /api/v1/teams. Smallest read; if this fails the whole
    API is unreachable from this environment."""
    today = date.today()
    season = today.year
    data, latency_ms, err = _time(lambda: fetch_teams(season=season))
    if err is not None:
        return {
            'step': 'teams',
            'result': 'FAIL',
            'latency_ms': round(latency_ms, 1),
            'detail': err.human_summary(),
        }
    return {
        'step': 'teams',
        'result': 'PASS',
        'latency_ms': round(latency_ms, 1),
        'detail': f'{len(data)} teams returned for season {season}',
    }


def _check_schedule_short_window() -> dict:
    """Step 2 — /api/v1/schedule for a one-day window (yesterday).
    Chunk-agnostic — the client wraps it in a single sub-week call so
    we exercise the same code path used by the historical backfill."""
    yesterday = date.today() - timedelta(days=1)
    data, latency_ms, err = _time(lambda: fetch_schedule(yesterday, yesterday))
    if err is not None:
        return {
            'step': 'schedule_one_day',
            'result': 'FAIL',
            'latency_ms': round(latency_ms, 1),
            'detail': err.human_summary(),
        }
    n = len(data)
    return {
        'step': 'schedule_one_day',
        'result': 'PASS',
        'latency_ms': round(latency_ms, 1),
        'detail': f'{n} games returned for {yesterday}',
    }


def _check_known_boxscore() -> dict:
    """Step 3 — /api/v1/game/{KNOWN_HISTORICAL_GAMEPK}/boxscore.
    Confirms boxscore path works AND the response shape matches what
    the ingest pipeline expects (top-level `teams.home/away.pitchers`)."""
    data, latency_ms, err = _time(
        lambda: fetch_boxscore(KNOWN_HISTORICAL_GAMEPK),
    )
    if err is not None:
        return {
            'step': f'boxscore_{KNOWN_HISTORICAL_GAMEPK}',
            'result': 'FAIL',
            'latency_ms': round(latency_ms, 1),
            'detail': err.human_summary(),
        }
    teams = data.get('teams') or {}
    home_pitchers = ((teams.get('home') or {}).get('pitchers') or [])
    away_pitchers = ((teams.get('away') or {}).get('pitchers') or [])
    if not home_pitchers and not away_pitchers:
        return {
            'step': f'boxscore_{KNOWN_HISTORICAL_GAMEPK}',
            'result': 'FAIL',
            'latency_ms': round(latency_ms, 1),
            'detail': (
                'boxscore returned 2xx but has no home/away.pitchers — '
                'API shape may have changed'
            ),
        }
    return {
        'step': f'boxscore_{KNOWN_HISTORICAL_GAMEPK}',
        'result': 'PASS',
        'latency_ms': round(latency_ms, 1),
        'detail': (
            f'shape OK: {len(home_pitchers)} home pitchers, '
            f'{len(away_pitchers)} away pitchers'
        ),
    }


def render(report: dict) -> str:
    """Plaintext renderer for the /analytics/bullpen-api-check/ view."""
    lines = []
    lines.append('=' * 78)
    lines.append('MLB STATS API CONNECTIVITY DIAGNOSTIC')
    lines.append('=' * 78)
    lines.append(f"Overall: {report['overall']}")
    lines.append('')
    for s in report['steps']:
        lines.append(f"[{s['result']:>4}] {s['step']:<40} {s['latency_ms']:>7.1f} ms")
        lines.append(f'       {s["detail"]}')
        lines.append('')
    if report['overall'] == 'FAIL':
        lines.append('READ THE RESULT')
        lines.append('-' * 78)
        lines.append('At least one probe failed. The `detail` line for each FAIL row')
        lines.append('contains the exact HTTP status + response body preview. If a')
        lines.append('backfill run also fails, it will hit the same underlying cause;')
        lines.append("resolve the diagnostic FAIL first (rate limit / user-agent /")
        lines.append('IP block) before re-triggering the historical backfill.')
    else:
        lines.append('All probes passed. The historical bullpen backfill is safe to trigger.')
    return '\n'.join(lines)
