import math
from django.utils import timezone

HOUSE_MODEL_VERSION = 'v1'
HOUSE_WEIGHTS = {
    'rating': 1.0,
    'hfa': 1.0,
    'injury': 1.0,
    'recent_form': 1.0,
    'conference': 1.0,
}


def _get_latest_odds(game):
    return game.odds_snapshots.order_by('-captured_at').first()


def _get_injuries(game):
    return list(game.injuries.all())


def _injury_adjustment(injuries, team, weight=1.0):
    impact_values = {'low': 0.01, 'med': 0.03, 'high': 0.06}
    adjustment = 0.0
    for inj in injuries:
        if inj.team_id == team.id:
            adjustment += impact_values.get(inj.impact_level, 0)
    return adjustment * weight


def _compute_win_prob(game, injuries, weights):
    rating_diff = (game.home_team.rating - game.away_team.rating) * weights.get('rating', 1.0)
    hfa = 3.5 * weights.get('hfa', 1.0) if not game.neutral_site else 0.0
    home_inj = _injury_adjustment(injuries, game.home_team, weights.get('injury', 1.0))
    away_inj = _injury_adjustment(injuries, game.away_team, weights.get('injury', 1.0))
    injury_effect = (away_inj - home_inj) * 100
    score = rating_diff + hfa + injury_effect
    prob = 1.0 / (1.0 + math.exp(-score / 15.0))
    return max(0.01, min(0.99, prob))


def compute_house_win_prob(game, latest_odds=None, injuries=None, context=None):
    if injuries is None:
        injuries = _get_injuries(game)
    return _compute_win_prob(game, injuries, HOUSE_WEIGHTS)


def compute_user_win_prob(game, user_config, injuries=None):
    if injuries is None:
        injuries = _get_injuries(game)
    weights = {
        'rating': user_config.rating_weight,
        'hfa': user_config.hfa_weight,
        'injury': user_config.injury_weight,
        'recent_form': user_config.recent_form_weight,
        'conference': user_config.conference_weight,
    }
    return _compute_win_prob(game, injuries, weights)


def compute_data_confidence(game, latest_odds=None, injuries=None):
    if latest_odds is None:
        latest_odds = _get_latest_odds(game)
    if injuries is None:
        injuries = _get_injuries(game)

    if not latest_odds:
        return 'low'

    age = (timezone.now() - latest_odds.captured_at).total_seconds() / 3600.0
    has_injuries = len(injuries) > 0

    if age < 2 and has_injuries:
        return 'high'
    elif age < 12:
        return 'med'
    return 'low'


def compute_edges(market_prob, house_prob, user_prob=None):
    market_pct = market_prob * 100
    house_pct = house_prob * 100
    result = {
        'house_edge': round(house_pct - market_pct, 1),
    }
    if user_prob is not None:
        user_pct = user_prob * 100
        result['user_edge'] = round(user_pct - market_pct, 1)
        result['delta'] = round(user_pct - house_pct, 1)
    else:
        result['user_edge'] = None
        result['delta'] = None
    return result


def compute_game_data(game, user=None):
    latest_odds = _get_latest_odds(game)
    injuries = _get_injuries(game)

    market_prob = latest_odds.market_home_win_prob if latest_odds else 0.5
    house_prob = compute_house_win_prob(game, latest_odds, injuries)

    user_prob = None
    if user and user.is_authenticated:
        from apps.accounts.models import UserModelConfig
        config = UserModelConfig.get_or_create_for_user(user)
        user_prob = compute_user_win_prob(game, config, injuries)

    edges = compute_edges(market_prob, house_prob, user_prob)
    confidence = compute_data_confidence(game, latest_odds, injuries)
    confidence_map = {'high': 'green', 'med': 'yellow', 'low': 'red'}

    # Favorite team detection
    is_favorite = False
    if user and user.is_authenticated:
        try:
            profile = user.profile
            fav = profile.favorite_cbb_team_id
            if fav and (game.home_team_id == fav or game.away_team_id == fav):
                is_favorite = True
        except Exception:
            pass

    # Line movement detection
    line_movement = None
    if latest_odds:
        snapshots = list(game.odds_snapshots.order_by('-captured_at')[:2])
        if len(snapshots) == 2:
            prob_diff = (snapshots[0].market_home_win_prob - snapshots[1].market_home_win_prob) * 100
            if abs(prob_diff) > 0.5:
                line_movement = 'up' if prob_diff > 0 else 'down'

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
        'confidence_class': confidence_map.get(confidence, 'red'),
        'is_favorite': is_favorite,
        'line_movement': line_movement,
        'injuries': injuries,
        'model_version': HOUSE_MODEL_VERSION,
    }
