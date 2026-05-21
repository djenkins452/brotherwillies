"""Bulk MockBet operations — place all recommended, cancel all open.

Both functions are:
  - transactional (atomic — partial failure rolls back)
  - idempotent (running twice produces zero duplicates / zero double-cancels)
  - safe (every existing per-bet guard still fires; no logic bypassed)

Default scope is MLB today's slate, since that's where the recommendation
engine produces actionable picks. Multi-sport extension later is easy: add
sport keys to _SUPPORTED_SPORTS.
"""
import logging
from decimal import Decimal
from typing import Iterable

from django.db import transaction
from django.utils import timezone

from apps.mockbets.models import MockBet

logger = logging.getLogger(__name__)


def _moneyline_only_block_summary(bet_type: str) -> dict:
    """Structured no-op response when MONEYLINE_ONLY_MODE blocks a bulk
    spread/total path. Same shape as a normal bulk summary so callers can
    treat it uniformly without try/except gymnastics.
    """
    logger.info(
        'Blocked non-moneyline bulk action due to MONEYLINE_ONLY_MODE bet_type=%s',
        bet_type,
    )
    return {
        'placed': 0,
        'skipped_existing': 0,
        'skipped_no_odds': 0,
        'tier_filter': f'proven_{bet_type}',
        'source_filter': 'verified',
        'bet_type': bet_type,
        'stake': float(_DEFAULT_STAKE),
        'blocked': 'moneyline_only_mode',
    }


# Sports whose recommendation engine produces moneyline picks suitable for
# bulk placement. Other sports' engines (golf positional, etc.) require
# more nuance and are explicitly out of scope for v1.
_SUPPORTED_SPORTS = ('mlb',)
_DEFAULT_STAKE = Decimal('100.00')


# ---------------------------------------------------------------------------
# Single source of truth — bulk eligibility predicate (2026-05-16 trust repair)
# ---------------------------------------------------------------------------
#
# The 2026-05-16 production trust issue: MLB hub showed "Bet All Moneyline
# Plays (5)" but only 3 were placed, with two games silently retaining
# "Bet This" — no error, no warning. Root cause was a count-vs-placement
# divergence: the button count came from `decision_sections['elite'] +
# decision_sections['recommended']` which checks status/tier but NOT lane;
# placement filter additionally required `lane == 'core'`. A game with
# status='recommended' but lane='qualified' (risk flag fired) inflated
# the count without being placed.
#
# Fix: this single predicate is the source of truth for "is this
# recommendation bulk-eligible?". Both the MLB hub view (count) and the
# bulk placement service (execution) call it. If they ever diverge, the
# button count is wrong by construction.
#
# DO NOT inline these checks anywhere else. New eligibility constraints
# MUST be added here; new callers MUST consume the predicate.

