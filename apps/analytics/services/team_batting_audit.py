"""v3.4 team-offense PHASE 2 — TeamBattingSnapshot backfill audit.

Zero-API-cost diagnostic report answering the operator questions raised
by a `completed_with_errors` backfill:

  1. What was attempted?
  2. What succeeded / errored / returned no-data?
  3. Which (team, date) pairs are missing, and are they legitimately
     empty (team not in season yet) or actual failures worth retrying?
  4. Do we have adequate paired snapshots for the intended isolated
     analysis?
  5. What game-level coverage does the intended analysis actually get?

READ-ONLY. Uses only local DB queries. Never modifies data.

Structure:
  audit_team_batting_backfill(days_horizon) → dict
    * run_summary        : latest N run rows w/ counters + status
    * requirements       : total (team, date) pairs expected
    * inventory          : missing pairs partitioned into:
                            - legitimate_empty (team hadn't played yet)
                            - suspect_missing (should retry)
    * error_hotspots     : does the missing set cluster by team/date/month?
    * game_coverage      : per-game both-team snapshot availability
    * candidate_coverage : per-candidate PA-gate-passing coverage
    * trustworthiness    : mechanical GO / HOLD verdict
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Set, Tuple


# --- The isolated analysis default is 180 days back from today. The
# audit uses the same horizon by default so its "game coverage" number
# matches what the analyzer would see.
DEFAULT_ANALYSIS_DAYS = 180


def audit_team_batting_backfill(
    *,
    reference_date: Optional[date] = None,
    days: int = DEFAULT_ANALYSIS_DAYS,
) -> Dict[str, Any]:
    from django.utils import timezone
    from apps.analytics.models import TeamBattingBackfillRun
    from apps.mlb.models import Game, Team, TeamBattingSnapshot

    if reference_date is None:
        reference_date = timezone.localdate()
    if isinstance(reference_date, datetime):
        reference_date = reference_date.date()
    analysis_from = reference_date - timedelta(days=days)
    analysis_to = reference_date - timedelta(days=1)

    # ---- 1. Snapshot totals + run summary ---------------------------
    snap_total = TeamBattingSnapshot.objects.count()
    runs = list(TeamBattingBackfillRun.objects.all().order_by('-created_at')[:10])
    runs_dump = []
    for r in runs:
        runs_dump.append({
            'id': str(r.id), 'kind': r.kind, 'status': r.status,
            'date_from': r.date_from.isoformat(),
            'date_to': r.date_to.isoformat(),
            'only_missing': getattr(r, 'only_missing', False),
            'started_at': r.started_at.isoformat() if r.started_at else None,
            'finished_at': r.finished_at.isoformat() if r.finished_at else None,
            'elapsed_seconds': r.elapsed_seconds,
            'counters': {
                'fetches_attempted': r.fetches_attempted,
                'fetches_succeeded': r.fetches_succeeded,
                'fetches_empty': r.fetches_empty,
                'fetches_errored': r.fetches_errored,
                'snapshots_created': r.snapshots_created,
                'snapshots_updated': r.snapshots_updated,
                'teams_seen': r.teams_seen,
            },
            'failure_summary': r.failure_summary,
            # First 3000 chars of log tail — enough to see error clustering.
            'log_tail': (r.log_tail or '')[-3000:],
        })

    # ---- 2. Expected inventory --------------------------------------
    teams = list(
        Team.objects
        .filter(source='mlb_stats_api')
        .exclude(external_id='')
        .order_by('name')
    )
    n_teams = len(teams)

    # The backfill window (union of all backfill runs' spans) — used to
    # scope "what should exist". If no runs, fall back to the last N
    # days horizon.
    if runs:
        earliest = min(r.date_from for r in runs)
        latest = max(r.date_to for r in runs)
        backfill_from = earliest
        backfill_to = latest
    else:
        backfill_from = analysis_from
        backfill_to = analysis_to

    expected_dates: List[date] = []
    d = backfill_from
    while d <= backfill_to:
        expected_dates.append(d)
        d += timedelta(days=1)
    expected_pairs = n_teams * len(expected_dates)

    # ---- 3. Present set ---------------------------------------------
    present_pairs: Set[Tuple[int, date]] = set(
        TeamBattingSnapshot.objects.filter(
            as_of_date__gte=backfill_from,
            as_of_date__lte=backfill_to,
        ).values_list('team_id', 'as_of_date')
    )

    # ---- 4. Missing pair classification -----------------------------
    # Legitimate empty = team hadn't played any final game yet on that
    # date OR the team's schedule shows no game before that date in the
    # season. We approximate this by: the team's earliest final game
    # date in the calendar season. Any missing (team, D) where D is
    # before team.earliest_final_date is legitimately empty (API would
    # return no splits — not an error, no data to store).
    from django.db.models import Case, F, IntegerField, Min, Q, When
    from apps.mlb.models import Game
    team_first_game_date: Dict[int, Optional[date]] = {}
    for t in teams:
        agg = (
            Game.objects
            .filter(status='final')
            .filter(Q(home_team=t) | Q(away_team=t))
            .aggregate(first=Min('first_pitch'))
        )
        first = agg.get('first')
        team_first_game_date[t.id] = first.date() if first else None

    legitimate_empty: List[Tuple[int, date]] = []
    suspect_missing: List[Tuple[int, date]] = []
    for t in teams:
        first_game = team_first_game_date.get(t.id)
        for dt_ in expected_dates:
            if (t.id, dt_) in present_pairs:
                continue
            if first_game is None:
                # Team never played a final game in DB — either seed
                # data missing OR team not in this season. Classify as
                # legitimate empty.
                legitimate_empty.append((t.id, dt_))
                continue
            # If the target date is strictly BEFORE the team's first
            # final game, the byDateRange API returns empty splits.
            # That's not an error.
            if dt_ < first_game:
                legitimate_empty.append((t.id, dt_))
            else:
                suspect_missing.append((t.id, dt_))

    # ---- 5. Error hotspots — cluster analysis on suspect_missing ----
    suspect_by_team = Counter(t_id for t_id, _ in suspect_missing)
    suspect_by_date = Counter(str(dt_) for _, dt_ in suspect_missing)
    suspect_by_month = Counter(str(dt_)[:7] for _, dt_ in suspect_missing)
    team_lookup = {t.id: (t.abbreviation or t.slug) for t in teams}
    top_teams = [
        {'team': team_lookup.get(tid, str(tid)), 'missing_count': cnt}
        for tid, cnt in suspect_by_team.most_common(10)
    ]
    top_dates = [
        {'date': d_, 'missing_count': cnt}
        for d_, cnt in suspect_by_date.most_common(10)
    ]
    monthly_suspect = [
        {'month': m, 'missing_count': cnt}
        for m, cnt in sorted(suspect_by_month.items())
    ]

    # ---- 6. Game-level coverage -------------------------------------
    games = list(
        Game.objects.filter(
            status='final',
            home_score__isnull=False,
            away_score__isnull=False,
            first_pitch__date__gte=analysis_from,
            first_pitch__date__lte=analysis_to,
        )
        .select_related('home_team', 'away_team')
        .order_by('first_pitch')
    )

    both_cov = 0
    home_only = 0
    away_only = 0
    neither = 0
    by_month_cov: Dict[str, Dict[str, int]] = defaultdict(
        lambda: {'total': 0, 'both': 0}
    )
    by_team_cov: Dict[int, Dict[str, int]] = defaultdict(
        lambda: {'total': 0, 'both': 0}
    )

    # Precompute per-team latest-strictly-before availability by
    # walking a sorted date list once per team → O(games) queries
    # avoided.
    by_team_dates: Dict[int, List[date]] = defaultdict(list)
    for tid, dt_ in present_pairs:
        by_team_dates[tid].append(dt_)
    for tid in by_team_dates:
        by_team_dates[tid].sort()

    def _has_strict_before(tid: int, game_date: date) -> bool:
        # Binary search for the largest date strictly less than game_date.
        arr = by_team_dates.get(tid, [])
        if not arr:
            return False
        # Simple bisect: since arr is sorted asc.
        import bisect
        idx = bisect.bisect_left(arr, game_date)
        return idx > 0  # any snapshot exists strictly before game_date

    for g in games:
        h_cov = _has_strict_before(g.home_team_id, g.first_pitch.date())
        a_cov = _has_strict_before(g.away_team_id, g.first_pitch.date())
        if h_cov and a_cov:
            both_cov += 1
        elif h_cov:
            home_only += 1
        elif a_cov:
            away_only += 1
        else:
            neither += 1
        m = g.first_pitch.strftime('%Y-%m')
        by_month_cov[m]['total'] += 1
        if h_cov and a_cov:
            by_month_cov[m]['both'] += 1
        # By-home-team for a compact per-team view (the away side is
        # symmetric — every team appears roughly half the time each way).
        by_team_cov[g.home_team_id]['total'] += 1
        if h_cov and a_cov:
            by_team_cov[g.home_team_id]['both'] += 1

    n_games = max(1, len(games))
    both_pct = round(100.0 * both_cov / n_games, 2)

    # ---- 7. Candidate-specific coverage -----------------------------
    # Use the real service to compute each candidate on a sample of
    # games (bounded so audit stays fast even at N=2700). Report the
    # fraction where BOTH teams' signals return non-low confidence.
    from apps.mlb.services.team_offense_v2 import (
        candidate_b_rolling_ops, candidate_c_rolling_obp_slg,
        candidate_d_blend_ops,
    )

    sample_size = min(500, len(games))
    if len(games) <= sample_size:
        sample = games
    else:
        # Take an evenly-spaced sample across the window so early and
        # late seasons are both represented.
        step = max(1, len(games) // sample_size)
        sample = games[::step][:sample_size]

    def _cov_for(fn):
        ok = 0
        total = 0
        for g in sample:
            try:
                hs = fn(g.home_team, g.first_pitch)
                as_ = fn(g.away_team, g.first_pitch)
            except Exception:
                total += 1
                continue
            total += 1
            # candidate_c returns a tuple → we need to unpack; here we
            # assume fn returns a single OffenseSignalV2. The tuple
            # variant is handled below.
            if hs.confidence != 'low' and as_.confidence != 'low':
                ok += 1
        return {'ok': ok, 'total': total,
                'pct': round(100.0 * ok / max(1, total), 2)}

    def _cov_for_pair(fn):
        """C variant returns (obp_sig, slg_sig)."""
        ok = 0
        total = 0
        for g in sample:
            try:
                h_o, h_s = fn(g.home_team, g.first_pitch)
                a_o, a_s = fn(g.away_team, g.first_pitch)
            except Exception:
                total += 1
                continue
            total += 1
            all_ok = all(x.confidence != 'low' for x in (h_o, h_s, a_o, a_s))
            if all_ok:
                ok += 1
        return {'ok': ok, 'total': total,
                'pct': round(100.0 * ok / max(1, total), 2)}

    cand_b = _cov_for(candidate_b_rolling_ops)
    cand_c = _cov_for_pair(candidate_c_rolling_obp_slg)
    cand_d = _cov_for(candidate_d_blend_ops)

    # ---- 8. Earliest usable game date for rolling metrics -----------
    # Rolling-30d requires paired snapshots (one strictly before game,
    # one at or before D-30). Estimate the earliest game in the window
    # for which BOTH teams have BOTH snapshots.
    earliest_rolling_capable = None
    for g in games:
        gd = g.first_pitch.date()
        h_before = _has_strict_before(g.home_team_id, gd)
        a_before = _has_strict_before(g.away_team_id, gd)
        # For rolling: also need a snapshot at or before (gd - 30d).
        cutoff = gd - timedelta(days=30)

        def _has_at_or_before(tid, target):
            arr = by_team_dates.get(tid, [])
            if not arr:
                return False
            import bisect
            idx = bisect.bisect_right(arr, target)
            return idx > 0

        if h_before and a_before and _has_at_or_before(g.home_team_id, cutoff) \
                and _has_at_or_before(g.away_team_id, cutoff):
            earliest_rolling_capable = gd.isoformat()
            break

    # ---- 9. Trustworthiness verdict ---------------------------------
    verdict, reasons = _trustworthiness(
        snap_total=snap_total,
        expected_pairs=expected_pairs,
        legitimate_empty=len(legitimate_empty),
        suspect_missing=len(suspect_missing),
        game_both_pct=both_pct,
        cand_b_pct=cand_b['pct'],
        cand_d_pct=cand_d['pct'],
        earliest_rolling=earliest_rolling_capable,
    )

    return {
        'reference_date': reference_date.isoformat(),
        'analysis_window': {
            'from': analysis_from.isoformat(),
            'to': analysis_to.isoformat(),
            'days': days,
        },
        'backfill_window': {
            'from': backfill_from.isoformat(),
            'to': backfill_to.isoformat(),
        },
        'snap_total': snap_total,
        'teams_in_scope': n_teams,
        'run_summary': runs_dump,
        'requirements': {
            'expected_pairs': expected_pairs,
            'present_pairs': len(present_pairs),
            'missing_pairs': expected_pairs - len(present_pairs),
            'coverage_pct': round(100.0 * len(present_pairs) / max(1, expected_pairs), 2),
        },
        'missing_classification': {
            'legitimate_empty': len(legitimate_empty),
            'suspect_missing': len(suspect_missing),
            'legitimate_empty_note': (
                'Legitimate empty = target date is before the team\'s '
                'earliest final game in the DB. MLB byDateRange returns '
                'no splits for these — expected, not a failure.'
            ),
            'suspect_missing_note': (
                'Suspect missing = target date is on/after the team\'s '
                'first final game, so the API SHOULD have returned data. '
                'These are the retry candidates.'
            ),
        },
        'error_hotspots': {
            'suspect_by_team_top10': top_teams,
            'suspect_by_date_top10': top_dates,
            'suspect_by_month': monthly_suspect,
        },
        'game_coverage': {
            'window_from': analysis_from.isoformat(),
            'window_to': analysis_to.isoformat(),
            'total_games': len(games),
            'both_covered': both_cov,
            'home_only': home_only,
            'away_only': away_only,
            'neither_covered': neither,
            'both_covered_pct': both_pct,
            'by_month': [
                {'month': m,
                 'total': v['total'], 'both': v['both'],
                 'both_pct': round(100.0 * v['both'] / max(1, v['total']), 2)}
                for m, v in sorted(by_month_cov.items())
            ],
            'by_home_team': sorted(
                [
                    {'team': team_lookup.get(tid, str(tid)),
                     'total': v['total'], 'both': v['both'],
                     'both_pct': round(100.0 * v['both'] / max(1, v['total']), 2)}
                    for tid, v in by_team_cov.items()
                ],
                key=lambda x: x['both_pct'],
            ),
        },
        'candidate_coverage_sample': {
            'sample_size': len(sample),
            'total_games_in_window': len(games),
            'candidate_b_rolling_ops': cand_b,
            'candidate_c_rolling_obp_slg': cand_c,
            'candidate_d_blend_season_recent_ops': cand_d,
            'note': (
                'Coverage = % of sampled games where BOTH teams\' '
                'candidate signals return non-low confidence. Sample '
                'is evenly spaced across the window.'
            ),
        },
        'earliest_rolling_capable_game_date': earliest_rolling_capable,
        'trustworthiness': {
            'verdict': verdict,
            'reasons': reasons,
        },
    }


def _trustworthiness(
    *, snap_total, expected_pairs, legitimate_empty,
    suspect_missing, game_both_pct, cand_b_pct, cand_d_pct,
    earliest_rolling,
):
    """Rules — pre-registered thresholds. Any FAIL → HOLD."""
    reasons = []

    # Snapshot count present.
    if snap_total >= 3000:
        reasons.append(('PASS', f'snapshot_total={snap_total} >= 3000 (sufficient volume)'))
    else:
        reasons.append(('FAIL', f'snapshot_total={snap_total} < 3000'))

    # Suspect missing must be a small fraction of expected.
    if expected_pairs > 0:
        suspect_pct = 100.0 * suspect_missing / expected_pairs
        if suspect_pct <= 5.0:
            reasons.append(('PASS',
                            f'suspect_missing={suspect_missing} '
                            f'({suspect_pct:.1f}% of expected) — within tolerance'))
        else:
            reasons.append(('FAIL',
                            f'suspect_missing={suspect_missing} '
                            f'({suspect_pct:.1f}% of expected) — retry required'))

    # Game-level both-covered must clear 80%.
    if game_both_pct >= 80.0:
        reasons.append(('PASS',
                        f'game both-team coverage={game_both_pct:.1f}% >= 80%'))
    else:
        reasons.append(('FAIL',
                        f'game both-team coverage={game_both_pct:.1f}% < 80% — retry required'))

    # Candidate B (rolling OPS) coverage on the sample.
    if cand_b_pct >= 60.0:
        reasons.append(('PASS',
                        f'candidate_b_rolling_ops sample coverage={cand_b_pct:.1f}% >= 60%'))
    else:
        reasons.append(('WARN',
                        f'candidate_b_rolling_ops sample coverage={cand_b_pct:.1f}% < 60% — '
                        'may still promote but on smaller sample'))

    # Earliest rolling-capable game must exist.
    if earliest_rolling is not None:
        reasons.append(('PASS',
                        f'rolling-capable window starts {earliest_rolling}'))
    else:
        reasons.append(('FAIL',
                        'no game in window has both teams with paired '
                        'snapshots for rolling-30d — retry required'))

    failed = any(r[0] == 'FAIL' for r in reasons)
    return ('HOLD' if failed else 'READY'), reasons


def render_team_batting_audit(a: Dict[str, Any]) -> str:
    lines = []
    lines.append('#' * 100)
    lines.append('#  TEAM-BATTING BACKFILL AUDIT')
    lines.append(f'#  reference_date={a["reference_date"]}  '
                 f'analysis={a["analysis_window"]["from"]}..'
                 f'{a["analysis_window"]["to"]}  '
                 f'backfill={a["backfill_window"]["from"]}..'
                 f'{a["backfill_window"]["to"]}')
    lines.append('#' * 100)
    lines.append('')

    lines.append('SNAPSHOT INVENTORY')
    lines.append('-' * 78)
    lines.append(f'  total snapshots in DB   : {a["snap_total"]}')
    r = a['requirements']
    lines.append(f'  expected pairs (backfill window × teams): {r["expected_pairs"]}')
    lines.append(f'  present pairs           : {r["present_pairs"]}')
    lines.append(f'  missing pairs           : {r["missing_pairs"]} '
                 f'({r["coverage_pct"]:.1f}% covered)')
    lines.append('')

    lines.append('MISSING CLASSIFICATION')
    lines.append('-' * 78)
    mc = a['missing_classification']
    lines.append(f'  legitimate empty (pre-season / team not yet playing)'
                 f' : {mc["legitimate_empty"]}')
    lines.append(f'  suspect missing (should have data — retry candidates) '
                 f': {mc["suspect_missing"]}')
    lines.append(f'    → {mc["legitimate_empty_note"]}')
    lines.append(f'    → {mc["suspect_missing_note"]}')
    lines.append('')

    lines.append('ERROR HOTSPOTS (suspect_missing clustering)')
    lines.append('-' * 78)
    eh = a['error_hotspots']
    if eh['suspect_by_team_top10']:
        lines.append('  Top-10 teams with missing snapshots:')
        for row in eh['suspect_by_team_top10']:
            lines.append(f'    {row["team"]:>4}: {row["missing_count"]}')
    else:
        lines.append('  (no suspect-missing pairs — nothing to cluster)')
    if eh['suspect_by_date_top10']:
        lines.append('  Top-10 dates with missing snapshots:')
        for row in eh['suspect_by_date_top10']:
            lines.append(f'    {row["date"]}: {row["missing_count"]}')
    if eh['suspect_by_month']:
        lines.append('  Suspect missing by month:')
        for row in eh['suspect_by_month']:
            lines.append(f'    {row["month"]}: {row["missing_count"]}')
    lines.append('')

    lines.append('GAME-LEVEL COVERAGE (for isolated analysis window)')
    lines.append('-' * 78)
    gc = a['game_coverage']
    lines.append(f'  window                  : {gc["window_from"]}..{gc["window_to"]}')
    lines.append(f'  total evaluable games   : {gc["total_games"]}')
    lines.append(f'  both teams covered      : {gc["both_covered"]} '
                 f'({gc["both_covered_pct"]:.1f}%)')
    lines.append(f'  home-only covered       : {gc["home_only"]}')
    lines.append(f'  away-only covered       : {gc["away_only"]}')
    lines.append(f'  neither covered         : {gc["neither_covered"]}')
    lines.append('  by month:')
    for row in gc['by_month']:
        lines.append(f'    {row["month"]}: n={row["total"]:>4}  '
                     f'both={row["both"]:>4} ({row["both_pct"]:.1f}%)')
    lines.append('  by home team (sorted by coverage %):')
    for row in gc['by_home_team']:
        lines.append(f'    {row["team"]:>4}: n={row["total"]:>4}  '
                     f'both={row["both"]:>4} ({row["both_pct"]:.1f}%)')
    lines.append('')

    lines.append('CANDIDATE-SPECIFIC COVERAGE (sampled)')
    lines.append('-' * 78)
    cc = a['candidate_coverage_sample']
    lines.append(f'  sample size             : {cc["sample_size"]} '
                 f'(from {cc["total_games_in_window"]} in-window games)')
    for k, label in [('candidate_b_rolling_ops', 'B (rolling 30d OPS)'),
                     ('candidate_c_rolling_obp_slg', 'C (rolling OBP+SLG)'),
                     ('candidate_d_blend_season_recent_ops',
                      'D (season+recent OPS blend)')]:
        v = cc[k]
        lines.append(f'  {label:>32}: {v["ok"]}/{v["total"]} '
                     f'({v["pct"]:.1f}%)')
    lines.append(f'  → {cc["note"]}')
    lines.append('')

    lines.append(f'earliest rolling-capable game: '
                 f'{a["earliest_rolling_capable_game_date"] or "NONE"}')
    lines.append('')

    lines.append('RECENT RUN SUMMARY (most recent first)')
    lines.append('-' * 78)
    for r in a['run_summary']:
        lines.append(
            f'  {r["kind"]:<11}  {r["status"]:<22}  '
            f'{r["date_from"]}..{r["date_to"]}  '
            f'only_missing={r["only_missing"]}  '
            f'elapsed={r["elapsed_seconds"]}s'
        )
        c = r['counters']
        lines.append(
            f'      attempted={c["fetches_attempted"]}  '
            f'succeeded={c["fetches_succeeded"]}  '
            f'empty={c["fetches_empty"]}  errored={c["fetches_errored"]}  '
            f'created={c["snapshots_created"]}  updated={c["snapshots_updated"]}'
        )
        if r['failure_summary']:
            lines.append(f'      FAILURE: {r["failure_summary"]}')
        # Include the trailing 400 chars of log_tail so the operator
        # sees actual error text without needing another endpoint.
        if r['log_tail']:
            tail = r['log_tail'][-400:].replace('\n', '\n        ')
            lines.append(f'      log tail: {tail}')
    lines.append('')

    lines.append('=' * 78)
    lines.append('TRUSTWORTHINESS VERDICT')
    lines.append('-' * 78)
    t = a['trustworthiness']
    lines.append(f'  {t["verdict"]}')
    for r in t['reasons']:
        lines.append(f'    [{r[0]}] {r[1]}')
    lines.append('=' * 78)
    if t['verdict'] == 'READY':
        lines.append('  → Dataset is trustworthy. Safe to trigger '
                     '"Run Isolated Predictive-Value Analysis".')
    else:
        lines.append('  → Dataset needs retry. Trigger '
                     '"Retry Missing Snapshots" to fill suspect gaps '
                     'without re-fetching successful rows.')
    return '\n'.join(lines)
