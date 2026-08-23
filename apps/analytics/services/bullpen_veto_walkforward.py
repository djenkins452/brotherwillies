"""v3.3 SHADOW — Final Bullpen Veto Walk-Forward Validation.

Only surviving formulation from the Attribution + Salvage Study:

  BULLPEN VETO: If the selected team's bullpen quality differential
                is <= -6 rating units, downgrade an otherwise
                V3.2-approved recommendation.

The -6 threshold is PRE-REGISTERED from the exploratory attribution
analysis. This module does NOT search additional thresholds. Testing
-4/-5/-7/-8 here would introduce another layer of overfitting.

METHOD

  * Reuses decompose_game + evaluate_config from bullpen_attribution
    (single DB pass, everything else in-memory).
  * Contiguous forward-holdout folds — no random splits, no future
    leakage (each fold's evaluation only inspects games in its own
    holdout window; the veto rule itself is fixed so no per-fold
    "training" is needed).
  * Per-fold A vs B metrics + aggregate held-out A vs B metrics.
  * Wilson 95% intervals on aggregate win rates.
  * Mechanical evaluation of 6 pre-registered ship criteria →
    PASS / NO-GO verdict.

VETO CONSTRAINTS (enforced by construction; locked by test)

  * Bullpen may only REMOVE a V3.2 recommendation.
  * NEVER creates a recommendation.
  * NEVER changes the selected side.
  * NEVER increases probability.
  * NEVER increases edge.
  * Pure post-prediction risk-control layer.

SHADOW-ONLY. Reads MLB data. Writes only to BullpenExperimentRun.
USE_V3_2_SELECTION unchanged. USE_BULLPEN_QUALITY / USE_BULLPEN_FATIGUE
remain false.
"""
from __future__ import annotations

import logging
import math
from datetime import date, datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, Tuple

from django.utils import timezone


logger = logging.getLogger(__name__)


# Pre-registered veto threshold (rating units). DO NOT change without
# a formal re-registration of the ship criteria.
VETO_THRESHOLD_UNITS = -6.0

# Fold classification thresholds (delta ROI, decimal).
FOLD_HELP_THRESHOLD = 0.005    # +0.5pp
FOLD_HURT_THRESHOLD = -0.005   # -0.5pp

# Ship-criteria thresholds.
SHIP_MIN_RETAINED_VOLUME_PCT = 70.0
SHIP_MIN_FOLD_HELP_VS_HURT_DIFF = 1  # helped folds - hurt folds >= 1
SHIP_MIN_VETOED_VS_RETAINED_ROI_GAP = 0.02  # 2pp — vetoed materially worse
SHIP_CLV_TOLERANCE = -0.02   # -2pp on positive-CLV rate is "material"


# ---------------------------------------------------------------------------
# Metrics helpers


def _american_to_decimal(odds: int) -> float:
    if odds > 0:
        return 1.0 + odds / 100.0
    return 1.0 + 100.0 / abs(odds)


def wilson_interval(wins: int, n: int, *, confidence: float = 0.95) -> Tuple[float, float]:
    if n == 0:
        return (0.0, 1.0)
    p = wins / n
    z = 1.96
    denom = 1.0 + z * z / n
    center = (p + z * z / (2.0 * n)) / denom
    half = z * math.sqrt(p * (1.0 - p) / n + z * z / (4.0 * n * n)) / denom
    return (max(0.0, center - half), min(1.0, center + half))