def is_bulk_moneyline_eligible(
    rec,
    *,
    source_filter: str = 'all',
    tier_filter: str = 'all',
) -> bool:
    """The single bulk-moneyline eligibility predicate.

    Returns True iff `rec` (a Recommendation dataclass or DB row) is
    eligible for bulk placement under the given filters. Operates on
    the recommendation only — game-level checks (start time, user has
    pending bet, sport scope) are the caller's responsibility.

    Used by:
      - apps/mlb/views.py::mlb_hub  for the button count.
      - apps/mockbets/services/bulk_actions.py::_eligible_games_for_user
        for the placement queryset.

    Filters in priority order (any failure → not eligible):
      1. rec is None
      2. rec.status != 'recommended'
      3. rec.lane != 'core'                  (Two-Lane System, 2026-04-28)
      4. value tier / status_reason='value'  (high edge, low prob — visible
                                              but never bulk)
      5. probability < MIN_PROBABILITY_FOR_RECOMMENDED * 100
      6. |odds| > MAX_ABS_ODDS_FOR_RECOMMENDED  (longshot cap)
      7. blocked tier / status_reason='derived_odds'  (synthesized odds)
      8. source_filter mismatch (verified excludes ESPN; espn excludes primary)
      9. tier_filter mismatch (elite-only / strong-or-elite-only modes)
    """
    if rec is None:
        return False
    if getattr(rec, 'status', '') != 'recommended':
        return False
    # Two-Lane gate — bulk-bet automation requires lane='core'. Picks
    # with risk flags drop to 'qualified' and are explicitly excluded
    # per the master prompt "Filter for automation, not visibility".
    if getattr(rec, 'lane', '') != 'core':
        return False
    if (
        getattr(rec, 'tier', '') == 'value'
        or getattr(rec, 'status_reason', '') == 'value'
    ):
        return False
    # Lazy import to avoid circular deps at module load.
    from apps.core.services.recommendations import (
        MAX_ABS_ODDS_FOR_RECOMMENDED, MIN_PROBABILITY_FOR_RECOMMENDED,
    )
    prob_pct = getattr(rec, 'confidence_score', None)
    if prob_pct is None or float(prob_pct) < (MIN_PROBABILITY_FOR_RECOMMENDED * 100):
        return False
    odds = getattr(rec, 'odds_american', None)
    if odds is None or abs(int(odds)) > MAX_ABS_ODDS_FOR_RECOMMENDED:
        return False
    if (
        getattr(rec, 'tier', '') == 'blocked'
        or getattr(rec, 'status_reason', '') == 'derived_odds'
    ):
        return False
    is_secondary = bool(getattr(rec, 'is_secondary', False))
    if source_filter == 'verified' and is_secondary:
        return False
    if source_filter == 'espn' and not is_secondary:
        return False
    if tier_filter == 'elite' and getattr(rec, 'tier', '') != 'elite':
        return False
    if tier_filter in ('strong', 'elite_or_strong') and getattr(rec, 'tier', '') not in ('elite', 'strong'):
        return False
    return True


# Per-game outcome categories returned by place_bulk_recommended_bets.
# Stable string keys; the JSON response shape contract.
OUTCOME_PLACED = 'placed'
OUTCOME_SKIPPED_DUPLICATE = 'skipped_duplicate'
OUTCOME_SKIPPED_DRIFT = 'skipped_recommendation_drift'
OUTCOME_SKIPPED_GAME_STARTED = 'skipped_game_started'
OUTCOME_SKIPPED_MISSING_ODDS = 'skipped_missing_odds'
OUTCOME_FAILED = 'failed'

OUTCOME_LABELS = {
    OUTCOME_PLACED: 'Placed',
    OUTCOME_SKIPPED_DUPLICATE: 'Skipped — duplicate pending bet exists',
    OUTCOME_SKIPPED_DRIFT: 'Skipped — recommendation changed since page load',
    OUTCOME_SKIPPED_GAME_STARTED: 'Skipped — game has already started',
    OUTCOME_SKIPPED_MISSING_ODDS: 'Skipped — odds snapshot no longer available',
    OUTCOME_FAILED: 'Failed',
}


def _eligible_games_for_user(user, sport: str = 'mlb', tier_filter: str = 'all',
                              source_filter: str = 'all'):
    """Yield (game, recommendation) tuples for games the user could bet today.

    Date scope: today's local slate (matches the MLB hub's today_tiles).

    2026-05-16 trust repair: this function now delegates eligibility to
    `is_bulk_moneyline_eligible` (the canonical predicate). Behavior
    matches the previous inlined logic; the refactor's purpose is to
    eliminate the count/placement divergence that produced the
    "5 displayed / 3 placed" bug. The MLB hub view also calls
    `is_bulk_moneyline_eligible` for its count — same predicate, same
    answer, by construction.
    """
    if sport != 'mlb':
        return  # other sports out of scope for v1

    from apps.mlb.models import Game
    from apps.core.services.recommendations import get_recommendation

    now = timezone.now()
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
        if is_bulk_moneyline_eligible(
            rec, source_filter=source_filter, tier_filter=tier_filter,
        ):
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


def _game_outcome_label(game) -> str:
    """Display label for the per-game outcome list. Falls back gracefully."""
    try:
        return f'{game.away_team.name} @ {game.home_team.name}'
    except Exception:
        return f'Game {getattr(game, "id", "<unknown>")}'


