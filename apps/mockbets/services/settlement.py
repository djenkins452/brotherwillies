"""Settlement engine for mock bets.

Resolves pending mock bets when the underlying game has finalized.
Handles CFB, CBB, and Golf sports with sport-specific logic.
"""

import logging
from decimal import Decimal

from django.db import models, transaction
from django.utils import timezone

from apps.mockbets.models import MockBet, MockBetSettlementLog

logger = logging.getLogger(__name__)


def settle_pending_bets(sport='all'):
    """Settle all pending mock bets for finalized games.

    Returns dict with counts per sport.
    """
    counts = {'cfb': 0, 'cbb': 0, 'golf': 0}

    if sport in ('cfb', 'all'):
        counts['cfb'] = _settle_cfb()

    if sport in ('cbb', 'all'):
        counts['cbb'] = _settle_cbb()

    if sport in ('golf', 'all'):
        counts['golf'] = _settle_golf()

    return counts


def _settle_cfb():
    bets = MockBet.objects.filter(
        sport='cfb',
        result='pending',
        cfb_game__status='final',
        cfb_game__home_score__isnull=False,
        cfb_game__away_score__isnull=False,
    ).select_related('cfb_game', 'cfb_game__home_team', 'cfb_game__away_team')

    settled = 0
    for bet in bets:
        game = bet.cfb_game
        try:
            result = _resolve_game_bet(bet, game.home_score, game.away_score,
                                       game.home_team.name, game.away_team.name,
                                       game)
            _apply_settlement(bet, result['result'], result['reason'])
            settled += 1
        except Exception as e:
            logger.error(f'Failed to settle CFB mock bet {bet.id}: {e}')

    return settled


def _settle_cbb():
    bets = MockBet.objects.filter(
        sport='cbb',
        result='pending',
        cbb_game__status='final',
        cbb_game__home_score__isnull=False,
        cbb_game__away_score__isnull=False,
    ).select_related('cbb_game', 'cbb_game__home_team', 'cbb_game__away_team')

    settled = 0
    for bet in bets:
        game = bet.cbb_game
        try:
            result = _resolve_game_bet(bet, game.home_score, game.away_score,
                                       game.home_team.name, game.away_team.name,
                                       game)
            _apply_settlement(bet, result['result'], result['reason'])
            settled += 1
        except Exception as e:
            logger.error(f'Failed to settle CBB mock bet {bet.id}: {e}')

    return settled


def _settle_golf():
    """Settle golf bets — requires event to have ended (end_date passed)."""
    now = timezone.now().date()
    bets = MockBet.objects.filter(
        sport='golf',
        result='pending',
        golf_event__end_date__lt=now,
    ).select_related('golf_event', 'golf_golfer')

    settled = 0
    for bet in bets:
        try:
            result = _resolve_golf_bet(bet)
            _apply_settlement(bet, result['result'], result['reason'])
            settled += 1
        except Exception as e:
            logger.error(f'Failed to settle Golf mock bet {bet.id}: {e}')

    return settled


def _resolve_game_bet(bet, home_score, away_score, home_name, away_name, game):
    """Resolve a CFB/CBB game bet."""
    margin = home_score - away_score  # positive = home won by margin

    if bet.bet_type == 'moneyline':
        return _resolve_moneyline(bet, home_score, away_score, home_name, away_name)
    elif bet.bet_type == 'spread':
        return _resolve_spread(bet, margin, game)
    elif bet.bet_type == 'total':
        return _resolve_total(bet, home_score, away_score)

    return {'result': 'loss', 'reason': f'Unknown bet type: {bet.bet_type}'}


def _resolve_moneyline(bet, home_score, away_score, home_name, away_name):
    selection_lower = bet.selection.lower()
    home_lower = home_name.lower()
    away_lower = away_name.lower()

    if home_score == away_score:
        return {'result': 'push', 'reason': f'Game ended in a tie {home_score}-{away_score}'}

    home_won = home_score > away_score

    if home_lower in selection_lower:
        result = 'win' if home_won else 'loss'
    elif away_lower in selection_lower:
        result = 'win' if not home_won else 'loss'
    else:
        result = 'loss'
        return {'result': result, 'reason': f'Could not match selection "{bet.selection}" to teams'}

    winner = home_name if home_won else away_name
    return {'result': result, 'reason': f'{winner} won {home_score}-{away_score}'}


def _resolve_spread(bet, margin, game):
    """Resolve spread bet. Selection format: 'Team Name -3.5' or 'Team Name +7'."""
    selection = bet.selection
    spread_val = None

    # Extract spread value from the selection string
    for part in reversed(selection.split()):
        try:
            spread_val = float(part.replace('+', ''))
            break
        except ValueError:
            continue

    if spread_val is None:
        # Try to get from latest odds
        time_field = game.kickoff if hasattr(game, 'kickoff') else game.tipoff
        latest_odds = game.odds_snapshots.filter(
            captured_at__lt=time_field
        ).order_by('-captured_at').first()
        if latest_odds and latest_odds.spread is not None:
            spread_val = latest_odds.spread
        else:
            return {'result': 'loss', 'reason': f'Could not determine spread from selection: {selection}'}

    home_lower = game.home_team.name.lower()
    selection_lower = selection.lower()

    # Determine if selection is home or away
    if home_lower in selection_lower:
        adjusted = margin + spread_val
    else:
        adjusted = -margin + spread_val

    if adjusted > 0:
        return {'result': 'win', 'reason': f'Covered spread ({spread_val:+g}). Margin: {margin}'}
    elif adjusted == 0:
        return {'result': 'push', 'reason': f'Pushed on spread ({spread_val:+g}). Margin: {margin}'}
    else:
        return {'result': 'loss', 'reason': f'Did not cover spread ({spread_val:+g}). Margin: {margin}'}