def _metrics(recs) -> Dict[str, Any]:
    """recs = list of (decomp, evaluated_variant). Aggregates the pick's
    outcome using $100 flat stakes."""
    from apps.analytics.services.bullpen_attribution import _american_to_decimal as _atd
    wins = losses = pending = 0
    stake_total = 0.0
    profit_total = 0.0
    clv_positive = 0
    clv_total = 0
    for d, v in recs:
        if d.won is None:
            pending += 1
            continue
        stake_total += 100.0
        pick_won = d.won if v.pick_side == 'home' else (not d.won)
        if pick_won:
            wins += 1
            profit_total += 100.0 * (_atd(v.pick_odds) - 1.0)
        else:
            losses += 1
            profit_total -= 100.0
        # CLV — closing available?
        if d.closing_ml_home is not None and d.closing_ml_away is not None:
            from apps.core.utils.odds import closing_line_value
            opening_pick_ml = (
                d.opening_ml_home if v.pick_side == 'home'
                else d.opening_ml_away
            )
            closing_pick_ml = (
                d.closing_ml_home if v.pick_side == 'home'
                else d.closing_ml_away
            )
            clv = closing_line_value(opening_pick_ml, closing_pick_ml)
            clv_total += 1
            if clv > 0:
                clv_positive += 1
    n = wins + losses
    return {
        'n': n,
        'wins': wins, 'losses': losses, 'pending': pending,
        'win_rate': (wins / n) if n else None,
        'roi': (profit_total / stake_total) if stake_total else None,
        'net_pl': profit_total,
        'wilson_ci_95': wilson_interval(wins, n) if n else (0.0, 1.0),
        'positive_clv_rate': (clv_positive / clv_total) if clv_total else None,
        'clv_sample': clv_total,
    }


def _picked_side_bullpen_diff(decomp, pick_side: str) -> float:
    """Bullpen quality diff from the PICKED team's perspective.
    Positive = picked team has better bullpen; negative = worse."""
    return (
        decomp.bullpen_quality_diff if pick_side == 'home'
        else -decomp.bullpen_quality_diff
    )


def _apply_veto(decomp, baseline_variant, threshold: float = VETO_THRESHOLD_UNITS):
    """Return True to VETO the recommendation. Only downgrades — cannot
    promote a non-recommendation into a recommendation."""
    if not baseline_variant.is_recommended:
        return False
    picked_diff = _picked_side_bullpen_diff(decomp, baseline_variant.pick_side)
    return picked_diff <= threshold


# ---------------------------------------------------------------------------
# Walk-forward driver