def _place_one_bet(user, game, rec, stake: Decimal) -> 'MockBet':
    """Place a single bet inside its own atomic block. Caller catches
    exceptions and classifies them — this function does the placement
    only.

    Wrapping per-bet rather than per-loop is the trust-repair contract:
    one bad bet must NOT roll back the others. See 2026-05-16 fix.
    """
    implied = _implied_prob_decimal(rec.odds_american)
    from apps.core.utils.multi_book import get_odds_source_for_game
    odds_source = get_odds_source_for_game(game)
    with transaction.atomic():
        bet = MockBet.objects.create(
            user=user,
            sport='mlb',
            mlb_game=game,
            bet_type=rec.bet_type,
            selection=rec.pick,
            odds_american=rec.odds_american,
            implied_probability=implied,
            stake_amount=stake,
            expected_edge=(
                Decimal(str(rec.model_edge)) if rec.model_edge is not None else None
            ),
            model_source=rec.model_source,
            recommendation_status=rec.status,
            recommendation_tier=rec.tier or '',
            recommendation_confidence=(
                Decimal(str(rec.confidence_score))
                if rec.confidence_score is not None else None
            ),
            status_reason=rec.status_reason or '',
            is_system_generated=True,
            odds_source=odds_source,
        )
        # Persist BettingRecommendation snapshot + link. Non-fatal on failure
        # (snapshot is analytics-only; primary placement is the MockBet row).
        try:
            from apps.core.services.recommendations import persist_recommendation
            rec_row = persist_recommendation('mlb', game, user)
            if rec_row is not None:
                bet.recommendation = rec_row
                bet.save(update_fields=['recommendation'])
        except Exception:
            logger.exception(
                'bulk_place: snapshot persist failed game=%s — bet still placed',
                game.id,
            )
    return bet


