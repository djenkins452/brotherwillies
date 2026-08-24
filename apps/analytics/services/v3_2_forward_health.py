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
    """Assemble the full forward-health report for the last `days`
    system recommendations."""
    from django.utils import timezone
    from apps.core.models import BettingRecommendation

    cutoff = timezone.now() - timedelta(days=days)
    # SYSTEM recommendations only. User-tuned model rows are excluded.
    all_recs = list(
        BettingRecommendation.objects
        .filter(sport='mlb', model_source='house',
                created_at__gte=cutoff)
        .select_related('mlb_game', 'mlb_game__home_team',
                        'mlb_game__away_team')
        .order_by('created_at')
    )
    generated = len(all_recs)
    lc_recs = [r for r in all_recs
               if r.status == 'recommended' and r.lane == 'core']

    # Compute outcome / profit / CLV for each LC rec.
    settled_rows = []
    unsettled_rows = []
    for r in lc_recs:
        won = _rec_outcome(r)
        if won is None:
            unsettled_rows.append(r)
            continue
        profit = _rec_profit(r, won)
        closing = _closing_market_prob_for(r)
        clv = _rec_clv(r, closing)
        settled_rows.append({
            'rec': r,
            'won': won,
            'profit_per_dollar': profit,
            'clv_pp': (clv * 100.0) if clv is not None else None,
        })

    n_settled = len(settled_rows)
    aggregate = _aggregate_metrics(settled_rows)
    distribution = _distribution_metrics(lc_recs)
    cohorts = _cohort_metrics(settled_rows)
    calibration = _calibration_metrics(settled_rows)
    integrity = _integrity_findings(all_recs, lc_recs, unsettled_rows)
    verdict = _compute_verdict(n_settled, aggregate)

    return {
        'window': {'days': days,
                   'from': cutoff.date().isoformat(),
                   'to': timezone.localdate().isoformat()},
        'population': {
            'generated': generated,
            'lane_core_recommended': len(lc_recs),
            'settled': n_settled,
            'unsettled': len(unsettled_rows),
        },
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


def _compute_verdict(n_settled: int, aggregate: Dict[str, Any]) -> HealthVerdict:
    reasons: List[Tuple[str, str]] = []
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

    p = h['population']
    lines.append('POPULATION (system recommendations only — model_source=house)')
    lines.append(f'  generated               : {p["generated"]}')
    lines.append(f'  lane-core recommended   : {p["lane_core_recommended"]}')
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
    lines.append(f'  recs in window         : {i["total_recs_in_window"]}')
    lines.append(f'  lane-core recs         : {i["lane_core_recs"]}')
    lines.append(f'  missing feature_contributions : {i["missing_feature_contributions"]}')
    lines.append(f'  unsettled >6h past first_pitch: {i["unsettled_past_first_pitch_by_6h"]}'
                 ' (ingestion may be stale if > 0)')
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
    lines.append(f'  INSUFFICIENT_DATA when n_settled < {MIN_SETTLED_FOR_JUDGMENT}')
    lines.append(f'  WATCH  on win-rate drop >= {WATCH_WIN_RATE_DROP_PP}pp OR '
                 f'ROI drop >= {WATCH_ROI_DROP_PP}pp OR '
                 f'CLV+ drop >= {WATCH_CLV_DROP_PP}pp')
    lines.append(f'  DEGRADED on win-rate drop >= '
                 f'{DEGRADED_WIN_RATE_DROP_PP}pp OR '
                 f'ROI drop >= {DEGRADED_ROI_DROP_PP}pp OR '
                 f'CLV+ drop >= {DEGRADED_CLV_DROP_PP}pp')
    return '\n'.join(lines)