def run_veto_walkforward(
    *,
    days: int = 180,
    train_days: int = 30,
    holdout_days: int = 14,
    step_days: int = 14,
    blend_weight: float = 0.55,
    veto_threshold: float = VETO_THRESHOLD_UNITS,
    reference_date=None,
    progress_cb: Optional[Callable] = None,
) -> Dict[str, Any]:
    """Expanding-training + forward-holdout walk-forward validation.

    Note: because the veto rule is PRE-REGISTERED at -6 units, no
    per-fold "training" step is needed — every held-out fold is
    evaluated with the same fixed rule. train_days still governs how
    long a warmup period we require before the first holdout (matches
    the discipline used to validate V3.2 in walk_forward.py)."""
    from apps.mlb.models import Game
    from apps.analytics.services.bullpen_attribution import (
        CONFIG_BASELINE, decompose_game, evaluate_config,
    )

    ref = reference_date or timezone.localdate()
    date_to = ref - timedelta(days=1)
    date_from = ref - timedelta(days=days)

    games = list(
        Game.objects.filter(
            status='final',
            home_score__isnull=False,
            away_score__isnull=False,
            first_pitch__date__gte=date_from,
            first_pitch__date__lte=date_to,
        )
        .select_related('home_team', 'away_team', 'home_pitcher', 'away_pitcher')
        .order_by('first_pitch')
    )
    total_games = len(games)

    # ---- Phase 1: decompose ALL games once ----
    decomps = []
    decomp_errors = 0
    for i, g in enumerate(games, 1):
        try:
            d = decompose_game(g, blend_weight)
        except Exception:
            decomp_errors += 1
            logger.exception(
                'veto_wf: decompose failed game=%s', getattr(g, 'id', None),
            )
            continue
        if d is not None:
            decomps.append(d)
        if progress_cb is not None and i % 100 == 0:
            progress_cb(phase='decompose', current=i, total=total_games)

    # ---- Phase 2: build folds (forward holdouts, contiguous) ----
    folds_meta = []
    fold_start = date_from + timedelta(days=train_days)
    while fold_start + timedelta(days=holdout_days) - timedelta(days=1) <= date_to:
        holdout_end = fold_start + timedelta(days=holdout_days) - timedelta(days=1)
        folds_meta.append({
            'holdout_from': fold_start,
            'holdout_to': holdout_end,
        })
        fold_start += timedelta(days=step_days)

    # ---- Phase 3: per-fold evaluation ----
    fold_results: List[Dict[str, Any]] = []
    aggregate_a: List = []
    aggregate_b: List = []
    aggregate_vetoed: List = []
    for fi, fold in enumerate(folds_meta, 1):
        fold_decomps = [
            d for d in decomps
            if fold['holdout_from']
               <= datetime.fromisoformat(d.first_pitch_iso).date()
               <= fold['holdout_to']
        ]
        a_recs = []
        b_recs = []
        vetoed = []
        for d in fold_decomps:
            v = evaluate_config(d, CONFIG_BASELINE, blend_weight=blend_weight)
            if not v.is_recommended:
                continue
            a_recs.append((d, v))
            if _apply_veto(d, v, threshold=veto_threshold):
                vetoed.append((d, v))
            else:
                b_recs.append((d, v))

        a_m = _metrics(a_recs)
        b_m = _metrics(b_recs)
        v_m = _metrics(vetoed)

        # Fold classification.
        a_roi = a_m.get('roi')
        b_roi = b_m.get('roi')
        if a_roi is None or b_roi is None or a_m['n'] == 0:
            classification = 'no_data'
        else:
            delta = b_roi - a_roi
            if delta > FOLD_HELP_THRESHOLD:
                classification = 'helped'
            elif delta < FOLD_HURT_THRESHOLD:
                classification = 'hurt'
            else:
                classification = 'neutral'

        fold_results.append({
            'fold': fold, 'index': fi,
            'a': a_m, 'b': b_m, 'vetoed': v_m,
            'classification': classification,
        })
        aggregate_a.extend(a_recs)
        aggregate_b.extend(b_recs)
        aggregate_vetoed.extend(vetoed)
        if progress_cb is not None:
            progress_cb(phase='fold', current=fi, total=len(folds_meta))

    # ---- Phase 4: aggregate ----
    agg_a = _metrics(aggregate_a)
    agg_b = _metrics(aggregate_b)
    agg_v = _metrics(aggregate_vetoed)

    retained_volume_pct = (
        100.0 * agg_b['n'] / agg_a['n']
    ) if agg_a['n'] else None

    # ---- Phase 5: ship-criteria evaluation ----
    criteria = _evaluate_ship_criteria(
        agg_a, agg_b, agg_v, fold_results, retained_volume_pct,
    )
    overall_verdict = 'PASS' if all(c['pass'] for c in criteria) else 'NO-GO'

    return {
        'window': {
            'days': days, 'from': date_from, 'to': date_to,
            'blend_weight': blend_weight,
            'veto_threshold_units': veto_threshold,
            'games_evaluable': total_games,
            'decomps_generated': len(decomps),
            'decomp_errors': decomp_errors,
        },
        'fold_config': {
            'train_days': train_days,
            'holdout_days': holdout_days,
            'step_days': step_days,
            'n_folds': len(folds_meta),
        },
        'aggregate': {
            'a_baseline': agg_a,
            'b_with_veto': agg_b,
            'vetoed': agg_v,
            'retained_volume_pct': retained_volume_pct,
            'delta_win_rate': (
                (agg_b['win_rate'] - agg_a['win_rate'])
                if (agg_b['win_rate'] is not None
                    and agg_a['win_rate'] is not None) else None
            ),
            'delta_roi': (
                (agg_b['roi'] - agg_a['roi'])
                if (agg_b['roi'] is not None
                    and agg_a['roi'] is not None) else None
            ),
            'delta_positive_clv_rate': (
                (agg_b['positive_clv_rate'] - agg_a['positive_clv_rate'])
                if (agg_b['positive_clv_rate'] is not None
                    and agg_a['positive_clv_rate'] is not None) else None
            ),
        },
        'folds': fold_results,
        'fold_classification_counts': {
            'helped': sum(1 for f in fold_results if f['classification'] == 'helped'),
            'neutral': sum(1 for f in fold_results if f['classification'] == 'neutral'),
            'hurt': sum(1 for f in fold_results if f['classification'] == 'hurt'),
            'no_data': sum(1 for f in fold_results if f['classification'] == 'no_data'),
        },
        'ship_criteria': criteria,
        'overall_verdict': overall_verdict,
    }


