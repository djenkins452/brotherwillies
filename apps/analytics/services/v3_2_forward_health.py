"""V3.2 forward-validation health service (2026-08-24).

Answers the strategic question raised after the team-offense NO-GO
closure:

  Does V3.2's ~71-72% win / +21-22% ROI historical replay performance
  translate into real forward operation, or is production drifting?

READ-ONLY. Never modifies recommendations, never activates flags.

DATA SOURCE
  * BettingRecommendation rows with `sport='mlb'`, `model_source='house'`
    (the V3.2 production model — user-tuned MockBet models are excluded
    entirely; user-placed bets DO NOT judge model quality).
  * Filtered to `status='recommended'` AND `lane='core'` — the
    lane-corrected slate the model would have acted on.
  * Outcome computed on the fly from the linked Game's home_score /
    away_score vs the persisted pick side (same convention the replay
    uses; no post-hoc mutation).

WHAT WE REPORT
  Population: n_generated, n_settled_recommended (LC slate), n_unsettled.
  Aggregate: win rate + Wilson 95% CI, ROI, CLV+ rate, avg CLV,
             avg recommended probability.
  Distribution: recommendations/day, odds distribution, edge distribution,
                probability distribution, tier/lane split.
  Cohort matching (vs pre-registered replay baselines):
    * probability buckets: 60-65%, 65-70%, 70-75%, 75%+
    * edge buckets:        6-8pp, 8-10pp, 10pp+
    * home vs away
    * short favorite vs mid favorite vs underdog
  Distribution-shift flag: chi-square-ish per cohort — flags when the
    LIVE cohort n / rate materially differs from the replay reference.
  Calibration: predicted vs actual win rate per prob-bucket.
  Data integrity: unsettled game count (implies stale ingestion),
                  recommendations with null feature_contributions
                  (implies pre-v3.1 rows or missing snapshot).

VERDICT (pre-registered — chosen BEFORE opening the results)
  * INSUFFICIENT_DATA: n_settled < 30
  * HEALTHY:          n_settled >= 30 AND all rules pass
  * WATCH:            one non-fatal rule fails (e.g. small CLV drift)
  * DEGRADED:         >=2 rules fail OR win rate Wilson lower bound
                      falls below the historical baseline - 8pp

The verdict is mechanical — no operator judgment applied. That way it
can't drift with mood or narrative.
"""
from __future__ import annotations

import math
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple


# --- Pre-registered baselines from V3.2 replay evidence ---
#
# The V3.2 activation brief locked these as the accepted validated
# historical baseline (window: 60-day windows repeatedly showing
# 70-72% win / +20-22% ROI at 0.62/7pp gate on the lane-corrected slate).
REPLAY_BASELINE_WIN_RATE = 71.5      # pp
REPLAY_BASELINE_ROI      = 21.0      # pp
REPLAY_BASELINE_CLV_POS  = 55.0      # pp — from prior offense-replay run
                                       # A_v3_2_baseline block

# Ship criteria — pre-registered thresholds.
#
# All comparisons use the POINT-ESTIMATE metric (win rate / ROI /
# CLV+), not the Wilson lower bound. The Wilson CI is still reported
# for information but does not gate the verdict — at n=30 the lower
# bound naturally lies far below any baseline just from small-sample
# variance, so gating on it would flag every early sample as
# DEGRADED regardless of actual performance.
MIN_SETTLED_FOR_JUDGMENT = 30       # Below this: INSUFFICIENT_DATA
WATCH_WIN_RATE_DROP_PP   = 4.0      # -4pp from baseline → WATCH
DEGRADED_WIN_RATE_DROP_PP = 8.0     # -8pp from baseline → DEGRADED
WATCH_ROI_DROP_PP        = 5.0
DEGRADED_ROI_DROP_PP     = 12.0
WATCH_CLV_DROP_PP        = 5.0
DEGRADED_CLV_DROP_PP     = 12.0

# Cohort bucket edges.
PROB_BUCKETS = [
    ('60-65', 0.60, 0.65),
    ('65-70', 0.65, 0.70),
    ('70-75', 0.70, 0.75),
    ('75+',   0.75, 1.01),
]
EDGE_BUCKETS = [
    ('6-8',  6.0,  8.0),
    ('8-10', 8.0, 10.0),
    ('10+', 10.0, 100.0),
]


@dataclass(frozen=True)
class HealthVerdict:
    verdict: str            # 'INSUFFICIENT_DATA' | 'HEALTHY' | 'WATCH' | 'DEGRADED'
    reasons: Tuple[Tuple[str, str], ...]  # sequence of ('PASS'|'WARN'|'FAIL', message)


# ---------------------------------------------------------------------------
# Wilson score interval — 95% two-sided.


def _wilson_ci(k: int, n: int, z: float = 1.96) -> Tuple[float, float]:
    """Wilson 95% CI for a binomial proportion. Returns (lo, hi) as
    proportions (0.0..1.0)."""
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    denom = 1.0 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, center - margin), min(1.0, center + margin))


def _american_to_decimal(odds: int) -> float:
    """American odds → decimal multiplier for a $1 stake (return, not
    profit). E.g. -150 → 1.667; +150 → 2.50."""
    if odds is None:
        return 1.0
    if odds > 0:
        return 1.0 + odds / 100.0
    return 1.0 + 100.0 / abs(odds)


def _american_to_implied(odds: int) -> float:
    if odds is None:
        return 0.5
    if odds > 0:
        return 100.0 / (odds + 100.0)
    return abs(odds) / (abs(odds) + 100.0)


# ---------------------------------------------------------------------------
# Outcome computation


def _pick_side(rec) -> Optional[str]:
    """Return 'home' or 'away' from the rec's pick text vs the linked
    game's team names. Falls back to None if it can't decide."""
    game = rec.game
    if game is None or rec.pick is None:
        return None
    pick_str = str(rec.pick).strip().lower()
    home_name = str(getattr(game, 'home_team', None) or '').lower()
    away_name = str(getattr(game, 'away_team', None) or '').lower()
    # Straight match first — pick is usually the team's full name.
    if pick_str and pick_str in home_name:
        return 'home'
    if pick_str and pick_str in away_name:
        return 'away'
    if home_name and home_name in pick_str:
        return 'home'
    if away_name and away_name in pick_str:
        return 'away'
    return None


def _rec_outcome(rec) -> Optional[bool]:
    """True if the recommendation would have won (score outcome
    matches pick side). None for pushes, unfinished games, or
    unparseable picks."""
    game = rec.game
    if game is None:
        return None
    if game.home_score is None or game.away_score is None:
        return None
    if game.home_score == game.away_score:
        return None  # push (extremely rare in MLB)
    side = _pick_side(rec)
    if side is None:
        return None
    home_won = game.home_score > game.away_score
    return (side == 'home' and home_won) or (side == 'away' and not home_won)


def _rec_profit(rec, won: Optional[bool]) -> Optional[float]:
    """Per-$1-stake profit at rec.odds_american."""
    if won is None or rec.odds_american is None:
        return None
    if won:
        return _american_to_decimal(rec.odds_american) - 1.0
    return -1.0


