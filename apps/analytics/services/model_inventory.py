"""Model Input Inventory — staff diagnostic, MLB-only for Phase 1A.

Builds a fully-traced view of how the production MLB model arrives at a
single game's recommendation. The page exists because the team should
never have to read source to know what the model actually consumes — and
because shadow-mode comparisons (Phase 1B) need a stable, named view of
the same trace fields under both rating modes.

This module performs NO recommendation mutation. It re-runs the existing
model pipeline (sigmoid → blend → clamp) and the recommendation engine
(`get_recommendation`) and returns a structured dict for the template.
Re-running is intentional: the inventory is "what would happen right
now?", not "what was emitted historically?". Historical reconstruction
is the backtesting service's job (apps/core/services/backtesting_service.py).

Output shape — every key is always present so the template never has to
defensively `.get()`. Missing data is rendered as `None`, not absent.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field, asdict
from typing import Optional

from django.utils import timezone

from apps.core.services import elo_service, probability_calibration
from apps.core.services.recommendations import (
    EXTREME_DISAGREEMENT_GAP,
    HARD_MIN_PROBABILITY,
    HEAVY_FAVORITE_ODDS,
    LANE_HARD_GATES_EDGE_MIN,
    LANE_HARD_GATES_MAX_ABS_ODDS,
    LANE_HARD_GATES_PROBABILITY_MIN,
    MAX_ABS_ODDS_FOR_RECOMMENDED,
    MIN_EDGE,
    MIN_PROBABILITY_FOR_RECOMMENDED,
    STRONG_EDGE,
    ELITE_EDGE,
    get_recommendation,
)
from apps.core.utils.odds import (
    american_to_implied_prob,
    devig_two_way,
    format_american_signed,
)
from apps.mlb.models import Game as MLBGame
from apps.mlb.services import model_service as mlb_model_service


# ---------------------------------------------------------------------------
# Trace dataclasses — one per stage. Plain values, JSON-serializable for
# easy hand-off to templates (or future API consumers).


@dataclass
class TeamRow:
    """One side of the matchup. Both static and Elo ratings are surfaced
    so the operator can see what the model would use under each mode
    WITHOUT flipping settings."""
    name: str
    abbreviation: str
    static_rating: float                  # team.rating (FloatField, default 50.0)
    elo_rating: Optional[float]           # team.elo_rating (None until rebuild)
    elo_last_updated: Optional[str]       # ISO string or None
    elo_projected_legacy: Optional[float] # elo on legacy scale, or None
    rating_used_now: float                # whichever the active mode picks
    rating_mode_active: str               # 'elo' | 'static'
    is_default_rating: bool               # static rating == 50.0 (likely seed)
    season_record: Optional[str]          # "W-L" or None


@dataclass
class PitcherRow:
    """Starting pitcher for one side. Stats may be missing for newly-called-up
    pitchers; `is_default_rating` flags when rating == 50.0 (model treats
    the matchup as a wash on the pitcher axis)."""
    name: Optional[str]
    rating: Optional[float]               # 50.0 default until pitcher_stats_provider runs
    era: Optional[float]
    whip: Optional[float]
    record: Optional[str]                 # "W-L" or None
    is_default_rating: bool
    is_known: bool                        # both starters present? (game-level signal)


@dataclass
class ScoreBreakdown:
    """How `_score()` arrives at its number. Values are recomputed from the
    same constants the model uses, so any drift is immediately visible
    (the operator can compare to model_service.HOUSE_WEIGHTS)."""
    rating_weight: float                  # HOUSE_WEIGHTS['rating']
    pitcher_weight: float                 # HOUSE_WEIGHTS['pitcher']
    hfa_weight: float                     # HOUSE_WEIGHTS['hfa']
    rating_diff_legacy: float             # used rating, home - away
    rating_term: float                    # rating_diff * 0.35 * rating_weight
    pitcher_diff: float                   # home_pitcher.rating - away_pitcher.rating
    pitcher_term: float                   # diff * 0.65 * pitcher_weight
    hfa_term: float                       # HFA * hfa_weight (or 0 if neutral_site)
    hfa_used: bool                        # not neutral_site
    total_score: float                    # sum of the three terms


@dataclass
class CalibrationStages:
    """Probability journey from raw sigmoid to final.

    raw_home_prob       sigmoid(score / 25.0), clamped to [0.01, 0.99]
    market_home_prob    OddsSnapshot.market_home_win_prob, or None
    blend_weight        MARKET_BLEND_WEIGHT (capped at 0.40)
    blended_home_prob   raw * (1 - w) + market * w  (or raw when no market)
    clamp_min/max       PROB_MIN / PROB_MAX
    final_home_prob     blended after soft-clamp
    """
    raw_home_prob: float
    market_home_prob: Optional[float]
    blend_weight: float
    blended_home_prob: float
    clamp_min: float
    clamp_max: float
    final_home_prob: float


@dataclass
class EdgeMath:
    """De-vig + edge for both sides. The recommender picks the larger edge."""
    raw_home_implied: float               # vig included
    raw_away_implied: float
    fair_home_prob: float                 # de-vigged
    fair_away_prob: float
    home_edge: float                      # final_home - fair_home (decimal)
    away_edge: float                      # final_away - fair_away (decimal)
    pick_side: str                        # 'home' | 'away'
    pick_edge_pp: float                   # picked-side edge in percentage points
    moneyline_home: int
    moneyline_away: int


@dataclass
class GateOutcomes:
    """Why the recommendation engine landed on `status` / `tier` / `lane`.
    Each gate field is True when that gate fired (i.e., would block / flag).
    Reading the inventory page should make it instantly clear which gate
    was the binding constraint — a recommendation that falls into 'pass'
    with `low_probability=True` and everything else False is unambiguously
    explained."""
    # compute_status gates
    hard_min_probability_failed: bool
    longshot_failed: bool
    secondary_source_failed: bool
    recommended_probability_failed: bool
    min_edge_failed: bool
    heavy_favorite_juice_failed: bool
    extreme_disagreement_fired: bool
    # lane hard gates
    lane_probability_failed: bool
    lane_edge_failed: bool
    lane_odds_failed: bool
    lane_source_failed: bool
    # lane risk flags (mirrors the dict on the recommendation)
    risk_flags: dict = field(default_factory=dict)


@dataclass
class GameInventory:
    """The full trace. Templates render this directly."""
    sport: str                            # 'mlb' for now
    game_id: str
    game_label: str                       # "Away @ Home"
    first_pitch_iso: str
    neutral_site: bool
    home: TeamRow
    away: TeamRow
    home_pitcher: PitcherRow
    away_pitcher: PitcherRow
    score: ScoreBreakdown
    calibration: CalibrationStages
    edge: Optional[EdgeMath]              # None when no usable odds snapshot
    odds_source: Optional[str]            # 'odds_api' | 'espn' | ...
    odds_source_quality: Optional[str]
    odds_is_derived: Optional[bool]
    odds_captured_at_iso: Optional[str]
    # Recommendation outputs (all None when no odds → no recommendation)
    status: Optional[str]
    status_reason: Optional[str]
    tier: Optional[str]
    lane: Optional[str]
    risk_score: Optional[int]
    pick_label: Optional[str]
    pick_odds_display: Optional[str]
    confidence_score_pct: Optional[float]
    model_edge_pp: Optional[float]
    market_implied_pct: Optional[float]
    gates: Optional[GateOutcomes]

    def to_dict(self):
        """Template-safe dict. Nested dataclasses become dicts."""
        return asdict(self)


# ---------------------------------------------------------------------------
# Build helpers


_DEFAULT_TEAM_RATING = 50.0
_DEFAULT_PITCHER_RATING = 50.0


def _team_row(team) -> TeamRow:
    static = float(team.rating)
    elo = float(team.elo_rating) if team.elo_rating is not None else None
    elo_proj = elo_service.elo_to_legacy_scale(elo) if elo is not None else None
    rating_used = elo_service.team_rating_for_model(team)
    record = None
    if team.wins is not None and team.losses is not None:
        record = f"{team.wins}-{team.losses}"
    return TeamRow(
        name=team.name,
        abbreviation=team.abbreviation or '',
        static_rating=static,
        elo_rating=elo,
        elo_last_updated=(
            team.elo_last_updated.isoformat()
            if team.elo_last_updated is not None
            else None
        ),
        elo_projected_legacy=(
            round(elo_proj, 2) if elo_proj is not None else None
        ),
        rating_used_now=round(float(rating_used), 2),
        rating_mode_active='elo' if elo_service.is_dynamic_active() else 'static',
        is_default_rating=(static == _DEFAULT_TEAM_RATING),
        season_record=record,
    )


def _pitcher_row(pitcher, both_known: bool) -> PitcherRow:
    if pitcher is None:
        return PitcherRow(
            name=None, rating=None, era=None, whip=None,
            record=None, is_default_rating=False, is_known=both_known,
        )
    record = None
    if pitcher.wins is not None and pitcher.losses is not None:
        record = f"{pitcher.wins}-{pitcher.losses}"
    return PitcherRow(
        name=pitcher.name,
        rating=float(pitcher.rating),
        era=pitcher.era,
        whip=pitcher.whip,
        record=record,
        is_default_rating=(float(pitcher.rating) == _DEFAULT_PITCHER_RATING),
        is_known=both_known,
    )


def _score_breakdown(game, home_used: float, away_used: float) -> ScoreBreakdown:
    weights = mlb_model_service.HOUSE_WEIGHTS
    rating_diff = home_used - away_used
    rating_term = rating_diff * 0.35 * weights['rating']
    home_p_rating = (
        float(game.home_pitcher.rating) if game.home_pitcher else 0.0
    )
    away_p_rating = (
        float(game.away_pitcher.rating) if game.away_pitcher else 0.0
    )
    pitcher_diff = (
        (home_p_rating - away_p_rating)
        if (game.home_pitcher and game.away_pitcher) else 0.0
    )
    pitcher_term = pitcher_diff * 0.65 * weights['pitcher']
    hfa_used = not game.neutral_site
    hfa_term = (
        mlb_model_service.HFA * weights['hfa'] if hfa_used else 0.0
    )
    return ScoreBreakdown(
        rating_weight=weights['rating'],
        pitcher_weight=weights['pitcher'],
        hfa_weight=weights['hfa'],
        rating_diff_legacy=round(rating_diff, 3),
        rating_term=round(rating_term, 3),
        pitcher_diff=round(pitcher_diff, 3),
        pitcher_term=round(pitcher_term, 3),
        hfa_term=round(hfa_term, 3),
        hfa_used=hfa_used,
        total_score=round(rating_term + pitcher_term + hfa_term, 3),
    )


def _calibration_stages(score: float, market_home_prob: Optional[float]) -> CalibrationStages:
    """Re-applies the same math as mlb_model_service / probability_calibration.

    Kept independent (not by calling _sigmoid → finalize_win_prob) so the
    inventory page is robust if those internals are refactored — the
    diagnostic should still tell the truth about the *constants currently
    in effect*.
    """
    raw = 1.0 / (1.0 + math.exp(-score / 25.0))
    raw = max(0.01, min(0.99, raw))
    weight = probability_calibration.MARKET_BLEND_WEIGHT
    if market_home_prob is None:
        blended = raw
    else:
        blended = raw * (1.0 - weight) + market_home_prob * weight
    clamp_min = probability_calibration.PROB_MIN
    clamp_max = probability_calibration.PROB_MAX
    if blended > 0.5:
        final = max(clamp_min, min(clamp_max, blended))
    elif blended < 0.5:
        final = max(1.0 - clamp_max, min(1.0 - clamp_min, blended))
    else:
        final = blended
    return CalibrationStages(
        raw_home_prob=round(raw, 4),
        market_home_prob=(round(market_home_prob, 4) if market_home_prob is not None else None),
        blend_weight=weight,
        blended_home_prob=round(blended, 4),
        clamp_min=clamp_min,
        clamp_max=clamp_max,
        final_home_prob=round(final, 4),
    )


def _edge_math(odds, final_home_prob: float) -> Optional[EdgeMath]:
    if (
        odds is None
        or odds.moneyline_home is None
        or odds.moneyline_away is None
    ):
        return None
    raw_home = american_to_implied_prob(odds.moneyline_home)
    raw_away = american_to_implied_prob(odds.moneyline_away)
    fair_home, fair_away = devig_two_way(raw_home, raw_away)
    away_prob = 1.0 - final_home_prob
    home_edge = final_home_prob - fair_home
    away_edge = away_prob - fair_away
    if home_edge >= away_edge:
        side = 'home'
        edge = home_edge
    else:
        side = 'away'
        edge = away_edge
    return EdgeMath(
        raw_home_implied=round(raw_home, 4),
        raw_away_implied=round(raw_away, 4),
        fair_home_prob=round(fair_home, 4),
        fair_away_prob=round(fair_away, 4),
        home_edge=round(home_edge, 4),
        away_edge=round(away_edge, 4),
        pick_side=side,
        pick_edge_pp=round(edge * 100, 2),
        moneyline_home=odds.moneyline_home,
        moneyline_away=odds.moneyline_away,
    )


def _evaluate_gates(
    *,
    final_home_prob: float,
    edge: Optional[EdgeMath],
    odds_source_quality: Optional[str],
    odds_is_secondary: bool,
    market_home_prob: Optional[float],
    risk_flags: Optional[dict],
) -> Optional[GateOutcomes]:
    """Re-runs each gate in isolation so the inventory shows which fired.

    Mirrors `compute_status` and `_lane_hard_gates_pass` exactly. If those
    helpers ever change, this needs to track them — the test suite locks
    that down.
    """
    if edge is None:
        return None
    if edge.pick_side == 'home':
        prob = final_home_prob
    else:
        prob = 1.0 - final_home_prob
    pick_odds = (
        edge.moneyline_home if edge.pick_side == 'home'
        else edge.moneyline_away
    )
    pick_edge_pp = edge.pick_edge_pp
    pick_edge_decimal = pick_edge_pp / 100.0

    # compute_status gates (in priority order)
    hard_min_failed = prob < HARD_MIN_PROBABILITY
    longshot_failed = abs(int(pick_odds)) > MAX_ABS_ODDS_FOR_RECOMMENDED
    secondary_failed = odds_is_secondary
    rec_prob_failed = prob < MIN_PROBABILITY_FOR_RECOMMENDED
    min_edge_failed = pick_edge_pp < MIN_EDGE
    heavy_juice_failed = (
        pick_odds <= HEAVY_FAVORITE_ODDS and pick_edge_pp < STRONG_EDGE
    )

    # Extreme disagreement (post-blend; mirrors the recommender)
    extreme_disagreement = False
    if market_home_prob is not None:
        # Picked side's market vs picked side's final.
        pick_market = (
            market_home_prob if edge.pick_side == 'home'
            else 1.0 - market_home_prob
        )
        # Note: recommender de-vigs market for edge; for the "extreme
        # disagreement" check it uses the post-blend `confidence` vs the
        # de-vigged `market_prob` which already has vig stripped. Mirror
        # that here — fair_home/fair_away are the de-vigged probabilities.
        pick_fair = (
            edge.fair_home_prob if edge.pick_side == 'home'
            else edge.fair_away_prob
        )
        extreme_disagreement = abs(prob - pick_fair) > EXTREME_DISAGREEMENT_GAP

    # Lane hard gates
    lane_prob_failed = prob < LANE_HARD_GATES_PROBABILITY_MIN
    lane_edge_failed = pick_edge_decimal < LANE_HARD_GATES_EDGE_MIN
    lane_odds_failed = abs(int(pick_odds)) > LANE_HARD_GATES_MAX_ABS_ODDS
    lane_source_failed = (odds_source_quality or '') != 'primary'

    return GateOutcomes(
        hard_min_probability_failed=hard_min_failed,
        longshot_failed=longshot_failed,
        secondary_source_failed=secondary_failed,
        recommended_probability_failed=rec_prob_failed,
        min_edge_failed=min_edge_failed,
        heavy_favorite_juice_failed=heavy_juice_failed,
        extreme_disagreement_fired=extreme_disagreement,
        lane_probability_failed=lane_prob_failed,
        lane_edge_failed=lane_edge_failed,
        lane_odds_failed=lane_odds_failed,
        lane_source_failed=lane_source_failed,
        risk_flags=dict(risk_flags or {}),
    )


# ---------------------------------------------------------------------------
# Public entry point


def build_mlb_inventory(game: MLBGame) -> GameInventory:
    """Trace one MLB game through the live model + recommendation engine.

    NOTHING IS PERSISTED. This re-runs `compute_house_win_prob` and
    `get_recommendation` against the current DB state — same call paths
    the live UI uses. Safe to call repeatedly.
    """
    # 1. Inputs
    home = _team_row(game.home_team)
    away = _team_row(game.away_team)

    # Pitchers
    both_known = (game.home_pitcher is not None and game.away_pitcher is not None)
    home_pitcher = _pitcher_row(game.home_pitcher, both_known)
    away_pitcher = _pitcher_row(game.away_pitcher, both_known)

    # 2. Score breakdown
    score = _score_breakdown(game, home.rating_used_now, away.rating_used_now)

    # 3. Get the live odds snapshot the recommender will use
    odds = mlb_model_service._get_latest_odds(game)
    market_home = odds.market_home_win_prob if odds else None

    # 4. Calibration stages
    calibration = _calibration_stages(score.total_score, market_home)

    # 5. Edge
    edge = _edge_math(odds, calibration.final_home_prob)

    # 6. Run the full recommendation engine to get status/tier/lane/risk_flags
    rec = get_recommendation('mlb', game, user=None)

    # 7. Re-evaluate gates so the inventory shows which fired
    odds_is_secondary = False
    if odds is not None:
        from apps.core.services.odds_trust import get_odds_trust_tier
        odds_is_secondary = (get_odds_trust_tier(odds) == 'secondary')

    gates = _evaluate_gates(
        final_home_prob=calibration.final_home_prob,
        edge=edge,
        odds_source_quality=getattr(odds, 'source_quality', None),
        odds_is_secondary=odds_is_secondary,
        market_home_prob=market_home,
        risk_flags=getattr(rec, 'risk_flags', None) if rec else None,
    )

    return GameInventory(
        sport='mlb',
        game_id=str(game.id),
        game_label=f"{game.away_team.name} @ {game.home_team.name}",
        first_pitch_iso=game.first_pitch.isoformat(),
        neutral_site=bool(game.neutral_site),
        home=home,
        away=away,
        home_pitcher=home_pitcher,
        away_pitcher=away_pitcher,
        score=score,
        calibration=calibration,
        edge=edge,
        odds_source=(getattr(odds, 'odds_source', None) if odds else None),
        odds_source_quality=(getattr(odds, 'source_quality', None) if odds else None),
        odds_is_derived=(bool(getattr(odds, 'is_derived', False)) if odds else None),
        odds_captured_at_iso=(odds.captured_at.isoformat() if odds else None),
        status=(rec.status if rec else None),
        status_reason=(rec.status_reason if rec else None),
        tier=(rec.tier if rec else None),
        lane=(rec.lane if rec else None),
        risk_score=(rec.risk_score if rec else None),
        pick_label=(rec.pick if rec else None),
        pick_odds_display=(format_american_signed(rec.odds_american) if rec else None),
        confidence_score_pct=(float(rec.confidence_score) if rec else None),
        model_edge_pp=(float(rec.model_edge) if rec else None),
        market_implied_pct=(rec.market_implied_pct if rec else None),
        gates=gates,
    )


# ---------------------------------------------------------------------------
# List view helper


def todays_mlb_games(now=None, window_hours: int = 36):
    """Slate selector — returns games within +/- window_hours of `now`.

    Default 36-hour window covers "yesterday's late games still in DB" plus
    "tomorrow's afternoon slate" — enough for the operator to find any
    relevant game on a single page without paginating.
    """
    now = now or timezone.now()
    from datetime import timedelta
    window = timedelta(hours=window_hours)
    return list(
        MLBGame.objects
        .filter(first_pitch__gte=now - window, first_pitch__lte=now + window)
        .select_related('home_team', 'away_team', 'home_pitcher', 'away_pitcher')
        .order_by('first_pitch')
    )