def _resolve_total(bet, home_score, away_score):
    """Resolve over/under total bet. Selection: 'Over 145.5' or 'Under 53'."""
    selection_lower = bet.selection.lower()
    actual_total = home_score + away_score
    target = None

    for part in selection_lower.split():
        try:
            target = float(part)
            break
        except ValueError:
            continue

    if target is None:
        return {'result': 'loss', 'reason': f'Could not parse total from: {bet.selection}'}

    is_over = 'over' in selection_lower

    if actual_total == target:
        return {'result': 'push', 'reason': f'Total {actual_total} equals line {target}'}
    elif is_over:
        result = 'win' if actual_total > target else 'loss'
    else:
        result = 'win' if actual_total < target else 'loss'

    return {'result': result, 'reason': f'Total: {actual_total}, Line: {target}, Selection: {bet.selection}'}


def _resolve_golf_bet(bet):
    """Resolve golf bets based on event results.

    Golf settlement uses round data to determine finishing position.
    Without complete round data, bets remain pending.
    """
    from apps.golf.models import GolfRound

    event = bet.golf_event
    golfer = bet.golf_golfer

    if not golfer:
        return {'result': 'loss', 'reason': 'No golfer associated with bet'}

    # Get all rounds for this event, ordered by total score
    all_rounds = GolfRound.objects.filter(
        event=event, score__isnull=False
    ).values('golfer_id').annotate(
        total=models.Sum('score')
    ).order_by('total')

    if not all_rounds:
        return {'result': 'loss', 'reason': 'No round data available for settlement'}

    # Find golfer's position
    golfer_total = None
    position = None
    for idx, entry in enumerate(all_rounds, 1):
        if entry['golfer_id'] == golfer.id:
            golfer_total = entry['total']
            position = idx
            break

    if position is None:
        # Golfer didn't finish / missed cut
        if bet.bet_type == 'make_cut':
            return {'result': 'loss', 'reason': f'{golfer.name} did not complete the event'}
        return {'result': 'loss', 'reason': f'{golfer.name} did not finish the event'}

    total_golfers = len(all_rounds)

    if bet.bet_type == 'outright':
        result = 'win' if position == 1 else 'loss'
        return {'result': result, 'reason': f'{golfer.name} finished #{position} of {total_golfers}'}

    elif bet.bet_type == 'top_5':
        result = 'win' if position <= 5 else 'loss'
        return {'result': result, 'reason': f'{golfer.name} finished #{position}'}

    elif bet.bet_type == 'top_10':
        result = 'win' if position <= 10 else 'loss'
        return {'result': result, 'reason': f'{golfer.name} finished #{position}'}

    elif bet.bet_type == 'top_20':
        result = 'win' if position <= 20 else 'loss'
        return {'result': result, 'reason': f'{golfer.name} finished #{position}'}

    elif bet.bet_type == 'make_cut':
        # If they have rounds, they made the cut
        golfer_rounds = GolfRound.objects.filter(event=event, golfer=golfer, score__isnull=False).count()
        result = 'win' if golfer_rounds >= 3 else 'loss'
        return {'result': result, 'reason': f'{golfer.name} completed {golfer_rounds} rounds'}

    elif bet.bet_type == 'matchup':
        return _resolve_golf_matchup(bet, event)

    return {'result': 'loss', 'reason': f'Unknown golf bet type: {bet.bet_type}'}


def _resolve_golf_matchup(bet, event):
    """Resolve head-to-head golf matchup bet."""
    from apps.golf.models import GolfRound

    # Matchup selection format: "Golfer A over Golfer B"
    # The bet's golfer is who they picked to win
    golfer = bet.golf_golfer
    if not golfer:
        return {'result': 'loss', 'reason': 'No golfer specified for matchup'}

    golfer_rounds = GolfRound.objects.filter(
        event=event, golfer=golfer, score__isnull=False
    )
    golfer_total = sum(r.score for r in golfer_rounds)
    golfer_count = golfer_rounds.count()

    if golfer_count == 0:
        return {'result': 'loss', 'reason': f'{golfer.name} did not complete any rounds'}

    # For matchup, we compare against the note which should mention the opponent
    return {'result': 'pending', 'reason': 'Matchup resolution requires manual review'}


def _apply_settlement(bet, result, reason):
    """Apply settlement result to a bet with audit log."""
    with transaction.atomic():
        bet.result = result
        bet.simulated_payout = bet.calculate_payout()
        bet.settled_at = timezone.now()
        bet.save()

        MockBetSettlementLog.objects.create(
            mock_bet=bet,
            result=result,
            payout=bet.simulated_payout or Decimal('0.00'),
            reason=reason,
        )
