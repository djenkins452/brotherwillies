"""Closing Line Value — capture closing odds per bet + compute CLV.

CLV is the gold-standard professional signal for bet quality. It resolves
in hours (at game start) rather than requiring 500+ settled bets before
win-rate stabilizes. A bet with consistently positive CLV has real edge
whether or not it wins.

Responsibilities:
  - Find the latest pre-game OddsSnapshot per game
  - Apply it to every pending MockBet on that game that doesn't yet have
    closing_odds_american populated
  - Compute clv_cents + clv_direction

Idempotence: only operates on bets with `closing_odds_american IS NULL`, so
repeat invocations are safe. The cron loop calls this at settlement time.
"""
import logging
from typing import Optional

from apps.core.utils.odds import closing_line_value

logger = logging.getLogger(__name__)


# (sport_key, MockBet FK attr, Game attribute for start time)
_SPORT_CONFIG = [
    ('cfb', 'cfb_game', 'kickoff'),
    ('cbb', 'cbb_game', 'tipoff'),
    ('mlb', 'mlb_game', 'first_pitch'),
    ('college_baseball', 'college_baseball_game', 'first_pitch'),
]


def _closing_snapshot(game, start_field: str):
    """Return the latest OddsSnapshot captured strictly before game start.

    Using `< start_time` avoids picking up a post-first-pitch snapshot (which
    would conflate in-game odds with closing odds). Returns None when no
    pre-game snapshot exists — the bet simply stays without CLV rather than
    getting bad data.
    """
    start_time = getattr(game, start_field, None)
    if start_time is None:
        return None
    return (
        game.odds_snapshots
        .filter(captured_at__lt=start_time)
        .order_by('-captured_at')
        .first()
    )


def _closing_ml_for_selection(bet, snap) -> Optional[int]:
    """Return the closing moneyline for the side this bet was placed on."""
    game = bet.game
    if game is None:
        return None
    selection_lower = (bet.selection or '').lower()
    home_lower = game.home_team.name.lower()
    away_lower = game.away_team.name.lower()
    if home_lower and home_lower in selection_lower:
        return snap.moneyline_home
    if away_lower and away_lower in selection_lower:
        return snap.moneyline_away
    return None


def capture_bet_clv(bet) -> bool:
    """Populate closing_odds_american / clv_cents / clv_direction on one bet.

    Returns True if anything was written. No-op for bets that already have
    CLV captured, non-team-sport bets, non-moneyline bets, or bets whose
    game lacks a pre-game OddsSnapshot.
    """
    if bet.closing_odds_american is not None:
        return False  # already captured — idempotent
    if bet.bet_type != 'moneyline':
        return False  # CLV math only defined for moneyline in v1
    if bet.sport not in ('cfb', 'cbb', 'mlb', 'college_baseball'):
        return False  # golf settles on position, not line movement

    # Resolve the game + its start-time field for this sport
    sport_config = next((cfg for cfg in _SPORT_CONFIG if cfg[0] == bet.sport), None)
    if sport_config is None:
        return False
    _, _, start_field = sport_config
    game = bet.game
    if game is None:
        return False

    snap = _closing_snapshot(game, start_field)
    if snap is None:
        return False

    closing_ml = _closing_ml_for_selection(bet, snap)
    if closing_ml is None:
        return False

    clv = closing_line_value(bet.odds_american, closing_ml)
    bet.closing_odds_american = closing_ml
    bet.clv_cents = clv
    bet.clv_direction = 'positive' if clv > 0 else 'negative' if clv < 0 else ''
    bet.save(update_fields=['closing_odds_american', 'clv_cents', 'clv_direction'])
    return True


def capture_closing_odds(game) -> int:
    """Capture closing odds for ALL pending-CLV moneyline bets on this game.

    Called by the settlement engine when a game becomes final. Returns the
    number of bets updated so callers can log a summary.
    """
    from apps.mockbets.models import MockBet

    sport_config = None
    for sport, fk_attr, start_field in _SPORT_CONFIG:
        if isinstance(game, _game_model_for(sport)):
            sport_config = (sport, fk_attr, start_field)
            break
    if sport_config is None:
        return 0
    sport, fk_attr, _ = sport_config

    bets = MockBet.objects.filter(
        sport=sport,
        bet_type='moneyline',
        closing_odds_american__isnull=True,
        **{fk_attr: game},
    )
    updated = 0
    for bet in bets:
        try:
            if capture_bet_clv(bet):
                updated += 1
        except Exception as e:
            logger.error(f'capture_bet_clv failed for bet {bet.id}: {e}')
    if updated:
        logger.info(f'clv_capture sport={sport} game={game.id} updated={updated}')
    return updated


def _game_model_for(sport: str):
    """Lazy-resolve the Game model class for a sport key."""
    if sport == 'cfb':
        from apps.cfb.models import Game
    elif sport == 'cbb':
        from apps.cbb.models import Game
    elif sport == 'mlb':
        from apps.mlb.models import Game
    elif sport == 'college_baseball':
        from apps.college_baseball.models import Game
    else:
        return type(None)  # nothing will isinstance-match
    return Game