def _evaluate_ship_criteria(agg_a, agg_b, agg_v, fold_results, retained_pct):
    """Mechanical evaluation of the 6 pre-registered criteria."""
    criteria = []

    # 1. B win rate >= A win rate
    a_wr, b_wr = agg_a.get('win_rate'), agg_b.get('win_rate')
    passes = (a_wr is not None and b_wr is not None and b_wr >= a_wr)
    criteria.append({
        'name': '1. B win rate >= A win rate',
        'pass': passes,
        'detail': (
            f'B={_pct(b_wr)}  A={_pct(a_wr)}  Δ={_delta_pct(b_wr, a_wr)}'
            if b_wr is not None and a_wr is not None else 'insufficient data'
        ),
    })

    # 2. B ROI >= A ROI
    a_roi, b_roi = agg_a.get('roi'), agg_b.get('roi')
    passes = (a_roi is not None and b_roi is not None and b_roi >= a_roi)
    criteria.append({
        'name': '2. B ROI >= A ROI',
        'pass': passes,
        'detail': (
            f'B={_pct(b_roi)}  A={_pct(a_roi)}  Δ={_delta_pct(b_roi, a_roi)}'
            if b_roi is not None and a_roi is not None else 'insufficient data'
        ),
    })

    # 3. CLV does not materially worsen (positive-CLV rate delta >= SHIP_CLV_TOLERANCE)
    a_clv, b_clv = agg_a.get('positive_clv_rate'), agg_b.get('positive_clv_rate')
    passes = (
        a_clv is None or b_clv is None
        or (b_clv - a_clv) >= SHIP_CLV_TOLERANCE
    )
    criteria.append({
        'name': f'3. CLV+ does not worsen materially (Δ >= {SHIP_CLV_TOLERANCE*100:.1f}pp)',
        'pass': passes,
        'detail': (
            f'B={_pct(b_clv)}  A={_pct(a_clv)}  Δ={_delta_pct(b_clv, a_clv)}'
            if b_clv is not None and a_clv is not None
            else 'insufficient CLV sample'
        ),
    })

    # 4. Retained volume >= SHIP_MIN_RETAINED_VOLUME_PCT
    passes = (retained_pct is not None
              and retained_pct >= SHIP_MIN_RETAINED_VOLUME_PCT)
    criteria.append({
        'name': f'4. Retained volume >= {SHIP_MIN_RETAINED_VOLUME_PCT:.0f}%',
        'pass': passes,
        'detail': f'retained={retained_pct:.2f}%' if retained_pct is not None else 'no baseline recs',
    })

    # 5. More folds HELPED than HURT
    helped = sum(1 for f in fold_results if f['classification'] == 'helped')
    hurt = sum(1 for f in fold_results if f['classification'] == 'hurt')
    neutral = sum(1 for f in fold_results if f['classification'] == 'neutral')
    passes = (helped - hurt) >= SHIP_MIN_FOLD_HELP_VS_HURT_DIFF
    criteria.append({
        'name': f'5. helped - hurt >= {SHIP_MIN_FOLD_HELP_VS_HURT_DIFF}',
        'pass': passes,
        'detail': f'helped={helped}  neutral={neutral}  hurt={hurt}',
    })

    # 6. Vetoed bets ROI materially worse than retained (B) ROI
    v_roi, b_roi_for_gap = agg_v.get('roi'), agg_b.get('roi')
    if v_roi is None or b_roi_for_gap is None:
        gap = None
        passes = False
        detail = 'insufficient vetoed-sample data'
    else:
        gap = b_roi_for_gap - v_roi   # positive => retained bets outperform vetoed
        passes = gap >= SHIP_MIN_VETOED_VS_RETAINED_ROI_GAP
        detail = (
            f'retained_ROI={_pct(b_roi_for_gap)}  vetoed_ROI={_pct(v_roi)}  '
            f'gap={_delta_pct(b_roi_for_gap, v_roi)}  '
            f'(need >= {SHIP_MIN_VETOED_VS_RETAINED_ROI_GAP*100:.1f}pp)'
        )
    criteria.append({
        'name': '6. Vetoed bets materially worse than retained bets',
        'pass': passes,
        'detail': detail,
    })

    return criteria


