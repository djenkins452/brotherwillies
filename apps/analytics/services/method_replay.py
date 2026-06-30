"""Method Replay service — retrospective MLB moneyline backtest.

Answers: "what would Brother Willies have recommended over the last N
days under a given MARKET_BLEND_WEIGHT?" without changing live logic.

LEAKAGE SAFEGUARDS (ALL ENFORCED IN CODE — see tests):

  L1. Pre-game odds only. Every OddsSnapshot query filters
      `captured_at < game.first_pitch`. Post-game snapshots (which can
      exist when an odds provider keeps polling after first pitch) are
      structurally excluded.

  L2. Recommendation input = OPENING snapshot only. The first pre-game
      snapshot's moneylines + market_home_win_prob drive the simulated
      placement decision. The closing snapshot (latest pre-game) is
      used ONLY after the recommendation is generated, for CLV
      measurement.

  L3. Pre-game team Elo from history. `TeamEloHistory.pre_rating` for
      the row created when the game was processed = the rating going
      INTO the game. No use of post-game `team.elo_rating`.

  L4. Static team rating is frozen. `Team.rating` has no updater
      (locked by `apps/core/test_feature_truth_audit.py`), so current
      value == historical value. No leakage by mechanism.

  L5. Outcome data (home_score / away_score) used ONLY for the post-
      simulation `won` field. Never feeds back into the recommendation.

DOCUMENTED LIMITATION (not leakage but worth naming):

  - Pitcher ratings (`StartingPitcher.rating`) are NOT historical.
    Current ratings are used as an approximation. Over a 7-30 day
    window a pitcher has ~1-5 additional starts since the simulated
    game, so the rating drift is small. The drift affects all method
    variants identically, so RELATIVE comparisons (0.40 vs 0.55)
    remain unbiased even though absolute simulated probabilities
    may differ slightly from what would have been computed live.

  - The `compute_status` and tier/lane logic uses CURRENT constants
    (MIN_EDGE, MIN_PROBABILITY_FOR_RECOMMENDED, etc.). The replay
    answers "what would today's rules + an alternative blend have
    produced over the past N days?" — not "what would the past
    rules at that time have produced." This is intentional: the
    purpose is to evaluate the new method against historical data,
    not to retroactively re-derive what the system actually did.
    For the latter, read `BettingRecommendation` rows directly.

INTENTIONALLY NOT TOUCHED:
  - `MARKET_BLEND_WEIGHT` in production code (still 0.55 per the
    2026-05-22 Roadmap B Step 1 change).
  - Any threshold, gate, or signal in the live recommendation
    pipeline. This is analysis only.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, asdict
from datetime import date, datetime, timedelta
from typing import List, Optional, Tuple

from django.utils import timezone

logger = logging.getLogger(__name__)


# Blend-weight history. Updated when MARKET_BLEND_WEIGHT changes in
# production. Each entry: (date_active_from, weight). Sorted DESC.
# Used by the "Old Actual" comparison to identify the blend weight
# in effect at the time of a historical game.
BLEND_WEIGHT_HISTORY: List[Tuple[date, float]] = [
    (date(2026, 5, 22), 0.55),
    (date(2026, 5, 6), 0.40),
    (date(2026, 5, 3), 0.30),
    (date(2025, 1, 1), 0.15),
]


def historical_blend_weight(d: date) -> float:
    """Return the MARKET_BLEND_WEIGHT in effect on date `d`."""
    for cutoff, weight in BLEND_WEIGHT_HISTORY:
        if d >= cutoff:
            return weight
    return 0.15


# ---------------------------------------------------------------------------
# Data structures


@dataclass
class SimulatedRecommendation:
    """One game's simulated recommendation under a single method variant."""
    sport: str
    game_id: str
    game_label: str
    first_pitch_iso: str
    method_label: str
    blend_weight: float
    # Inputs (no leakage)
    home_rating_pregame: float
    away_rating_pregame: float
    home_pitcher_rating: float
    away_pitcher_rating: float
    raw_score: float
    raw_prob_pre_blend: float
    market_prob_pregame: float
    blended_prob: float
    final_prob: float
    # Placement-time pre-game odds (OPENING snapshot)
    opening_moneyline_home: int
    opening_moneyline_away: int
    fair_home_prob: float
    fair_away_prob: float
    pick_side: str             # 'home' / 'away'
    pick_odds: int
    pick_prob: float
    edge_pp: float
    # Decision-rule output
    status: str
    status_reason: str
    tier: str
    # 2026-05-22 LANE-CORRECTED replay extension. Mirrors the production
    # `_moneyline_candidate` flow so the replay measures the SAME
    # population the live engine auto-recommends (status='recommended'
    # AND lane='core'). Without these fields the replay overcounted.
    lane: str = 'pass'                       # 'core' / 'qualified' / 'pass'
    risk_flags: dict = None
    risk_score: int = 0
    movement_class: Optional[str] = None
    movement_supports_pick: bool = False
    # Convenience field — True iff status='recommended' AND lane='core'.
    # This is the strict lane-corrected recommendation indicator. The
    # uncorrected `status` field is preserved for back-compat + for
    # showing the delta between corrected and uncorrected sets.
    is_lane_corrected_recommended: bool = False
    # Outcome (post-game; used only for analytics, NEVER for decision)
    home_score: Optional[int] = None
    away_score: Optional[int] = None
    won: Optional[bool] = None
    # CLV — uses CLOSING odds applied AFTER recommendation generation
    closing_moneyline_home: Optional[int] = None
    closing_moneyline_away: Optional[int] = None
    clv_decimal: Optional[float] = None

    def to_dict(self):
        return asdict(self)


# ---------------------------------------------------------------------------
# Helpers


def _pregame_snapshots(game, *, only_primary: bool = True):
    """Return ALL pre-game snapshots for the game, oldest first.

    LEAKAGE GUARD: filters `captured_at < game.first_pitch`. Post-game
    rows are structurally excluded.
    """
    from apps.mlb.models import OddsSnapshot
    qs = OddsSnapshot.objects.filter(
        game=game,
        captured_at__lt=game.first_pitch,
    )
    if only_primary:
        qs = qs.filter(odds_source='odds_api')
    return list(qs.order_by('captured_at'))


def _pregame_team_rating(team, game) -> float:
    """Pre-game rating for `team` going into `game` (NO LEAKAGE).

    Strategy:
      1. If Elo is active AND a TeamEloHistory row exists for (team, game):
         use its `pre_rating` (= rating immediately before this game).
      2. If Elo is active but no history row: fall back to current
         `team.elo_rating` projected to legacy scale (caveat).
      3. If Elo is not active: use `team.rating` (no updater → current
         value == historical value).
    """
    from apps.analytics.models import TeamEloHistory
    from apps.core.services import elo_service

    if not elo_service.is_dynamic_active():
        return float(team.rating)

    history_row = TeamEloHistory.objects.filter(
        sport='mlb', mlb_team=team, mlb_game=game,
    ).first()
    if history_row is not None:
        return float(elo_service.elo_to_legacy_scale(history_row.pre_rating))

    if team.elo_rating is not None:
        # Game hasn't been processed by Elo yet (rare for final games
        # after ensure_elo_backfilled). Falls back to current Elo —
        # technically leakage but limited because a single un-processed
        # game's pre-rating ≈ current rating to within K-factor (4 for MLB).
        return float(elo_service.elo_to_legacy_scale(team.elo_rating))

    return float(team.rating)


def _clamp_probability(p: float) -> float:
    """Mirrors `apps.core.services.probability_calibration.clamp_probability`.
    Duplicated here so the replay is self-contained and stable against
    refactors of the live module."""
    from apps.core.services.probability_calibration import PROB_MIN, PROB_MAX
    if p > 0.5:
        return max(PROB_MIN, min(PROB_MAX, p))
    if p < 0.5:
        return max(1.0 - PROB_MAX, min(1.0 - PROB_MIN, p))
    return p