def place_bulk_recommended_bets(
    user,
    sport: str = 'mlb',
    stake: Decimal = _DEFAULT_STAKE,
    tier_filter: str = 'all',
    source_filter: str = 'all',
    game_ids=None,
) -> dict:
    """Place a $stake mock bet on every eligible game.

    2026-05-16 TRUST REPAIR:
    -----------------------
    Per-game isolation. One bad bet does NOT terminate the loop. Every
    game gets a documented outcome — placed, skipped (with reason), or
    failed (with reason). The legacy "atomic-around-the-whole-loop"
    architecture was replaced because:
      - One IntegrityError would roll back all bets in the batch,
        leaving the user with a stale UI and no error surfaced.
      - Worse, the prior count-vs-placement divergence (status/tier
        counted by hub, lane checked by placement) caused silent skips
        with no operator-visible reason.

    candidate-set lock:
    -------------------
    When `game_ids` is provided, the function processes EXACTLY those
    games — no candidate-set recomputation. The MLB hub passes the
    list it used to compute the button count, so the displayed
    "(N)" and the bets attempted are the same N. Per-game drift is
    still possible (odds moved between page load and click), but it
    is surfaced as `OUTCOME_SKIPPED_DRIFT` rather than silently
    dropping the game.

    When `game_ids` is None (legacy callers, tests), the function
    behaves as before: re-computes the eligible set itself.

    source_filter / tier_filter: see is_bulk_moneyline_eligible.

    Returns:
        {
            'placed': int,
            'skipped': int,
            'failed': int,
            'requested': int,           # the count the operator expected
            'placed_items': [...],      # per-game outcome lists
            'skipped_items': [...],
            'failed_items': [...],
            # Legacy keys kept for back-compat (old JS will still
            # display something sensible if a deploy crosses with an
            # old browser tab):
            'skipped_existing': int,
            'skipped_started': int,
            'skipped_no_odds': int,
            'tier_filter': str,
            'source_filter': str,
            'stake': float,
        }
    """
    if sport not in _SUPPORTED_SPORTS:
        return {
            'placed': 0, 'skipped': 0, 'failed': 0, 'requested': 0,
            'placed_items': [], 'skipped_items': [], 'failed_items': [],
            'skipped_existing': 0, 'skipped_started': 0, 'skipped_no_odds': 0,
            'tier_filter': tier_filter, 'source_filter': source_filter,
            'stake': float(stake),
            'error': f'Bulk placement not supported for sport: {sport}',
        }

    from apps.mlb.models import Game
    from apps.core.services.recommendations import get_recommendation

    placed_items = []
    skipped_items = []
    failed_items = []

    # Legacy diagnostic counter — pre-pass walks today's slate to count
    # ineligible-due-to-no-odds and ineligible-due-to-game-started games.
    # Only used when game_ids is None (legacy / test path); the production
    # path's game_ids is pre-filtered by the hub view to exclude these.
    legacy_pre_skipped_no_odds = 0
    legacy_pre_skipped_started = 0

    if game_ids is not None:
        # CANDIDATE-SET LOCKED: process exactly the games the operator
        # saw on the hub. Each is re-evaluated against the eligibility
        # predicate; drift surfaces as skipped_recommendation_drift.
        # Order is preserved from the input list.
        requested_ids = list(game_ids)
        games_by_id = {
            str(g.id): g for g in
            Game.objects
            .filter(id__in=requested_ids)
            .select_related('home_team', 'away_team')
        }
        candidates = [
            (str(gid), games_by_id.get(str(gid)))
            for gid in requested_ids
        ]
    else:
        # LEGACY PATH: recompute the eligible set ourselves. Pre-pass
        # captures the legacy diagnostic counters first.
        now_check = timezone.now()
        all_today = (
            Game.objects
            .filter(first_pitch__date=timezone.localdate())
            .select_related('home_team', 'away_team')
        )
        for g in all_today:
            if g.first_pitch <= now_check or g.status != 'scheduled':
                legacy_pre_skipped_started += 1
                continue
            pre_rec = get_recommendation('mlb', g, user)
            if pre_rec is None:
                legacy_pre_skipped_no_odds += 1

        eligible = list(_eligible_games_for_user(
            user, sport=sport, tier_filter=tier_filter, source_filter=source_filter,
        ))
        candidates = [
            (str(g.id), g) for g, _ in eligible
        ]

    requested_count = len(candidates)
    now = timezone.now()

    for gid, game in candidates:
        label = _game_outcome_label(game) if game else f'Game {gid}'

        if game is None:
            # Caller passed an ID that doesn't resolve — could be a
            # deleted game between page render and click. Treat as
            # missing-odds for outcome purposes; rare in practice.
            skipped_items.append({
                'game_id': gid,
                'label': label,
                'outcome': OUTCOME_SKIPPED_MISSING_ODDS,
                'reason': OUTCOME_LABELS[OUTCOME_SKIPPED_MISSING_ODDS],
            })
            continue

        # Game-state checks (cheap; pre-recommendation).
        if game.first_pitch <= now or game.status != 'scheduled':
            skipped_items.append({
                'game_id': gid,
                'label': label,
                'outcome': OUTCOME_SKIPPED_GAME_STARTED,
                'reason': OUTCOME_LABELS[OUTCOME_SKIPPED_GAME_STARTED],
            })
            continue

        # Re-compute recommendation. This is the drift check — the rec
        # at placement time may differ from the rec at page-render time.
        # Per Law 3 transparency, drift is surfaced explicitly.
        try:
            rec = get_recommendation(sport, game, user)
        except Exception as exc:
            logger.exception(
                'bulk_place: recommendation compute failed game=%s', gid,
            )
            failed_items.append({
                'game_id': gid,
                'label': label,
                'outcome': OUTCOME_FAILED,
                'reason': f'{OUTCOME_LABELS[OUTCOME_FAILED]} — '
                          f'recommendation compute raised: {exc!r}',
            })
            continue

        if rec is None:
            skipped_items.append({
                'game_id': gid,
                'label': label,
                'outcome': OUTCOME_SKIPPED_MISSING_ODDS,
                'reason': OUTCOME_LABELS[OUTCOME_SKIPPED_MISSING_ODDS],
            })
            continue

        if not is_bulk_moneyline_eligible(
            rec, source_filter=source_filter, tier_filter=tier_filter,
        ):
            # Recommendation drifted out of eligibility since the page
            # was rendered (odds moved → lane flipped to qualified,
            # status flipped to not_recommended, edge collapsed, etc).
            skipped_items.append({
                'game_id': gid,
                'label': label,
                'outcome': OUTCOME_SKIPPED_DRIFT,
                'reason': OUTCOME_LABELS[OUTCOME_SKIPPED_DRIFT],
            })
            continue

        # Duplicate-pending check.
        if _user_has_pending_bet_on_game(user, game):
            skipped_items.append({
                'game_id': gid,
                'label': label,
                'outcome': OUTCOME_SKIPPED_DUPLICATE,
                'reason': OUTCOME_LABELS[OUTCOME_SKIPPED_DUPLICATE],
            })
            continue

        # Per-game atomic placement. Exception isolation is the whole
        # point of this refactor — one bad bet does not affect others.
        try:
            bet = _place_one_bet(user, game, rec, stake)
            placed_items.append({
                'game_id': gid,
                'label': label,
                'outcome': OUTCOME_PLACED,
                'reason': OUTCOME_LABELS[OUTCOME_PLACED],
                'bet_id': str(bet.id),
            })
        except Exception as exc:
            logger.exception(
                'bulk_place: place failed game=%s', gid,
            )
            failed_items.append({
                'game_id': gid,
                'label': label,
                'outcome': OUTCOME_FAILED,
                'reason': f'{OUTCOME_LABELS[OUTCOME_FAILED]} — {exc!r}',
            })

    # Back-compat legacy counters — old JS that doesn't read the new
    # structured items array still gets reasonable numbers. For the
    # legacy (game_ids=None) path, we add the pre-pass counts so the
    # historic diagnostic semantics are preserved.
    legacy_skipped_existing = sum(
        1 for s in skipped_items
        if s['outcome'] == OUTCOME_SKIPPED_DUPLICATE
    )
    legacy_skipped_started = sum(
        1 for s in skipped_items
        if s['outcome'] == OUTCOME_SKIPPED_GAME_STARTED
    ) + legacy_pre_skipped_started
    legacy_skipped_no_odds = sum(
        1 for s in skipped_items
        if s['outcome'] == OUTCOME_SKIPPED_MISSING_ODDS
    ) + legacy_pre_skipped_no_odds

    return {
        'placed': len(placed_items),
        'skipped': len(skipped_items),
        'failed': len(failed_items),
        'requested': requested_count,
        'placed_items': placed_items,
        'skipped_items': skipped_items,
        'failed_items': failed_items,
        # Legacy fields for back-compat.
        'skipped_existing': legacy_skipped_existing,
        'skipped_started': legacy_skipped_started,
        'skipped_no_odds': legacy_skipped_no_odds,
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
    # Master-switch gate — short-circuit before any DB work or eligibility
    # scanning. Returns the canonical "blocked" summary; callers see a
    # structured zero-placed response instead of an exception.
    from apps.core.config import is_moneyline_only_mode
    if is_moneyline_only_mode():
        return _moneyline_only_block_summary('spread')
    return _place_bulk_proven_spread_bets_impl(user, stake)


def _place_bulk_proven_spread_bets_impl(user, stake: Decimal) -> dict:
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
            from apps.core.utils.multi_book import get_odds_source_for_game
            odds_source = get_odds_source_for_game(game)
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
                is_system_generated=True,
                odds_source=odds_source,
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
    from apps.core.config import is_moneyline_only_mode
    if is_moneyline_only_mode():
        return _moneyline_only_block_summary('total')
    return _place_bulk_proven_total_bets_impl(user, stake)


def _place_bulk_proven_total_bets_impl(user, stake: Decimal) -> dict:
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
            from apps.core.utils.multi_book import get_odds_source_for_game
            odds_source = get_odds_source_for_game(game)
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
                is_system_generated=True,
                odds_source=odds_source,
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
