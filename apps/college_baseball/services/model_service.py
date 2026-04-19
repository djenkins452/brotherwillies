"""College Baseball house + user prediction model.

Mirrors apps.mlb.services.model_service; differs only in HFA (2.0 vs 2.5).
ESPN does not currently supply probable pitchers for college baseball,
so in practice `pitcher_diff` is zero for most games and confidence
degrades to 'low' — this is deliberate per the project direction
("do not fabricate pitcher assumptions").

If a future data source is added, this module needs NO changes — it
reads `game.home_pitcher` / `game.away_pitcher` like MLB.
"""
import math

from django.utils import timezone

HOUSE_MODEL_VERSION = 'v1'
HFA = 2.0

HOUSE_WEIGHTS = {
    'rating': 1.0,
    'pitcher': 1.0,
    'hfa': 1.0,
    'injury': 1.0,
}


def _get_latest_odds(game):
    return game.odds_snapshots.order_by('-captured_at').first()


def _injuries(game):
    return list(game.injuries.all())


def _pitcher_diff(game):
    hp = game.home_pitcher
    ap = game.away_pitcher
    if hp is None or ap is None:
        return 0.0, False
    return (hp.rating - ap.rating), True


def _score(game, weights):
    team_diff = (game.home_team.rating - game.away_team.rating) * 0.35 * weights['rating']
    pitcher_diff_raw, _ = _pitcher_diff(game)
    pitcher_diff = pitcher_diff_raw * 0.65 * weights['pitcher']
    hfa = HFA * weights['hfa'] if not game.neutral_site else 0.0
    return team_diff + pitcher_diff + hfa


def _sigmoid(x):
    return 1.0 / (1.0 + math.exp(-x / 15.0))


def compute_house_win_prob(game, latest_odds=None, injuries=None, context=None):
    return max(0.01, min(0.99, _sigmoid(_score(game, HOUSE_WEIGHTS))))


def compute_user_win_prob(game, user_config, injuries=None):
    weights = {
        'rating': user_config.rating_weight,
        'pitcher': getattr(user_config, 'pitcher_weight', 1.0),
        'hfa': user_config.hfa_weight,
        'injury': user_config.injury_weight,
    }
    return max(0.01, min(0.99, _sigmoid(_score(game, weights))))


def compute_data_confidence(game, latest_odds=None, injuries=None):
    if latest_odds is None:
        latest_odds = _get_latest_odds(game)
    if not latest_odds:
        return 'low'
    _, both_pitchers = _pitcher_diff(game)
    age_h = (timezone.now() - latest_odds.captured_at).total_seconds() / 3600.0
    if not both_pitchers:
        return 'low'
    if age_h < 2:
        return 'high'
    if age_h < 12:
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
    latest_odds = _get_latest_odds(game)
    injuries = _injuries(game)

    market_prob = latest_odds.market_home_win_prob if latest_odds else 0.5
    house_prob = compute_house_win_prob(game, latest_odds, injuries)

    user_prob = None
    if user and user.is_authenticated:
        from apps.accounts.models import UserModelConfig
        config = UserModelConfig.get_or_create_for_user(user)
        user_prob = compute_user_win_prob(game, config, injuries)

    edges = compute_edges(market_prob, house_prob, user_prob)
    confidence = compute_data_confidence(game, latest_odds, injuries)

    line_movement = None
    if latest_odds:
        snaps = list(game.odds_snapshots.order_by('-captured_at')[:2])
        if len(snaps) == 2:
            diff = (snaps[0].market_home_win_prob - snaps[1].market_home_win_prob) * 100
            if abs(diff) > 0.5:
                line_movement = 'up' if diff > 0 else 'down'

    return {
        'game': game,
        'latest_odds': latest_odds,
        'market_prob': market_prob * 100,
        'house_prob': house_prob * 100,
        'user_prob': (user_prob * 100) if user_prob else None,
        'house_edge': edges['house_edge'],
        'user_edge': edges['user_edge'],
        'delta': edges['delta'],
        'confidence': confidence,
        'confidence_class': {'high': 'green', 'med': 'yellow', 'low': 'red'}[confidence],
        'is_favorite': False,
        'line_movement': line_movement,
        'injuries': injuries,
        'model_version': HOUSE_MODEL_VERSION,
    }
