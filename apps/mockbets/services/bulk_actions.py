"""Bulk MockBet operations — place all recommended, cancel all open.

Both functions are:
  - transactional (atomic — partial failure rolls back)
  - idempotent (running twice produces zero duplicates / zero double-cancels)
  - safe (every existing per-bet guard still fires; no logic bypassed)

Default scope is MLB today's slate, since that's where the recommendation
engine produces actionable picks. Multi-sport extension later is easy: add
sport keys to _SUPPORTED_SPORTS.
"""
from decimal import Decimal
from typing import Iterable

from django.db import transaction
from django.utils import timezone

from apps.mockbets.models import MockBet


# Sports whose recommendation engine produces moneyline picks suitable for
# bulk placement. Other sports' engines (golf positional, etc.) require
# more nuance and are explicitly out of scope for v1.
_SUPPORTED_SPORTS = ('mlb',)
_DEFAULT_STAKE = Decimal('100.00')


def _eligible_games_for_user(user, sport: str = 'mlb', tier_filter: str = 'all'):
    """Yield (game, recommendation) tuples for games the user could bet today.

    Filters out:
      - games not in 'scheduled' status (already started/finished)
      - games whose start time has already passed
      - games without odds (no recommendation can be computed)
      - games where status != 'recommended' (per spec)
      - games user already has a pending bet on (handled in place fn —
        would otherwise be a join here, but cleaner as a per-iter check)
    """
    if sport != 'mlb':
        return  # other sports out of scope for v1

    from apps.mlb.models import Game
    from apps.core.services.recommendations import get_recommendation

    now = timezone.now()
    upcoming = (
        Game.objects
        .filter(first_pitch__gt=now, status='scheduled')
        .select_related('home_team', 'away_team')
    )
    for game in upcoming:
        rec = get_recommendation('mlb', game, user)
        if rec is None:
            continue
        if rec.status != 'recommended':
            continue
        if tier_filter == 'elite' and rec.tier != 'elite':
            continue
        if tier_filter in ('strong', 'elite_or_strong') and rec.tier not in ('elite', 'strong'):
            continue
        yield game, rec


def _user_has_pending_bet_on_game(user, game) -> bool:
    """Has the user already placed a pending mock bet on this MLB game?"""
    return MockBet.objects.filter(
        user=user,
        sport='mlb',
        mlb_game=game,
        result='pending',
    ).exists()


def _implied_prob_decimal(odds: int) -> Decimal:
    """Implied probability from American odds, returned as a Decimal so it
    fits the MockBet schema directly without float drift."""
    if odds > 0:
        return Decimal('100') / (Decimal(odds) + Decimal('100'))
    return Decimal(abs(odds)) / (Decimal(abs(odds)) + Decimal('100'))


def place_bulk_recommended_bets(
    user,
    sport: str = 'mlb',
    stake: Decimal = _DEFAULT_STAKE,
    tier_filter: str = 'all',
) -> dict:
    """Place a $stake mock bet on every game whose model recommendation is
    `status='recommended'` and the user doesn't already have a pending bet.

    Wraps the whole batch in a single atomic block — either all bets land
    or none do, so a partial failure can't leave the user with a stale UI.

    Returns:
        {
            'placed': int,
            'skipped_existing': int,
            'skipped_started': int,    # games whose start is now past
            'skipped_no_odds': int,    # rec was None
            'tier_filter': str,
            'stake': float,
        }
    """
    placed = 0
    skipped_existing = 0
    skipped_started = 0
    skipped_no_odds = 0

    if sport not in _SUPPORTED_SPORTS:
        return {
            'placed': 0,
            'skipped_existing': 0,
            'skipped_started': 0,
            'skipped_no_odds': 0,
            'tier_filter': tier_filter,
            'stake': float(stake),
            'error': f'Bulk placement not supported for sport: {sport}',
        }

    # First pass — count games that are ineligible because they've already
    # started OR have no odds. Doing this BEFORE the atomic block so the
    # numbers reflect the slate state at request time without DB writes.
    from apps.mlb.models import Game
    from apps.core.services.recommendations import get_recommendation
    now = timezone.now()
    all_today = Game.objects.filter(
        first_pitch__date=timezone.localdate(),
    ).select_related('home_team', 'away_team')
    for g in all_today:
        if g.first_pitch <= now or g.status != 'scheduled':
            skipped_started += 1
            continue
        rec = get_recommendation('mlb', g, user)
        if rec is None:
            skipped_no_odds += 1

    # Second pass — actually place the bets, transactional.
    eligible = list(_eligible_games_for_user(user, sport=sport, tier_filter=tier_filter))
    with transaction.atomic():
        for game, rec in eligible:
            # Re-check inside the transaction. If a concurrent placement
            # snuck a row in between the eligibility scan and now, this
            # exists() catches it.
            if _user_has_pending_bet_on_game(user, game):
                skipped_existing += 1
                continue
            implied = _implied_prob_decimal(rec.odds_american)
            bet = MockBet.objects.create(
                user=user,
                sport='mlb',
                mlb_game=game,
                bet_type=rec.bet_type,
                selection=rec.pick,
                odds_american=rec.odds_american,
                implied_probability=implied,
                stake_amount=stake,
                expected_edge=Decimal(str(rec.model_edge)) if rec.model_edge is not None else None,
                model_source=rec.model_source,
                # Snapshot the recommendation context at placement time —
                # mirrors what place_bet view does so analytics work the same
                # whether placed individually or in bulk.
                recommendation_status=rec.status,
                recommendation_tier=rec.tier or '',
                recommendation_confidence=Decimal(str(rec.confidence_score)) if rec.confidence_score is not None else None,
                status_reason=rec.status_reason or '',
            )
            # Persist the BettingRecommendation row + link as the per-bet
            # placement view does. Non-fatal on failure.
            try:
                from apps.core.services.recommendations import persist_recommendation
                rec_row = persist_recommendation('mlb', game, user)
                if rec_row is not None:
                    bet.recommendation = rec_row
                    bet.save(update_fields=['recommendation'])
            except Exception:
                pass
            placed += 1

    return {
        'placed': placed,
        'skipped_existing': skipped_existing,
        'skipped_started': skipped_started,
        'skipped_no_odds': skipped_no_odds,
        'tier_filter': tier_filter,
        'stake': float(stake),
    }


def cancel_all_open_bets(user) -> dict:
    """Cancel every pending bet whose linked game hasn't started yet.

    Reuses MockBet.can_cancel as the single source of truth for eligibility
    so the bulk path can never cancel a bet the per-bet endpoint wouldn't.
    Deletes the bet (matches the existing per-bet cancel semantics).

    Returns:
        {'cancelled': int, 'skipped_started': int}
    """
    cancelled = 0
    skipped_started = 0
    pending = MockBet.objects.filter(user=user, result='pending').select_related(
        'cfb_game__home_team', 'cfb_game__away_team',
        'cbb_game__home_team', 'cbb_game__away_team',
        'mlb_game__home_team', 'mlb_game__away_team',
        'college_baseball_game__home_team', 'college_baseball_game__away_team',
        'golf_event',
    )
    with transaction.atomic():
        for bet in pending:
            if bet.can_cancel:
                bet.delete()
                cancelled += 1
            else:
                skipped_started += 1
    return {'cancelled': cancelled, 'skipped_started': skipped_started}