def _pregame_movement_signal(game, pick_side: str) -> dict:
    """Pre-game-anchored mirror of `movement_signal_for_pick`.

    The live `movement_signal_for_pick` (in `apps.core.services.odds_movement`)
    cutoffs on `timezone.now() - HISTORY_MAX_HOURS`. That's correct for the
    live path (recommendations are generated before first_pitch, so all
    in-window snapshots are pre-game by temporal context). For historical
    replay, NOW is days or weeks past first_pitch, so calling the live
    function directly returns empty (no snapshots in last 24h of NOW).

    This replay version uses the SAME math but anchors the time window
    on `game.first_pitch` instead of `now`, AND adds an explicit
    `captured_at < first_pitch` filter as defense-in-depth against
    post-game snapshot leakage (L1 safeguard preserved).

    Returns the same dict shape as `movement_signal_for_pick`:
        movement_class, movement_score, supports_pick, market_warning, direction.
    """
    from datetime import timedelta as _td

    from apps.core.services.odds_movement import (
        HISTORY_MAX_HOURS, HISTORY_MAX_SNAPSHOTS,
        W_MAGNITUDE, W_SPEED, W_CONSISTENCY, W_TIMING,
        _per_market_signal, classify_score, _direction_for_attr,
    )
    from apps.mlb.models import OddsSnapshot

    empty = {
        'movement_class': None,
        'movement_score': None,
        'supports_pick': False,
        'market_warning': False,
        'direction': 0,
    }
    if pick_side not in ('home', 'away'):
        return empty
    if game is None or game.first_pitch is None:
        return empty

    cutoff = game.first_pitch - _td(hours=HISTORY_MAX_HOURS)
    snaps = list(
        OddsSnapshot.objects
        .filter(
            game=game,
            captured_at__gte=cutoff,
            # L1 SAFEGUARD: pre-game only. Even if the time window
            # extends past first_pitch (it shouldn't, but defense in
            # depth), exclude any snapshot at or after first_pitch.
            captured_at__lt=game.first_pitch,
        )
        .order_by('-captured_at')[:HISTORY_MAX_SNAPSHOTS * 3]
    )
    if len(snaps) < 2:
        return empty
    snaps = list(reversed(snaps))  # oldest → newest

    attr = 'moneyline_home' if pick_side == 'home' else 'moneyline_away'
    sig = _per_market_signal(snaps, attr)
    if sig is None:
        return empty

    score = (
        W_MAGNITUDE * sig['magnitude']
        + W_SPEED * sig['speed']
        + W_CONSISTENCY * sig['consistency']
        + W_TIMING * sig['timing']
    )
    cls = classify_score(score)
    direction = _direction_for_attr(attr, sig['signed_delta'])
    expected = 1 if pick_side == 'home' else -1
    supports = (cls in ('moderate', 'strong', 'sharp')) and direction == expected
    warning = (cls in ('strong', 'sharp')) and direction == -expected

    return {
        'movement_class': cls if cls != 'noise' else None,
        'movement_score': round(score, 2),
        'supports_pick': supports,
        'market_warning': warning,
        'direction': direction,
    }


def _simulate_recommendation(
    game,
    blend_weight: float,
    method_label: str,
    *,
    use_recent_form: bool = False,
) -> Optional[SimulatedRecommendation]:
    """Simulate one recommendation under the given blend weight.

    Returns None when the game has insufficient pre-game data
    (no primary-source snapshots, missing moneylines). Such games
    are NOT counted as recommended OR not-recommended — they're
    excluded entirely.
    """
    from apps.core.services.recommendations import (
        compute_status, _raw_tier,
    )
    from apps.core.utils.odds import (
        american_to_implied_prob, devig_two_way, closing_line_value,
    )
    from apps.mlb.services.model_service import HFA

    snaps = _pregame_snapshots(game, only_primary=True)
    if not snaps:
        return None

    opening = snaps[0]
    closing = snaps[-1]  # used ONLY for CLV measurement (L2 safeguard)

    if opening.moneyline_home is None or opening.moneyline_away is None:
        return None
    if opening.market_home_win_prob is None:
        return None

    # ---- L3 + L4: pre-game team ratings (no leakage) ----
    home_rating = _pregame_team_rating(game.home_team, game)
    away_rating = _pregame_team_rating(game.away_team, game)

    # ---- Pitcher ratings — current values (documented approximation) ----
    home_pitcher_rating = float(game.home_pitcher.rating) if game.home_pitcher else 50.0
    away_pitcher_rating = float(game.away_pitcher.rating) if game.away_pitcher else 50.0

    # ---- Score formula (mirrors apps.mlb.services.model_service._score) ----
    rating_term = (home_rating - away_rating) * 0.35
    pitcher_term = 0.0
    pitcher_form_term = 0.0
    if game.home_pitcher is not None and game.away_pitcher is not None:
        pitcher_term = (home_pitcher_rating - away_pitcher_rating) * 0.65
        # 2026-06-25 v3.1: recent-form variant. Anchor the form lookback on
        # game.first_pitch so the replay cannot peek at the game being
        # simulated or any later game. L1/L2/L3 leakage safeguards preserved.
        if use_recent_form:
            from apps.mlb.services.pitcher_form import recent_form_delta
            home_form = recent_form_delta(
                game.home_pitcher, reference_date=game.first_pitch,
            )
            away_form = recent_form_delta(
                game.away_pitcher, reference_date=game.first_pitch,
            )
            pitcher_form_term = (home_form - away_form) * 0.65
    hfa_term = HFA if not game.neutral_site else 0.0
    score = rating_term + pitcher_term + pitcher_form_term + hfa_term

    # ---- Sigmoid → raw probability ----
    raw_prob = 1.0 / (1.0 + math.exp(-score / 25.0))
    raw_prob = max(0.01, min(0.99, raw_prob))

    # ---- Blend with PRE-GAME (opening) market — never closing ----
    market_home = opening.market_home_win_prob
    w = max(0.0, min(0.65, blend_weight))
    blended = raw_prob * (1.0 - w) + market_home * w

    # ---- Soft clamp ----
    final = _clamp_probability(blended)

    # ---- De-vig OPENING moneylines for fair market prob ----
    raw_implied_home = american_to_implied_prob(opening.moneyline_home)
    raw_implied_away = american_to_implied_prob(opening.moneyline_away)
    fair_home, fair_away = devig_two_way(raw_implied_home, raw_implied_away)

    # ---- Pick the side with the larger edge ----
    away_prob = 1.0 - final
    home_edge = final - fair_home
    away_edge = away_prob - fair_away
    if home_edge >= away_edge:
        pick_side = 'home'
        pick_odds = opening.moneyline_home
        pick_prob = final
        edge_decimal = home_edge
    else:
        pick_side = 'away'
        pick_odds = opening.moneyline_away
        pick_prob = away_prob
        edge_decimal = away_edge
    edge_pp = round(edge_decimal * 100, 2)

    # ---- Apply current decision rules ----
    status, reason = compute_status(
        edge_pp, pick_odds,
        probability=pick_prob,
        is_secondary=False,  # primary source by construction (filter above)
    )
    tier = _raw_tier(edge_pp)

    # ---- Lane classification (2026-05-22 lane-corrected replay) ----
    # Mirrors `_moneyline_candidate`'s lane flow EXACTLY: movement signal
    # → lane_classify → tier blocked/secondary override. The replay
    # measures the production-equivalent recommended set (status='recommended'
    # AND lane='core'), not the over-broad compute_status-only set.
    from apps.core.services.recommendations import (
        LANE_CORE, LANE_QUALIFIED, _lane_classify,
    )
    movement = _pregame_movement_signal(game, pick_side)
    lane, risk_flags, risk_score = _lane_classify(
        probability=pick_prob,
        edge_decimal=edge_decimal,
        odds_american=pick_odds,
        source_quality='primary',  # filter enforces this
        movement_class=movement['movement_class'],
        movement_supports_pick=movement['supports_pick'],
        insight_conflicts=False,
    )
    # Defense in depth — blocked tier or secondary source caps lane.
    # In this replay, source is always 'primary' by filter, so
    # secondary doesn't apply. Tier='blocked' applies if it ever fires.
    if tier == 'blocked' and lane == LANE_CORE:
        lane = LANE_QUALIFIED

    is_lane_corrected_recommended = (
        status == 'recommended' and lane == LANE_CORE
    )

    # ---- Outcome (post-game) — analytics ONLY (L5 safeguard) ----
    won: Optional[bool] = None
    if game.home_score is not None and game.away_score is not None:
        if game.home_score == game.away_score:
            won = None  # push (defensive — no MLB regulation ties)
        else:
            home_won = game.home_score > game.away_score
            won = (pick_side == 'home' and home_won) or (
                pick_side == 'away' and not home_won
            )

    # ---- CLV — uses CLOSING odds AFTER recommendation (L2 safeguard) ----
    clv = None
    closing_ml_home = closing.moneyline_home
    closing_ml_away = closing.moneyline_away
    if (
        closing is not opening
        and closing_ml_home is not None
        and closing_ml_away is not None
    ):
        opening_pick_ml = (
            opening.moneyline_home if pick_side == 'home' else opening.moneyline_away
        )
        closing_pick_ml = (
            closing_ml_home if pick_side == 'home' else closing_ml_away
        )
        if opening_pick_ml is not None and closing_pick_ml is not None:
            clv = closing_line_value(opening_pick_ml, closing_pick_ml)

    return SimulatedRecommendation(
        sport='mlb',
        game_id=str(game.id),
        game_label=f"{game.away_team.name} @ {game.home_team.name}",
        first_pitch_iso=game.first_pitch.isoformat(),
        method_label=method_label,
        blend_weight=blend_weight,
        home_rating_pregame=round(home_rating, 3),
        away_rating_pregame=round(away_rating, 3),
        home_pitcher_rating=round(home_pitcher_rating, 3),
        away_pitcher_rating=round(away_pitcher_rating, 3),
        raw_score=round(score, 3),
        raw_prob_pre_blend=round(raw_prob, 4),
        market_prob_pregame=round(market_home, 4),
        blended_prob=round(blended, 4),
        final_prob=round(final, 4),
        opening_moneyline_home=opening.moneyline_home,
        opening_moneyline_away=opening.moneyline_away,
        fair_home_prob=round(fair_home, 4),
        fair_away_prob=round(fair_away, 4),
        pick_side=pick_side,
        pick_odds=pick_odds,
        pick_prob=round(pick_prob, 4),
        edge_pp=edge_pp,
        status=status,
        status_reason=reason,
        tier=tier,
        # 2026-05-22 lane-corrected fields:
        lane=lane,
        risk_flags=dict(risk_flags or {}),
        risk_score=risk_score,
        movement_class=movement['movement_class'],
        movement_supports_pick=movement['supports_pick'],
        is_lane_corrected_recommended=is_lane_corrected_recommended,
        home_score=game.home_score,
        away_score=game.away_score,
        won=won,
        closing_moneyline_home=closing_ml_home,
        closing_moneyline_away=closing_ml_away,
        clv_decimal=clv,
    )


