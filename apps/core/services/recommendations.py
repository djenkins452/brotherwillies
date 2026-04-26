"""Decision layer — turns model probabilities into a single actionable pick per game.

Reuses each sport's existing `compute_game_data` (via SPORT_REGISTRY) and the latest
OddsSnapshot. No new modeling happens here.

v1 emits moneyline picks only: that is the market where the existing sport model
services actually produce a comparable win probability. Spread picks require a
margin-of-victory model; total picks require a runs/points projection. Both are
future extensions — stubbed below with `None` rather than fabricated.
"""
from dataclasses import dataclass, asdict, field
from decimal import Decimal
from typing import List, Optional

from apps.core.sport_registry import SPORT_REGISTRY


# Decision thresholds — units are percentage points (pp) to match the stored
# `model_edge` scale (e.g. 5.2 means 5.2 pp above market). Mixing units here
# with decimal probabilities would silently misclassify every recommendation.
MIN_EDGE = 4.0      # 4 pp — below this, the pick is not recommended at all
STRONG_EDGE = 6.0   # 6 pp — edge required to overcome heavy-favorite juice
ELITE_EDGE = 8.0    # 8 pp — strong enough to always recommend regardless of juice

# Heavy-favorite juice threshold. American odds at or below this are "expensive"
# enough that the model needs a strong edge to clear them. -150 ≈ 60% implied.
HEAVY_FAVORITE_ODDS = -150

# Per-slate guardrail caps elite at MAX_ELITE_PER_SLATE — any extras are
# downgraded to strong so the "elite" signal stays rare and meaningful.
MAX_ELITE_PER_SLATE = 2

_TIER_LABELS = {
    'elite': '🔥 High Confidence',
    'strong': 'Strong Edge',
    'standard': 'Model Pick',
}

STATUS_RECOMMENDED = 'recommended'
STATUS_NOT_RECOMMENDED = 'not_recommended'

_STATUS_LABELS = {
    STATUS_RECOMMENDED: 'Recommended',
    STATUS_NOT_RECOMMENDED: 'Not Recommended',
}

# Action-oriented CTA copy — what the user should actually do. Keeps the
# passive "Model Pick" language out of the UI while still being honest when
# the system does not recommend the bet (then it's a "Model Lean" — here's
# what the model would pick, but the decision rules say don't bet).
_STATUS_ACTION_LABELS = {
    STATUS_RECOMMENDED: 'Recommended Bet',
    STATUS_NOT_RECOMMENDED: 'Model Lean',
}

_STATUS_REASON_LABELS = {
    'low_edge': 'Low Edge',
    'high_juice': 'High Juice Risk',
    'marginal': 'Marginal',
    '': '',
}

# Lower = higher priority in sort keys.
TIER_ORDER = {'elite': 0, 'strong': 1, 'standard': 2, None: 3}


def _raw_tier(model_edge: float) -> str:
    """Classify a recommendation by its edge vs market (in pp), not by confidence.

    Rationale: the product reality is "strength of the opportunity". A 92%
    model confidence against -900 market odds is not a strong edge — the
    market already priced it in. Edge-based tiering avoids that trap.
    """
    if model_edge is None:
        return 'standard'
    if model_edge >= ELITE_EDGE:
        return 'elite'
    if model_edge >= STRONG_EDGE:
        return 'strong'
    return 'standard'


def compute_status(model_edge: float, odds_american: int):
    """Apply decision rules to determine recommended vs not_recommended.

    Evaluation order lets the elite-edge override short-circuit heavy-juice
    skepticism — a model edge of 10pp against -200 odds is still worth flagging.

    Returns (status, status_reason). `status_reason` is '' for recommended picks.
    """
    # Rule 3 first — elite edge overrides juice risk
    if model_edge is not None and model_edge >= ELITE_EDGE:
        return STATUS_RECOMMENDED, ''
    # Rule 1 — minimum edge to bother
    if model_edge is None or model_edge < MIN_EDGE:
        return STATUS_NOT_RECOMMENDED, 'low_edge'
    # Rule 2 — heavy-favorite juice gate
    if odds_american is not None and odds_american <= HEAVY_FAVORITE_ODDS and model_edge < STRONG_EDGE:
        return STATUS_NOT_RECOMMENDED, 'high_juice'
    # Rule 4 — default
    return STATUS_RECOMMENDED, ''


def top_play_reasons(model_edge, confidence_score, tier, status) -> list:
    """Bulleted explanation for why a recommendation qualifies as a top play.

    Used on the elite-tier banner so users see the *justification* — not just
    the conclusion. Pure presentation: derives from already-computed fields,
    never re-runs the decision rules.
    """
    bullets = []
    if model_edge is not None:
        try:
            bullets.append(f"+{float(model_edge):.1f}pp model edge over fair-value market")
        except (TypeError, ValueError):
            pass
    if model_edge is not None and float(model_edge) >= ELITE_EDGE:
        bullets.append("Market mispricing detected (≥8pp edge — elite threshold)")
    if tier == 'elite':
        bullets.append("Top-of-slate pick (capped at 2 elites per cycle)")
    if confidence_score is not None:
        try:
            cs = float(confidence_score)
            if cs >= 70:
                bullets.append(f"High projected win probability ({cs:.0f}%)")
        except (TypeError, ValueError):
            pass
    if status == STATUS_RECOMMENDED:
        bullets.append("Cleared decision rules: edge threshold + juice gate")
    return bullets


