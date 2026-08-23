"""v3.4 team-offense PHASE 2 — BOUNDED integration replay (offense v2).

Runs ONLY when the isolated predictive-value analysis promoted ONE
candidate. The selected candidate's signal is applied to V3.2 baseline
with a HARD CAP on the picked-side probability adjustment.

DESIGN DIFFERENCES vs. phase 1 offense_replay
  * Bounded cap: adjust picked-side probability by AT MOST ±1pp.
    Phase 1 used weight=0.5 on quality units which produced side
    changes of up to ±32pp — that is exactly the "too much
    authority" pattern the Phase 2 brief warned against.
  * Post-hoc cap applied AFTER re-derivation of the pick — so we
    never let offense flip a pick that market + Elo + pitcher +
    form disagree with strongly. This defends against artificial-
    edge pathology directly.
  * Cap policy pre-registered BEFORE any results. No searching for
    a cap that makes the backtest pass — the cap number reflects
    "small enough to not damage V3.2 baseline" from Phase 2 brief.

PROMOTION RULE
  This replay is only meaningful when isolated analysis surfaces
  meaningful independent signal. If the isolated verdict was NO-GO,
  DO NOT run this — the candidate signal has already failed the
  earlier gate.

READ-ONLY. Cannot activate scoring. USE_TEAM_OFFENSE remains false.
"""
from __future__ import annotations

import logging
from dataclasses import replace
from datetime import date, timedelta
from typing import Any, Callable, Dict, List, Optional

from django.utils import timezone


logger = logging.getLogger(__name__)


# Pre-registered CAP: ±1pp on the picked-side probability.
# Rationale (documented before seeing any offense results):
#   The bullpen NO-GO taught us that letting a shadow signal push
#   probabilities by ~30pp does artificial-edge damage. A 1pp cap
#   is small enough that offense can only refine (not overturn)
#   market + Elo + pitcher + form; a signal that meaningfully
#   improves outcomes at 1pp of authority proves it deserves more.
PROB_CAP_PP = 1.0


def _selected_candidate_signal_fn(selected_key: str):
    """Return the extractor for the promoted candidate — same map as
    isolated analysis."""
    from apps.analytics.services.team_offense_isolated_analysis import (
        CANDIDATE_EXTRACTORS,
    )
    return CANDIDATE_EXTRACTORS.get(selected_key)