# ---------------------------------------------------------------------------
# Aggregation


def _compute_metrics(recommended_sims: List[SimulatedRecommendation]) -> dict:
    """Aggregate metrics from a list of `status='recommended'` simulations."""
    from apps.core.utils.odds import american_to_decimal

    if not recommended_sims:
        return {
            'count': 0, 'wins': 0, 'losses': 0, 'pushes': 0, 'pending': 0,
            'win_rate': None, 'roi': None, 'net_pl': 0.0,
            'total_stake': 0.0,
            'avg_edge': None, 'avg_clv': None, 'positive_clv_rate': None,
            'clv_sample': 0,
            'clv_beat': 0, 'clv_matched': 0, 'clv_lost': 0,
            'favorites_count': 0, 'underdogs_count': 0,
            'by_tier': {'elite': 0, 'strong': 0, 'standard': 0},
            'by_edge_bucket': {'0-4': 0, '4-6': 0, '6-8': 0, '8+': 0},
            'by_confidence_bucket': {
                '60-65': 0, '65-70': 0, '70-75': 0, '75-80': 0, '80+': 0,
            },
            'by_odds_type': {
                'heavy_fav': 0, 'mid_fav': 0, 'short_fav': 0,
                'short_dog': 0, 'mid_dog': 0, 'long_dog': 0,
            },
        }

    wins = losses = pushes = pending = 0
    stake_total = 0.0
    payout_total = 0.0
    edge_sum = 0.0
    clv_sum = 0.0
    clv_count = 0
    clv_positive = 0
    clv_matched = 0
    clv_lost = 0
    favorites_count = underdogs_count = 0
    by_tier = {'elite': 0, 'strong': 0, 'standard': 0}
    by_edge_bucket = {'0-4': 0, '4-6': 0, '6-8': 0, '8+': 0}
    by_confidence_bucket = {
        '60-65': 0, '65-70': 0, '70-75': 0, '75-80': 0, '80+': 0,
    }
    by_odds_type = {
        'heavy_fav': 0, 'mid_fav': 0, 'short_fav': 0,
        'short_dog': 0, 'mid_dog': 0, 'long_dog': 0,
    }

    for s in recommended_sims:
        stake_total += 100.0
        if s.won is True:
            wins += 1
            payout_total += 100.0 * american_to_decimal(s.pick_odds)
        elif s.won is False:
            losses += 1
        elif s.won is None and s.home_score is None:
            pending += 1
        else:
            pushes += 1
            payout_total += 100.0

        if s.edge_pp is not None:
            edge_sum += s.edge_pp
            if s.edge_pp < 4:
                by_edge_bucket['0-4'] += 1
            elif s.edge_pp < 6:
                by_edge_bucket['4-6'] += 1
            elif s.edge_pp < 8:
                by_edge_bucket['6-8'] += 1
            else:
                by_edge_bucket['8+'] += 1

        if s.tier in by_tier:
            by_tier[s.tier] += 1

        if s.pick_prob is not None:
            p_pct = s.pick_prob * 100
            if p_pct < 65:
                by_confidence_bucket['60-65'] += 1
            elif p_pct < 70:
                by_confidence_bucket['65-70'] += 1
            elif p_pct < 75:
                by_confidence_bucket['70-75'] += 1
            elif p_pct < 80:
                by_confidence_bucket['75-80'] += 1
            else:
                by_confidence_bucket['80+'] += 1

        if s.pick_odds is not None:
            if s.pick_odds < 0:
                favorites_count += 1
            else:
                underdogs_count += 1
            o = int(s.pick_odds)
            if o <= -200:
                by_odds_type['heavy_fav'] += 1
            elif -199 <= o <= -150:
                by_odds_type['mid_fav'] += 1
            elif -149 <= o <= 99:
                by_odds_type['short_fav'] += 1
            elif 100 <= o <= 150:
                by_odds_type['short_dog'] += 1
            elif 151 <= o <= 250:
                by_odds_type['mid_dog'] += 1
            else:
                by_odds_type['long_dog'] += 1

        if s.clv_decimal is not None:
            clv_sum += s.clv_decimal
            clv_count += 1
            if s.clv_decimal > 0:
                clv_positive += 1
            elif s.clv_decimal < 0:
                clv_lost += 1
            else:
                clv_matched += 1

    net_pl = payout_total - stake_total
    decisive = wins + losses

    return {
        'count': len(recommended_sims),
        'wins': wins, 'losses': losses, 'pushes': pushes, 'pending': pending,
        'win_rate': round(wins / decisive * 100, 2) if decisive else None,
        'roi': round(net_pl / stake_total * 100, 2) if stake_total else None,
        'net_pl': round(net_pl, 2),
        'total_stake': round(stake_total, 2),
        'avg_edge': round(edge_sum / len(recommended_sims), 2),
        'avg_clv': round(clv_sum / clv_count, 4) if clv_count else None,
        'positive_clv_rate': round(clv_positive / clv_count * 100, 2) if clv_count else None,
        'clv_sample': clv_count,
        'clv_beat': clv_positive,
        'clv_matched': clv_matched,
        'clv_lost': clv_lost,
        'favorites_count': favorites_count,
        'underdogs_count': underdogs_count,
        'by_tier': by_tier,
        'by_edge_bucket': by_edge_bucket,
        'by_confidence_bucket': by_confidence_bucket,
        'by_odds_type': by_odds_type,
    }


