"""v3.3 SHADOW — Bullpen Attribution + Salvage Study.

Runs against the already-populated production bullpen history to
answer the 8-section brief following the 180-day A/B/C experiment
that showed:

  A  n=238   71.85% win, +21.95% ROI, CLV+ 56.0%   (V3.2 baseline)
  B  n=415   64.82% win, +11.17% ROI  (V3.2 + bullpen quality)
  C  n=416   65.14% win, +11.82% ROI  (V3.2 + quality + fatigue)

  Delta: bullpen NEARLY DOUBLED recommendation volume while cutting
  win rate by ~7pp and ROI by ~11pp. Classic "artificial edge"
  signature — bullpen contribution is pushing marginal games through
  the 62% / 7pp gates.

DIAGNOSTIC METHOD

  1. simulate ONCE per game with full score decomposition (rating,
     pitcher-static, pitcher-form, HFA, bullpen-quality-delta,
     bullpen-fatigue-delta, opening odds, market prob, movement).
     Cost: 1× DB pass; ~2700 games in a few minutes.

  2. evaluate_config(decomp, cfg) reconstructs pick+prob+edge+gate
     under ARBITRARY bullpen weighting (scale factor + cap + veto
     rules) without re-simulating. All salvage tests (bounded
     weights, veto rules, isolated feature analysis) become in-
     memory operations over the cached decompositions.

  3. Report every analysis section the brief asks for + a verdict.

READ-ONLY. Never touches production. Bullpen flags remain OFF.
"""
from __future__ import annotations

import logging
import math
import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, Tuple

from django.utils import timezone


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Per-game decomposition — one DB pass per game, everything cached.


@dataclass
class GameDecomposition:
    """All data needed to reconstruct pick + prob + edge + gate under
    any bullpen weighting / veto config. Populated ONCE per game.
    Everything downstream operates on lists of these objects."""
    game_id: str
    first_pitch_iso: str
    won: Optional[bool]
    home_score: Optional[int]
    away_score: Optional[int]
    # Opening / closing odds (integer American).
    opening_ml_home: int
    opening_ml_away: int
    closing_ml_home: Optional[int]
    closing_ml_away: Optional[int]
    # Market prob (opening snapshot's market_home_win_prob).
    market_home_prob: float
    # De-vigged fair prob per side (from opening odds).
    fair_home_prob: float
    fair_away_prob: float
    # Score components — home perspective, rating-scale units.
    rating_term: float
    pitcher_static_term: float
    pitcher_form_term: float
    hfa_term: float
    # Bullpen deltas (home - away, rating-scale units).
    bullpen_quality_diff: float   # home_quality - away_quality
    bullpen_fatigue_diff: float   # home_fatigue - away_fatigue
    # Bullpen raw per side (for isolated-value analysis).
    home_bullpen_quality: float
    away_bullpen_quality: float
    home_bullpen_fatigue: float
    away_bullpen_fatigue: float
    home_bullpen_confidence: str  # 'low' / 'med' / 'high'
    away_bullpen_confidence: str
    home_top_reliever_available: Optional[bool]
    away_top_reliever_available: Optional[bool]
    # Movement signal (for lane classification).
    movement_class: Optional[str]
    movement_supports_home: bool
    movement_supports_away: bool
    # Whether both teams' bullpens are covered.
    both_bullpens_covered: bool

    @property
    def market_away_prob(self) -> float:
        return 1.0 - self.market_home_prob


@dataclass
class EvaluatedVariant:
    """One config's evaluation of a game. What the sim WOULD have
    picked under this bullpen weighting."""
    pick_side: str          # 'home' / 'away'
    pick_odds: int
    pick_prob: float
    edge_pp: float
    status: str             # 'recommended' / 'not_recommended'
    lane: str               # 'core' / 'qualified' / 'pass'
    is_recommended: bool    # status == recommended AND lane == core
    tier: str


# Empty variant used for games where opening odds were missing —
# won't recommend anything.
_EMPTY_VARIANT = EvaluatedVariant(
    pick_side='home', pick_odds=0, pick_prob=0.0, edge_pp=0.0,
    status='not_recommended', lane='pass', is_recommended=False,
    tier='standard',
)


