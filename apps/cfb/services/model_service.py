"""
Model service layer for CFB probability calculations.
House model is fixed and versioned. User model uses configurable weights.
"""
from django.utils import timezone

HOUSE_MODEL_VERSION = 'v1'

# House model default weights (fixed)
HOUSE_WEIGHTS = {
    'rating': 1.0,
    'hfa': 1.0,
    'injury': 1.0,
    'recent_form': 1.0,
    'conference': 1.0,
}


def _get_latest_odds(game):
    """Get the most recent OddsSnapshot for a game."""
    return game.odds_snapshots.first()  # ordered by -captured_at


def _get_injuries(game):
    """Get injury impacts for a game."""
    return list(game.injuries.all())


def _injury_adjustment(injuries, team, weight=1.0):
    """Calculate injury adjustment for a team."""
    adj = 0.0
    impact_values = {'low': 0.01, 'med': 0.03, 'high': 0.06}
    for inj in injuries:
        if inj.team_id == team.id:
            adj += impact_values.get(inj.impact_level, 0.0)
    return adj * weight


def _compute_win_prob(game, injuries, weights):
    """
    Core probability computation using team ratings and adjustments.
    Returns home win probability (0.0 to 1.0).
    """
    home = game.home_team
    away = game.away_team

    rating_diff = (home.rating - away.rating) * weights.get('rating', 1.0)

    # Home field advantage
    hfa = 3.0 * weights.get('hfa', 1.0) if not game.neutral_site else 0.0

    # Injury adjustments (negative for home injuries, positive for away injuries)
    home_injury = _injury_adjustment(injuries, home, weights.get('injury', 1.0))
    away_injury = _injury_adjustment(injuries, away, weights.get('injury', 1.0))
    injury_effect = (away_injury - home_injury) * 100  # scale to rating units

    # Combined score
    score = rating_diff + hfa + injury_effect

    # Convert to probability using logistic function
    import math
    prob = 1.0 / (1.0 + math.exp(-score / 15.0))

    # Clamp
    return max(0.01, min(0.99, prob))


def compute_house_win_prob(game, latest_odds=None, injuries=None, context=None):
    """Compute house model probability for home team winning."""
    if injuries is None:
        injuries = _get_injuries(game)
    return _compute_win_prob(game, injuries, HOUSE_WEIGHTS)


def compute_user_win_prob(game, user_config, injuries=None):
    """Compute user model probability using their weight configuration."""
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
    """Determine data confidence level based on freshness and completeness."""
    if latest_odds is None:
        latest_odds = _get_latest_odds(game)

    if latest_odds is None:
        return 'low'

    now = timezone.now()
    age = (now - latest_odds.captured_at).total_seconds() / 3600  # hours

    has_injuries = game.injuries.exists()

    if age < 2 and has_injuries:
        return 'high'
    elif age < 12:
        return 'med'
    else:
        return 'low'


def compute_edges(market_prob, house_prob, user_prob=None):
    """Calculate edge values as percentages."""
    market_pct = market_prob * 100
    house_pct = house_prob * 100
    house_edge = house_pct - market_pct
    result = {
        'house_edge': round(house_edge, 1),
        'user_edge': None,
        'delta': None,
    }
    if user_prob is not None:
        user_pct = user_prob * 100
        result['user_edge'] = round(user_pct - market_pct, 1)
        result['delta'] = round(user_pct - house_pct, 1)
    return result


def compute_game_data(game, user=None):
    """
    Compute full game analysis data.
    Returns a dict with all computed values for template rendering.
    """
    latest_odds = _get_latest_odds(game)
    injuries = _get_injuries(game)

    market_prob = latest_odds.market_home_win_prob if latest_odds else 0.5
    house_prob = compute_house_win_prob(game, latest_odds, injuries)
    confidence = compute_data_confidence(game, latest_odds, injuries)

    user_prob = None
    user_config = None
    if user and user.is_authenticated:
        from apps.accounts.models import UserModelConfig
        user_config = UserModelConfig.get_or_create_for_user(user)
        user_prob = compute_user_win_prob(game, user_config, injuries)

    edges = compute_edges(market_prob, house_prob, user_prob)

    # Check if favorite team
    is_favorite = False
    if user and user.is_authenticated:
        try:
            profile = user.profile
            fav = profile.favorite_team_id
            if fav and (game.home_team_id == fav or game.away_team_id == fav):
                is_favorite = True
        except Exception:
            pass

    # Line movement
    line_movement = None
    snapshots = list(game.odds_snapshots.all()[:2])
    if len(snapshots) >= 2:
        newer = snapshots[0].market_home_win_prob
        older = snapshots[1].market_home_win_prob
        diff = newer - older
        if abs(diff) > 0.005:
            line_movement = 'up' if diff > 0 else 'down'

    confidence_class = {'high': 'green', 'med': 'yellow', 'low': 'red'}.get(confidence, 'yellow')

    return {
        'game': game,
        'latest_odds': latest_odds,
        'market_prob': market_prob * 100,
        'house_prob': house_prob * 100,
        'user_prob': (user_prob * 100) if user_prob is not None else None,
        'house_edge': edges['house_edge'],
        'user_edge': edges['user_edge'],
        'delta': edges['delta'],
        'confidence': confidence,
        'confidence_class': confidence_class,
        'is_favorite': is_favorite,
        'line_movement': line_movement,
        'injuries': injuries,
        'model_version': HOUSE_MODEL_VERSION,
    }