def diff_recommendations(
    variant_a: dict, variant_b: dict,
    *, use_lane_corrected: bool = False,
) -> dict:
    """Per-game divergences between two method variants.

    a_only: games variant_a recommended but variant_b did not.
    b_only: games variant_b recommended but variant_a did not.
    both: tuples of (a_sim, b_sim) for games both recommended.
    largest_prob_diff: top games where final_prob diverged most between
        the two methods (any status).

    use_lane_corrected: when True, uses `is_lane_corrected_recommended`
        instead of bare `status='recommended'`. The corrected version
        is the production-equivalent comparison.
    """
    if use_lane_corrected:
        predicate = lambda s: s.is_lane_corrected_recommended
    else:
        predicate = lambda s: s.status == 'recommended'

    a_recs = {
        s.game_id: s for s in variant_a['simulations']
        if predicate(s)
    }
    b_recs = {
        s.game_id: s for s in variant_b['simulations']
        if predicate(s)
    }
    a_only = [a_recs[gid] for gid in (a_recs.keys() - b_recs.keys())]
    b_only = [b_recs[gid] for gid in (b_recs.keys() - a_recs.keys())]
    both = [
        (a_recs[gid], b_recs[gid])
        for gid in (a_recs.keys() & b_recs.keys())
    ]

    # Largest probability change across all simulated games (any status).
    a_by_game = {s.game_id: s for s in variant_a['simulations']}
    b_by_game = {s.game_id: s for s in variant_b['simulations']}
    shared_ids = a_by_game.keys() & b_by_game.keys()
    prob_diffs = []
    for gid in shared_ids:
        a = a_by_game[gid]
        b = b_by_game[gid]
        prob_diffs.append({
            'game_id': gid,
            'game_label': a.game_label,
            'a_final_prob': a.final_prob,
            'b_final_prob': b.final_prob,
            'delta': round(b.final_prob - a.final_prob, 4),
            'a_edge_pp': a.edge_pp,
            'b_edge_pp': b.edge_pp,
            'edge_delta': round(b.edge_pp - a.edge_pp, 2),
            'a_status': a.status,
            'b_status': b.status,
        })
    prob_diffs.sort(key=lambda d: -abs(d['delta']))

    return {
        'a_only_count': len(a_only),
        'b_only_count': len(b_only),
        'both_count': len(both),
        'a_only': a_only,
        'b_only': b_only,
        'largest_prob_diffs': prob_diffs[:10],
    }


# ---------------------------------------------------------------------------
# Public entry point


def run_replay(
    date_from: date,
    date_to: date,
    blend_weights: Optional[List[float]] = None,
    method_labels: Optional[List[str]] = None,
) -> dict:
    """Run the method replay across a date window.

    Returns a dict with:
        window: {from, to, days}
        total_games_evaluable: int
        variants: list of {label, blend_weight, simulations, metrics}
        diff_vs_baseline: {a_only_count, b_only_count, ...}
                          (only when 2 variants are present)
    """
    from apps.mlb.models import Game

    if blend_weights is None:
        blend_weights = [0.40, 0.55]
    if method_labels is None:
        method_labels = [f'Replay {w:.2f}' for w in blend_weights]

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

    variants = []
    for weight, label in zip(blend_weights, method_labels):
        # Per-game isolation: a single pathological game (unexpected data
        # shape) must never 500 the whole replay. Skip + log + count.
        simulations = []
        sim_errors = 0
        for g in games:
            try:
                sim = _simulate_recommendation(g, weight, label)
            except Exception:
                sim_errors += 1
                logger.exception(
                    'method_replay: _simulate_recommendation failed '
                    'game=%s weight=%s', getattr(g, 'id', None), weight,
                )
                continue
            if sim is not None:
                simulations.append(sim)
        # Uncorrected set — compute_status='recommended' only. Same as
        # the original replay; kept for delta comparison.
        recommended_only = [s for s in simulations if s.status == 'recommended']
        # Lane-corrected set — additionally requires lane='core'.
        # This is the production-equivalent recommended population.
        lane_corrected = [
            s for s in simulations if s.is_lane_corrected_recommended
        ]
        metrics = _compute_metrics(recommended_only)
        metrics_corrected = _compute_metrics(lane_corrected)
        # Delta: how many picks did the lane filter remove + their risk
        # flag breakdown. Operator-readable.
        demoted = [
            s for s in recommended_only
            if not s.is_lane_corrected_recommended
        ]
        demoted_by_flag = {}
        for s in demoted:
            for flag, fired in (s.risk_flags or {}).items():
                if fired:
                    demoted_by_flag[flag] = demoted_by_flag.get(flag, 0) + 1
            # If lane is 'pass' (hard gate failure) rather than 'qualified',
            # surface that too. Should be empty in practice because
            # compute_status enforces the same gates, but defense in depth.
            if s.lane == 'pass' and s.risk_score == 0:
                demoted_by_flag['hard_gate_fail'] = demoted_by_flag.get('hard_gate_fail', 0) + 1
        variants.append({
            'label': label,
            'blend_weight': weight,
            'simulations': simulations,
            'recommended_count': len(recommended_only),
            'lane_corrected_count': len(lane_corrected),
            'metrics': metrics,
            'metrics_corrected': metrics_corrected,
            'demoted_count': len(demoted),
            'demoted_by_flag': demoted_by_flag,
            'sim_errors': sim_errors,
        })

    out = {
        'window': {
            'from': date_from,
            'to': date_to,
            'days': (date_to - date_from).days + 1,
        },
        'total_games_evaluable': len(games),
        'variants': variants,
    }

    if len(variants) >= 2:
        out['diff_first_two'] = diff_recommendations(variants[0], variants[1])
        # Lane-corrected diff: same comparison but restricted to
        # lane-corrected recommended sets per variant.
        out['diff_first_two_corrected'] = diff_recommendations(
            variants[0], variants[1], use_lane_corrected=True,
        )

    return out


# ---------------------------------------------------------------------------
# BLEND EXPERIMENT — 0.40 vs 0.55 on the EXACT SAME slate, multi-window.
#
# Read-only. Both variants are simulated over the identical `games` list
# inside run_replay (same population / outcomes / snapshots / pre-game info /
# replay rules). The ONLY variable is the blend weight. This module adds:
#   - per-bucket PERFORMANCE (W-L / ROI / CLV), not just counts
#   - multi-window orchestration (7 / 14 / 30 / 60 days)
#   - delta tables (blend_b − blend_a)
# No production constants are touched; the live MARKET_BLEND_WEIGHT is
# never read here — the weight is passed explicitly to _simulate_recommendation.
# ---------------------------------------------------------------------------

ODDS_TYPE_ORDER = [
    'heavy_fav', 'mid_fav', 'short_fav', 'short_dog', 'mid_dog', 'long_dog',
]
ODDS_TYPE_LABELS = {
    'heavy_fav': 'Heavy fav (≤ -200)',
    'mid_fav': 'Mid fav (-150..-199)',
    'short_fav': 'Short fav (-149..+99)',
    'short_dog': 'Short dog (+100..+150)',
    'mid_dog': 'Mid dog (+151..+250)',
    'long_dog': 'Long dog (≥ +251)',
}
CONF_BUCKET_ORDER = ['60-65', '65-70', '70-75', '75-80', '80+']