def decompose_game(game, blend_weight: float) -> Optional[GameDecomposition]:
    """Simulate one game and return the full decomposition. Returns
    None when the game has insufficient pre-game data (matches the
    filter behavior of method_replay._simulate_recommendation)."""
    from apps.analytics.services.method_replay import (
        _pregame_snapshots, _pregame_team_rating, _pregame_movement_signal,
    )
    from apps.core.utils.odds import american_to_implied_prob, devig_two_way
    from apps.mlb.services.bullpen import team_bullpen_signal
    from apps.mlb.services.model_service import HFA
    from apps.mlb.services.pitcher_form import recent_form_delta
    from apps.mlb.models import TeamBullpenSnapshot

    snaps = _pregame_snapshots(game, only_primary=True)
    if not snaps:
        return None
    opening = snaps[0]
    closing = snaps[-1]
    if opening.moneyline_home is None or opening.moneyline_away is None:
        return None
    if opening.market_home_win_prob is None:
        return None

    home_rating = _pregame_team_rating(game.home_team, game)
    away_rating = _pregame_team_rating(game.away_team, game)
    rating_term = (home_rating - away_rating) * 0.35

    pitcher_static_term = 0.0
    pitcher_form_term = 0.0
    if game.home_pitcher is not None and game.away_pitcher is not None:
        pitcher_static_term = (
            float(game.home_pitcher.rating) - float(game.away_pitcher.rating)
        ) * 0.65
        home_form = recent_form_delta(
            game.home_pitcher, reference_date=game.first_pitch,
        )
        away_form = recent_form_delta(
            game.away_pitcher, reference_date=game.first_pitch,
        )
        pitcher_form_term = (home_form - away_form) * 0.65

    hfa_term = HFA if not game.neutral_site else 0.0

    home_pen = team_bullpen_signal(game.home_team, game.first_pitch)
    away_pen = team_bullpen_signal(game.away_team, game.first_pitch)
    both_covered = (
        home_pen.snapshot_as_of is not None
        and away_pen.snapshot_as_of is not None
    )

    # Look up top-reliever-available flags directly on the snapshots
    # (bullpen_signal returns the summary but not the flag).
    def _top_avail(team, ref):
        s = TeamBullpenSnapshot.objects.filter(
            team=team, as_of__lt=ref,
        ).order_by('-as_of').first()
        return s.top_reliever_available if s else None

    home_top = _top_avail(game.home_team, game.first_pitch)
    away_top = _top_avail(game.away_team, game.first_pitch)

    raw_home = american_to_implied_prob(opening.moneyline_home)
    raw_away = american_to_implied_prob(opening.moneyline_away)
    fair_home, fair_away = devig_two_way(raw_home, raw_away)

    # Movement signal for BOTH sides so evaluate_config can pick the
    # relevant one when the config's pick differs from baseline's.
    mv_home = _pregame_movement_signal(game, 'home')
    mv_away = _pregame_movement_signal(game, 'away')

    won: Optional[bool] = None
    if game.home_score is not None and game.away_score is not None:
        if game.home_score == game.away_score:
            won = None  # push (defensive)
        else:
            won = game.home_score > game.away_score  # from HOME perspective

    return GameDecomposition(
        game_id=str(game.id),
        first_pitch_iso=game.first_pitch.isoformat(),
        won=won,
        home_score=game.home_score,
        away_score=game.away_score,
        opening_ml_home=opening.moneyline_home,
        opening_ml_away=opening.moneyline_away,
        closing_ml_home=closing.moneyline_home if closing is not opening else None,
        closing_ml_away=closing.moneyline_away if closing is not opening else None,
        market_home_prob=float(opening.market_home_win_prob),
        fair_home_prob=fair_home,
        fair_away_prob=fair_away,
        rating_term=rating_term,
        pitcher_static_term=pitcher_static_term,
        pitcher_form_term=pitcher_form_term,
        hfa_term=hfa_term,
        bullpen_quality_diff=home_pen.quality_delta - away_pen.quality_delta,
        bullpen_fatigue_diff=home_pen.fatigue_delta - away_pen.fatigue_delta,
        home_bullpen_quality=home_pen.quality_delta,
        away_bullpen_quality=away_pen.quality_delta,
        home_bullpen_fatigue=home_pen.fatigue_delta,
        away_bullpen_fatigue=away_pen.fatigue_delta,
        home_bullpen_confidence=home_pen.data_confidence,
        away_bullpen_confidence=away_pen.data_confidence,
        home_top_reliever_available=home_top,
        away_top_reliever_available=away_top,
        movement_class=mv_home['movement_class'],
        movement_supports_home=mv_home['supports_pick'],
        movement_supports_away=mv_away['supports_pick'],
        both_bullpens_covered=both_covered,
    )


# ---------------------------------------------------------------------------
# Variant evaluator


@dataclass(frozen=True)
class BullpenConfig:
    """A single bullpen weighting to evaluate against cached decompositions.

    * bullpen_quality_scale — multiplier on bullpen_quality_diff (0.0
      disables entirely; 1.0 matches the initial B/C variants).
    * bullpen_quality_cap_prob_pp — cap on the FINAL probability-point
      change contributed by bullpen quality. None = uncapped.
    * bullpen_fatigue_scale — same for fatigue.
    * apply_veto — callable(decomp, baseline_pick_side) -> True to VETO
      the recommendation. When None, no veto logic.
    """
    label: str
    bullpen_quality_scale: float = 0.0
    bullpen_fatigue_scale: float = 0.0
    bullpen_quality_cap_pp: Optional[float] = None
    bullpen_fatigue_cap_pp: Optional[float] = None
    apply_veto: Optional[Callable] = None


def _clamp_probability_shim(p: float) -> float:
    """Mirror of apps.core.services.probability_calibration.clamp_probability."""
    from apps.core.services.probability_calibration import PROB_MIN, PROB_MAX
    if p > 0.5:
        return max(PROB_MIN, min(PROB_MAX, p))
    if p < 0.5:
        return max(1.0 - PROB_MAX, min(1.0 - PROB_MIN, p))
    return p