def _apply_bounded_offense(sim, game, selected_candidate: str):
    """Re-derive pick / probability / edge / gates under the bounded
    candidate signal, then CAP the picked-side probability adjustment
    at ±PROB_CAP_PP.

    Preserves sim's `won` field relative to the (possibly-flipped) pick.
    """
    from apps.core.services.recommendations import (
        LANE_CORE, LANE_QUALIFIED, _lane_classify, _raw_tier, compute_status,
    )

    extractor = _selected_candidate_signal_fn(selected_candidate)
    if extractor is None:
        return sim
    signal = extractor(game)
    if signal is None:
        return sim
    _, _, diff_units, ok = signal
    if not ok:
        return sim

    # ADJUSTMENT LAYER — cap the impact directly on the probability,
    # not on the pre-sigmoid score. This is the design difference from
    # phase 1: because the model's rating→probability curve is steep,
    # even small score adjustments produced huge probability swings
    # (the ±32pp phase-1 range came from a ±5-unit signal fed into
    # sigmoid(score/25)). Capping AT the probability level guarantees
    # the ±1pp ceiling holds regardless of the signal's units.
    #
    # Sign convention: positive diff_units = home offense stronger →
    # nudge picked-side probability up if picked-side is home, down if
    # picked-side is away.
    if diff_units == 0:
        return sim

    # Normalize the raw diff to a canonical [-1, +1] range using tanh
    # (so a very large signal saturates instead of blowing past the cap
    # via subsequent linear math). The cap is applied by direct clamp
    # below regardless.
    import math
    nudge_pp = math.tanh(diff_units) * PROB_CAP_PP

    if sim.pick_side == 'home':
        new_pick_prob = sim.pick_prob + (nudge_pp / 100.0)
    else:
        # away side: positive home-offense signal HURTS the away pick
        new_pick_prob = sim.pick_prob - (nudge_pp / 100.0)

    # Explicit hard clamp — defense in depth. Even if the tanh math
    # ever changed, the numerical cap holds.
    delta = new_pick_prob - sim.pick_prob
    delta = max(-PROB_CAP_PP / 100.0,
                min(PROB_CAP_PP / 100.0, delta))
    new_pick_prob = sim.pick_prob + delta

    # New edge — implied probability of picked-side odds stays the same
    # (we're not changing the odds), so edge = new_pick_prob - implied.
    if sim.pick_side == 'home':
        fair_pick = sim.fair_home_prob
    else:
        fair_pick = sim.fair_away_prob
    new_edge_pp = round((new_pick_prob - fair_pick) * 100, 2)

    status, reason = compute_status(
        new_edge_pp, sim.pick_odds, probability=new_pick_prob,
        is_secondary=False,
    )
    tier = _raw_tier(new_edge_pp)
    lane, risk_flags, risk_score = _lane_classify(
        probability=new_pick_prob,
        edge_decimal=(new_edge_pp / 100.0),
        odds_american=sim.pick_odds,
        source_quality='primary',
        movement_class=sim.movement_class,
        movement_supports_pick=sim.movement_supports_pick,
        insight_conflicts=False,
    )
    if tier == 'blocked' and lane == LANE_CORE:
        lane = LANE_QUALIFIED
    is_lc = (status == 'recommended' and lane == LANE_CORE)

    return replace(
        sim,
        pick_prob=new_pick_prob,
        edge_pp=new_edge_pp,
        status=status, status_reason=reason,
        tier=tier, lane=lane,
        risk_flags=risk_flags, risk_score=risk_score,
        is_lane_corrected_recommended=is_lc,
        # `won` unchanged because pick_side never flips under a
        # capped ±1pp nudge (the picked-side probability moves at
        # most 1pp — cannot exceed the crossover to the other side
        # unless the sim was already within 1pp of it).
        won=sim.won,
    )


def _simulate_variant(
    games, *, blend_weight, selected_candidate,
    label, progress_cb=None,
):
    """Run leakage-safe sim across games; apply the bounded offense
    adjustment when a candidate is provided."""
    from apps.analytics.services.method_replay import _simulate_recommendation
    sims = []
    errors = 0
    error_categories: dict = {}
    none_returns = 0
    total = len(games)
    for i, g in enumerate(games, 1):
        try:
            sim = _simulate_recommendation(
                g, blend_weight, label,
                use_recent_form=True,
                use_bullpen_quality=False,
                use_bullpen_fatigue=False,
            )
        except Exception as exc:
            errors += 1
            cat = type(exc).__name__
            error_categories[cat] = error_categories.get(cat, 0) + 1
            logger.exception('offense_v2_replay sim failed game=%s',
                             getattr(g, 'id', None))
            continue
        if sim is None:
            none_returns += 1
            continue
        if selected_candidate:
            sim = _apply_bounded_offense(sim, g, selected_candidate)
        sims.append(sim)
        if progress_cb is not None and i % 25 == 0:
            progress_cb(phase=label, current=i, total=total)
    return sims, {
        'errors': errors, 'categories': error_categories,
        'none_returns': none_returns,
        'total_games_attempted': total,
    }


def _pctile(vals, p):
    if not vals:
        return None
    s = sorted(vals)
    idx = min(len(s) - 1, int(round(p / 100.0 * (len(s) - 1))))
    return s[idx]