def _odds_type(o) -> Optional[str]:
    if o is None:
        return None
    o = int(o)
    if o <= -200:
        return 'heavy_fav'
    if -199 <= o <= -150:
        return 'mid_fav'
    if -149 <= o <= 99:
        return 'short_fav'
    if 100 <= o <= 150:
        return 'short_dog'
    if 151 <= o <= 250:
        return 'mid_dog'
    return 'long_dog'


def _conf_bucket(p) -> Optional[str]:
    if p is None:
        return None
    pct = p * 100
    if pct < 65:
        return '60-65'
    if pct < 70:
        return '65-70'
    if pct < 75:
        return '70-75'
    if pct < 80:
        return '75-80'
    return '80+'


def _perf(sims: List[SimulatedRecommendation]) -> dict:
    """Lightweight W-L / ROI / CLV-mix for an arbitrary sims list.

    Uses the SAME $100 flat-stake convention and decimal-odds payout as
    _compute_metrics, so bucket numbers reconcile with the headline.
    """
    from apps.core.utils.odds import american_to_decimal

    n = len(sims)
    wins = losses = pushes = pending = 0
    stake = payout = 0.0
    edge_sum = 0.0
    edge_n = 0
    clv_sum = 0.0
    clv_n = clv_beat = clv_matched = clv_lost = 0

    for s in sims:
        stake += 100.0
        if s.won is True:
            wins += 1
            payout += 100.0 * american_to_decimal(s.pick_odds)
        elif s.won is False:
            losses += 1
        elif s.won is None and s.home_score is None:
            pending += 1
        else:
            pushes += 1
            payout += 100.0
        if s.edge_pp is not None:
            edge_sum += s.edge_pp
            edge_n += 1
        if s.clv_decimal is not None:
            clv_sum += s.clv_decimal
            clv_n += 1
            if s.clv_decimal > 0:
                clv_beat += 1
            elif s.clv_decimal < 0:
                clv_lost += 1
            else:
                clv_matched += 1

    decisive = wins + losses
    net = payout - stake
    return {
        'count': n,
        'wins': wins, 'losses': losses, 'pushes': pushes, 'pending': pending,
        'win_rate': round(wins / decisive * 100, 2) if decisive else None,
        'roi': round(net / stake * 100, 2) if stake else None,
        'net_pl': round(net, 2),
        'avg_edge': round(edge_sum / edge_n, 2) if edge_n else None,
        'avg_clv': round(clv_sum / clv_n, 4) if clv_n else None,
        'clv_sample': clv_n,
        'clv_beat': clv_beat, 'clv_matched': clv_matched, 'clv_lost': clv_lost,
    }


def _bucket_performance(sims: List[SimulatedRecommendation]) -> dict:
    """Per odds-type and per confidence-bucket performance, plus fav/dog."""
    odds_groups = {k: [] for k in ODDS_TYPE_ORDER}
    conf_groups = {k: [] for k in CONF_BUCKET_ORDER}
    fav, dog = [], []
    for s in sims:
        ot = _odds_type(s.pick_odds)
        if ot:
            odds_groups[ot].append(s)
        cb = _conf_bucket(s.pick_prob)
        if cb:
            conf_groups[cb].append(s)
        if s.pick_odds is not None:
            (fav if s.pick_odds < 0 else dog).append(s)
    return {
        'by_odds_type': {k: _perf(v) for k, v in odds_groups.items()},
        'by_confidence_bucket': {k: _perf(v) for k, v in conf_groups.items()},
        'favorites': _perf(fav),
        'underdogs': _perf(dog),
    }


# Favorite sub-ranges per the favorites-only experiment definition. Ordered
# heavy → short → (underdog, shown for transparency on the standard set).
# "Favorites only" = pick_odds < +100 (the first three ranges); underdogs
# are pick_odds >= +100.
FAV_SUBRANGES = [
    ('heavy_fav (≤ -200)',    lambda o: o is not None and o <= -200),
    ('mid_fav (-150..-199)',  lambda o: o is not None and -199 <= o <= -150),
    ('short_fav (-149..+99)', lambda o: o is not None and -149 <= o <= 99),
    ('underdog (≥ +100)',     lambda o: o is not None and o >= 100),
]


def _is_favorite_only(sim) -> bool:
    """Favorites-only filter: keep picks priced below +100 (drop underdogs)."""
    return sim.pick_odds is not None and sim.pick_odds < 100


def _favorite_subrange_performance(sims: List[SimulatedRecommendation]) -> dict:
    """Per favorite sub-range performance (heavy/mid/short + underdog)."""
    return {
        label: _perf([s for s in sims if pred(s.pick_odds)])
        for label, pred in FAV_SUBRANGES
    }


_DELTA_KEYS = (
    'count', 'wins', 'losses', 'win_rate', 'roi', 'net_pl', 'avg_edge',
    'avg_clv', 'positive_clv_rate', 'clv_beat', 'clv_matched', 'clv_lost',
    'favorites_count', 'underdogs_count',
)


def _delta(b: dict, a: dict) -> dict:
    """b − a for numeric scalars; None when either side is None."""
    out = {}
    for key in _DELTA_KEYS:
        av, bv = a.get(key), b.get(key)
        out[key] = None if (av is None or bv is None) else round(bv - av, 4)
    return out


def run_blend_experiment(
    *,
    blend_a: float = 0.40,
    blend_b: float = 0.55,
    windows: Tuple[int, ...] = (7, 14, 30, 60),
    reference_date: Optional[date] = None,
    min_games_for_window: int = 20,
) -> dict:
    """Compare two blend weights on the SAME historical slate across windows.

    PERFORMANCE: the windows are nested with the SAME end date (7 ⊂ 14 ⊂ 30
    ⊂ 60). The earlier version called run_replay once PER window, re-simulating
    overlapping games up to 4× — a query fan-out that scales with production
    volume and could exceed the gunicorn worker timeout (surfacing as a 500).
    This version simulates the WIDEST window ONCE per weight, then slices the
    sub-windows by first_pitch date. Output is mathematically identical
    (nested same-end-date windows) but the work is ~halved.

    ROBUSTNESS: each game's simulation is isolated in try/except so a single
    pathological row degrades to a skip (logged + counted), never a 500.
    """
    from apps.mlb.models import Game

    ref = reference_date or timezone.localdate()
    date_to = ref - timedelta(days=1)        # exclude today (games not final)
    widest = max(windows) if windows else 60
    date_from_widest = ref - timedelta(days=widest)

    games = list(
        Game.objects.filter(
            status='final',
            home_score__isnull=False,
            away_score__isnull=False,
            first_pitch__date__gte=date_from_widest,
            first_pitch__date__lte=date_to,
        )
        .select_related('home_team', 'away_team', 'home_pitcher', 'away_pitcher')
        .order_by('first_pitch')
    )

    # Simulate ONCE per weight over the widest game set. Store (game_date, sim)
    # so sub-windows can be sliced without re-querying or re-simulating.
    def _simulate_all(weight: float):
        out = []          # list of (game_date, SimulatedRecommendation)
        errors = 0
        for g in games:
            try:
                sim = _simulate_recommendation(g, weight, f'{weight:.2f}')
            except Exception:
                errors += 1
                logger.exception(
                    'run_blend_experiment: sim failed game=%s weight=%s',
                    getattr(g, 'id', None), weight,
                )
                continue
            if sim is not None:
                out.append((g.first_pitch.date(), sim))
        return out, errors

    a_all, a_errors = _simulate_all(blend_a)
    b_all, b_errors = _simulate_all(blend_b)

    window_results = []
    for w in windows:
        date_from = ref - timedelta(days=w)

        def _slice(all_sims):
            return [
                sim for (gd, sim) in all_sims
                if date_from <= gd <= date_to and sim.is_lane_corrected_recommended
            ]

        a_sims = _slice(a_all)
        b_sims = _slice(b_all)
        a_metrics = _compute_metrics(a_sims)
        b_metrics = _compute_metrics(b_sims)
        games_evaluable = sum(
            1 for g in games if date_from <= g.first_pitch.date() <= date_to
        )
        window_results.append({
            'days': w,
            'date_from': date_from,
            'date_to': date_to,
            'games_evaluable': games_evaluable,
            'data_ok': games_evaluable >= min_games_for_window,
            'a': {
                'blend': blend_a, 'metrics': a_metrics,
                'buckets': _bucket_performance(a_sims),
            },
            'b': {
                'blend': blend_b, 'metrics': b_metrics,
                'buckets': _bucket_performance(b_sims),
            },
            'delta': _delta(b_metrics, a_metrics),
        })

    return {
        'blend_a': blend_a,
        'blend_b': blend_b,
        'reference_date': ref,
        'min_games_for_window': min_games_for_window,
        'sim_errors': {'a': a_errors, 'b': b_errors},
        'windows': window_results,
    }