def evaluate_config(
    decomp: GameDecomposition,
    config: BullpenConfig,
    *,
    blend_weight: float = 0.55,
) -> EvaluatedVariant:
    """Reconstruct pick + prob + edge + gate under `config` from the
    cached decomposition. Never touches the DB.

    Applies caps on the SCORE-UNIT contribution, then blends into
    prob-point space via the sigmoid + market blend as the original
    sim does — so caps expressed in pp are approximate (an 8pp cap
    on prob-point delta translates to ~a rating-scale cap that
    depends on the sigmoid slope at the operating point). For the
    salvage study this level of precision is sufficient — caps here
    are exploratory, not the final production shape.
    """
    from apps.core.services.recommendations import (
        LANE_CORE, LANE_QUALIFIED, _lane_classify, _raw_tier, compute_status,
    )

    # Bullpen contributions (rating-scale units), after scale/cap.
    bp_q = decomp.bullpen_quality_diff * config.bullpen_quality_scale
    bp_f = decomp.bullpen_fatigue_diff * config.bullpen_fatigue_scale
    # Apply caps expressed in probability-point space by mapping back
    # to rating-scale using the sigmoid slope (approx 6.25 pp per
    # rating unit near p=0.5). Rough but sufficient.
    if config.bullpen_quality_cap_pp is not None:
        cap_units = config.bullpen_quality_cap_pp / 100.0 * 25.0
        bp_q = max(-cap_units, min(cap_units, bp_q))
    if config.bullpen_fatigue_cap_pp is not None:
        cap_units = config.bullpen_fatigue_cap_pp / 100.0 * 25.0
        bp_f = max(-cap_units, min(cap_units, bp_f))

    score = (
        decomp.rating_term + decomp.pitcher_static_term
        + decomp.pitcher_form_term + decomp.hfa_term
        + bp_q + bp_f
    )
    raw_prob = 1.0 / (1.0 + math.exp(-score / 25.0))
    raw_prob = max(0.01, min(0.99, raw_prob))

    w = max(0.0, min(0.65, blend_weight))
    blended = raw_prob * (1.0 - w) + decomp.market_home_prob * w
    final_home_prob = _clamp_probability_shim(blended)

    home_edge = final_home_prob - decomp.fair_home_prob
    away_edge = (1.0 - final_home_prob) - decomp.fair_away_prob
    if home_edge >= away_edge:
        pick_side = 'home'
        pick_odds = decomp.opening_ml_home
        pick_prob = final_home_prob
        edge_decimal = home_edge
        movement_supports = decomp.movement_supports_home
    else:
        pick_side = 'away'
        pick_odds = decomp.opening_ml_away
        pick_prob = 1.0 - final_home_prob
        edge_decimal = away_edge
        movement_supports = decomp.movement_supports_away
    edge_pp = round(edge_decimal * 100, 2)

    status, _reason = compute_status(
        edge_pp, pick_odds, probability=pick_prob, is_secondary=False,
    )
    tier = _raw_tier(edge_pp)

    lane, _flags, _score = _lane_classify(
        probability=pick_prob,
        edge_decimal=edge_decimal,
        odds_american=pick_odds,
        source_quality='primary',
        movement_class=decomp.movement_class,
        movement_supports_pick=movement_supports,
        insight_conflicts=False,
    )
    if tier == 'blocked' and lane == LANE_CORE:
        lane = LANE_QUALIFIED

    is_recommended = (status == 'recommended' and lane == LANE_CORE)

    # Veto pass — applied AFTER all gates. Callable receives the
    # decomposition + the pick side; can look at bullpen data + baseline
    # signals to decide whether to veto. Only downgrades — cannot
    # promote a non-recommended pick.
    if config.apply_veto is not None and is_recommended:
        if config.apply_veto(decomp, pick_side):
            is_recommended = False
            status = 'not_recommended'

    return EvaluatedVariant(
        pick_side=pick_side, pick_odds=pick_odds,
        pick_prob=pick_prob, edge_pp=edge_pp,
        status=status, lane=lane,
        is_recommended=is_recommended, tier=tier,
    )


# ---------------------------------------------------------------------------
# Metric helpers


def _american_to_decimal(odds: int) -> float:
    if odds > 0:
        return 1.0 + odds / 100.0
    return 1.0 + 100.0 / abs(odds)


def _metrics_for(recs: List[Tuple[GameDecomposition, EvaluatedVariant]]) -> Dict[str, Any]:
    """Compute n / W-L / win-rate / ROI over a list of (decomp, variant)
    pairs. `variant` is the pick to be scored; `won` on the decomp is
    from HOME perspective — flip when pick_side == 'away'."""
    wins = losses = pending = 0
    stake_total = 0.0
    profit_total = 0.0
    for d, v in recs:
        if d.won is None:
            pending += 1
            continue
        stake_total += 100.0
        pick_won = d.won if v.pick_side == 'home' else (not d.won)
        if pick_won:
            wins += 1
            profit_total += 100.0 * (_american_to_decimal(v.pick_odds) - 1.0)
        else:
            losses += 1
            profit_total -= 100.0
    n = wins + losses
    return {
        'n': n,
        'wins': wins,
        'losses': losses,
        'pending': pending,
        'win_rate': (wins / n) if n else None,
        'roi': (profit_total / stake_total) if stake_total else None,
        'net_pl': profit_total,
    }


# ---------------------------------------------------------------------------
# Reusable configs


CONFIG_BASELINE = BullpenConfig(label='V3.2 baseline (bullpen off)')
CONFIG_B_FULL_QUALITY = BullpenConfig(
    label='V3.2 + full quality (scale=1.0)',
    bullpen_quality_scale=1.0,
)
CONFIG_C_FULL_QUALITY_FATIGUE = BullpenConfig(
    label='V3.2 + full quality + fatigue',
    bullpen_quality_scale=1.0,
    bullpen_fatigue_scale=1.0,
)


def _bounded_configs() -> List[BullpenConfig]:
    """Bounded-weight variants for the salvage study."""
    out = []
    for scale in (0.10, 0.25, 0.50, 0.75):
        out.append(BullpenConfig(
            label=f'quality scale={scale:.2f}',
            bullpen_quality_scale=scale,
        ))
    for cap in (0.5, 1.0, 2.0, 3.0):
        out.append(BullpenConfig(
            label=f'quality full + cap ±{cap:.1f}pp',
            bullpen_quality_scale=1.0,
            bullpen_quality_cap_pp=cap,
        ))
    return out


def _veto_configs() -> List[BullpenConfig]:
    """Veto-only architectures: bullpen NEVER promotes; only downgrades."""
    def _veto_negative_pen(threshold_units):
        def _veto(decomp, pick_side):
            # For picked side, is the bullpen materially worse than opp?
            if pick_side == 'home':
                delta = decomp.bullpen_quality_diff  # positive = home better
                return delta < -threshold_units
            else:
                return decomp.bullpen_quality_diff > threshold_units
        return _veto

    def _veto_top_out(decomp, pick_side):
        picked_top = (
            decomp.home_top_reliever_available if pick_side == 'home'
            else decomp.away_top_reliever_available
        )
        return picked_top is False

    def _veto_fatigued(threshold_units):
        def _veto(decomp, pick_side):
            fatigue = (
                decomp.bullpen_fatigue_diff if pick_side == 'home'
                else -decomp.bullpen_fatigue_diff
            )
            return fatigue < -threshold_units
        return _veto

    def _combined(decomp, pick_side):
        picked_top = (
            decomp.home_top_reliever_available if pick_side == 'home'
            else decomp.away_top_reliever_available
        )
        pen_delta = (
            decomp.bullpen_quality_diff if pick_side == 'home'
            else -decomp.bullpen_quality_diff
        )
        return picked_top is False or pen_delta < -3.0

    return [
        BullpenConfig(label='veto: pen delta ≤ -2 units',
                      apply_veto=_veto_negative_pen(2.0)),
        BullpenConfig(label='veto: pen delta ≤ -4 units',
                      apply_veto=_veto_negative_pen(4.0)),
        BullpenConfig(label='veto: pen delta ≤ -6 units',
                      apply_veto=_veto_negative_pen(6.0)),
        BullpenConfig(label='veto: top reliever unavailable',
                      apply_veto=_veto_top_out),
        BullpenConfig(label='veto: fatigue delta ≤ -1',
                      apply_veto=_veto_fatigued(1.0)),
        BullpenConfig(label='veto: top-out OR delta ≤ -3',
                      apply_veto=_combined),
    ]