def _magnitude_analysis(a_by_gid, b_by_gid):
    pp_changes: List[float] = []
    prob_crossings = 0
    edge_crossings = 0
    tier_changes = 0
    lane_changes = 0
    status_changes = 0
    side_changes = 0
    for gid in a_by_gid.keys() & b_by_gid.keys():
        a = a_by_gid[gid]; b = b_by_gid[gid]
        change = (b.pick_prob - a.pick_prob) * 100.0
        pp_changes.append(change)
        if (a.pick_prob >= 0.62) != (b.pick_prob >= 0.62):
            prob_crossings += 1
        if (a.edge_pp >= 7.0) != (b.edge_pp >= 7.0):
            edge_crossings += 1
        if a.tier != b.tier: tier_changes += 1
        if a.lane != b.lane: lane_changes += 1
        if a.status != b.status: status_changes += 1
        if a.pick_side != b.pick_side: side_changes += 1
    return {
        'n_compared': len(pp_changes),
        'mean_pp': (sum(pp_changes) / len(pp_changes)) if pp_changes else None,
        'median_pp': _pctile(pp_changes, 50),
        'min_pp': min(pp_changes) if pp_changes else None,
        'max_pp': max(pp_changes) if pp_changes else None,
        'abs_percentiles': {
            'p50': _pctile([abs(v) for v in pp_changes], 50),
            'p90': _pctile([abs(v) for v in pp_changes], 90),
            'p95': _pctile([abs(v) for v in pp_changes], 95),
            'p99': _pctile([abs(v) for v in pp_changes], 99),
        },
        'gate_crossings': {
            'probability_62pct': prob_crossings,
            'edge_7pp': edge_crossings,
            'tier': tier_changes, 'lane': lane_changes,
            'status': status_changes, 'side': side_changes,
        },
    }


def run_offense_v2_replay(
    *,
    days: int = 180,
    blend_weight: float = 0.55,
    selected_candidate: str = '',
    reference_date=None,
    min_games_for_window: int = 20,
    progress_cb: Optional[Callable] = None,
) -> Dict[str, Any]:
    """Compare A (V3.2 baseline) vs B (V3.2 + bounded selected candidate)."""
    from apps.analytics.services.method_replay import _compute_metrics
    from apps.mlb.models import Game

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
        .select_related('home_team', 'away_team',
                        'home_pitcher', 'away_pitcher')
        .order_by('first_pitch')
    )

    a_sims, a_err = _simulate_variant(
        games, blend_weight=blend_weight,
        selected_candidate='',
        label='A_v3_2_baseline', progress_cb=progress_cb,
    )
    b_sims, b_err = _simulate_variant(
        games, blend_weight=blend_weight,
        selected_candidate=selected_candidate,
        label=f'B_v2_{selected_candidate}', progress_cb=progress_cb,
    )

    a_lc = [s for s in a_sims if s.is_lane_corrected_recommended]
    b_lc = [s for s in b_sims if s.is_lane_corrected_recommended]
    a_metrics = _compute_metrics(a_lc)
    b_metrics = _compute_metrics(b_lc)

    a_by_gid = {s.game_id: s for s in a_sims}
    b_by_gid = {s.game_id: s for s in b_sims}
    magnitude = _magnitude_analysis(a_by_gid, b_by_gid)

    a_recs = {s.game_id for s in a_sims if s.is_lane_corrected_recommended}
    b_recs = {s.game_id for s in b_sims if s.is_lane_corrected_recommended}
    partition = {
        'both': len(a_recs & b_recs),
        'a_only': len(a_recs - b_recs),
        'b_only': len(b_recs - a_recs),
    }

    return {
        'window': {
            'days': days, 'from': date_from, 'to': date_to,
            'blend_weight': blend_weight,
            'selected_candidate': selected_candidate,
            'prob_cap_pp': PROB_CAP_PP,
            'games_evaluable': len(games),
        },
        'a_v3_2_baseline': {'metrics': a_metrics, 'count': len(a_lc),
                            'sim_errors': a_err},
        'b_v2_bounded':    {'metrics': b_metrics, 'count': len(b_lc),
                            'sim_errors': b_err},
        'sim_populations': {'a': len(a_sims), 'b': len(b_sims)},
        'magnitude': magnitude,
        'partition': partition,
        'data_ok': len(games) >= min_games_for_window,
    }