def status_label(status: str) -> str:
    return _STATUS_LABELS.get(status, '')


def action_label(status: str) -> str:
    """Actionable CTA copy — 'Recommended Bet' vs 'Model Lean'."""
    return _STATUS_ACTION_LABELS.get(status, _STATUS_ACTION_LABELS[STATUS_RECOMMENDED])


def status_reason_label(status_reason: str) -> str:
    return _STATUS_REASON_LABELS.get(status_reason or '', '')


@dataclass
class Recommendation:
    sport: str
    game: object
    bet_type: str
    pick: str
    line: str
    odds_american: int
    confidence_score: float
    model_edge: float
    model_source: str
    tier: str = 'standard'
    status: str = STATUS_RECOMMENDED
    status_reason: str = ''
    # Market Movement integration. None / False when no signal is available
    # — UI and analytics treat that case as "no signal" without breaking.
    movement_class: Optional[str] = None
    movement_score: Optional[float] = None
    movement_supports_pick: bool = False
    market_warning: bool = False

    @property
    def tier_label(self) -> str:
        return _TIER_LABELS.get(self.tier, _TIER_LABELS['standard'])

    @property
    def status_label(self) -> str:
        return _STATUS_LABELS.get(self.status, '')

    @property
    def action_label(self) -> str:
        return _STATUS_ACTION_LABELS.get(self.status, _STATUS_ACTION_LABELS[STATUS_RECOMMENDED])

    @property
    def status_reason_label(self) -> str:
        return _STATUS_REASON_LABELS.get(self.status_reason or '', '')

    @property
    def top_play_reasons(self) -> list:
        return top_play_reasons(self.model_edge, self.confidence_score, self.tier, self.status)

    @property
    def is_recommended(self) -> bool:
        return self.status == STATUS_RECOMMENDED

    @property
    def market_implied_probability(self) -> Optional[float]:
        """Market's implied probability (0-100) for the picked side, from odds_american.
        Returns None if odds are missing or zero (defensive — shouldn't normally happen)."""
        if not self.odds_american:
            return None
        return _implied_prob(self.odds_american) * 100.0

    @property
    def explanation_rows(self):
        """Deterministic, scannable rows for the elite card explanation block.
        Skips any metric that can't be computed — never fabricates a number."""
        return _build_explanation_rows(self.confidence_score, self.odds_american, self.model_edge)

    # --- Market movement properties (parallel BettingRecommendation) ----
    @property
    def confidence_nudge_pp(self) -> float:
        from apps.core.services.odds_movement import confidence_nudge_pp
        return confidence_nudge_pp(self.movement_class, self.movement_supports_pick)

    @property
    def displayed_confidence(self):
        from apps.core.services.odds_movement import displayed_confidence
        return displayed_confidence(self.confidence_score, self.movement_class, self.movement_supports_pick)

    @property
    def market_movement_chip(self):
        from apps.core.services.odds_movement import chip_label_for
        return chip_label_for(self.movement_class, self.movement_supports_pick, self.market_warning)

    def to_context(self):
        """Template-safe dict. Keeps the game object alive for related-field access."""
        d = asdict(self)
        d['game'] = self.game
        d['tier_label'] = self.tier_label
        d['explanation_rows'] = self.explanation_rows
        return d


def _build_explanation_rows(confidence_score, odds_american, model_edge):
    """Shared builder used by both the dataclass and the persisted DB model."""
    rows = []
    if confidence_score is not None:
        rows.append({'label': 'Win Probability', 'value': f'{float(confidence_score):.0f}%'})
    if odds_american:
        implied = _implied_prob(int(odds_american)) * 100.0
        rows.append({'label': 'Market Implied', 'value': f'{implied:.0f}%'})
    if model_edge is not None:
        sign = '+' if float(model_edge) > 0 else ''
        rows.append({'label': 'Edge', 'value': f'{sign}{float(model_edge):.1f}%'})
    return rows


def assign_tiers(recommendations: List['Recommendation']) -> List['Recommendation']:
    """Classify each recommendation and enforce the slate-level elite cap.

    Mutates tier in place and returns the same list for convenience.

    Rules (post-edge migration):
      - Raw tier from model_edge (_raw_tier). Edge in pp matches stored scale.
      - If more than MAX_ELITE_PER_SLATE qualify as elite, only the top N by
        (model_edge desc, confidence_score desc) keep elite; the rest drop to
        strong. Edge is the primary ranking signal per the selection spec.
    """
    for rec in recommendations:
        rec.tier = _raw_tier(rec.model_edge)

    elites = [r for r in recommendations if r.tier == 'elite']
    if len(elites) > MAX_ELITE_PER_SLATE:
        elites.sort(
            key=lambda r: (r.model_edge, r.confidence_score),
            reverse=True,
        )
        for demoted in elites[MAX_ELITE_PER_SLATE:]:
            demoted.tier = 'strong'

    return recommendations