# ---------------------------------------------------------------------------
# Analyses


def _partition_populations(
    decomps: List[GameDecomposition],
    baseline: List[EvaluatedVariant],
    plus_quality: List[EvaluatedVariant],
) -> Dict[str, List[Tuple[GameDecomposition, EvaluatedVariant, EvaluatedVariant]]]:
    """Partition games into both / A-only / B-only / neither."""
    both, a_only, b_only, neither = [], [], [], []
    for d, a, b in zip(decomps, baseline, plus_quality):
        if a.is_recommended and b.is_recommended:
            both.append((d, a, b))
        elif a.is_recommended and not b.is_recommended:
            a_only.append((d, a, b))
        elif not a.is_recommended and b.is_recommended:
            b_only.append((d, a, b))
        else:
            neither.append((d, a, b))
    return {
        'both': both,
        'a_only': a_only,
        'b_only': b_only,
        'neither': neither,
    }


def _partition_metrics(partition_rows, *, use_variant='b'):
    """Compute metrics on a partition list using the specified variant
    (a=baseline pick, b=plus-quality pick)."""
    pairs = [(d, (a if use_variant == 'a' else b)) for d, a, b in partition_rows]
    return _metrics_for(pairs)


def _contribution_magnitude(
    decomps: List[GameDecomposition],
    baseline: List[EvaluatedVariant],
    plus_quality: List[EvaluatedVariant],
) -> Dict[str, Any]:
    """Distribution of the probability-point change bullpen quality
    introduces to the picked-side probability."""
    pp_changes: List[float] = []
    pp_pos: List[float] = []
    pp_neg: List[float] = []
    gate_prob_crossings = 0
    gate_edge_crossings = 0
    tier_changes = 0
    lane_changes = 0
    status_changes = 0
    side_changes = 0
    for a, b in zip(baseline, plus_quality):
        # Change to picked-side probability under +quality. Positive
        # when +quality made the pick MORE probable.
        change_pp = (b.pick_prob - a.pick_prob) * 100.0 if a.pick_side == b.pick_side else (
            b.pick_prob * 100.0 - a.pick_prob * 100.0
        )
        pp_changes.append(change_pp)
        if change_pp > 0:
            pp_pos.append(change_pp)
        elif change_pp < 0:
            pp_neg.append(change_pp)
        # Gate crossings.
        if (a.pick_prob >= 0.62) != (b.pick_prob >= 0.62):
            gate_prob_crossings += 1
        if (a.edge_pp >= 7.0) != (b.edge_pp >= 7.0):
            gate_edge_crossings += 1
        if a.tier != b.tier:
            tier_changes += 1
        if a.lane != b.lane:
            lane_changes += 1
        if a.status != b.status:
            status_changes += 1
        if a.pick_side != b.pick_side:
            side_changes += 1

    def _pctile(vals, p):
        if not vals:
            return None
        vals_sorted = sorted(vals)
        idx = min(len(vals_sorted) - 1, int(round(p / 100.0 * (len(vals_sorted) - 1))))
        return vals_sorted[idx]

    def _bucket_count(vals, lo, hi):
        return sum(1 for v in vals if lo <= abs(v) < hi)

    return {
        'n_games_compared': len(pp_changes),
        'positive_changes': len(pp_pos),
        'negative_changes': len(pp_neg),
        'mean_change_pp': (sum(pp_changes) / len(pp_changes)) if pp_changes else None,
        'median_change_pp': _pctile(pp_changes, 50),
        'std_change_pp': (
            statistics.stdev(pp_changes) if len(pp_changes) > 1 else None
        ),
        'min_change_pp': min(pp_changes) if pp_changes else None,
        'max_change_pp': max(pp_changes) if pp_changes else None,
        'abs_percentiles_pp': {
            'p10': _pctile([abs(v) for v in pp_changes], 10),
            'p25': _pctile([abs(v) for v in pp_changes], 25),
            'p50': _pctile([abs(v) for v in pp_changes], 50),
            'p75': _pctile([abs(v) for v in pp_changes], 75),
            'p90': _pctile([abs(v) for v in pp_changes], 90),
            'p95': _pctile([abs(v) for v in pp_changes], 95),
            'p99': _pctile([abs(v) for v in pp_changes], 99),
        },
        'buckets_abs_pp': {
            '<1pp': _bucket_count(pp_changes, 0, 1),
            '1-2pp': _bucket_count(pp_changes, 1, 2),
            '2-3pp': _bucket_count(pp_changes, 2, 3),
            '3-5pp': _bucket_count(pp_changes, 3, 5),
            '5-8pp': _bucket_count(pp_changes, 5, 8),
            '8pp+': sum(1 for v in pp_changes if abs(v) >= 8),
        },
        'gate_crossings': {
            'probability_62pct': gate_prob_crossings,
            'edge_7pp': gate_edge_crossings,
            'tier_change': tier_changes,
            'lane_change': lane_changes,
            'status_change': status_changes,
            'side_change': side_changes,
        },
    }


