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


# Tier thresholds based on confidence_score (0-100).
# Per-slate guardrail caps elite at MAX_ELITE_PER_SLATE — any extras are
# downgraded to strong so the "elite" signal stays rare and meaningful.
ELITE_THRESHOLD = 80.0
STRONG_THRESHOLD = 65.0
MAX_ELITE_PER_SLATE = 2

_TIER_LABELS = {
    'elite': '🔥 High Confidence',
    'strong': 'Strong Edge',
    'standard': 'Model Pick',
}

# Lower = higher priority in sort keys.
TIER_ORDER = {'elite': 0, 'strong': 1, 'standard': 2, None: 3}


def _raw_tier(confidence_score: float) -> str:
    """Classify a single recommendation by confidence alone — ignores slate guardrail."""
    if confidence_score >= ELITE_THRESHOLD:
        return 'elite'
    if confidence_score >= STRONG_THRESHOLD:
        return 'strong'
    return 'standard'


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

    @property
    def tier_label(self) -> str:
        return _TIER_LABELS.get(self.tier, _TIER_LABELS['standard'])

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

    Mutates tier on each input in place and returns the same list for convenience.

    Rules:
      - Raw tier from confidence (_raw_tier).
      - If more than MAX_ELITE_PER_SLATE qualify as elite, only the top N by
        (confidence_score desc, model_edge desc) keep elite; the rest drop to strong.
    """
    # First pass: every rec gets its raw tier.
    for rec in recommendations:
        rec.tier = _raw_tier(rec.confidence_score)

    # Guardrail: cap elites.
    elites = [r for r in recommendations if r.tier == 'elite']
    if len(elites) > MAX_ELITE_PER_SLATE:
        elites.sort(
            key=lambda r: (r.confidence_score, r.model_edge),
            reverse=True,
        )
        for demoted in elites[MAX_ELITE_PER_SLATE:]:
            demoted.tier = 'strong'

    return recommendations


def _implied_prob(american: int) -> float:
    """American odds → implied probability in [0, 1]."""
    if american > 0:
        return 100.0 / (american + 100.0)
    return abs(american) / (abs(american) + 100.0)


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

    home_edge = home_prob - _implied_prob(odds.moneyline_home)
    away_edge = away_prob - _implied_prob(odds.moneyline_away)

    if home_edge >= away_edge:
        pick_name = game.home_team.name
        pick_odds = odds.moneyline_home
        edge = home_edge
        confidence = home_prob
    else:
        pick_name = game.away_team.name
        pick_odds = odds.moneyline_away
        edge = away_edge
        confidence = away_prob

    return Recommendation(
        sport='',
        game=game,
        bet_type='moneyline',
        pick=pick_name,
        line=_format_american(pick_odds),
        odds_american=pick_odds,
        confidence_score=round(confidence * 100, 1),
        model_edge=round(edge * 100, 1),
        model_source=model_source,
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
        **{game_fk_field: game},
    )
