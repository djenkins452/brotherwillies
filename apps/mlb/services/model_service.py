"""MLB house + user prediction model.

Score formula (home team perspective):
    score =  weights['rating']   * 0.35 * (home.rating - away.rating)
           + weights['pitcher']  * 0.65 * (home_pitcher.rating - away_pitcher.rating)
           + weights['hfa']      * HFA  if not neutral_site else 0

    prob = sigmoid(score / 15.0), clamped to [0.01, 0.99]

Pitching is the primary driver (0.65 coefficient vs 0.35 for team rating)
per product direction. If either starting pitcher is unknown we set
pitcher_diff = 0 and downgrade confidence to 'low' — we never fabricate
a substitute pitcher rating.

HFA is smaller than basketball/football because MLB's home edge is ~54%
historical, vs ~60%+ for college sports.
"""
import math

from django.utils import timezone

HOUSE_MODEL_VERSION = 'v1'
HFA = 2.5

HOUSE_WEIGHTS = {
    'rating': 1.0,
    'pitcher': 1.0,
    'hfa': 1.0,
    'injury': 1.0,
}


def _get_latest_odds(game):
    """Pick the most trustworthy fresh snapshot, with a graceful fall-through.

    Trust ladder (highest priority first):
        1. Primary (odds_api) within FRESH_ODDS_MAX_AGE_MINUTES.
        2. Non-derived secondary (espn, is_derived=False) within the same window.
        3. Most recent snapshot of any source (legacy fallback — used when the
           game has nothing fresh and we'd rather show stale data with the
           confidence indicator turned down than display "no odds").

    Why this matters: the previous one-liner used `-captured_at` only, so a
    30-second-old ESPN row would shadow a 10-minute-old paid Odds API row.
    The paid feed is authoritative — we should prefer it whenever it's fresh,
    not just when it happens to be the most recent insert.

    Derived rows (synthetic moneylines from symmetric inversion) are still
    reachable via the tier-3 fallback when nothing better exists, but the
    recommendation engine and the trust-badge layer already know how to
    flag/suppress them — we don't gate them out here so the UI can choose.
    """
    from datetime import timedelta
    from django.conf import settings

    fresh_window = getattr(settings, 'FRESH_ODDS_MAX_AGE_MINUTES', 180)
    fresh_cutoff = timezone.now() - timedelta(minutes=fresh_window)

    base = game.odds_snapshots.order_by('-captured_at')

    primary = base.filter(odds_source='odds_api', captured_at__gte=fresh_cutoff).first()
    if primary:
        return primary

    secondary = base.filter(
        odds_source='espn',
        is_derived=False,
        captured_at__gte=fresh_cutoff,
    ).first()
    if secondary:
        return secondary

    return base.first()


def _injuries(game):
    return list(game.injuries.all())


def _pitcher_diff(game):
    """Home - away pitcher rating. Returns (diff, both_known_bool)."""
    hp = game.home_pitcher
    ap = game.away_pitcher
    if hp is None or ap is None:
        return 0.0, False
    return (hp.rating - ap.rating), True


def _score(game, weights):
    team_diff = (game.home_team.rating - game.away_team.rating) * 0.35 * weights['rating']
    pitcher_diff_raw, _both_known = _pitcher_diff(game)
    pitcher_diff = pitcher_diff_raw * 0.65 * weights['pitcher']
    hfa = HFA * weights['hfa'] if not game.neutral_site else 0.0
    return team_diff + pitcher_diff + hfa


def _sigmoid(x):
    return 1.0 / (1.0 + math.exp(-x / 15.0))


def compute_house_win_prob(game, latest_odds=None, injuries=None, context=None):
    prob = _sigmoid(_score(game, HOUSE_WEIGHTS))
    return max(0.01, min(0.99, prob))


def compute_user_win_prob(game, user_config, injuries=None):
    weights = {
        'rating': user_config.rating_weight,
        'pitcher': getattr(user_config, 'pitcher_weight', 1.0),
        'hfa': user_config.hfa_weight,
        'injury': user_config.injury_weight,
    }
    prob = _sigmoid(_score(game, weights))
    return max(0.01, min(0.99, prob))


def compute_data_confidence(game, latest_odds=None, injuries=None):
    """Confidence weighed toward pitcher availability.

    - Missing starting pitcher (either side) -> always 'low'.
    - No odds snapshot -> always 'low'.
    - Odds within 2h AND both pitchers known -> 'high'.
    - Odds within 12h -> 'med'.
    - Otherwise 'low'.
    """
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

    # Line movement detection (same convention as CFB/CBB)
    line_movement = None
    if latest_odds:
        snaps = list(game.odds_snapshots.order_by('-captured_at')[:2])
        if len(snaps) == 2:
            diff = (snaps[0].market_home_win_prob - snaps[1].market_home_win_prob) * 100
            if abs(diff) > 0.5:
                line_movement = 'up' if diff > 0 else 'down'

    # Source-Aware Betting trust tier — exposed to the template so the
    # game-detail page can render the same Verified/ESPN/Derived badge
    # the MLB hub already shows. Without this, the operator cannot see
    # at a glance whether the displayed spread/total/ML came from the
    # paid Odds API, ESPN's free fallback, or a synthesized row — and
    # that visibility is the whole point of source-aware betting.
    from apps.core.services.odds_trust import get_odds_trust_tier, trust_badge
    trust_tier = get_odds_trust_tier(latest_odds)

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
        'trust_tier': trust_tier,
        'trust_badge': trust_badge(trust_tier),
    }
