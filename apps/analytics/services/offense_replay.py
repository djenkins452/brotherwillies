"""v3.4 SHADOW — team-offense replay experiment.

Answers Track B's question directly:

  Does adding a BOUNDED team-offense contribution to the V3.2 score
  materially improve moneyline prediction, or does Elo already
  subsume this information?

Compares two variants on the same historical MLB slate:

  A. V3.2 baseline (Elo + starter + form + HFA, blend 0.55, 0.62/7pp)
  B. V3.2 + bounded team-offense contribution
     (bounded via team_offense.QUALITY_ABS_CAP_UNITS on the SIGNAL,
      then further weight ≤ 1.0 in the score composition — small
      enough to avoid the bullpen artificial-edge pathology)

Reports discipline-preserving diagnostics (contribution magnitude,
gate crossings, side changes) — the same shape that surfaced the
bullpen NO-GO. If offense degrades the model, we see it here BEFORE
it damages production.

READ-ONLY. Cannot activate scoring. USE_TEAM_OFFENSE remains false
regardless of what this experiment produces.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, Tuple

from django.utils import timezone


logger = logging.getLogger(__name__)


# --- Bounded contribution weight for the "B" variant.
#
# Rationale: the signal itself is already capped at
# team_offense.QUALITY_ABS_CAP_UNITS (±10 rating units). Multiplying
# by 0.5 here gives an effective ±5-unit max score contribution,
# meaningfully smaller than the pitcher_static term (which typically
# ranges ±10-15 units on real starter rating differentials). Starting
# CONSERVATIVE was the specific lesson from the bullpen study.
B_VARIANT_OFFENSE_WEIGHT = 0.5


def _simulate_variant(games, *, blend_weight, use_team_offense,
                      offense_weight, label, progress_cb=None):
    """Run the leakage-safe sim across games under the variant config.
    Returns (sims, error_report)."""
    from apps.analytics.services.method_replay import _simulate_recommendation

    sims = []
    errors = 0
    error_categories: dict = {}
    none_returns = 0
    total = len(games)
    for i, g in enumerate(games, 1):
        try:
            # We pass the offense flag through _simulate_recommendation's
            # standard mechanism — the sim's own team_offense_signal
            # call still respects reference_date=game.first_pitch, so
            # leakage discipline is inherited.
            sim = _simulate_recommendation(
                g, blend_weight, label,
                use_recent_form=True,
                # Bullpen stays OFF in both variants (V3.2 baseline
                # doesn't include it, and the bullpen NO-GO closure
                # prohibits it from re-entering here).
                use_bullpen_quality=False,
                use_bullpen_fatigue=False,
            )
            # Team offense isn't yet a first-class param on
            # _simulate_recommendation. We compute the sim, then
            # ADD our offense contribution to the score by re-running
            # the last few sim stages. For simplicity in this replay
            # we hijack the flag through the settings layer.
            #
            # ALTERNATIVE (chosen): re-derive the score adjustment
            # in-memory using team_offense_signal and evaluator logic,
            # so we don't monkey-patch settings. See _apply_offense
            # below.
        except Exception as exc:
            errors += 1
            cat = type(exc).__name__
            error_categories[cat] = error_categories.get(cat, 0) + 1
            logger.exception(
                'offense_replay: sim failed game=%s label=%s',
                getattr(g, 'id', None), label,
            )
            continue
        if sim is None:
            none_returns += 1
            continue
        if use_team_offense:
            sim = _apply_offense(sim, g, offense_weight, blend_weight)
        sims.append(sim)
        if progress_cb is not None and i % 25 == 0:
            progress_cb(phase=label, current=i, total=total)
    return sims, {
        'errors': errors, 'categories': error_categories,
        'none_returns': none_returns,
        'total_games_attempted': total,
    }


def _apply_offense(sim, game, offense_weight, blend_weight):
    """Re-compute pick + prob + edge + gate with the offense
    contribution added to the raw score. Preserves the sim's other
    fields (opening odds, movement, won, etc.) untouched.

    Because SimulatedRecommendation is a frozen-ish dataclass, we
    construct a new one with the updated fields."""
    import math
    from dataclasses import replace
    from apps.core.services.recommendations import (
        LANE_CORE, LANE_QUALIFIED, _lane_classify, _raw_tier, compute_status,
    )
    from apps.core.utils.odds import devig_two_way, american_to_implied_prob
    from apps.mlb.services.team_offense import team_offense_signal
    from apps.analytics.services.method_replay import _clamp_probability

    home_off = team_offense_signal(game.home_team, game.first_pitch)
    away_off = team_offense_signal(game.away_team, game.first_pitch)
    offense_diff = (home_off.quality_delta - away_off.quality_delta) * offense_weight

    # Reconstruct raw_score from sim.raw_score PLUS the offense delta.
    # This is a pure additive adjustment on the pre-sigmoid score.
    new_score = sim.raw_score + offense_diff
    new_raw = 1.0 / (1.0 + math.exp(-new_score / 25.0))
    new_raw = max(0.01, min(0.99, new_raw))

    w = max(0.0, min(0.65, blend_weight))
    new_blended = new_raw * (1.0 - w) + sim.market_prob_pregame * w
    new_final = _clamp_probability(new_blended)

    # Recompute pick/edge from new_final + existing opening moneylines.
    raw_home = american_to_implied_prob(sim.opening_moneyline_home)
    raw_away = american_to_implied_prob(sim.opening_moneyline_away)
    fair_home, fair_away = devig_two_way(raw_home, raw_away)
    new_away = 1.0 - new_final
    home_edge = new_final - fair_home
    away_edge = new_away - fair_away
    if home_edge >= away_edge:
        pick_side = 'home'
        pick_odds = sim.opening_moneyline_home
        pick_prob = new_final
        edge_decimal = home_edge
    else:
        pick_side = 'away'
        pick_odds = sim.opening_moneyline_away
        pick_prob = new_away
        edge_decimal = away_edge
    edge_pp = round(edge_decimal * 100, 2)

    status, reason = compute_status(
        edge_pp, pick_odds, probability=pick_prob, is_secondary=False,
    )
    tier = _raw_tier(edge_pp)

    # Recompute lane using the sim's original movement signal — the
    # movement values are frozen with the sim; only the pick may shift.
    lane, risk_flags, risk_score = _lane_classify(
        probability=pick_prob,
        edge_decimal=edge_decimal,
        odds_american=pick_odds,
        source_quality='primary',
        movement_class=sim.movement_class,
        movement_supports_pick=sim.movement_supports_pick,
        insight_conflicts=False,
    )
    if tier == 'blocked' and lane == LANE_CORE:
        lane = LANE_QUALIFIED
    is_lc = (status == 'recommended' and lane == LANE_CORE)

    # Won stays the same relative to the ACTUAL outcome — but the
    # `won` field on the sim was computed relative to the ORIGINAL
    # pick_side. If our new pick_side differs, we must flip.
    new_won = sim.won
    if pick_side != sim.pick_side and sim.won is not None:
        new_won = not sim.won

    return replace(
        sim,
        raw_score=new_score,
        raw_prob_pre_blend=new_raw,
        blended_prob=new_blended,
        final_prob=new_final,
        fair_home_prob=fair_home,
        fair_away_prob=fair_away,
        pick_side=pick_side,
        pick_odds=pick_odds,
        pick_prob=pick_prob,
        edge_pp=edge_pp,
        status=status, status_reason=reason,
        tier=tier, lane=lane,
        risk_flags=risk_flags, risk_score=risk_score,
        is_lane_corrected_recommended=is_lc,
        won=new_won,
    )


def run_offense_experiment(
    *,
    days: int = 180,
    blend_weight: float = 0.55,
    offense_weight: float = B_VARIANT_OFFENSE_WEIGHT,
    reference_date=None,
    min_games_for_window: int = 20,
    progress_cb: Optional[Callable] = None,
) -> Dict[str, Any]:
    """Compare A (V3.2 baseline) vs B (V3.2 + bounded team offense) on
    the same historical slate."""
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
        use_team_offense=False, offense_weight=0.0,
        label='A_v3_2_baseline', progress_cb=progress_cb,
    )
    b_sims, b_err = _simulate_variant(
        games, blend_weight=blend_weight,
        use_team_offense=True, offense_weight=offense_weight,
        label='B_v3_2_plus_offense', progress_cb=progress_cb,
    )

    a_lc = [s for s in a_sims if s.is_lane_corrected_recommended]
    b_lc = [s for s in b_sims if s.is_lane_corrected_recommended]
    a_metrics = _compute_metrics(a_lc)
    b_metrics = _compute_metrics(b_lc)

    # Contribution magnitude + gate crossings — pair sims by game_id
    # so we're comparing THE SAME game under both configs.
    a_by_gid = {s.game_id: s for s in a_sims}
    magnitude = _magnitude_analysis(a_by_gid, {s.game_id: s for s in b_sims})

    # Population attribution — same partition shape as the bullpen
    # attribution study.
    partition = _population_partition(a_sims, b_sims)

    populations_match = len(a_sims) == len(b_sims)

    return {
        'window': {
            'days': days, 'from': date_from, 'to': date_to,
            'blend_weight': blend_weight,
            'offense_weight': offense_weight,
            'games_evaluable': len(games),
        },
        'a_v3_2_baseline': {'metrics': a_metrics, 'count': len(a_lc),
                            'sim_errors': a_err},
        'b_plus_offense':  {'metrics': b_metrics, 'count': len(b_lc),
                            'sim_errors': b_err},
        'sim_populations': {'a': len(a_sims), 'b': len(b_sims)},
        'populations_match': populations_match,
        'magnitude': magnitude,
        'partition': partition,
        'data_ok': len(games) >= min_games_for_window,
    }


def _magnitude_analysis(a_by_gid, b_by_gid):
    """Same shape as bullpen_attribution._contribution_magnitude:
    distribution of picked-side probability deltas + gate-crossing
    counts."""
    pp_changes: List[float] = []
    prob_crossings = 0
    edge_crossings = 0
    tier_changes = 0
    lane_changes = 0
    status_changes = 0
    side_changes = 0
    for gid in a_by_gid.keys() & b_by_gid.keys():
        a = a_by_gid[gid]; b = b_by_gid[gid]
        # Change in picked-side probability (in pp).
        if a.pick_side == b.pick_side:
            change = (b.pick_prob - a.pick_prob) * 100.0
        else:
            change = (b.pick_prob * 100.0) - (a.pick_prob * 100.0)
        pp_changes.append(change)
        if (a.pick_prob >= 0.62) != (b.pick_prob >= 0.62):
            prob_crossings += 1
        if (a.edge_pp >= 7.0) != (b.edge_pp >= 7.0):
            edge_crossings += 1
        if a.tier != b.tier: tier_changes += 1
        if a.lane != b.lane: lane_changes += 1
        if a.status != b.status: status_changes += 1
        if a.pick_side != b.pick_side: side_changes += 1

    def _pctile(vals, p):
        if not vals: return None
        s = sorted(vals)
        idx = min(len(s) - 1, int(round(p / 100.0 * (len(s) - 1))))
        return s[idx]

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


def _population_partition(a_sims, b_sims):
    a_recs = {s.game_id for s in a_sims if s.is_lane_corrected_recommended}
    b_recs = {s.game_id for s in b_sims if s.is_lane_corrected_recommended}
    return {
        'both': len(a_recs & b_recs),
        'a_only': len(a_recs - b_recs),
        'b_only': len(b_recs - a_recs),
    }


def render_offense_experiment(exp: Dict[str, Any]) -> str:
    lines = []
    lines.append('#' * 100)
    lines.append('#  TEAM-OFFENSE REPLAY EXPERIMENT — A: V3.2 baseline / B: V3.2 + bounded offense')
    w = exp['window']
    lines.append(f'#  Window {w["from"]}..{w["to"]} ({w["days"]}d)  '
                 f'games={w["games_evaluable"]}  '
                 f'blend={w["blend_weight"]:.2f}  '
                 f'offense_weight={w["offense_weight"]:.2f}')
    lines.append('#' * 100)
    lines.append('')

    a, b = exp['a_v3_2_baseline'], exp['b_plus_offense']
    def _line(label, block):
        m = block['metrics']
        n = m.get('count', 0)
        wins = m.get('wins', 0); losses = m.get('losses', 0)
        win = f"{m['win_rate']:.2f}%" if m.get('win_rate') is not None else '  n/a'
        roi = f"{m['roi']:+.2f}%" if m.get('roi') is not None else '   n/a'
        clv = f"{m['positive_clv_rate']:.1f}%" if m.get('positive_clv_rate') is not None else '  n/a'
        return f"  {label:<28} n={n:>4}  W-L {wins:>3}-{losses:<3}  win {win}  ROI {roi}  CLV+ {clv}"

    lines.append('AGGREGATE (lane-corrected recommendations)')
    lines.append('-' * 78)
    lines.append(_line('A — V3.2 baseline',   a))
    lines.append(_line('B — V3.2 + offense',  b))
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
    lines.append(f'  mean Δ prob          : {m["mean_pp"]:+.3f}pp' if m['mean_pp'] is not None else '  mean: n/a')
    lines.append(f'  median Δ prob        : {m["median_pp"]:+.3f}pp' if m['median_pp'] is not None else '  median: n/a')
    lines.append(f'  range                : [{m["min_pp"]:+.2f}, {m["max_pp"]:+.2f}]pp'
                 if m['min_pp'] is not None else '  range: n/a')
    ap = m['abs_percentiles']
    lines.append(f'  |Δ| p50/p90/p95/p99  : {ap["p50"]}/{ap["p90"]}/{ap["p95"]}/{ap["p99"]}')
    gc = m['gate_crossings']
    lines.append(f'  gate crossings       : prob-62%={gc["probability_62pct"]}  '
                 f'edge-7pp={gc["edge_7pp"]}  tier={gc["tier"]}  '
                 f'lane={gc["lane"]}  status={gc["status"]}  side={gc["side"]}')
    lines.append('')

    lines.append('READ THE RESULT')
    lines.append('-' * 78)
    lines.append('  If B win rate AND ROI both meet or beat A with retained volume (>=50%)')
    lines.append('  AND contribution magnitude is small (median |Δ| < 2pp, few gate crossings),')
    lines.append('  the candidate merits walk-forward validation.')
    lines.append('  If B EXPANDS volume dramatically while degrading win rate/ROI, the feature')
    lines.append('  is repeating the bullpen artificial-edge pathology → NO-GO.')
    return '\n'.join(lines)