def render_offense_v2_replay(exp: Dict[str, Any]) -> str:
    lines = []
    lines.append('#' * 100)
    lines.append('#  TEAM-OFFENSE V2 BOUNDED REPLAY — A: V3.2 baseline / '
                 'B: V3.2 + capped candidate')
    w = exp['window']
    lines.append(f'#  Window {w["from"]}..{w["to"]} ({w["days"]}d)  '
                 f'games={w["games_evaluable"]}  '
                 f'blend={w["blend_weight"]:.2f}  '
                 f'selected={w["selected_candidate"]}  '
                 f'cap=±{w["prob_cap_pp"]:.1f}pp')
    lines.append('#' * 100)
    lines.append('')

    a, b = exp['a_v3_2_baseline'], exp['b_v2_bounded']
    def _line(label, block):
        m = block['metrics']
        n = m.get('count', 0)
        wins = m.get('wins', 0); losses = m.get('losses', 0)
        win = f"{m['win_rate']:.2f}%" if m.get('win_rate') is not None else '  n/a'
        roi = f"{m['roi']:+.2f}%" if m.get('roi') is not None else '   n/a'
        clv = f"{m['positive_clv_rate']:.1f}%" if m.get('positive_clv_rate') is not None else '  n/a'
        return f"  {label:<32} n={n:>4}  W-L {wins:>3}-{losses:<3}  win {win}  ROI {roi}  CLV+ {clv}"

    lines.append('AGGREGATE (lane-corrected recommendations)')
    lines.append('-' * 78)
    lines.append(_line('A — V3.2 baseline', a))
    lines.append(_line('B — V3.2 + bounded offense', b))
    lines.append('')

    lines.append('POPULATION PARTITION')
    lines.append('-' * 78)
    p = exp['partition']
    lines.append(f'  recommended by BOTH   : {p["both"]}')
    lines.append(f'  recommended by A only : {p["a_only"]}')
    lines.append(f'  recommended by B only : {p["b_only"]}')
    lines.append('')

    lines.append('CONTRIBUTION MAGNITUDE + GATE CROSSINGS')
    lines.append('-' * 78)
    m = exp['magnitude']
    lines.append(f'  n compared           : {m["n_compared"]}')
    if m['mean_pp'] is not None:
        lines.append(f'  mean Δ prob          : {m["mean_pp"]:+.4f}pp')
    if m['median_pp'] is not None:
        lines.append(f'  median Δ prob        : {m["median_pp"]:+.4f}pp')
    if m['min_pp'] is not None:
        lines.append(f'  range                : [{m["min_pp"]:+.3f}, '
                     f'{m["max_pp"]:+.3f}]pp')
    ap = m['abs_percentiles']
    lines.append(f'  |Δ| p50/p90/p95/p99  : {ap["p50"]}/{ap["p90"]}/'
                 f'{ap["p95"]}/{ap["p99"]}')
    gc = m['gate_crossings']
    lines.append(f'  gate crossings       : prob-62%={gc["probability_62pct"]}  '
                 f'edge-7pp={gc["edge_7pp"]}  tier={gc["tier"]}  '
                 f'lane={gc["lane"]}  status={gc["status"]}  side={gc["side"]}')
    lines.append('')

    lines.append('ADVANCEMENT RULE (pre-registered)')
    lines.append('-' * 78)
    lines.append('  Walk-forward eligible ONLY if all hold:')
    lines.append('    * B win rate >= A win rate')
    lines.append('    * B ROI >= A ROI')
    lines.append('    * B CLV does not materially worsen (drop <= 2pp)')
    lines.append('    * Recommendation volume retained (>= 50% of A)')
    lines.append('    * No artificial-edge pathology (side changes small,')
    lines.append('      status/lane crossings limited)')
    lines.append('  Any FAIL → NO-GO. Close team offense.')
    return '\n'.join(lines)