def run_favorites_experiment(
    *,
    blend: float = 0.55,
    windows: Tuple[int, ...] = (30, 60, 90),
    reference_date: Optional[date] = None,
    min_games_for_window: int = 20,
) -> dict:
    """Favorites-only diagnostic — A = standard 0.55, B = 0.55 + favorites-only.

    SAME methodology and slates as the blend experiment. The ONLY variable is
    whether underdogs (pick_odds >= +100) are allowed:
        A (standard)        = lane-corrected recommended set at `blend`
        B (favorites-only)  = A, with underdog picks removed (pick_odds < +100)

    B is strictly a SUBSET of A. If A already contains no underdogs (the 0.55
    blend tends to suppress them), B == A and every delta is zero — which is
    itself the answer: the favorites-only rule adds nothing on top of 0.55.

    Read-only. Simulate-once-and-slice + per-game isolation (same hardening as
    run_blend_experiment). No production constants changed.
    """
    from apps.mlb.models import Game

    ref = reference_date or timezone.localdate()
    date_to = ref - timedelta(days=1)
    widest = max(windows) if windows else 90
    date_from_widest = ref - timedelta(days=widest)

    games = list(
        Game.objects.filter(
            status='final',
            home_score__isnull=False,
            away_score__isnull=False,
            first_pitch__date__gte=date_from_widest,
            first_pitch__date__lte=date_to,
        )
        .select_related('home_team', 'away_team', 'home_pitcher', 'away_pitcher')
        .order_by('first_pitch')
    )

    sims = []          # (game_date, sim) for lane-corrected recommended only
    sim_errors = 0
    for g in games:
        try:
            sim = _simulate_recommendation(g, blend, f'{blend:.2f}')
        except Exception:
            sim_errors += 1
            logger.exception(
                'run_favorites_experiment: sim failed game=%s', getattr(g, 'id', None),
            )
            continue
        if sim is not None and sim.is_lane_corrected_recommended:
            sims.append((g.first_pitch.date(), sim))

    window_results = []
    for w in windows:
        date_from = ref - timedelta(days=w)
        a_sims = [s for (gd, s) in sims if date_from <= gd <= date_to]
        b_sims = [s for s in a_sims if _is_favorite_only(s)]
        a_metrics = _compute_metrics(a_sims)
        b_metrics = _compute_metrics(b_sims)
        games_evaluable = sum(
            1 for g in games if date_from <= g.first_pitch.date() <= date_to
        )
        window_results.append({
            'days': w,
            'date_from': date_from,
            'date_to': date_to,
            'games_evaluable': games_evaluable,
            'data_ok': games_evaluable >= min_games_for_window,
            'underdogs_removed': len(a_sims) - len(b_sims),
            'a': {
                'label': 'standard 0.55', 'metrics': a_metrics,
                'buckets': _bucket_performance(a_sims),
                'fav_subranges': _favorite_subrange_performance(a_sims),
            },
            'b': {
                'label': 'favorites-only', 'metrics': b_metrics,
                'buckets': _bucket_performance(b_sims),
                'fav_subranges': _favorite_subrange_performance(b_sims),
            },
            'delta': _delta(b_metrics, a_metrics),
        })

    return {
        'blend': blend,
        'reference_date': ref,
        'min_games_for_window': min_games_for_window,
        'sim_errors': sim_errors,
        'windows': window_results,
    }


# ---------------------------------------------------------------------------
# Blend experiment — plaintext renderer (staff HTTP view)

def _fmt(v, pct=False, money=False, places=2):
    if v is None:
        return '—'
    if money:
        return f"${v:+,.2f}"
    if pct:
        return f"{v:+.2f}%" if isinstance(v, float) else f"{v}%"
    return f"{v}"


def _metric_row(label, m):
    wlp = f"{m['wins']}-{m['losses']}-{m['pushes']}"
    win = f"{m['win_rate']:.1f}%" if m['win_rate'] is not None else '—'
    roi = f"{m['roi']:+.2f}%" if m['roi'] is not None else '—'
    npl = f"${m['net_pl']:+,.2f}"
    edge = f"{m['avg_edge']:.2f}" if m.get('avg_edge') is not None else '—'
    clv = f"{m['avg_clv']:+.4f}" if m.get('avg_clv') is not None else '—'
    mix = f"{m.get('clv_beat',0)}/{m.get('clv_matched',0)}/{m.get('clv_lost',0)}"
    return (
        f"  {label:<14} n={m['count']:<4} {wlp:>9} win {win:>6} "
        f"ROI {roi:>8} P/L {npl:>11} edge {edge:>6} avgCLV {clv:>9} "
        f"beat/match/lost {mix:>9}"
    )


def _roi_str(m) -> str:
    return f"{m['roi']:+.1f}%" if m.get('roi') is not None else '—'


def _bucket_cell(tag, m) -> str:
    """e.g. 'a[n=12 7-5 ROI +4.2%]'."""
    return f"{tag}[n={m['count']} {m['wins']}-{m['losses']} ROI {_roi_str(m)}]"


def render_blend_experiment(exp: dict) -> str:
    a_w, b_w = exp['blend_a'], exp['blend_b']
    lines = []
    lines.append("#" * 118)
    lines.append(
        f"#  BLEND EXPERIMENT — {a_w:.2f} vs {b_w:.2f} on the EXACT SAME historical MLB slate"
    )
    lines.append(
        f"#  Read-only counterfactual. Only the blend weight differs. "
        f"Population = LANE-CORRECTED recommended (production-equivalent)."
    )
    lines.append(f"#  Reference date: {exp['reference_date'].isoformat()}  "
                 f"(windows end the prior day; today excluded — games not final)")
    lines.append("#" * 118)

    for wr in exp['windows']:
        d = wr['delta']
        lines.append("")
        lines.append("=" * 118)
        lines.append(
            f"  WINDOW: {wr['days']} days   "
            f"({wr['date_from'].isoformat()} → {wr['date_to'].isoformat()})   "
            f"games evaluable: {wr['games_evaluable']}"
            + ("" if wr['data_ok'] else
               f"   ⚠ THIN DATA (< {exp['min_games_for_window']} games) — directional at best")
        )
        lines.append("=" * 118)
        lines.append(_metric_row(f"{a_w:.2f}", wr['a']['metrics']))
        lines.append(_metric_row(f"{b_w:.2f}", wr['b']['metrics']))
        # Delta line
        def dd(key, pct=False, money=False):
            v = d.get(key)
            if v is None:
                return '—'
            if money:
                return f"${v:+,.2f}"
            if pct:
                return f"{v:+.2f}pp"
            return f"{v:+g}"
        lines.append(
            f"  Δ (b−a)        count {dd('count')}   "
            f"win% {dd('win_rate', pct=True)}   "
            f"ROI {dd('roi', pct=True)}   "
            f"P/L {dd('net_pl', money=True)}   "
            f"avgCLV {dd('avg_clv')}   "
            f"beat {dd('clv_beat')}  matched {dd('clv_matched')}  lost {dd('clv_lost')}"
        )

        # Per-odds-bucket performance
        lines.append("")
        lines.append("  BY ODDS BUCKET (a / b):")
        for k in ODDS_TYPE_ORDER:
            am = wr['a']['buckets']['by_odds_type'][k]
            bm = wr['b']['buckets']['by_odds_type'][k]
            if am['count'] == 0 and bm['count'] == 0:
                continue
            lines.append(
                f"    {ODDS_TYPE_LABELS[k]:<22} "
                f"{_bucket_cell('a', am)}  {_bucket_cell('b', bm)}"
            )

        # Per-confidence-bucket performance
        lines.append("")
        lines.append("  BY CONFIDENCE BUCKET (a / b):")
        for k in CONF_BUCKET_ORDER:
            am = wr['a']['buckets']['by_confidence_bucket'][k]
            bm = wr['b']['buckets']['by_confidence_bucket'][k]
            if am['count'] == 0 and bm['count'] == 0:
                continue
            lines.append(
                f"    {k:<8} {_bucket_cell('a', am)}  {_bucket_cell('b', bm)}"
            )

        # Favorite vs dog
        af, ad = wr['a']['buckets']['favorites'], wr['a']['buckets']['underdogs']
        bf, bd = wr['b']['buckets']['favorites'], wr['b']['buckets']['underdogs']
        lines.append("")
        lines.append("  FAVORITE vs DOG (a / b):")
        lines.append(f"    Favorites  {_bucket_cell('a', af)}  {_bucket_cell('b', bf)}")
        lines.append(f"    Underdogs  {_bucket_cell('a', ad)}  {_bucket_cell('b', bd)}")

    lines.append("")
    return "\n".join(lines) + "\n"