def _rec_clv(rec, closing_market_prob: Optional[float]) -> Optional[float]:
    """CLV (in probability points) = implied prob at recommendation
    time minus implied prob at close. Positive = beat the close."""
    if rec.odds_american is None or closing_market_prob is None:
        return None
    open_implied = _american_to_implied(rec.odds_american)
    # closing_market_prob is stored as HOME probability; if the pick is
    # the home side, CLV = open_home - close_home. For away pick we
    # invert to the away perspective.
    side = _pick_side(rec)
    if side == 'home':
        return open_implied - closing_market_prob
    if side == 'away':
        return open_implied - (1.0 - closing_market_prob)
    return None


def _closing_market_prob_for(rec) -> Optional[float]:
    """Return the linked game's closing market prob (from a
    ModelResultSnapshot if resolve_outcomes has run) or None."""
    from apps.analytics.models import ModelResultSnapshot
    game = rec.game
    if game is None:
        return None
    try:
        snap = (
            ModelResultSnapshot.objects
            .filter(mlb_game=game)
            .exclude(closing_market_prob__isnull=True)
            .order_by('-captured_at')
            .first()
        )
    except Exception:
        return None
    return snap.closing_market_prob if snap else None


# ---------------------------------------------------------------------------
# Cohort bucketing


def _bucket_for_value(value: float, buckets) -> Optional[str]:
    if value is None:
        return None
    for label, lo, hi in buckets:
        if lo <= value < hi:
            return label
    return None


# ---------------------------------------------------------------------------
# Main assembly


def compute_forward_health(*, days: int = 30) -> Dict[str, Any]:
    """Assemble the full forward-health report from the autonomous
    canonical capture population.

    2026-08-24 pivot: was previously reading `BettingRecommendation`
    filtered by model_source='house' — but persistence there was
    gated on user activity (place_mock_bet / bulk_actions), so the
    report was always generated=0 in production. Now reads
    `ForwardValidationSnapshot` written every refresh cycle by
    `capture_v3_2_validation`.
    """
    from django.utils import timezone
    from apps.analytics.models import ForwardValidationSnapshot
    from apps.analytics.services.v3_2_capture import (
        capture_pending, ENGINE_VERSION, MIN_WINDOW_MIN, MAX_WINDOW_MIN,
        get_forward_validation_started_at,
    )
    from apps.mlb.models import Game

    now = timezone.now()
    cutoff = now - timedelta(days=days)

    all_snaps = list(
        ForwardValidationSnapshot.objects
        .filter(engine_version=ENGINE_VERSION,
                captured_at__gte=cutoff)
        .select_related('mlb_game', 'mlb_game__home_team',
                        'mlb_game__away_team')
        .order_by('captured_at')
    )
    generated = len(all_snaps)
    rec_snaps = [s for s in all_snaps if s.decision_class == 'recommended']
    settled = [s for s in rec_snaps if s.settled_at is not None]
    unsettled = [s for s in rec_snaps if s.settled_at is None]

    aggregate = _aggregate_metrics_snap(settled)
    distribution = _distribution_metrics_snap(rec_snaps)
    cohorts = _cohort_metrics_snap(settled)
    calibration = _calibration_metrics_snap(settled)
    capture_health = _capture_health(days=days, now=now)
    capture_health['cadence'] = _refresh_cadence_audit(now=now, hours=24)
    capture_health['missed_captures'] = _missed_capture_details(
        now=now, days=days,
    )
    integrity = {
        'total_snaps_in_window': len(all_snaps),
        'recommended_snaps': len(rec_snaps),
        'potential_snaps': sum(1 for s in all_snaps
                               if s.decision_class == 'potential'),
        'not_recommended_snaps': sum(1 for s in all_snaps
                                     if s.decision_class == 'not_recommended'),
        'no_signal_snaps': sum(1 for s in all_snaps
                               if s.decision_class == 'no_signal'),
        'snaps_missing_feature_contributions': sum(
            1 for s in all_snaps if not s.feature_contributions
        ),
    }
    forward_started = get_forward_validation_started_at()
    verdict = _compute_verdict(
        n_settled=len(settled),
        aggregate=aggregate,
        capture_health=capture_health,
        forward_started=forward_started,
    )
    return {
        'window': {'days': days,
                   'from': cutoff.date().isoformat(),
                   'to': timezone.localdate().isoformat()},
        'engine_version': ENGINE_VERSION,
        'canonical_window': {
            'min_min_to_first_pitch': MIN_WINDOW_MIN,
            'max_min_to_first_pitch': MAX_WINDOW_MIN,
            'target_min_to_first_pitch': (MIN_WINDOW_MIN + MAX_WINDOW_MIN) // 2,
        },
        'forward_validation_started_at': (
            forward_started.isoformat() if forward_started else None
        ),
        'population': {
            'total_captured': generated,
            'recommended': len(rec_snaps),
            'settled': len(settled),
            'unsettled': len(unsettled),
        },
        'capture_health': capture_health,
        'aggregate': aggregate,
        'distribution': distribution,
        'cohorts': cohorts,
        'calibration': calibration,
        'integrity': integrity,
        'baseline': {
            'replay_win_rate_pp': REPLAY_BASELINE_WIN_RATE,
            'replay_roi_pp': REPLAY_BASELINE_ROI,
            'replay_clv_pos_pp': REPLAY_BASELINE_CLV_POS,
        },
        'verdict': {
            'verdict': verdict.verdict,
            'reasons': [{'level': l, 'message': m} for l, m in verdict.reasons],
        },
    }