def _isolated_predictive_value(decomps: List[GameDecomposition]) -> Dict[str, Any]:
    """Across ALL covered games, does bullpen quality (fatigue) diff
    predict outcomes if we mechanically bet the side FAVORED by
    bullpen? Bucketed by differential magnitude."""

    def _bucket_delta(delta):
        if delta <= -6:
            return 'large_negative_home'
        if delta <= -2:
            return 'moderate_negative_home'
        if delta < 2:
            return 'neutral'
        if delta < 6:
            return 'moderate_positive_home'
        return 'large_positive_home'

    def _analyze(attr):
        buckets: Dict[str, Dict[str, int]] = defaultdict(
            lambda: {'games': 0, 'wins': 0, 'losses': 0}
        )
        for d in decomps:
            if not d.both_bullpens_covered:
                continue
            if d.won is None:
                continue
            delta = getattr(d, attr)
            key = _bucket_delta(delta)
            buckets[key]['games'] += 1
            # Bet the side favored by delta (delta > 0 favors home).
            picked_home = delta > 0
            if delta == 0:
                # Skip neutral picks for the ROI calc — no side to bet.
                continue
            picked_won = d.won if picked_home else (not d.won)
            if picked_won:
                buckets[key]['wins'] += 1
            else:
                buckets[key]['losses'] += 1
        out = {}
        for k, b in buckets.items():
            decisive = b['wins'] + b['losses']
            out[k] = {
                'games': b['games'],
                'wins': b['wins'],
                'losses': b['losses'],
                'win_rate': (b['wins'] / decisive) if decisive else None,
            }
        return out

    return {
        'by_quality_diff': _analyze('bullpen_quality_diff'),
        'by_fatigue_diff': _analyze('bullpen_fatigue_diff'),
    }


def _interaction_analysis(
    decomps: List[GameDecomposition],
    baseline: List[EvaluatedVariant],
    plus_quality: List[EvaluatedVariant],
) -> Dict[str, Any]:
    """For each cohort of interest, how does +quality perform vs baseline
    on the RECOMMENDED subset within that cohort?"""
    from apps.mlb.services.pitcher_form import recent_form_delta  # noqa

    cohorts: Dict[str, List[int]] = {
        'weak_starter_strong_pen_home': [],
        'strong_starter_weak_pen_home': [],
        'short_favorite_baseline_pick': [],
        'mid_favorite_baseline_pick': [],
        'underdog_baseline_pick': [],
        'home_baseline_pick': [],
        'road_baseline_pick': [],
        'baseline_low_prob': [],
        'baseline_high_prob': [],
        'baseline_low_edge': [],
        'baseline_high_edge': [],
    }
    for i, (d, a, b) in enumerate(zip(decomps, baseline, plus_quality)):
        # Starter vs bullpen contrast (rating-scale). Positive => starter
        # better than bullpen for home team.
        home_starter_vs_pen = d.pitcher_static_term - d.bullpen_quality_diff
        if d.bullpen_quality_diff > 4 and d.pitcher_static_term < 2:
            cohorts['weak_starter_strong_pen_home'].append(i)
        if d.pitcher_static_term > 4 and d.bullpen_quality_diff < -2:
            cohorts['strong_starter_weak_pen_home'].append(i)
        # Odds bucket on baseline pick.
        odds = a.pick_odds
        if -149 <= odds <= 99:
            cohorts['short_favorite_baseline_pick'].append(i)
        elif -300 <= odds <= -150:
            cohorts['mid_favorite_baseline_pick'].append(i)
        elif odds > 99:
            cohorts['underdog_baseline_pick'].append(i)
        # Side.
        if a.pick_side == 'home':
            cohorts['home_baseline_pick'].append(i)
        else:
            cohorts['road_baseline_pick'].append(i)
        # Baseline prob / edge cohort split by median-esque thresholds.
        if a.pick_prob < 0.64:
            cohorts['baseline_low_prob'].append(i)
        else:
            cohorts['baseline_high_prob'].append(i)
        if a.edge_pp < 8:
            cohorts['baseline_low_edge'].append(i)
        else:
            cohorts['baseline_high_edge'].append(i)

    def _cohort_metrics(idxs, variant):
        pairs = [(decomps[i], (baseline[i] if variant == 'a' else plus_quality[i]))
                 for i in idxs if (baseline[i].is_recommended if variant == 'a'
                                   else plus_quality[i].is_recommended)]
        m = _metrics_for(pairs)
        m['n_in_cohort'] = len(idxs)
        m['n_recommended'] = len(pairs)
        return m

    return {
        name: {
            'baseline': _cohort_metrics(idxs, 'a'),
            'plus_quality': _cohort_metrics(idxs, 'b'),
        } for name, idxs in cohorts.items() if idxs
    }


