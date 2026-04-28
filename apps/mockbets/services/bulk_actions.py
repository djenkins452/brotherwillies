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


def _eligible_games_for_user(user, sport: str = 'mlb', tier_filter: str = 'all',
                              source_filter: str = 'all'):
    """Yield (game, recommendation) tuples for games the user could bet today.

    Date scope: this function returns ONLY games whose first pitch falls on
    the viewer's local "today". This matches the MLB hub's `today_tiles`
    list — the source of truth for what the user actually sees in the
    Top Plays + Recommended Bets sections. Without this filter, "Bet All
    Verified Plays" would also place bets on tomorrow's slate (which has
    its own recommendations carried in the same query) — surprising and
    undesired, since the user is acting on what they can see on screen.

    source_filter (Source-Aware Betting, Commit B):
      'all'      — primary + secondary (legacy behavior)
      'verified' — only is_secondary=False (Odds API path)
      'espn'     — only is_secondary=True (ESPN fallback path)
    Derived-odds rows (tier='blocked' / status_reason='derived_odds') are
    ALWAYS excluded — synthesized odds must never feed a bulk action,
    regardless of which filter mode is active.

    Filters out:
      - games whose first_pitch is NOT on today's local date (NEW)
      - games not in 'scheduled' status (already started/finished)
      - games whose start time has already passed
      - games without odds (no recommendation can be computed)
      - games where status != 'recommended' (per spec)
      - blocked recommendations (derived odds)
      - games user already has a pending bet on (handled in place fn —
        would otherwise be a join here, but cleaner as a per-iter check)
    """
    if sport != 'mlb':
        return  # other sports out of scope for v1

    from apps.mlb.models import Game
    from apps.core.services.recommendations import get_recommendation

    now = timezone.now()
    # Scope to today's local slate so this matches the hub's visible
    # `today_tiles` exactly. Using timezone.localdate() respects the
    # UserTimezoneMiddleware-activated timezone so a user on PT and a
    # user on ET see (and act on) their respective "today".
    today_local = timezone.localdate()
    upcoming = (
        Game.objects
        .filter(first_pitch__gt=now, status='scheduled')
        .select_related('home_team', 'away_team')
    )
    upcoming = [
        g for g in upcoming
        if timezone.localtime(g.first_pitch).date() == today_local
    ]
    for game in upcoming:
        rec = get_recommendation('mlb', game, user)
        if rec is None:
            continue
        if rec.status != 'recommended':
            continue
        # Source-Aware Betting: derived odds NEVER bulk-bet, regardless
        # of source_filter. Defense in depth — the recommendation engine
        # already sets status='not_recommended' for derived rows so this
        # branch should be unreachable, but the explicit check guarantees
        # the contract no matter how upstream evolves.
        if (
            getattr(rec, 'tier', '') == 'blocked'
            or getattr(rec, 'status_reason', '') == 'derived_odds'
        ):
            continue
        is_secondary = bool(getattr(rec, 'is_secondary', False))
        if source_filter == 'verified' and is_secondary:
            continue
        if source_filter == 'espn' and not is_secondary:
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
    source_filter: str = 'all',
) -> dict:
    """Place a $stake mock bet on every game whose model recommendation is
    `status='recommended'` and the user doesn't already have a pending bet.

    source_filter ('all' / 'verified' / 'espn') restricts placement to the
    matching trust tier — used by the split UI buttons. Derived odds are
    always excluded regardless of filter.

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
            'source_filter': source_filter,
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
    eligible = list(_eligible_games_for_user(
        user, sport=sport, tier_filter=tier_filter, source_filter=source_filter,
    ))
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
        'source_filter': source_filter,
        'stake': float(stake),
    }


# --------------------------------------------------------------------- #
# Phase 3 — bulk placement on PROMOTED Spread / Total recommendations.
#
# Hard rules (enforced here, not just at the UI layer):
#   1. Only is_recommended=True rows are eligible.
#   2. ESPN-source and is_derived rows are filtered out as defense in
#      depth — _is_promotable_source already guarantees those rows
#      can't have is_recommended=True, but we double-check on the
#      placement path so a future data-layer bug can't leak.
#   3. Same today's-local-slate scope as the moneyline bulk endpoint.
#   4. Same skip-existing-pending guard.
# --------------------------------------------------------------------- #


def _eligible_spread_recommendations(user):
    """Yield SpreadOpportunity rows the user could bet right now."""
    from apps.mlb.models import SpreadOpportunity
    today_local = timezone.localdate()
    qs = (
        SpreadOpportunity.objects
        .filter(is_recommended=True)
        .exclude(source='espn')
        .select_related('game', 'game__home_team', 'game__away_team')
    )
    for opp in qs:
        game = opp.game
        if game.first_pitch is None or game.first_pitch <= timezone.now():
            continue
        if timezone.localtime(game.first_pitch).date() != today_local:
            continue
        if game.status != 'scheduled':
            continue
        # NOTE: skip-existing-pending check is INTENTIONALLY in the
        # placement loop, not here. Doing it here would silently drop
        # the row from the count, leaving idempotency callers thinking
        # 0 rows were eligible rather than 0 newly placed.
        yield opp


def _eligible_total_recommendations(user):
    """Yield TotalOpportunity rows the user could bet right now."""
    from apps.mlb.models import TotalOpportunity
    today_local = timezone.localdate()
    qs = (
        TotalOpportunity.objects
        .filter(is_recommended=True)
        .exclude(source='espn')
        .select_related('game', 'game__home_team', 'game__away_team')
    )
    for opp in qs:
        game = opp.game
        if game.first_pitch is None or game.first_pitch <= timezone.now():
            continue
        if timezone.localtime(game.first_pitch).date() != today_local:
            continue
        if game.status != 'scheduled':
            continue
        if MockBet.objects.filter(
            user=user, sport='mlb', mlb_game=game,
            bet_type='total', result='pending',
        ).exists():
            continue
        yield opp


def place_bulk_proven_spread_bets(user, stake: Decimal = _DEFAULT_STAKE) -> dict:
    """Place a $stake mock bet on every promoted spread recommendation
    in today's slate. -110 standard pricing assumed; selection is the
    side the EVALUATED_DIRECTION points at (underdog for tight_spread,
    favorite for large_favorite).

    Belt-and-suspenders source-safety: even if a stale row somehow
    has is_recommended=True with source='espn' or is_derived=True
    (the data layer prevents this), we re-check inside the loop and
    skip. is_derived isn't on the opportunity row but we exclude
    'espn' source and that catches the most common contamination path.
    """
    placed = 0
    skipped_existing = 0
    with transaction.atomic():
        for opp in _eligible_spread_recommendations(user):
            game = opp.game
            # Selection text describes the side per EVALUATED_DIRECTION
            # so the user can read the bet at a glance.
            line = abs(opp.spread)
            if opp.signal_type == 'tight_spread':
                # Underdog covers — bet at +line.
                team = opp.underdog_team_name or game.away_team.name
                selection = f"{team} +{line}"
            else:  # large_favorite
                team = opp.favorite_team_name or game.home_team.name
                selection = f"{team} -{line}"
            odds = -110  # standard run-line pricing
            # Re-check pending — within atomic block, defensive.
            if MockBet.objects.filter(
                user=user, sport='mlb', mlb_game=game,
                bet_type='spread', result='pending',
            ).exists():
                skipped_existing += 1
                continue
            implied = _implied_prob_decimal(odds)
            MockBet.objects.create(
                user=user, sport='mlb', mlb_game=game,
                bet_type='spread', selection=selection,
                odds_american=odds,
                implied_probability=implied,
                stake_amount=stake,
                expected_edge=(
                    Decimal(str(round(opp.roi * 100, 2)))
                    if opp.roi is not None else None
                ),
                model_source='house',
                # Snapshot context so analytics can see this came from
                # a Phase 3 promoted recommendation, not a moneyline rec.
                recommendation_status='recommended',
                recommendation_tier='proven_spread',
                recommendation_confidence=(
                    Decimal(str(round(opp.historical_win_rate * 100, 2)))
                    if opp.historical_win_rate is not None else None
                ),
                status_reason='proven_spread_phase3',
            )
            placed += 1
    return {
        'placed': placed,
        'skipped_existing': skipped_existing,
        'skipped_no_odds': 0,  # spread/total don't have a "no odds" path
        'tier_filter': 'proven_spread',
        'source_filter': 'verified',
        'bet_type': 'spread',
        'stake': float(stake),
    }


def place_bulk_proven_total_bets(user, stake: Decimal = _DEFAULT_STAKE) -> dict:
    """Place a $stake mock bet on every promoted total recommendation
    in today's slate. EVALUATED_DIRECTION: high_scoring → over,
    low_scoring → under. -110 standard pricing assumed."""
    placed = 0
    skipped_existing = 0
    with transaction.atomic():
        for opp in _eligible_total_recommendations(user):
            game = opp.game
            line = opp.total
            if opp.signal_type == 'high_scoring':
                selection = f"Over {line}"
            else:  # low_scoring
                selection = f"Under {line}"
            odds = -110
            if MockBet.objects.filter(
                user=user, sport='mlb', mlb_game=game,
                bet_type='total', result='pending',
            ).exists():
                skipped_existing += 1
                continue
            implied = _implied_prob_decimal(odds)
            MockBet.objects.create(
                user=user, sport='mlb', mlb_game=game,
                bet_type='total', selection=selection,
                odds_american=odds,
                implied_probability=implied,
                stake_amount=stake,
                expected_edge=(
                    Decimal(str(round(opp.roi * 100, 2)))
                    if opp.roi is not None else None
                ),
                model_source='house',
                recommendation_status='recommended',
                recommendation_tier='proven_total',
                recommendation_confidence=(
                    Decimal(str(round(opp.historical_win_rate * 100, 2)))
                    if opp.historical_win_rate is not None else None
                ),
                status_reason='proven_total_phase3',
            )
            placed += 1
    return {
        'placed': placed,
        'skipped_existing': skipped_existing,
        'skipped_no_odds': 0,
        'tier_filter': 'proven_total',
        'source_filter': 'verified',
        'bet_type': 'total',
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