def _capture_health(*, days: int, now) -> Dict[str, Any]:
    """CAPTURE HEALTH block — was capture actually running?

    2026-08-24 fix: the previous denominator counted every past-window
    game in the reporting range. That erased the scientific boundary
    between "before autonomous capture existed" and "after". A game
    whose canonical capture window closed BEFORE the activation
    timestamp was never eligible for prospective capture and MUST NOT
    count as missed.

    Corrected denominator:
      post_activation_eligible = games whose canonical capture opportunity
      (first_pitch - MAX_WINDOW_MIN .. first_pitch - MIN_WINDOW_MIN) fell
      AFTER activation AND has now passed (is_final = True).

    Coverage % is computed against post_activation_eligible only. If
    that's 0, verdict = AWAITING_FIRST_CAPTURE (not DATA_COLLECTION_
    DEGRADED). No historical games count as missed.
    """
    from apps.analytics.models import ForwardValidationSnapshot
    from apps.analytics.services.v3_2_capture import (
        ENGINE_VERSION, MIN_WINDOW_MIN, MAX_WINDOW_MIN, activation_at,
    )
    from apps.mlb.models import Game

    activation = activation_at()
    cutoff = now - timedelta(days=days)

    # Every MLB game whose first_pitch fell inside the reporting window
    # AND is now final. These are the RAW candidates — some are pre-
    # activation (excluded from denominator), some post-activation.
    report_window_games = list(
        Game.objects
        .filter(source='mlb_stats_api',
                first_pitch__gte=cutoff,
                first_pitch__lt=now,
                status='final')
        .only('id', 'first_pitch')
    )

    # Classify each game by whether its canonical capture WINDOW END
    # (first_pitch - MIN_WINDOW_MIN) fell after activation.
    # Rationale: a game whose T-45min moment was before activation had
    # ZERO chance of being captured prospectively. Even if part of its
    # window opened before activation, we still count it if the window
    # closed after activation — a partial post-activation window is
    # enough opportunity for the scheduler to fire.
    pre_activation_ids = set()
    post_activation_eligible = []
    for g in report_window_games:
        capture_window_end = g.first_pitch - timedelta(minutes=MIN_WINDOW_MIN)
        if capture_window_end < activation:
            pre_activation_ids.add(g.id)
        else:
            post_activation_eligible.append(g)

    post_activation_eligible_ids = {g.id for g in post_activation_eligible}
    captured_ids = set(
        ForwardValidationSnapshot.objects
        .filter(engine_version=ENGINE_VERSION,
                mlb_game_id__in=post_activation_eligible_ids)
        .values_list('mlb_game_id', flat=True)
    )
    missed_ids = post_activation_eligible_ids - captured_ids

    total_captured_in_window = ForwardValidationSnapshot.objects.filter(
        engine_version=ENGINE_VERSION,
        captured_at__gte=max(cutoff, activation),
    ).count()

    avg_min_to_fp = None
    if total_captured_in_window:
        vals = list(
            ForwardValidationSnapshot.objects
            .filter(engine_version=ENGINE_VERSION,
                    captured_at__gte=max(cutoff, activation))
            .values_list('minutes_to_first_pitch', flat=True)
        )
        if vals:
            avg_min_to_fp = round(sum(vals) / len(vals), 1)

    # Currently-in-window (would be captured on next refresh cycle).
    window_lo = now + timedelta(minutes=MIN_WINDOW_MIN)
    window_hi = now + timedelta(minutes=MAX_WINDOW_MIN)
    in_window = list(
        Game.objects
        .filter(source='mlb_stats_api',
                first_pitch__gte=window_lo,
                first_pitch__lte=window_hi)
        .exclude(status='final')
        .only('id', 'first_pitch', 'home_team_id', 'away_team_id')
        .order_by('first_pitch')[:20]
    )

    # Next eligible upcoming game (first game whose first_pitch is
    # after now + MIN_WINDOW_MIN — the next scheduled auto-capture).
    next_upcoming = (
        Game.objects
        .filter(source='mlb_stats_api',
                first_pitch__gt=window_hi)
        .exclude(status='final')
        .order_by('first_pitch')
        .only('id', 'first_pitch')
        .first()
    )

    denom = max(1, len(post_activation_eligible_ids))
    coverage_pct = round(100.0 * len(captured_ids) / denom, 2) \
        if post_activation_eligible_ids else None
    return {
        'window_days': days,
        'activation_at': activation.isoformat(),
        # NEW: pre/post activation split so the denominator is honest.
        'report_window_games': len(report_window_games),
        'pre_activation_excluded': len(pre_activation_ids),
        'post_activation_eligible': len(post_activation_eligible_ids),
        'snapshots_captured_for_eligible': len(captured_ids),
        'missed_eligible': len(missed_ids),
        'capture_coverage_pct': coverage_pct,
        'total_snapshots_written_since_activation': total_captured_in_window,
        'avg_min_to_first_pitch': avg_min_to_fp,
        'currently_in_window': [
            {'game_id': str(g.id), 'first_pitch': g.first_pitch.isoformat()}
            for g in in_window
        ],
        'next_upcoming': (
            {
                'game_id': str(next_upcoming.id),
                'first_pitch': next_upcoming.first_pitch.isoformat(),
                'canonical_window_start': (
                    (next_upcoming.first_pitch
                     - timedelta(minutes=MAX_WINDOW_MIN)).isoformat()
                ),
                'canonical_window_end': (
                    (next_upcoming.first_pitch
                     - timedelta(minutes=MIN_WINDOW_MIN)).isoformat()
                ),
            }
            if next_upcoming else None
        ),
        # Cadence audit + missed-capture classification are computed
        # separately and merged in by compute_forward_health.
    }


