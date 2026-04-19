"""Placeholder College Baseball model service — real implementation in Phase 3.

Same shape as apps.mlb.services.model_service so views importing it work
from Phase 1 onward. Returns neutral probabilities + low confidence.
"""
from django.utils import timezone  # noqa: F401

HOUSE_MODEL_VERSION = 'v1-stub'


def compute_game_data(game, user=None):
    latest_odds = game.odds_snapshots.order_by('-captured_at').first()
    market_prob = latest_odds.market_home_win_prob if latest_odds else 0.5
    return {
        'game': game,
        'latest_odds': latest_odds,
        'market_prob': market_prob * 100,
        'house_prob': 50.0,
        'user_prob': None,
        'house_edge': 0.0,
        'user_edge': None,
        'delta': None,
        'confidence': 'low',
        'confidence_class': 'red',
        'is_favorite': False,
        'line_movement': None,
        'injuries': [],
        'model_version': HOUSE_MODEL_VERSION,
    }