def _implied_prob(american: int) -> float:
    """American odds → implied probability in [0, 1].

    Kept as a thin wrapper so existing callers/tests don't break. The
    canonical implementation now lives in apps.core.utils.odds.
    """
    from apps.core.utils.odds import american_to_implied_prob
    return american_to_implied_prob(american)


def _format_american(odds: int) -> str:
    return f"+{odds}" if odds > 0 else str(odds)


def _moneyline_candidate(game, data, model_source: str) -> Optional[Recommendation]:
    odds = data.get('latest_odds')
    if not odds or odds.moneyline_home is None or odds.moneyline_away is None:
        return None

    if model_source == 'user' and data.get('user_prob') is not None:
        home_prob = data['user_prob'] / 100.0
    else:
        home_prob = data['house_prob'] / 100.0
        model_source = 'house'
    away_prob = 1.0 - home_prob

    # De-vig the market BEFORE computing edge. The raw implied probabilities
    # sum to > 1.0 (the overround); using them directly inflates apparent
    # edge by the vig amount. Fair probs normalize to sum = 1 and give the
    # true EV-relevant baseline for the bet.
    from apps.core.utils.odds import devig_two_way
    raw_home_implied = _implied_prob(odds.moneyline_home)
    raw_away_implied = _implied_prob(odds.moneyline_away)
    fair_home, fair_away = devig_two_way(raw_home_implied, raw_away_implied)

    home_edge = home_prob - fair_home
    away_edge = away_prob - fair_away

    if home_edge >= away_edge:
        pick_name = game.home_team.name
        pick_odds = odds.moneyline_home
        edge = home_edge
        confidence = home_prob
        pick_side = 'home'
    else:
        pick_name = game.away_team.name
        pick_odds = odds.moneyline_away
        edge = away_edge
        confidence = away_prob
        pick_side = 'away'

    model_edge_pp = round(edge * 100, 1)
    status, reason = compute_status(model_edge_pp, pick_odds)

    # Movement signal — purely additive. The model still drives the pick,
    # the tier, and the recommendation status. Movement only affects the
    # *displayed* confidence (bounded nudge) and an optional warning chip.
    # type(odds) gives us the per-sport OddsSnapshot model class without
    # needing to thread the model through SPORT_REGISTRY.
    from apps.core.services.odds_movement import movement_signal_for_pick
    sig = movement_signal_for_pick(type(odds), game, pick_side)

    return Recommendation(
        sport='',
        game=game,
        bet_type='moneyline',
        pick=pick_name,
        line=_format_american(pick_odds),
        odds_american=pick_odds,
        confidence_score=round(confidence * 100, 1),
        model_edge=model_edge_pp,
        model_source=model_source,
        tier=_raw_tier(model_edge_pp),
        status=status,
        status_reason=reason,
        movement_class=sig['movement_class'],
        movement_score=sig['movement_score'],
        movement_supports_pick=sig['supports_pick'],
        market_warning=sig['market_warning'],
    )


def get_recommendation(sport: str, game, user=None) -> Optional[Recommendation]:
    """Compute the single highest-edge pick for a game. Returns None if no odds exist.

    Preference order for model source:
      - If the user has a configured model AND its prob differs meaningfully from house,
        use the user model. Otherwise fall back to the house model.
    """
    entry = SPORT_REGISTRY.get(sport)
    if not entry:
        return None

    data = entry['compute_fn'](game, user)

    prefer_user = (
        user is not None
        and getattr(user, 'is_authenticated', False)
        and data.get('user_prob') is not None
    )
    source = 'user' if prefer_user else 'house'

    candidates = [
        _moneyline_candidate(game, data, source),
        # spread/total candidates require margin/runs models — not implemented in v1
    ]
    candidates = [c for c in candidates if c is not None]
    if not candidates:
        return None

    best = max(candidates, key=lambda c: c.model_edge)
    best.sport = sport
    return best


def persist_recommendation(sport: str, game, user=None):
    """Compute and save a BettingRecommendation row. Returns the saved model or None."""
    from apps.core.models import BettingRecommendation

    rec = get_recommendation(sport, game, user)
    if rec is None:
        return None

    game_fk_field = f"{sport}_game"
    return BettingRecommendation.objects.create(
        sport=sport,
        bet_type=rec.bet_type,
        pick=rec.pick,
        line=rec.line,
        odds_american=rec.odds_american,
        confidence_score=Decimal(str(rec.confidence_score)),
        model_edge=Decimal(str(rec.model_edge)),
        model_source=rec.model_source,
        status=rec.status,
        status_reason=rec.status_reason,
        movement_class=rec.movement_class or '',
        movement_score=rec.movement_score,
        movement_supports_pick=rec.movement_supports_pick,
        market_warning=rec.market_warning,
        **{game_fk_field: game},
    )