def _decide_verdict(
    baseline_metrics, quality_metrics, fatigue_metrics,
    bounded_results, veto_results, isolated,
) -> Dict[str, str]:
    """Mechanical verdict per brief's A-G decision framework.

    Rule of thumb: bullpen "salvage" is worth pursuing only if some
    variant meets the primary product objective — retained ROI within
    ±2pp of V3.2 baseline AND retained win rate within ±1pp AND
    volume is at least 50% of baseline. Otherwise recommend REMOVAL."""
    def _relative_delta(baseline, candidate, key):
        b = baseline.get(key)
        c = candidate.get(key)
        if b is None or c is None:
            return None
        return c - b

    a_roi = baseline_metrics.get('roi')
    a_win = baseline_metrics.get('win_rate')
    a_n = baseline_metrics.get('n', 0) or 1

    best_bounded = None
    for r in bounded_results:
        roi = r['metrics'].get('roi')
        win = r['metrics'].get('win_rate')
        n = r['metrics'].get('n', 0)
        if roi is None or win is None:
            continue
        if n < 0.5 * a_n:
            continue
        # Score: roi delta + 0.5 * win delta (arbitrary tie-breaker).
        score = (roi - (a_roi or 0)) + 0.5 * (win - (a_win or 0))
        if best_bounded is None or score > best_bounded['score']:
            best_bounded = {'row': r, 'score': score,
                            'roi_delta': roi - (a_roi or 0),
                            'win_delta': win - (a_win or 0)}

    best_veto = None
    for r in veto_results:
        roi = r['metrics'].get('roi')
        win = r['metrics'].get('win_rate')
        n = r['metrics'].get('n', 0)
        if roi is None or win is None:
            continue
        if n < 0.5 * a_n:
            continue
        score = (roi - (a_roi or 0)) + 0.5 * (win - (a_win or 0))
        if best_veto is None or score > best_veto['score']:
            best_veto = {'row': r, 'score': score,
                         'roi_delta': roi - (a_roi or 0),
                         'win_delta': win - (a_win or 0)}

    # Isolated predictive value: is there any bucket with meaningful
    # signal (win rate > 55% AND games >= 30)?
    has_isolated_signal = False
    for scheme in ('by_quality_diff', 'by_fatigue_diff'):
        for k, v in (isolated.get(scheme) or {}).items():
            if v['games'] >= 30 and v.get('win_rate') is not None and v['win_rate'] > 0.55:
                has_isolated_signal = True
                break

    # Thresholds (decimal terms; 0.005 = 0.5pp, 0.02 = 2pp).
    # A useful salvage must (a) improve ROI by at least a small margin,
    # (b) not degrade win rate materially, (c) retain at least 50% of
    # baseline volume — enforced upstream in the `n < 0.5 * a_n` check.
    ROI_IMPROVEMENT_MIN = 0.005     # +0.5pp of ROI
    WIN_RATE_DEGRADATION_LIMIT = -0.005  # -0.5pp win-rate max tolerable

    verdict_letter = 'A'
    reason = ''
    if (
        best_bounded is not None
        and best_bounded['roi_delta'] > ROI_IMPROVEMENT_MIN
        and best_bounded['win_delta'] > WIN_RATE_DEGRADATION_LIMIT
    ):
        verdict_letter = 'B'
        reason = (
            f'Bounded weight `{best_bounded["row"]["config"]}` retains volume '
            f'and beats baseline by {best_bounded["roi_delta"]*100:+.2f}pp ROI, '
            f'{best_bounded["win_delta"]*100:+.2f}pp win. Redesign contribution scale.'
        )
    elif (
        best_veto is not None
        and best_veto['roi_delta'] > ROI_IMPROVEMENT_MIN
        and best_veto['win_delta'] > WIN_RATE_DEGRADATION_LIMIT
    ):
        verdict_letter = 'C'
        reason = (
            f'Veto rule `{best_veto["row"]["config"]}` improves retained ROI '
            f'by {best_veto["roi_delta"]*100:+.2f}pp with volume preserved. '
            f'Bullpen works better as a downgrade signal than as a probability input.'
        )
    elif has_isolated_signal:
        verdict_letter = 'F'
        reason = (
            'Isolated predictive value present in some bullpen-differential '
            'buckets but no salvage config beats V3.2 baseline. '
            'Requires a specific defensible interaction — do NOT ship broadly.'
        )
    else:
        # No salvage variant beat baseline. Whether best_bounded/veto
        # exist (they failed the threshold) or not (nothing eligible),
        # the answer is the same: bullpen has no meaningful predictive
        # value in this data.
        verdict_letter = 'A'
        reason = (
            'No bullpen variant retains volume + beats baseline. Isolated '
            'predictive value is not present. Bullpen appears to lack meaningful '
            'predictive value at the population level. Recommend REMOVAL from '
            'the predictive roadmap.'
        )

    return {'verdict': verdict_letter, 'reason': reason}


# ---------------------------------------------------------------------------
# Public entry point