def _refresh_cadence_audit(*, now, hours: int = 24) -> Dict[str, Any]:
    """Inspect refresh_data run timestamps and compute median/max
    intervals + longest gap. Answers mechanically: can the current
    cadence guarantee at least one execution inside every 30-minute
    canonical capture window?"""
    from apps.ops.models import CronRunLog
    from apps.analytics.services.v3_2_capture import (
        MIN_WINDOW_MIN, MAX_WINDOW_MIN,
    )

    since = now - timedelta(hours=hours)
    runs = list(
        CronRunLog.objects
        .filter(command='refresh_data', started_at__gte=since)
        .order_by('started_at')
        .values('started_at', 'status')
    )
    n = len(runs)
    if n < 2:
        return {
            'window_hours': hours,
            'run_count': n,
            'runs': [r['started_at'].isoformat() for r in runs],
            'median_interval_min': None,
            'max_interval_min': None,
            'longest_gap_min': None,
            'failed_run_count': sum(1 for r in runs if r['status'] != 'success'),
            'guarantees_capture': False,
            'guarantee_reason': (
                f'only {n} refresh_data run(s) in the last {hours}h — '
                'cannot compute cadence yet.'
            ),
        }
    intervals = []
    for a, b in zip(runs, runs[1:]):
        intervals.append(int((b['started_at'] - a['started_at'])
                             .total_seconds() / 60))
    intervals.sort()
    mid = intervals[len(intervals) // 2]
    mx = max(intervals)
    canonical_width = MAX_WINDOW_MIN - MIN_WINDOW_MIN  # 30
    # Guaranteed capture requires MAX interval < canonical window width.
    guarantees = mx < canonical_width
    return {
        'window_hours': hours,
        'run_count': n,
        'runs_sample_first_5': [r['started_at'].isoformat()
                                for r in runs[:5]],
        'runs_sample_last_5': [r['started_at'].isoformat()
                               for r in runs[-5:]],
        'median_interval_min': mid,
        'max_interval_min': mx,
        'longest_gap_min': mx,
        'canonical_window_width_min': canonical_width,
        'failed_run_count': sum(1 for r in runs if r['status'] != 'success'),
        'guarantees_capture': guarantees,
        'guarantee_reason': (
            f'max interval {mx}min < canonical window width {canonical_width}min'
            if guarantees else
            f'max interval {mx}min >= canonical window width {canonical_width}min '
            '— a game whose first_pitch lands during that gap CAN be missed. '
            'Tighten refresh_data cadence or split capture onto a dedicated '
            'sub-30min schedule.'
        ),
    }


def _missed_capture_details(*, now, days: int) -> List[Dict[str, Any]]:
    """For each post-activation eligible game with no snapshot, classify
    the miss and record scheduler runs that were active during its
    canonical window."""
    from apps.analytics.models import ForwardValidationSnapshot
    from apps.analytics.services.v3_2_capture import (
        ENGINE_VERSION, MIN_WINDOW_MIN, MAX_WINDOW_MIN, activation_at,
    )
    from apps.mlb.models import Game
    from apps.ops.models import CronRunLog

    activation = activation_at()
    cutoff = now - timedelta(days=days)
    games = list(
        Game.objects
        .filter(source='mlb_stats_api',
                first_pitch__gte=cutoff,
                first_pitch__lt=now,
                status='final')
        .only('id', 'first_pitch')
    )
    captured = set(
        ForwardValidationSnapshot.objects
        .filter(engine_version=ENGINE_VERSION,
                mlb_game_id__in={g.id for g in games})
        .values_list('mlb_game_id', flat=True)
    )
    misses = []
    for g in games:
        # Same eligibility filter used above.
        window_start = g.first_pitch - timedelta(minutes=MAX_WINDOW_MIN)
        window_end = g.first_pitch - timedelta(minutes=MIN_WINDOW_MIN)
        if window_end < activation:
            continue
        if g.id in captured:
            continue
        # A miss — classify by whether refresh_data fired during window.
        overlapping = CronRunLog.objects.filter(
            command='refresh_data',
            started_at__gte=window_start,
            started_at__lte=window_end,
        ).values('started_at', 'status')
        overlap_list = list(overlapping)
        if not overlap_list:
            classification = 'SCHEDULER_MISS'
            reason = ('refresh_data never fired between '
                      f'{window_start.isoformat()} and '
                      f'{window_end.isoformat()}')
        else:
            successful = [r for r in overlap_list if r['status'] == 'success']
            if not successful:
                classification = 'SCHEDULER_MISS'
                reason = (f'refresh_data fired {len(overlap_list)} '
                          f'time(s) in-window but none succeeded')
            else:
                # scheduler DID fire — something else broke. Cannot
                # distinguish DATA_UNAVAILABLE vs MODEL_ERROR from the
                # CronRunLog alone (would need per-command tail parse).
                # Tag OTHER for now with the diagnostic hint.
                classification = 'OTHER'
                reason = (f'refresh_data fired successfully during window '
                          f'but no snapshot exists — likely the capture '
                          'sub-step raised (odds missing / model error) '
                          'without failing the parent run.')
        misses.append({
            'game_id': str(g.id),
            'first_pitch': g.first_pitch.isoformat(),
            'canonical_window_start': window_start.isoformat(),
            'canonical_window_end': window_end.isoformat(),
            'scheduler_runs_in_window': len(overlap_list),
            'classification': classification,
            'reason': reason,
        })
    return misses[:100]


def _aggregate_metrics_snap(settled_snaps) -> Dict[str, Any]:
    """Snapshot-based version of _aggregate_metrics. Reads
    ForwardValidationSnapshot rows that have been settled."""
    n = len(settled_snaps)
    if n == 0:
        return {'n': 0, 'wins': 0, 'losses': 0,
                'win_rate_pp': None, 'wilson_lo_pp': None, 'wilson_hi_pp': None,
                'roi_pp': None, 'clv_pos_pp': None,
                'avg_clv_pp': None, 'avg_prob_pp': None,
                'net_p_l_per_dollar': None, 'clv_sample_n': 0}
    wins = sum(1 for s in settled_snaps if s.won is True)
    losses = sum(1 for s in settled_snaps if s.won is False)
    profits = [s.profit_per_dollar for s in settled_snaps
               if s.profit_per_dollar is not None]
    total_profit = sum(profits)
    lo, hi = _wilson_ci(wins, wins + losses)
    clvs = [s.clv_pp for s in settled_snaps if s.clv_pp is not None]
    pos_clvs = [c for c in clvs if c > 0]
    probs = [s.final_model_prob for s in settled_snaps
             if s.final_model_prob is not None]
    return {
        'n': n, 'wins': wins, 'losses': losses,
        'win_rate_pp': round(100.0 * wins / max(1, wins + losses), 2)
                       if (wins + losses) else None,
        'wilson_lo_pp': round(100.0 * lo, 2),
        'wilson_hi_pp': round(100.0 * hi, 2),
        'roi_pp': round(100.0 * total_profit / len(profits), 2) if profits else None,
        'net_p_l_per_dollar': round(total_profit, 2) if profits else None,
        'clv_sample_n': len(clvs),
        'clv_pos_pp': round(100.0 * len(pos_clvs) / len(clvs), 2) if clvs else None,
        'avg_clv_pp': round(sum(clvs) / len(clvs), 2) if clvs else None,
        'avg_prob_pp': round(100.0 * sum(probs) / len(probs), 2) if probs else None,
    }


def _distribution_metrics_snap(rec_snaps) -> Dict[str, Any]:
    by_day: Counter = Counter()
    odds: List[int] = []
    edges: List[float] = []
    probs: List[float] = []
    tier_ct = Counter()
    lane_ct = Counter()
    for s in rec_snaps:
        by_day[s.captured_at.date().isoformat()] += 1
        if s.odds_american is not None:
            odds.append(int(s.odds_american))
        if s.edge_pp is not None:
            edges.append(float(s.edge_pp))
        if s.final_model_prob is not None:
            probs.append(float(s.final_model_prob))
        tier_ct[s.tier or ''] += 1
        lane_ct[s.lane or ''] += 1
    return {
        'per_day': dict(sorted(by_day.items())),
        'avg_per_day': (sum(by_day.values()) / len(by_day)) if by_day else 0,
        'odds_distribution': _distribution_summary(odds),
        'edge_distribution': _distribution_summary(edges),
        'prob_distribution': _distribution_summary([p * 100 for p in probs]),
        'tier_counts': dict(tier_ct),
        'lane_counts': dict(lane_ct),
    }


def _cohort_metrics_snap(settled_snaps) -> Dict[str, Any]:
    def _bucket(rows):
        n = len(rows)
        if n == 0:
            return {'n': 0}
        wins = sum(1 for r in rows if r.won is True)
        losses = sum(1 for r in rows if r.won is False)
        lo, hi = _wilson_ci(wins, wins + losses) if (wins + losses) else (0, 0)
        profits = [r.profit_per_dollar for r in rows
                   if r.profit_per_dollar is not None]
        return {
            'n': n, 'wins': wins,
            'win_rate_pp': round(100.0 * wins / max(1, wins + losses), 2)
                           if (wins + losses) else None,
            'wilson_lo_pp': round(100.0 * lo, 2),
            'wilson_hi_pp': round(100.0 * hi, 2),
            'roi_pp': round(100.0 * sum(profits) / len(profits), 2) if profits else None,
        }

    by_prob = {label: [] for label, _, _ in PROB_BUCKETS}
    for s in settled_snaps:
        p = s.final_model_prob
        if p is None: continue
        b = _bucket_for_value(float(p), PROB_BUCKETS)
        if b: by_prob[b].append(s)

    by_edge = {label: [] for label, _, _ in EDGE_BUCKETS}
    for s in settled_snaps:
        e = s.edge_pp
        if e is None: continue
        b = _bucket_for_value(float(e), EDGE_BUCKETS)
        if b: by_edge[b].append(s)

    by_side = {'home': [], 'away': []}
    for s in settled_snaps:
        if s.pick_side in by_side:
            by_side[s.pick_side].append(s)

    by_role = {'favorite_short': [], 'favorite_mid': [], 'underdog': []}
    for s in settled_snaps:
        o = s.odds_american
        if o is None: continue
        if o <= -150: by_role['favorite_mid'].append(s)
        elif o < 0:   by_role['favorite_short'].append(s)
        else:         by_role['underdog'].append(s)

    return {
        'by_probability': {k: _bucket(v) for k, v in by_prob.items()},
        'by_edge':        {k: _bucket(v) for k, v in by_edge.items()},
        'by_side':        {k: _bucket(v) for k, v in by_side.items()},
        'by_role':        {k: _bucket(v) for k, v in by_role.items()},
    }


def _calibration_metrics_snap(settled_snaps) -> Dict[str, Any]:
    rows = [s for s in settled_snaps if s.final_model_prob is not None
            and s.won is not None]
    if not rows:
        return {'bins': [], 'brier_like': None}
    rows.sort(key=lambda s: s.final_model_prob)
    n = len(rows); step = n / 5.0
    bins = []
    brier_terms = []
    for i in range(5):
        lo = int(round(i * step)); hi = int(round((i + 1) * step))
        chunk = rows[lo:hi]
        if not chunk: continue
        avg_p = sum(float(s.final_model_prob) for s in chunk) / len(chunk)
        wins = sum(1 for s in chunk if s.won)
        actual = wins / len(chunk)
        bins.append({
            'bucket': f'q{i + 1}',
            'n': len(chunk),
            'avg_predicted_pp': round(100.0 * avg_p, 2),
            'actual_win_rate_pp': round(100.0 * actual, 2),
            'diff_pp': round(100.0 * (actual - avg_p), 2),
        })
        for s in chunk:
            p = float(s.final_model_prob)
            brier_terms.append((p - (1.0 if s.won else 0.0)) ** 2)
    brier_like = round(sum(brier_terms) / len(brier_terms), 4) if brier_terms else None
    return {'bins': bins, 'brier_like': brier_like}


def _aggregate_metrics(settled_rows) -> Dict[str, Any]:
    n = len(settled_rows)
    if n == 0:
        return {'n': 0, 'wins': 0, 'losses': 0,
                'win_rate_pp': None, 'wilson_lo_pp': None, 'wilson_hi_pp': None,
                'roi_pp': None, 'clv_pos_pp': None,
                'avg_clv_pp': None, 'avg_prob_pp': None,
                'net_p_l_per_dollar': None}
    wins = sum(1 for s in settled_rows if s['won'])
    losses = n - wins
    profits = [s['profit_per_dollar'] for s in settled_rows
               if s['profit_per_dollar'] is not None]
    total_profit = sum(profits)
    lo, hi = _wilson_ci(wins, n)
    clvs = [s['clv_pp'] for s in settled_rows if s['clv_pp'] is not None]
    pos_clvs = [c for c in clvs if c > 0]
    probs = [float(s['rec'].final_model_prob or s['rec'].confidence_score / 100.0)
             for s in settled_rows
             if s['rec'].final_model_prob is not None
             or s['rec'].confidence_score is not None]
    return {
        'n': n, 'wins': wins, 'losses': losses,
        'win_rate_pp': round(100.0 * wins / n, 2),
        'wilson_lo_pp': round(100.0 * lo, 2),
        'wilson_hi_pp': round(100.0 * hi, 2),
        'roi_pp': round(100.0 * total_profit / n, 2) if profits else None,
        'net_p_l_per_dollar': round(total_profit, 2) if profits else None,
        'clv_sample_n': len(clvs),
        'clv_pos_pp': round(100.0 * len(pos_clvs) / len(clvs), 2) if clvs else None,
        'avg_clv_pp': round(sum(clvs) / len(clvs), 2) if clvs else None,
        'avg_prob_pp': round(100.0 * sum(probs) / len(probs), 2) if probs else None,
    }


def _distribution_metrics(lc_recs) -> Dict[str, Any]:
    by_day: Counter = Counter()
    odds: List[int] = []
    edges: List[float] = []
    probs: List[float] = []
    tier_ct = Counter()
    lane_ct = Counter()
    for r in lc_recs:
        by_day[r.created_at.date().isoformat()] += 1
        if r.odds_american is not None:
            odds.append(int(r.odds_american))
        if r.model_edge is not None:
            edges.append(float(r.model_edge))
        if r.final_model_prob is not None:
            probs.append(float(r.final_model_prob))
        tier_ct[r.tier] += 1
        lane_ct[r.lane] += 1
    return {
        'per_day': dict(sorted(by_day.items())),
        'avg_per_day': (sum(by_day.values()) / len(by_day)) if by_day else 0,
        'odds_distribution': _distribution_summary(odds),
        'edge_distribution': _distribution_summary(edges),
        'prob_distribution': _distribution_summary([p * 100 for p in probs]),
        'tier_counts': dict(tier_ct),
        'lane_counts': dict(lane_ct),
    }


def _distribution_summary(values):
    if not values:
        return {'n': 0}
    s = sorted(values)
    n = len(s)
    def _pct(p):
        return s[min(n - 1, int(round(p / 100.0 * (n - 1))))]
    return {
        'n': n, 'min': s[0], 'max': s[-1],
        'p10': _pct(10), 'p25': _pct(25), 'p50': _pct(50),
        'p75': _pct(75), 'p90': _pct(90),
        'mean': sum(s) / n,
    }


def _cohort_metrics(settled_rows) -> Dict[str, Any]:
    def _bucket_win_rate(rows):
        n = len(rows)
        if n == 0:
            return {'n': 0}
        wins = sum(1 for r in rows if r['won'])
        lo, hi = _wilson_ci(wins, n)
        profits = [r['profit_per_dollar'] for r in rows
                   if r['profit_per_dollar'] is not None]
        return {
            'n': n, 'wins': wins,
            'win_rate_pp': round(100.0 * wins / n, 2),
            'wilson_lo_pp': round(100.0 * lo, 2),
            'wilson_hi_pp': round(100.0 * hi, 2),
            'roi_pp': round(100.0 * sum(profits) / n, 2) if profits else None,
        }

    # Probability buckets
    by_prob = {label: [] for label, _, _ in PROB_BUCKETS}
    for s in settled_rows:
        p = s['rec'].final_model_prob
        if p is None:
            continue
        b = _bucket_for_value(float(p), PROB_BUCKETS)
        if b:
            by_prob[b].append(s)

    # Edge buckets
    by_edge = {label: [] for label, _, _ in EDGE_BUCKETS}
    for s in settled_rows:
        e = s['rec'].model_edge
        if e is None:
            continue
        b = _bucket_for_value(float(e), EDGE_BUCKETS)
        if b:
            by_edge[b].append(s)

    # Home/away — via pick_side.
    by_side = {'home': [], 'away': []}
    for s in settled_rows:
        side = _pick_side(s['rec'])
        if side in by_side:
            by_side[side].append(s)

    # Favorite/underdog by odds_american sign.
    by_role = {'favorite_short': [], 'favorite_mid': [], 'underdog': []}
    for s in settled_rows:
        o = s['rec'].odds_american
        if o is None:
            continue
        if o <= -150:
            by_role['favorite_mid'].append(s)
        elif o < 0:
            by_role['favorite_short'].append(s)
        else:
            by_role['underdog'].append(s)

    return {
        'by_probability': {k: _bucket_win_rate(v) for k, v in by_prob.items()},
        'by_edge':        {k: _bucket_win_rate(v) for k, v in by_edge.items()},
        'by_side':        {k: _bucket_win_rate(v) for k, v in by_side.items()},
        'by_role':        {k: _bucket_win_rate(v) for k, v in by_role.items()},
    }


def _calibration_metrics(settled_rows) -> Dict[str, Any]:
    """Per-bucket predicted vs actual win rate. Buckets = prob quintiles."""
    if not settled_rows:
        return {'bins': [], 'brier_like': None}
    # 5-quantile bin on final_model_prob.
    rows = [s for s in settled_rows if s['rec'].final_model_prob is not None]
    if not rows:
        return {'bins': [], 'brier_like': None}
    rows.sort(key=lambda s: s['rec'].final_model_prob)
    n = len(rows)
    step = n / 5.0
    bins = []
    brier_terms = []
    for i in range(5):
        lo = int(round(i * step))
        hi = int(round((i + 1) * step))
        chunk = rows[lo:hi]
        if not chunk:
            continue
        avg_p = sum(float(s['rec'].final_model_prob) for s in chunk) / len(chunk)
        wins = sum(1 for s in chunk if s['won'])
        actual = wins / len(chunk)
        bins.append({
            'bucket': f'q{i + 1}',
            'n': len(chunk),
            'avg_predicted_pp': round(100.0 * avg_p, 2),
            'actual_win_rate_pp': round(100.0 * actual, 2),
            'diff_pp': round(100.0 * (actual - avg_p), 2),
        })
        for s in chunk:
            p = float(s['rec'].final_model_prob)
            outcome = 1.0 if s['won'] else 0.0
            brier_terms.append((p - outcome) ** 2)
    brier_like = round(sum(brier_terms) / len(brier_terms), 4) if brier_terms else None
    return {'bins': bins, 'brier_like': brier_like}


def _integrity_findings(all_recs, lc_recs, unsettled_rows) -> Dict[str, Any]:
    missing_fc = sum(1 for r in all_recs if not r.feature_contributions)
    stale_first_pitch = 0
    unsettled_past_first_pitch = 0
    from django.utils import timezone
    now = timezone.now()
    for r in unsettled_rows:
        g = r.game
        if g is None or g.first_pitch is None:
            continue
        if g.first_pitch < now - timedelta(hours=6):
            # Game finished at least 6 hours ago but not settled — stale.
            unsettled_past_first_pitch += 1
    return {
        'total_recs_in_window': len(all_recs),
        'lane_core_recs': len(lc_recs),
        'missing_feature_contributions': missing_fc,
        'unsettled_past_first_pitch_by_6h': unsettled_past_first_pitch,
    }


def _compute_verdict(*, n_settled: int, aggregate: Dict[str, Any],
                     capture_health: Optional[Dict[str, Any]] = None,
                     forward_started=None) -> HealthVerdict:
    reasons: List[Tuple[str, str]] = []

    # Capture-health check FIRST: bad model performance because capture
    # is broken is a different diagnosis than bad model performance
    # because the model is drifting. If the capture pipeline itself is
    # unhealthy, headline that instead of pretending the model has
    # insufficient data.
    #
    # 2026-08-24 fix: denominator now uses post-activation eligible
    # only. Pre-activation games CANNOT trigger DATA_COLLECTION_DEGRADED
    # — they never had a chance to be captured.
    if capture_health is not None:
        elig = capture_health.get('post_activation_eligible', 0)
        cov = capture_health.get('capture_coverage_pct')
        if elig == 0:
            # No post-activation games have completed their window yet.
            reasons.append((
                'INFO',
                'No post-activation eligible games yet. Forward '
                'validation is ACTIVE and awaiting the first game '
                f'whose canonical capture window (T-{75}..T-{45}min) '
                'opens after activation.',
            ))
            nu = capture_health.get('next_upcoming')
            if nu:
                reasons.append((
                    'INFO',
                    f'next upcoming eligible game_id={nu["game_id"]} '
                    f'first_pitch={nu["first_pitch"]} '
                    f'canonical_window={nu["canonical_window_start"]}..'
                    f'{nu["canonical_window_end"]}',
                ))
            return HealthVerdict('AWAITING_FIRST_CAPTURE', tuple(reasons))
        if cov is not None and elig >= 10 and cov < 60.0:
            reasons.append((
                'FAIL',
                f'post-activation capture coverage {cov:.1f}% of {elig} '
                'eligible games — DATA COLLECTION DEGRADED, model '
                'metrics below are unreliable.',
            ))
            return HealthVerdict('DATA_COLLECTION_DEGRADED', tuple(reasons))
        elif cov is not None and elig >= 10 and cov < 80.0:
            reasons.append((
                'WARN',
                f'post-activation capture coverage {cov:.1f}% of {elig} '
                'eligible games — WATCH the collection pipeline.',
            ))

    if forward_started is None:
        reasons.append((
            'INFO',
            'No canonical snapshots persisted yet. Forward validation '
            'is ACTIVE and awaiting the first refresh cycle that finds '
            'a game inside the T-60min ±15min window post-activation.',
        ))
        return HealthVerdict('AWAITING_FIRST_CAPTURE', tuple(reasons))

    if n_settled < MIN_SETTLED_FOR_JUDGMENT:
        reasons.append((
            'INFO',
            f'n_settled={n_settled} < {MIN_SETTLED_FOR_JUDGMENT} — '
            'insufficient data for a judgment call. Continue collecting.',
        ))
        return HealthVerdict('INSUFFICIENT_DATA', tuple(reasons))

    fails = 0
    warns = 0

    # Win-rate rule. Uses point estimate, not Wilson lower bound —
    # small-sample lower bounds sit far below any baseline just from
    # variance and would spuriously trigger DEGRADED on healthy data.
    win = aggregate.get('win_rate_pp')
    lo = aggregate.get('wilson_lo_pp')
    hi = aggregate.get('wilson_hi_pp')
    if win is None:
        reasons.append(('WARN', 'win rate: no settled outcomes'))
        warns += 1
    else:
        drop = REPLAY_BASELINE_WIN_RATE - win
        if drop >= DEGRADED_WIN_RATE_DROP_PP:
            reasons.append((
                'FAIL',
                f'win rate {win:.2f}% is {drop:.2f}pp below baseline '
                f'{REPLAY_BASELINE_WIN_RATE:.1f}% (Wilson95=[{lo:.2f}, '
                f'{hi:.2f}]) — DEGRADED threshold breached',
            ))
            fails += 1
        elif drop >= WATCH_WIN_RATE_DROP_PP:
            reasons.append((
                'WARN',
                f'win rate {win:.2f}% is {drop:.2f}pp below baseline '
                f'{REPLAY_BASELINE_WIN_RATE:.1f}% (Wilson95=[{lo:.2f}, '
                f'{hi:.2f}]) — WATCH threshold breached',
            ))
            warns += 1
        else:
            reasons.append((
                'PASS',
                f'win rate {win:.2f}% within tolerance of baseline '
                f'{REPLAY_BASELINE_WIN_RATE:.1f}% (Wilson95=[{lo:.2f}, '
                f'{hi:.2f}])',
            ))

    # ROI rule.
    roi = aggregate.get('roi_pp')
    if roi is None:
        reasons.append(('WARN', 'ROI: no profit sample'))
        warns += 1
    else:
        drop = REPLAY_BASELINE_ROI - roi
        if drop >= DEGRADED_ROI_DROP_PP:
            reasons.append((
                'FAIL',
                f'ROI {roi:+.2f}% is {drop:.2f}pp below baseline '
                f'{REPLAY_BASELINE_ROI:+.1f}% — DEGRADED threshold breached',
            ))
            fails += 1
        elif drop >= WATCH_ROI_DROP_PP:
            reasons.append((
                'WARN',
                f'ROI {roi:+.2f}% is {drop:.2f}pp below baseline '
                f'{REPLAY_BASELINE_ROI:+.1f}% — WATCH threshold breached',
            ))
            warns += 1
        else:
            reasons.append((
                'PASS',
                f'ROI {roi:+.2f}% within tolerance of baseline '
                f'{REPLAY_BASELINE_ROI:+.1f}%',
            ))

    # CLV rule.
    clv_pos = aggregate.get('clv_pos_pp')
    if clv_pos is None:
        reasons.append(('WARN', 'CLV+: no closing-line sample'))
        warns += 1
    else:
        drop = REPLAY_BASELINE_CLV_POS - clv_pos
        if drop >= DEGRADED_CLV_DROP_PP:
            reasons.append((
                'FAIL',
                f'CLV+ {clv_pos:.1f}% is {drop:.1f}pp below baseline '
                f'{REPLAY_BASELINE_CLV_POS:.1f}% — DEGRADED breached',
            ))
            fails += 1
        elif drop >= WATCH_CLV_DROP_PP:
            reasons.append((
                'WARN',
                f'CLV+ {clv_pos:.1f}% is {drop:.1f}pp below baseline '
                f'{REPLAY_BASELINE_CLV_POS:.1f}% — WATCH breached',
            ))
            warns += 1
        else:
            reasons.append((
                'PASS',
                f'CLV+ {clv_pos:.1f}% within tolerance of baseline '
                f'{REPLAY_BASELINE_CLV_POS:.1f}%',
            ))

    # A single hard FAIL is enough for DEGRADED — a real regression on
    # win rate / ROI / CLV is not a WATCH-level event. WARN-only
    # samples map to WATCH so the operator sees the drift without
    # over-reacting.
    if fails >= 1:
        verdict = 'DEGRADED'
    elif warns > 0:
        verdict = 'WATCH'
    else:
        verdict = 'HEALTHY'

    return HealthVerdict(verdict, tuple(reasons))


# ---------------------------------------------------------------------------
# Renderer


def render_forward_health(h: Dict[str, Any]) -> str:
    lines = []
    lines.append('#' * 100)
    lines.append('#  V3.2 FORWARD-VALIDATION HEALTH')
    w = h['window']
    lines.append(f'#  window {w["from"]}..{w["to"]} ({w["days"]}d)')
    lines.append(f'#  baseline: {h["baseline"]["replay_win_rate_pp"]:.1f}% win / '
                 f'{h["baseline"]["replay_roi_pp"]:+.1f}% ROI / '
                 f'{h["baseline"]["replay_clv_pos_pp"]:.1f}% CLV+')
    lines.append('#' * 100)
    lines.append('')

    if h.get('forward_validation_started_at'):
        lines.append(f'#  forward validation started: '
                     f'{h["forward_validation_started_at"]}')
    else:
        lines.append('#  forward validation: NOT YET STARTED — '
                     'awaiting first eligible capture window post-deploy.')
    lines.append('#  engine version: ' + h.get('engine_version', 'v3_2'))
    cw = h.get('canonical_window', {})
    lines.append(f'#  canonical window: T-{cw.get("min_min_to_first_pitch")}min '
                 f'to T-{cw.get("max_min_to_first_pitch")}min '
                 f'(target ~T-{cw.get("target_min_to_first_pitch")}min)')
    lines.append('')

    ch = h.get('capture_health', {})
    lines.append('CAPTURE HEALTH (autonomous — no user activity required)')
    lines.append('-' * 78)
    lines.append(f'  activation_at                : {ch.get("activation_at")}')
    lines.append(f'  report-window games          : {ch.get("report_window_games", 0)}')
    lines.append(f'  pre-activation excluded      : {ch.get("pre_activation_excluded", 0)}'
                 ' (never eligible; not counted as missed)')
    lines.append(f'  post-activation eligible     : {ch.get("post_activation_eligible", 0)}')
    lines.append(f'  captured (of post-activation): {ch.get("snapshots_captured_for_eligible", 0)}'
                 f'  (coverage {ch.get("capture_coverage_pct")}%)')
    lines.append(f'  missed post-activation       : {ch.get("missed_eligible", 0)}')
    lines.append(f'  total snapshots since activation: {ch.get("total_snapshots_written_since_activation", 0)}')
    lines.append(f'  avg min-to-first-pitch       : {ch.get("avg_min_to_first_pitch")}')
    in_win = ch.get('currently_in_window', [])
    lines.append(f'  currently in window          : {len(in_win)} game(s)')
    for g in in_win[:10]:
        lines.append(f'    - game_id={g["game_id"]} first_pitch={g["first_pitch"]}')
    nu = ch.get('next_upcoming')
    if nu:
        lines.append(f'  next upcoming eligible       : game_id={nu["game_id"]}')
        lines.append(f'    first_pitch                : {nu["first_pitch"]}')
        lines.append(f'    canonical window           : '
                     f'{nu["canonical_window_start"]} .. {nu["canonical_window_end"]}')
    else:
        lines.append('  next upcoming eligible       : none scheduled')
    lines.append('')

    cad = ch.get('cadence', {})
    lines.append('REFRESH_DATA CADENCE (last 24h)')
    lines.append('-' * 78)
    lines.append(f'  run count                   : {cad.get("run_count", 0)}')
    lines.append(f'  failed run count            : {cad.get("failed_run_count", 0)}')
    lines.append(f'  median interval             : {cad.get("median_interval_min")} min')
    lines.append(f'  max interval (longest gap)  : {cad.get("max_interval_min")} min')
    lines.append(f'  canonical window width      : {cad.get("canonical_window_width_min", 30)} min')
    lines.append(f'  guarantees capture?         : {cad.get("guarantees_capture")}')
    lines.append(f'    → {cad.get("guarantee_reason", "")}')
    lines.append('')

    misses = ch.get('missed_captures', [])
    if misses:
        lines.append(f'MISSED-CAPTURE DETAIL ({len(misses)} shown; up to 100)')
        lines.append('-' * 78)
        for m in misses[:20]:
            lines.append(
                f'  game={m["game_id"]} fp={m["first_pitch"]} '
                f'classification={m["classification"]}'
            )
            lines.append(f'    window={m["canonical_window_start"]}..'
                         f'{m["canonical_window_end"]}  '
                         f'scheduler_runs={m["scheduler_runs_in_window"]}')
            lines.append(f'    reason: {m["reason"]}')
        lines.append('')

    p = h['population']
    lines.append('POPULATION (autonomous canonical capture — model_source=house implicit)')
    lines.append(f'  total captured          : {p["total_captured"]}')
    lines.append(f'  recommended             : {p["recommended"]}')
    lines.append(f'  settled                 : {p["settled"]}')
    lines.append(f'  unsettled               : {p["unsettled"]}')
    lines.append('')

    a = h['aggregate']
    lines.append('AGGREGATE (lane-core, settled)')
    if a['n'] == 0:
        lines.append('  (no settled outcomes yet)')
    else:
        lines.append(f'  n                      : {a["n"]}')
        lines.append(f'  W-L                    : {a["wins"]}-{a["losses"]}')
        lines.append(f'  win rate               : {a["win_rate_pp"]}%   '
                     f'Wilson95=[{a["wilson_lo_pp"]}, {a["wilson_hi_pp"]}]')
        lines.append(f'  ROI                    : {a["roi_pp"]:+.2f}%')
        lines.append(f'  net P/L / $1 stake     : {a.get("net_p_l_per_dollar")}')
        lines.append(f'  CLV+ rate (of {a["clv_sample_n"]}) : {a["clv_pos_pp"]}%')
        lines.append(f'  avg CLV                : {a["avg_clv_pp"]}pp')
        lines.append(f'  avg recommended prob   : {a["avg_prob_pp"]}%')
    lines.append('')

    d = h['distribution']
    lines.append('DISTRIBUTION')
    lines.append(f'  avg recs/day           : {d["avg_per_day"]:.2f}')
    lines.append(f'  tiers                  : {d["tier_counts"]}')
    lines.append(f'  lanes                  : {d["lane_counts"]}')
    for k, label in [('odds_distribution', 'odds'),
                     ('edge_distribution', 'edge'),
                     ('prob_distribution', 'prob %')]:
        v = d[k]
        if v['n'] == 0:
            lines.append(f'  {label:>22}: (no sample)')
        else:
            lines.append(f'  {label:>22}: n={v["n"]:>4} '
                         f'min={v["min"]} p25={v["p25"]} p50={v["p50"]} '
                         f'p75={v["p75"]} max={v["max"]} mean={v["mean"]:.2f}')
    lines.append('  per-day counts:')
    for day, ct in d['per_day'].items():
        lines.append(f'    {day}: {ct}')
    lines.append('')

    lines.append('COHORTS')
    for cohort_key, label in [('by_probability', 'probability bucket'),
                               ('by_edge', 'edge bucket'),
                               ('by_side', 'pick side'),
                               ('by_role', 'market role')]:
        lines.append(f'  {label}:')
        for k, v in h['cohorts'][cohort_key].items():
            if v['n'] == 0:
                lines.append(f'    {k:>18}: n=0')
                continue
            lines.append(
                f'    {k:>18}: n={v["n"]:>3}  '
                f'W={v["wins"]:>2}  win={v["win_rate_pp"]}%  '
                f'Wilson95=[{v["wilson_lo_pp"]}, {v["wilson_hi_pp"]}]  '
                f'ROI={v["roi_pp"]}%'
            )
    lines.append('')

    c = h['calibration']
    lines.append('CALIBRATION (final_model_prob quintiles vs actual)')
    if c.get('bins'):
        for b in c['bins']:
            lines.append(
                f'  {b["bucket"]}: n={b["n"]:>3}  '
                f'predicted={b["avg_predicted_pp"]}%  '
                f'actual={b["actual_win_rate_pp"]}%  '
                f'diff={b["diff_pp"]:+.2f}pp'
            )
        if c.get('brier_like') is not None:
            lines.append(f'  brier-like: {c["brier_like"]}')
    else:
        lines.append('  (no sample yet)')
    lines.append('')

    i = h['integrity']
    lines.append('DATA INTEGRITY')
    lines.append(f'  total snapshots in window     : {i["total_snaps_in_window"]}')
    lines.append(f'  recommended snapshots         : {i["recommended_snaps"]}')
    lines.append(f'  potential snapshots           : {i["potential_snaps"]}')
    lines.append(f'  not_recommended snapshots     : {i["not_recommended_snaps"]}')
    lines.append(f'  no_signal snapshots           : {i["no_signal_snaps"]}')
    lines.append(f'  snapshots missing feature_contributions : '
                 f'{i["snaps_missing_feature_contributions"]}')
    lines.append('')

    v = h['verdict']
    lines.append('=' * 78)
    lines.append(f'HEALTH VERDICT: {v["verdict"]}')
    lines.append('-' * 78)
    for r in v['reasons']:
        lines.append(f'  [{r["level"]}] {r["message"]}')
    lines.append('=' * 78)
    lines.append('')
    lines.append('Verdict thresholds (pre-registered, do NOT change without evidence):')
    lines.append(f'  AWAITING_FIRST_CAPTURE when 0 post-activation eligible '
                 f'games OR no snapshots yet')
    lines.append(f'  DATA_COLLECTION_DEGRADED when post-activation capture '
                 f'coverage <60% AND >=10 post-activation eligible games. '
                 f'Pre-activation history NEVER triggers this state.')
    lines.append(f'  INSUFFICIENT_DATA when eligible games exist but '
                 f'n_settled < {MIN_SETTLED_FOR_JUDGMENT}')
    lines.append(f'  WATCH  on win-rate drop >= {WATCH_WIN_RATE_DROP_PP}pp OR '
                 f'ROI drop >= {WATCH_ROI_DROP_PP}pp OR '
                 f'CLV+ drop >= {WATCH_CLV_DROP_PP}pp')
    lines.append(f'  DEGRADED on win-rate drop >= '
                 f'{DEGRADED_WIN_RATE_DROP_PP}pp OR '
                 f'ROI drop >= {DEGRADED_ROI_DROP_PP}pp OR '
                 f'CLV+ drop >= {DEGRADED_CLV_DROP_PP}pp')
    return '\n'.join(lines)