# ---------------------------------------------------------------------------
# Formatting


def _pct(v, decimals=2):
    return 'n/a' if v is None else f'{v*100:.{decimals}f}%'


def _delta_pct(a, b, decimals=2):
    if a is None or b is None:
        return 'n/a'
    return f'{(a - b)*100:+.{decimals}f}pp'


def render_veto_walkforward(result: Dict[str, Any]) -> str:
    lines = []
    lines.append('#' * 100)
    lines.append('#  BULLPEN VETO WALK-FORWARD VALIDATION (v3.3 FINAL)')
    lines.append(f'#  Rule: veto V3.2 recommendation when picked-side bullpen quality diff <= '
                 f'{result["window"]["veto_threshold_units"]:.0f} rating units')
    w = result['window']
    fc = result['fold_config']
    lines.append(f'#  Window {w["from"]}..{w["to"]} ({w["days"]}d)  '
                 f'games={w["games_evaluable"]}  '
                 f'decomps={w["decomps_generated"]}  '
                 f'decomp_errors={w["decomp_errors"]}')
    lines.append(f'#  Folds: {fc["n_folds"]}  '
                 f'(train_warmup={fc["train_days"]}d  '
                 f'holdout={fc["holdout_days"]}d  '
                 f'step={fc["step_days"]}d)')
    lines.append('#' * 100)
    lines.append('')

    # Aggregate — the true out-of-sample verdict.
    agg = result['aggregate']
    a, b, v = agg['a_baseline'], agg['b_with_veto'], agg['vetoed']
    lines.append('AGGREGATE HELD-OUT (union of all fold holdouts)')
    lines.append('-' * 78)
    lines.append(f'  A  V3.2 baseline           n={a["n"]:>4}  {a["wins"]:>3}-{a["losses"]:<3}  '
                 f'win {_pct(a["win_rate"])}  '
                 f'95%CI [{a["wilson_ci_95"][0]*100:.2f}%, {a["wilson_ci_95"][1]*100:.2f}%]  '
                 f'ROI {_pct(a["roi"])}  '
                 f'CLV+ {_pct(a["positive_clv_rate"])}')
    lines.append(f'  B  V3.2 + veto ≤-6         n={b["n"]:>4}  {b["wins"]:>3}-{b["losses"]:<3}  '
                 f'win {_pct(b["win_rate"])}  '
                 f'95%CI [{b["wilson_ci_95"][0]*100:.2f}%, {b["wilson_ci_95"][1]*100:.2f}%]  '
                 f'ROI {_pct(b["roi"])}  '
                 f'CLV+ {_pct(b["positive_clv_rate"])}')
    lines.append(f'  V  vetoed bets             n={v["n"]:>4}  {v["wins"]:>3}-{v["losses"]:<3}  '
                 f'win {_pct(v["win_rate"])}  '
                 f'ROI {_pct(v["roi"])}  '
                 f'CLV+ {_pct(v["positive_clv_rate"])}')
    lines.append(f'  Deltas B-A: Δwin={_pct(agg["delta_win_rate"])} '
                 f' ΔROI={_pct(agg["delta_roi"])} '
                 f' ΔCLV+={_pct(agg["delta_positive_clv_rate"])}   '
                 f'retained_volume={agg["retained_volume_pct"]:.2f}%'
                 if agg['retained_volume_pct'] is not None else '')
    lines.append('')

    # Per-fold table.
    lines.append('FOLD-BY-FOLD RESULTS')
    lines.append('-' * 78)
    lines.append(f'  {"fold":>4}  {"window":<27}  {"A":>18}  {"B":>18}  {"delta":>7}  class')
    for f in result['folds']:
        fold = f['fold']
        aa, bb = f['a'], f['b']
        a_str = (f'n={aa["n"]:>3} W{aa["wins"]:>2}-L{aa["losses"]:<2} '
                 f'{aa["win_rate"]*100:.1f}%' if aa["win_rate"] is not None
                 else f'n={aa["n"]:>3}  --')
        b_str = (f'n={bb["n"]:>3} W{bb["wins"]:>2}-L{bb["losses"]:<2} '
                 f'{bb["win_rate"]*100:.1f}%' if bb["win_rate"] is not None
                 else f'n={bb["n"]:>3}  --')
        delta_roi = ''
        if aa['roi'] is not None and bb['roi'] is not None:
            delta_roi = f'{(bb["roi"]-aa["roi"])*100:+.2f}pp'
        else:
            delta_roi = 'n/a'
        lines.append(
            f'  {f["index"]:>4}  '
            f'{fold["holdout_from"]}..{fold["holdout_to"]}  '
            f'{a_str:>18}  {b_str:>18}  {delta_roi:>7}  {f["classification"]}'
        )
    fc_counts = result['fold_classification_counts']
    lines.append('')
    lines.append(
        f'  Fold classification: helped={fc_counts["helped"]}  '
        f'neutral={fc_counts["neutral"]}  '
        f'hurt={fc_counts["hurt"]}  '
        f'no_data={fc_counts["no_data"]}'
    )
    lines.append('')

    # Ship criteria — the mechanical PASS/NO-GO.
    lines.append('SHIP CRITERIA (all must PASS)')
    lines.append('-' * 78)
    for c in result['ship_criteria']:
        mark = '✓ PASS' if c['pass'] else '✗ FAIL'
        lines.append(f'  {mark}  {c["name"]}')
        lines.append(f'         {c["detail"]}')
    lines.append('')

    # Verdict.
    verdict = result['overall_verdict']
    lines.append('=' * 78)
    if verdict == 'PASS':
        lines.append('OVERALL VERDICT: ✓ PASS — bullpen veto ≤-6 rule cleared all 6 criteria')
        lines.append('  Next: implement USE_BULLPEN_VETO flag (default False); Danny explicitly')
        lines.append('  authorizes flipping it True on Railway to activate. USE_BULLPEN_QUALITY /')
        lines.append('  USE_BULLPEN_FATIGUE remain False.')
    else:
        failed = [c['name'] for c in result['ship_criteria'] if not c['pass']]
        lines.append('OVERALL VERDICT: ✗ NO-GO — bullpen veto ≤-6 rule failed at least one criterion')
        lines.append(f'  Failed: {", ".join(failed)}')
        lines.append('  Action: do NOT implement USE_BULLPEN_VETO. Preserve historical infrastructure')
        lines.append('  and shadow data for future research. Classify bullpen as NOT CURRENTLY')
        lines.append('  PRODUCTION-VALUABLE. Move to the next predictive feature.')
    lines.append('=' * 78)
    return '\n'.join(lines)