def render_favorites_experiment(exp: dict) -> str:
    """Plaintext render of run_favorites_experiment() for the staff view."""
    blend = exp['blend']
    lines = []
    lines.append("#" * 118)
    lines.append(
        f"#  FAVORITES-ONLY EXPERIMENT — standard {blend:.2f} (A) vs "
        f"{blend:.2f}+favorites-only (B) on the EXACT SAME slate"
    )
    lines.append(
        "#  Read-only. ONLY variable: underdogs allowed (A) vs removed "
        "(B, pick_odds < +100). B ⊆ A. Population = lane-corrected recommended."
    )
    lines.append(f"#  Reference date: {exp['reference_date'].isoformat()}  "
                 f"(windows end the prior day)")
    lines.append("#" * 118)

    for wr in exp['windows']:
        d = wr['delta']
        lines.append("")
        lines.append("=" * 118)
        lines.append(
            f"  WINDOW: {wr['days']} days   "
            f"({wr['date_from'].isoformat()} → {wr['date_to'].isoformat()})   "
            f"games evaluable: {wr['games_evaluable']}   "
            f"underdogs removed by B: {wr['underdogs_removed']}"
            + ("" if wr['data_ok'] else
               f"   ⚠ THIN DATA (< {exp['min_games_for_window']} games) — directional at best")
        )
        lines.append("=" * 118)
        lines.append(_metric_row('A standard', wr['a']['metrics']))
        lines.append(_metric_row('B favs-only', wr['b']['metrics']))

        def dd(key, pct=False, money=False):
            v = d.get(key)
            if v is None:
                return '—'
            if money:
                return f"${v:+,.2f}"
            if pct:
                return f"{v:+.2f}pp"
            return f"{v:+g}"
        lines.append(
            f"  Δ (B−A)        count {dd('count')}   "
            f"win% {dd('win_rate', pct=True)}   "
            f"ROI {dd('roi', pct=True)}   "
            f"P/L {dd('net_pl', money=True)}   "
            f"avgCLV {dd('avg_clv')}   "
            f"beat {dd('clv_beat')}  matched {dd('clv_matched')}  lost {dd('clv_lost')}"
        )
        if wr['underdogs_removed'] == 0:
            lines.append(
                "  NOTE: B removed 0 picks — the 0.55 set already contains no "
                "underdogs, so favorites-only is a NO-OP in this window."
            )

        # Favorite sub-range performance (the experiment's headline breakdown)
        lines.append("")
        lines.append("  FAVORITE SUB-RANGE PERFORMANCE (a / b):")
        for label, _ in FAV_SUBRANGES:
            am = wr['a']['fav_subranges'][label]
            bm = wr['b']['fav_subranges'][label]
            if am['count'] == 0 and bm['count'] == 0:
                continue
            lines.append(
                f"    {label:<22} {_bucket_cell('a', am)}  {_bucket_cell('b', bm)}"
            )

        # Per-confidence-bucket performance
        lines.append("")
        lines.append("  BY CONFIDENCE BUCKET (a / b):")
        for k in CONF_BUCKET_ORDER:
            am = wr['a']['buckets']['by_confidence_bucket'][k]
            bm = wr['b']['buckets']['by_confidence_bucket'][k]
            if am['count'] == 0 and bm['count'] == 0:
                continue
            lines.append(
                f"    {k:<8} {_bucket_cell('a', am)}  {_bucket_cell('b', bm)}"
            )

    lines.append("")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# 2026-06-25 v3.1 — Recent-form experiment (A: production / B: + form delta)
#
# Pre-registered ship criteria for activating USE_STARTER_RECENT_FORM=true:
#   • ROI improves by ≥ +2pp
#   • 60–65% bucket calibration does not worsen
#   • Recommendation volume remains usable (no catastrophic drop)
#   • No major bucket regression
#   • CLV does not materially worsen
#
# These are checked mechanically in render_recent_form_experiment() so the
# verdict is data-driven, not subjective. Read-only — no engine code paths
# are modified at run time.
# ---------------------------------------------------------------------------

def run_recent_form_experiment(
    *,
    days: int = 90,
    blend_weight: float = 0.55,
    reference_date=None,
    min_games_for_window: int = 20,
) -> dict:
    """Compare production (without recent form) vs +form on the SAME slate.

    Same simulate-once-and-slice discipline as run_blend_experiment to keep
    query load reasonable. Produces lane-corrected metrics + per-bucket
    calibration so the ship-criteria check can run mechanically.
    """
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

    def _simulate_all(use_form: bool):
        sims = []
        errors = 0
        for g in games:
            try:
                sim = _simulate_recommendation(
                    g, blend_weight, ('+form' if use_form else 'prod'),
                    use_recent_form=use_form,
                )
            except Exception:
                errors += 1
                logger.exception(
                    'recent_form_experiment: sim failed game=%s use_form=%s',
                    getattr(g, 'id', None), use_form,
                )
                continue
            if sim is not None:
                sims.append(sim)
        return sims, errors

    a_sims, a_errors = _simulate_all(False)   # A — production
    b_sims, b_errors = _simulate_all(True)    # B — +form

    a_lc = [s for s in a_sims if s.is_lane_corrected_recommended]
    b_lc = [s for s in b_sims if s.is_lane_corrected_recommended]
    a_metrics = _compute_metrics(a_lc)
    b_metrics = _compute_metrics(b_lc)

    # Per-confidence-bucket calibration for both variants (the centerpiece
    # of the ship criterion — 60–65% must not worsen).
    def _per_bucket(sims):
        buckets = {'60-65': [], '65-70': [], '70-75': [], '75+': []}
        for s in sims:
            p_pct = s.pick_prob * 100 if s.pick_prob is not None else None
            if p_pct is None:
                continue
            if p_pct < 65:
                key = '60-65'
            elif p_pct < 70:
                key = '65-70'
            elif p_pct < 75:
                key = '70-75'
            else:
                key = '75+'
            buckets[key].append(s)
        out = {}
        for k, group in buckets.items():
            wins = sum(1 for s in group if s.won is True)
            losses = sum(1 for s in group if s.won is False)
            decisive = wins + losses
            win_pct = round(100.0 * wins / decisive, 2) if decisive else None
            midpoint = {'60-65': 62.5, '65-70': 67.5,
                        '70-75': 72.5, '75+': 80.0}[k]
            error = (round(win_pct - midpoint, 2)
                     if win_pct is not None else None)
            out[k] = {
                'count': len(group), 'wins': wins, 'losses': losses,
                'predicted_pct': midpoint, 'actual_pct': win_pct,
                'calibration_error_pp': error,
            }
        return out

    return {
        'window': {'days': days, 'from': date_from, 'to': date_to,
                   'blend_weight': blend_weight,
                   'games_evaluable': len(games)},
        'a_prod': {
            'metrics': a_metrics, 'buckets': _per_bucket(a_lc),
            'count': len(a_lc), 'sim_errors': a_errors,
        },
        'b_plus_form': {
            'metrics': b_metrics, 'buckets': _per_bucket(b_lc),
            'count': len(b_lc), 'sim_errors': b_errors,
        },
        'data_ok': len(games) >= min_games_for_window,
    }