def run_bullpen_attribution(
    *,
    days: int = 180,
    blend_weight: float = 0.55,
    reference_date=None,
    progress_cb=None,
) -> Dict[str, Any]:
    """One DB pass, everything else in-memory. Returns a large dict
    covering all 8 brief sections + verdict."""
    ref = reference_date or timezone.localdate()
    date_to = ref - timedelta(days=1)
    date_from = ref - timedelta(days=days)

    from apps.mlb.models import Game
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

    decomps: List[GameDecomposition] = []
    decomp_errors = 0
    for i, g in enumerate(games, 1):
        try:
            d = decompose_game(g, blend_weight)
        except Exception:
            decomp_errors += 1
            logger.exception('attribution: decompose failed game=%s', g.id)
            continue
        if d is not None:
            decomps.append(d)
        if progress_cb is not None and i % 100 == 0:
            progress_cb(phase='decompose', current=i, total=total_games)

    # Evaluate baseline + full-quality + full-quality+fatigue for
    # partition + magnitude analyses.
    baseline_variants = [
        evaluate_config(d, CONFIG_BASELINE, blend_weight=blend_weight)
        for d in decomps
    ]
    quality_variants = [
        evaluate_config(d, CONFIG_B_FULL_QUALITY, blend_weight=blend_weight)
        for d in decomps
    ]
    fatigue_variants = [
        evaluate_config(d, CONFIG_C_FULL_QUALITY_FATIGUE, blend_weight=blend_weight)
        for d in decomps
    ]

    # ---- Section 1: population attribution ----
    partitions = _partition_populations(decomps, baseline_variants, quality_variants)
    partition_metrics = {
        'both':    {'baseline': _partition_metrics(partitions['both'], use_variant='a'),
                    'plus_quality': _partition_metrics(partitions['both'], use_variant='b')},
        'a_only':  {'baseline': _partition_metrics(partitions['a_only'], use_variant='a'),
                    'plus_quality': _partition_metrics(partitions['a_only'], use_variant='b')},
        'b_only':  {'baseline': _partition_metrics(partitions['b_only'], use_variant='a'),
                    'plus_quality': _partition_metrics(partitions['b_only'], use_variant='b')},
        'neither': {'n': len(partitions['neither'])},
    }
    # Also compute averages within b_only for the incremental-bet analysis.
    def _averages(rows):
        if not rows:
            return {}
        pick_probs = [b.pick_prob for _, _, b in rows]
        edges = [b.edge_pp for _, _, b in rows]
        odds = [b.pick_odds for _, _, b in rows]
        bq_diffs = [d.bullpen_quality_diff for d, _, _ in rows]
        return {
            'avg_bullpen_prob': sum(pick_probs) / len(pick_probs),
            'avg_edge_pp': sum(edges) / len(edges),
            'avg_pick_odds': sum(odds) / len(odds),
            'avg_bullpen_quality_diff': sum(bq_diffs) / len(bq_diffs),
        }
    incremental_analysis = _averages(partitions['b_only'])

    # ---- Section 2: contribution magnitude ----
    magnitude = _contribution_magnitude(decomps, baseline_variants, quality_variants)

    # ---- Section 4: veto experiments ----
    veto_results = []
    for cfg in _veto_configs():
        pairs = []
        for d, a in zip(decomps, baseline_variants):
            if not a.is_recommended:
                continue
            # Apply veto to the BASELINE pick (bullpen only downgrades).
            if cfg.apply_veto and cfg.apply_veto(d, a.pick_side):
                continue
            pairs.append((d, a))
        veto_results.append({
            'config': cfg.label,
            'metrics': _metrics_for(pairs),
        })

    # ---- Section 5: bounded weight ----
    bounded_results = []
    for cfg in _bounded_configs():
        rec_pairs = []
        for d in decomps:
            v = evaluate_config(d, cfg, blend_weight=blend_weight)
            if v.is_recommended:
                rec_pairs.append((d, v))
        bounded_results.append({
            'config': cfg.label,
            'metrics': _metrics_for(rec_pairs),
        })

    # ---- Section 6: isolated predictive value ----
    isolated = _isolated_predictive_value(decomps)

    # ---- Section 7: interaction analysis ----
    interactions = _interaction_analysis(
        decomps, baseline_variants, quality_variants,
    )

    # ---- Aggregate metrics (used by verdict) ----
    a_recs = [(d, v) for d, v in zip(decomps, baseline_variants) if v.is_recommended]
    b_recs = [(d, v) for d, v in zip(decomps, quality_variants) if v.is_recommended]
    c_recs = [(d, v) for d, v in zip(decomps, fatigue_variants) if v.is_recommended]
    a_metrics = _metrics_for(a_recs)
    b_metrics = _metrics_for(b_recs)
    c_metrics = _metrics_for(c_recs)

    verdict = _decide_verdict(
        a_metrics, b_metrics, c_metrics,
        bounded_results, veto_results, isolated,
    )

    return {
        'window': {
            'days': days, 'from': date_from, 'to': date_to,
            'blend_weight': blend_weight, 'games_evaluable': total_games,
            'decomps_generated': len(decomps), 'decomp_errors': decomp_errors,
        },
        'coverage': {
            'total_games': len(decomps),
            'both_bullpens_covered': sum(1 for d in decomps if d.both_bullpens_covered),
            'both_covered_pct': round(
                100.0 * sum(1 for d in decomps if d.both_bullpens_covered)
                / max(1, len(decomps)), 2,
            ),
        },
        'aggregate': {
            'a_v3_2_baseline':        {'metrics': a_metrics, 'count': len(a_recs)},
            'b_plus_quality':         {'metrics': b_metrics, 'count': len(b_recs)},
            'c_plus_quality_fatigue': {'metrics': c_metrics, 'count': len(c_recs)},
        },
        'section_1_partition': {
            'counts': {
                'both':    len(partitions['both']),
                'a_only':  len(partitions['a_only']),
                'b_only':  len(partitions['b_only']),
                'neither': len(partitions['neither']),
            },
            'metrics': partition_metrics,
            'b_only_averages': incremental_analysis,
        },
        'section_2_magnitude': magnitude,
        'section_4_veto': veto_results,
        'section_5_bounded': bounded_results,
        'section_6_isolated': isolated,
        'section_7_interactions': interactions,
        'section_8_verdict': verdict,
    }


# ---------------------------------------------------------------------------
# Renderer


def _fmt_metric(m: Dict, prefix: str = '  ') -> str:
    n = m.get('n', 0)
    if n == 0:
        return f'{prefix}n=0 (no picks)'
    win = f"{m['win_rate']*100:.2f}%" if m.get('win_rate') is not None else '  n/a'
    roi = f"{m['roi']*100:+.2f}%" if m.get('roi') is not None else '   n/a'
    return (f'{prefix}n={n:>4}  W-L {m["wins"]:>3}-{m["losses"]:<3}  '
            f'win {win}  ROI {roi}  P/L ${m["net_pl"]:+,.0f}')


