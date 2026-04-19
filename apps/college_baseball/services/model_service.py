"""Placeholder College Baseball model service — real implementation in Phase 3.

See apps.mlb.services.model_service docstring for design notes.
"""
from django.utils import timezone

HOUSE_MODEL_VERSION = 'v1-stub'


def compute_house_win_prob(game, latest_odds=None, injuries=None, context=None):
    return 0.5


def compute_user_win_prob(game, user_config, injuries=None):
    return 0.5


def compute_data_confidence(game, latest_odds=None, injuries=None):
    if latest_odds is None:
        latest_odds = game.odds_snapshots.order_by('-captured_at').first()
    if not latest_odds:
        return 'low'
    age = (timezone.now() - latest_odds.captured_at).total_seconds() / 3600.0
    if age < 6:
        return 'med'
    return 'low'


def compute_edges(market_prob, house_prob, user_prob=None):
    result = {
        'house_edge': round((house_prob - market_prob) * 100, 1),
        'user_edge': None,
        'delta': None,
    }
    if user_prob is not None:
        result['user_edge'] = round((user_prob - market_prob) * 100, 1)
        result['delta'] = round((user_prob - house_prob) * 100, 1)
    return result


def compute_game_data(game, user=None):
    latest_odds = game.odds_snapshots.order_by('-captured_at').first()
    market_prob = latest_odds.market_home_win_prob if latest_odds else 0.5
    house_prob = 0.5
    confidence = compute_data_confidence(game, latest_odds)
    edges = compute_edges(market_prob, house_prob)
    return {
        'game': game,
        'latest_odds': latest_odds,
        'market_prob': market_prob * 100,
        'house_prob': house_prob * 100,
        'user_prob': None,
        'house_edge': edges['house_edge'],
        'user_edge': edges['user_edge'],
        'delta': edges['delta'],
        'confidence': confidence,
        'confidence_class': {'high': 'green', 'med': 'yellow', 'low': 'red'}[confidence],
        'is_favorite': False,
        'line_movement': None,
        'injuries': [],
        'model_version': HOUSE_MODEL_VERSION,
    }