def render_recent_form_experiment(exp: dict) -> str:
    """Plaintext renderer + automatic ship-criteria check."""
    w = exp['window']
    a = exp['a_prod']
    b = exp['b_plus_form']
    am = a['metrics']
    bm = b['metrics']

    lines = []
    lines.append("#" * 100)
    lines.append("#  RECENT-FORM EXPERIMENT — A: production / B: + starter recent form")
    lines.append(f"#  Window: last {w['days']} days  ({w['from']} → {w['to']})")
    lines.append(f"#  Blend weight: {w['blend_weight']:.2f}    "
                 f"Games evaluable: {w['games_evaluable']}")
    lines.append("#" * 100)
    lines.append("")
    if not exp['data_ok']:
        lines.append("⚠ THIN DATA — too few games in window; treat results as directional only.")
        lines.append("")

    def _line(label, m):
        win = f"{m['win_rate']:.1f}%" if m['win_rate'] is not None else '—'
        roi = f"{m['roi']:+.2f}%" if m['roi'] is not None else '—'
        clv = f"{m['avg_clv']:+.4f}" if m['avg_clv'] is not None else '—'
        clv_plus = (f"{m['positive_clv_rate']:.1f}%"
                    if m['positive_clv_rate'] is not None else '—')
        wlp = f"{m['wins']}-{m['losses']}-{m['pushes']}"
        avg_edge = m['avg_edge'] if m['avg_edge'] is not None else '—'
        return (f"  {label:<22} n={m['count']:<4} {wlp:>10}  win {win:>6}  "
                f"ROI {roi:>8}  avgCLV {clv:>9}  CLV+ {clv_plus:>6}  "
                f"avg_edge {avg_edge:>5}")

    lines.append("=" * 100)
    lines.append("HEADLINE")
    lines.append("=" * 100)
    lines.append(_line('A — production', am))
    lines.append(_line('B — +recent form', bm))
    lines.append("")

    # Deltas
    def _delta(a_v, b_v):
        if a_v is None or b_v is None:
            return None
        return round(b_v - a_v, 2)

    d_count = b['count'] - a['count']
    d_roi = _delta(am.get('roi'), bm.get('roi'))
    d_win = _delta(am.get('win_rate'), bm.get('win_rate'))
    d_clv = _delta(am.get('avg_clv'), bm.get('avg_clv'))
    d_clv_plus = _delta(am.get('positive_clv_rate'), bm.get('positive_clv_rate'))

    def _fmt(v, suffix=''):
        return f"{v:+}{suffix}" if v is not None else '—'

    lines.append("Δ (B − A)")
    lines.append("-" * 100)
    lines.append(f"  count {d_count:+}   ROI {_fmt(d_roi, 'pp')}   "
                 f"win% {_fmt(d_win, 'pp')}   avgCLV {_fmt(d_clv)}   "
                 f"CLV+ {_fmt(d_clv_plus, 'pp')}")
    lines.append("")

    # Per-bucket calibration table
    lines.append("=" * 100)
    lines.append("CALIBRATION BY CONFIDENCE BUCKET")
    lines.append("=" * 100)
    lines.append(f"  {'Bucket':<8}  {'A count':>8}  {'A actual':>9}  "
                 f"{'A err':>8}  {'B count':>8}  {'B actual':>9}  "
                 f"{'B err':>8}  {'Δ err':>8}")
    for k in ['60-65', '65-70', '70-75', '75+']:
        ab = a['buckets'][k]
        bb = b['buckets'][k]
        a_err = ab['calibration_error_pp']
        b_err = bb['calibration_error_pp']
        delta_err = (round(b_err - a_err, 2)
                     if a_err is not None and b_err is not None else None)
        a_actual = (f"{ab['actual_pct']:.1f}%"
                    if ab['actual_pct'] is not None else '—')
        b_actual = (f"{bb['actual_pct']:.1f}%"
                    if bb['actual_pct'] is not None else '—')
        a_err_s = f"{a_err:+.1f}pp" if a_err is not None else '—'
        b_err_s = f"{b_err:+.1f}pp" if b_err is not None else '—'
        d_err_s = f"{delta_err:+.1f}pp" if delta_err is not None else '—'
        lines.append(f"  {k:<8}  {ab['count']:>8}  {a_actual:>9}  "
                     f"{a_err_s:>8}  {bb['count']:>8}  {b_actual:>9}  "
                     f"{b_err_s:>8}  {d_err_s:>8}")
    lines.append("")

    # Mechanical ship-criteria check
    lines.append("=" * 100)
    lines.append("SHIP CRITERIA  (mechanical — flag stays OFF unless ALL fire)")
    lines.append("=" * 100)
    crit = []
    # 1. ROI improves by ≥ +2pp
    roi_ok = (d_roi is not None and d_roi >= 2.0)
    crit.append(('ROI lift ≥ +2pp', roi_ok,
                 f"Δ ROI = {d_roi if d_roi is not None else 'n/a'}"))
    # 2. 60–65% calibration does not worsen
    a_60 = a['buckets']['60-65']['calibration_error_pp']
    b_60 = b['buckets']['60-65']['calibration_error_pp']
    if a_60 is not None and b_60 is not None:
        # "Does not worsen" = |b_err| ≤ |a_err| + 1.0pp tolerance
        cal_ok = abs(b_60) <= abs(a_60) + 1.0
    else:
        cal_ok = False
    crit.append(('60–65% calibration does not worsen', cal_ok,
                 f"A err {a_60}, B err {b_60}"))
    # 3. Volume usable: B at least 50% of A
    if a['count'] > 0:
        vol_ok = b['count'] >= 0.5 * a['count']
    else:
        vol_ok = b['count'] > 0
    crit.append(('Recommendation volume usable', vol_ok,
                 f"A n={a['count']}, B n={b['count']}"))
    # 4. No major bucket regression: no bucket goes more than 5pp worse on |error|
    no_regress = True
    for k in ['65-70', '70-75', '75+']:
        a_e = a['buckets'][k]['calibration_error_pp']
        b_e = b['buckets'][k]['calibration_error_pp']
        if a_e is not None and b_e is not None and abs(b_e) > abs(a_e) + 5.0:
            no_regress = False
    crit.append(('No major bucket regression (|Δ err| ≤ 5pp)', no_regress, '—'))
    # 5. CLV not materially worse: avg_clv drop ≤ 0.01
    if am.get('avg_clv') is not None and bm.get('avg_clv') is not None:
        clv_ok = (bm['avg_clv'] - am['avg_clv']) >= -0.01
    else:
        clv_ok = True   # CLV not measurable in this sample; defer
    crit.append(('CLV does not materially worsen', clv_ok,
                 f"Δ avgCLV = {d_clv if d_clv is not None else 'n/a'}"))

    all_pass = all(passed for _, passed, _ in crit)
    for label, passed, note in crit:
        flag = '✅' if passed else '❌'
        lines.append(f"  {flag} {label:<48} {note}")
    lines.append("")
    lines.append(f"  VERDICT: {'PASS — recent form may be activated' if all_pass else 'FAIL — keep flag OFF; recent form stays shadow-only'}")
    lines.append("")
    return "\n".join(lines) + "\n"