def render_bullpen_attribution(result: Dict[str, Any]) -> str:
    lines: List[str] = []
    lines.append('#' * 100)
    lines.append('#  BULLPEN ATTRIBUTION + SALVAGE STUDY (v3.3)')
    w = result['window']
    lines.append(f'#  Window {w["from"]}..{w["to"]} ({w["days"]}d)  '
                 f'games={w["games_evaluable"]}  '
                 f'decomps={w["decomps_generated"]}  '
                 f'decomp_errors={w["decomp_errors"]}')
    cov = result['coverage']
    lines.append(f'#  Bullpen coverage: {cov["both_covered_pct"]}% '
                 f'({cov["both_bullpens_covered"]}/{cov["total_games"]})')
    lines.append('#' * 100)
    lines.append('')

    # Aggregate.
    agg = result['aggregate']
    lines.append('AGGREGATE (recommended = lane=core AND status=recommended)')
    lines.append('-' * 78)
    lines.append(f'  A  V3.2 baseline               {_fmt_metric(agg["a_v3_2_baseline"]["metrics"], prefix="")}')
    lines.append(f'  B  V3.2 + full quality          {_fmt_metric(agg["b_plus_quality"]["metrics"], prefix="")}')
    lines.append(f'  C  V3.2 + full quality+fatigue  {_fmt_metric(agg["c_plus_quality_fatigue"]["metrics"], prefix="")}')
    lines.append('')

    # Section 1
    s1 = result['section_1_partition']
    lines.append('SECTION 1 — RECOMMENDATION POPULATION ATTRIBUTION (baseline A vs +quality B)')
    lines.append('-' * 78)
    lines.append(f'  Counts: both={s1["counts"]["both"]}  '
                 f'A_only={s1["counts"]["a_only"]}  '
                 f'B_only={s1["counts"]["b_only"]}  '
                 f'neither={s1["counts"]["neither"]}')
    lines.append('')
    for name in ('both', 'a_only', 'b_only'):
        m = s1['metrics'][name]
        lines.append(f'  Partition: {name.upper()}')
        lines.append(f'    baseline-side pick:    ' + _fmt_metric(m['baseline'], prefix=''))
        lines.append(f'    plus-quality-side pick: ' + _fmt_metric(m['plus_quality'], prefix=''))
    avg = s1['b_only_averages']
    if avg:
        lines.append('')
        lines.append('  B-ONLY (bullpen-created recommendations) averages:')
        lines.append(f'    avg pick prob (bullpen adjusted): {avg["avg_bullpen_prob"]*100:.2f}%')
        lines.append(f'    avg edge pp                     : {avg["avg_edge_pp"]:.2f}pp')
        lines.append(f'    avg pick odds                   : {avg["avg_pick_odds"]:+.0f}')
        lines.append(f'    avg bullpen quality diff        : {avg["avg_bullpen_quality_diff"]:+.2f} rating units')
    lines.append('')

    # Section 2
    m2 = result['section_2_magnitude']
    lines.append('SECTION 2 — CONTRIBUTION MAGNITUDE (delta to picked-side probability, pp)')
    lines.append('-' * 78)
    lines.append(f'  n_games_compared={m2["n_games_compared"]}  '
                 f'positive={m2["positive_changes"]}  negative={m2["negative_changes"]}')
    lines.append(f'  mean={m2["mean_change_pp"]:+.3f}pp  '
                 f'median={m2["median_change_pp"]:+.3f}pp  '
                 f'std={m2["std_change_pp"]:.3f}pp  '
                 f'range=[{m2["min_change_pp"]:+.2f}, {m2["max_change_pp"]:+.2f}]pp')
    ap = m2['abs_percentiles_pp']
    lines.append(f'  |delta| percentiles: '
                 f'p10={ap["p10"]:.2f} p25={ap["p25"]:.2f} p50={ap["p50"]:.2f} '
                 f'p75={ap["p75"]:.2f} p90={ap["p90"]:.2f} p95={ap["p95"]:.2f} p99={ap["p99"]:.2f}')
    lines.append('  buckets |delta pp|: ' + ' '.join(
        f'{k}={v}' for k, v in m2['buckets_abs_pp'].items()
    ))
    gc = m2['gate_crossings']
    lines.append(f'  gate crossings: prob-62%={gc["probability_62pct"]} '
                 f'edge-7pp={gc["edge_7pp"]} tier={gc["tier_change"]} '
                 f'lane={gc["lane_change"]} status={gc["status_change"]} '
                 f'side={gc["side_change"]}')
    lines.append('')

    # Section 4 — veto
    lines.append('SECTION 4 — VETO / DOWNGRADE ARCHITECTURES')
    lines.append('-' * 78)
    lines.append('  (starts from V3.2 A-recommended set; bullpen can only downgrade)')
    for r in result['section_4_veto']:
        lines.append(f'  {r["config"]:<48} {_fmt_metric(r["metrics"], prefix="")}')
    lines.append('')

    # Section 5 — bounded
    lines.append('SECTION 5 — BOUNDED CONTRIBUTION EXPERIMENTS')
    lines.append('-' * 78)
    lines.append('  (recommendation set produced by the sim under scaled/capped bullpen)')
    for r in result['section_5_bounded']:
        lines.append(f'  {r["config"]:<48} {_fmt_metric(r["metrics"], prefix="")}')
    lines.append('')

    # Section 6 — isolated
    iso = result['section_6_isolated']
    lines.append('SECTION 6 — ISOLATED PREDICTIVE VALUE (bet the bullpen-favored side)')
    lines.append('-' * 78)
    for scheme, buckets in iso.items():
        lines.append(f'  {scheme}:')
        for k, v in sorted(buckets.items()):
            win = f"{v['win_rate']*100:.2f}%" if v.get('win_rate') is not None else '  n/a'
            lines.append(f'    {k:<32} games={v["games"]:>4}  W-L {v["wins"]:>3}-{v["losses"]:<3}  win {win}')
    lines.append('')

    # Section 7 — interactions
    lines.append('SECTION 7 — INTERACTION ANALYSIS (baseline vs +quality per cohort)')
    lines.append('-' * 78)
    for name, cohort in result['section_7_interactions'].items():
        lines.append(f'  {name} (cohort n={cohort["baseline"].get("n_in_cohort", "?")}):')
        lines.append(f'    baseline recommended    ({cohort["baseline"].get("n_recommended", 0)}) '
                     + _fmt_metric(cohort['baseline'], prefix=''))
        lines.append(f'    plus_quality recommended ({cohort["plus_quality"].get("n_recommended", 0)}) '
                     + _fmt_metric(cohort['plus_quality'], prefix=''))
    lines.append('')

    # Section 8 — verdict
    v8 = result['section_8_verdict']
    lines.append('SECTION 8 — VERDICT')
    lines.append('-' * 78)
    lines.append(f'  Letter: {v8["verdict"]}')
    lines.append(f'  {v8["reason"]}')
    return '\n'.join(lines)
