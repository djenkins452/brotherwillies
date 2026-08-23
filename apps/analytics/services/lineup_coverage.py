"""v3.4 SHADOW — lineup collection coverage + operational health diagnostic.

Read-only report on progress toward the pre-registered lineup
experiment PLUS operational health of the polling loop. Answers:

  * How many games in the window are eligible for lineup coverage?
  * How many had a legitimate pregame lineup observed?
  * What's the distribution of first-confirmation lead time (minutes
    before first_pitch)?
  * How often does the lineup CHANGE after first observation?
  * How far are we from the pre-registered minimum sample size for a
    lineup replay experiment?
  * When did the poll last run? Did it succeed?
  * Are there suspicious collection gaps (a day with games and no rows)?

The pre-registered minimum sample for a lineup replay is set here as
a constant (see rationale in the doc string of that constant). This
document also feeds the changelog / experiment-design record.

Read-only. Never writes. Never triggers scoring.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

from django.utils import timezone


# --- Pre-registered minimum sample for a lineup replay experiment.
#
# Effect-size reasoning (recorded for later audit — do NOT change
# without a formal re-registration):
#   * V3.2 baseline is a strong methodology (~71% observed win rate
#     over the recent forward window).
#   * A lineup-quality feature that MATERIALLY helps moneyline should
#     move win rate by at least ~1pp — smaller effects wouldn't
#     survive Wilson lower-bound tests at reasonable sample sizes.
#   * Detecting a ~1pp effect around p≈0.71 with alpha=0.05 requires
#     n ≈ 400 per arm using a standard normal approximation
#     (SE ≈ 0.023, so Δ=0.01 is ~0.4σ per arm; 400 gives ~90% power
#     for a 2pp effect and reasonable power for 1pp).
#   * We only care about the recommended-set arm size, which is
#     typically ~30-40% of evaluable games (per V3.2 base rate).
#   * So we need lineups covering enough games to yield ~400
#     recommendations in each arm — approximately 1000-1200 covered
#     games. At ~15 games/day, that's ~70-90 days of coverage.
#
# The threshold below is 800 rather than 1000 as a "READY TO REVIEW"
# gate; the actual experiment PASS bar is enforced by ship criteria
# on that experiment's walk-forward, not by this threshold. This just
# tells us when to run the experiment.
PRE_REGISTERED_MIN_COVERED_GAMES = 800


def build_coverage_report(*, days: int = 60, reference_date=None) -> Dict[str, Any]:
    """Walk games in the window, report lineup coverage stats."""
    from apps.mlb.models import ConfirmedLineup, Game

    ref = reference_date or timezone.localdate()
    date_from = ref - timedelta(days=days)
    date_to = ref

    games = list(
        Game.objects.filter(
            first_pitch__date__gte=date_from,
            first_pitch__date__lte=date_to,
        )
        .select_related('home_team', 'away_team')
        .order_by('first_pitch')
    )

    # Pre-fetch all ConfirmedLineup rows in the window once.
    lineup_rows = list(
        ConfirmedLineup.objects
        .filter(observed_at__gte=date_from - timedelta(hours=6))
        .order_by('game_id', 'team_id', 'observed_at')
        .select_related('team')
    )

    # Group by (game_id, team_id).
    by_pair: Dict[tuple, List] = {}
    for row in lineup_rows:
        by_pair.setdefault((row.game_id, row.team_id), []).append(row)

    total_games = len(games)
    both_covered = 0
    one_covered = 0
    neither_covered = 0
    home_covered = 0
    away_covered = 0
    changed_after_confirmation = 0
    post_first_pitch_only = 0

    lead_time_minutes: List[float] = []

    for g in games:
        home_rows = by_pair.get((g.id, g.home_team_id), [])
        away_rows = by_pair.get((g.id, g.away_team_id), [])
        home_pregame = [r for r in home_rows
                        if r.observed_at < g.first_pitch]
        away_pregame = [r for r in away_rows
                        if r.observed_at < g.first_pitch]
        h_cov = bool(home_pregame)
        a_cov = bool(away_pregame)
        if h_cov and a_cov:
            both_covered += 1
        elif h_cov or a_cov:
            one_covered += 1
        else:
            # Either no rows at all or only post_first_pitch.
            if home_rows or away_rows:
                post_first_pitch_only += 1
            else:
                neither_covered += 1
        if h_cov:
            home_covered += 1
            first = home_pregame[0]
            lead = (g.first_pitch - first.observed_at).total_seconds() / 60.0
            lead_time_minutes.append(lead)
            if len(home_pregame) > 1 or any(
                r.lineup_state == 'updated_after_confirmation'
                for r in home_rows
            ):
                changed_after_confirmation += 1
        if a_cov:
            away_covered += 1
            first = away_pregame[0]
            lead = (g.first_pitch - first.observed_at).total_seconds() / 60.0
            lead_time_minutes.append(lead)
            if len(away_pregame) > 1 or any(
                r.lineup_state == 'updated_after_confirmation'
                for r in away_rows
            ):
                changed_after_confirmation += 1

    lead_time_minutes.sort()
    def _pct(p):
        if not lead_time_minutes:
            return None
        idx = min(len(lead_time_minutes) - 1,
                  int(round(p * (len(lead_time_minutes) - 1))))
        return lead_time_minutes[idx]

    both_covered_pct = (
        100.0 * both_covered / total_games if total_games else 0.0
    )

    # --- Operational health: last poll runs + collection gaps.
    from apps.ops.models import CronRunLog
    from django.db.models import Q as _Q
    poll_rows = list(
        CronRunLog.objects.filter(command='ingest_lineups').order_by('-started_at')[:20]
    )
    last_run = poll_rows[0] if poll_rows else None
    last_success = next((r for r in poll_rows if r.status == 'success'), None)
    last_failure = next((r for r in poll_rows if r.status == 'failure'), None)

    # Collection-gap detector: for each day in the window that had games,
    # do we have AT LEAST one row observed on that day (any (game, team))?
    from collections import defaultdict as _dd
    games_per_day: dict = _dd(int)
    rows_per_day: dict = _dd(int)
    for g in games:
        games_per_day[g.first_pitch.date()] += 1
    for r in lineup_rows:
        rows_per_day[r.observed_at.date()] += 1
    gap_days = [
        d.isoformat() for d in sorted(games_per_day)
        if games_per_day[d] > 0 and rows_per_day.get(d, 0) == 0
    ]

    ops_health = {
        'last_run_at': last_run.started_at.isoformat() if last_run else None,
        'last_run_status': last_run.status if last_run else None,
        'last_success_at': last_success.started_at.isoformat() if last_success else None,
        'last_failure_at': last_failure.started_at.isoformat() if last_failure else None,
        'recent_run_count_20': len(poll_rows),
        'games_currently_watched': _games_currently_watched(),
        'collection_gap_days': gap_days,
        'stale_warning': _stale_warning(last_run, last_success),
    }

    return {
        'window': {
            'days': days, 'from': date_from.isoformat(),
            'to': date_to.isoformat(),
            'games_evaluable': total_games,
        },
        'ops_health': ops_health,
        'coverage': {
            'both_covered': both_covered,
            'one_side_covered': one_covered,
            'neither_covered': neither_covered,
            'post_first_pitch_only': post_first_pitch_only,
            'both_covered_pct': round(both_covered_pct, 2),
        },
        'lineup_observations': {
            'home_covered_games': home_covered,
            'away_covered_games': away_covered,
            'changed_after_confirmation': changed_after_confirmation,
            'total_confirmed_rows': sum(
                1 for r in lineup_rows if r.lineup_state == 'confirmed'
            ),
            'total_updated_rows': sum(
                1 for r in lineup_rows if r.lineup_state == 'updated_after_confirmation'
            ),
            'total_post_first_pitch_rows': sum(
                1 for r in lineup_rows if r.lineup_state == 'post_first_pitch'
            ),
        },
        'lead_time_minutes': {
            'n': len(lead_time_minutes),
            'p10': _pct(0.10),
            'p25': _pct(0.25),
            'p50': _pct(0.50),
            'p75': _pct(0.75),
            'p90': _pct(0.90),
        },
        'experiment_readiness': {
            'pre_registered_min_covered_games': PRE_REGISTERED_MIN_COVERED_GAMES,
            'covered_games_so_far': both_covered,
            'progress_pct': round(
                100.0 * both_covered / PRE_REGISTERED_MIN_COVERED_GAMES, 2,
            ),
            'ready_for_experiment': both_covered >= PRE_REGISTERED_MIN_COVERED_GAMES,
        },
    }


def _games_currently_watched() -> int:
    """MLB Games in the [now-1h, now+8h] window — what the poll would
    inspect on its next invocation."""
    from apps.mlb.models import Game
    now = timezone.now()
    return Game.objects.filter(
        source='mlb_stats_api',
        first_pitch__gte=now - timedelta(hours=1),
        first_pitch__lte=now + timedelta(hours=8),
    ).count()


def _stale_warning(last_run, last_success) -> Optional[str]:
    """Return a short human-readable stale warning when the poll hasn't
    run recently OR the most recent run failed."""
    from apps.mlb.models import Game
    now = timezone.now()
    games_soon = Game.objects.filter(
        source='mlb_stats_api',
        first_pitch__gte=now,
        first_pitch__lte=now + timedelta(hours=4),
    ).exists()
    if last_run is None:
        return 'ingest_lineups has NEVER run — wire it into Railway cron.'
    age_min = (now - last_run.started_at).total_seconds() / 60.0
    if games_soon and age_min > 30.0:
        return (
            f'games start within 4h but last poll was {age_min:.1f} min ago — '
            f'stale collection risks losing pregame confirmation lead time.'
        )
    if last_success is None:
        return 'ingest_lineups has run but NEVER SUCCEEDED — investigate.'
    if last_run and last_run.status != 'success':
        return f'last poll status = {last_run.status} — investigate.'
    return None


def render(report: Dict[str, Any]) -> str:
    lines = []
    lines.append('=' * 78)
    lines.append('v3.4 LINEUP COLLECTION COVERAGE + OPS REPORT')
    lines.append('=' * 78)
    w = report['window']
    c = report['coverage']
    obs = report['lineup_observations']
    lt = report['lead_time_minutes']
    er = report['experiment_readiness']
    lines.append(f'Window: {w["from"]} → {w["to"]} ({w["days"]} days)')
    lines.append(f'Games evaluable: {w["games_evaluable"]}')
    lines.append('')
    oh = report.get('ops_health', {}) or {}
    lines.append('OPERATIONAL HEALTH')
    lines.append('-' * 78)
    lines.append(f'  last run at             : {oh.get("last_run_at") or "NEVER"}')
    lines.append(f'  last run status         : {oh.get("last_run_status") or "NEVER"}')
    lines.append(f'  last successful run at  : {oh.get("last_success_at") or "NEVER"}')
    lines.append(f'  last failed run at      : {oh.get("last_failure_at") or "n/a"}')
    lines.append(f'  recent run count (20)   : {oh.get("recent_run_count_20", 0)}')
    lines.append(f'  games currently watched : {oh.get("games_currently_watched", 0)}')
    lines.append(f'  collection gap days     : {len(oh.get("collection_gap_days") or [])}'
                 + (f' — {", ".join((oh.get("collection_gap_days") or [])[:5])}'
                    if oh.get("collection_gap_days") else ''))
    if oh.get('stale_warning'):
        lines.append(f'  ⚠ STALE WARNING          : {oh["stale_warning"]}')
    lines.append('')
    lines.append('COVERAGE')
    lines.append('-' * 78)
    lines.append(f'  both teams covered      : {c["both_covered"]:>4} ({c["both_covered_pct"]}%)')
    lines.append(f'  one side covered        : {c["one_side_covered"]:>4}')
    lines.append(f'  neither (no rows)       : {c["neither_covered"]:>4}')
    lines.append(f'  post-first-pitch only   : {c["post_first_pitch_only"]:>4}')
    lines.append('')
    lines.append('OBSERVATIONS')
    lines.append('-' * 78)
    lines.append(f'  home covered games      : {obs["home_covered_games"]:>4}')
    lines.append(f'  away covered games      : {obs["away_covered_games"]:>4}')
    lines.append(f'  changed after confirm   : {obs["changed_after_confirmation"]:>4}')
    lines.append(f'  confirmed rows          : {obs["total_confirmed_rows"]:>4}')
    lines.append(f'  updated rows            : {obs["total_updated_rows"]:>4}')
    lines.append(f'  post-first-pitch rows   : {obs["total_post_first_pitch_rows"]:>4}')
    lines.append('')
    lines.append(f'FIRST-CONFIRMATION LEAD TIME (minutes before first_pitch, n={lt["n"]})')
    lines.append('-' * 78)
    if lt['n']:
        for p in ('p10', 'p25', 'p50', 'p75', 'p90'):
            v = lt.get(p)
            lines.append(f'  {p}: {v:>6.1f} min' if v is not None else f'  {p}: —')
    else:
        lines.append('  no lineups observed yet — polling has not populated data')
    lines.append('')
    lines.append('EXPERIMENT READINESS')
    lines.append('-' * 78)
    lines.append(f'  pre-registered min covered games : {er["pre_registered_min_covered_games"]}')
    lines.append(f'  covered so far                   : {er["covered_games_so_far"]}')
    lines.append(f'  progress                         : {er["progress_pct"]}%')
    if er['ready_for_experiment']:
        lines.append('  status                           : READY — pre-registered sample reached.')
    else:
        remaining = er['pre_registered_min_covered_games'] - er['covered_games_so_far']
        lines.append(f'  status                           : NOT READY — need {remaining} more covered games.')
    return '\n'.join(lines)
