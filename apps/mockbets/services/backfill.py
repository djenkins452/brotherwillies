"""Backfill historical MockBet data — closing odds + CLV.

Older bets (placed before the snapshot fields landed) are missing
`closing_odds_american`, `clv_cents`, and the recommendation snapshot.
This module fills in what we can derive from data still in the DB and
explicitly refuses to fabricate anything else.

Rules (non-negotiable):
  1. Never fabricate. Every backfilled value comes from a real
     OddsSnapshot or a real persisted BettingRecommendation row.
  2. Never overwrite valid existing data. Only fill fields that are NULL.
  3. Idempotent. Running this twice produces the same result as once.
  4. Recommendation snapshots are NOT recomputed from the current model —
     today's model could disagree with what was true at bet time. We only
     copy from a still-linked BettingRecommendation row when one exists.

Run via the `backfill_mockbets` management command (--dry-run by default).
"""
import logging

from django.db import transaction

from apps.core.utils.odds import closing_line_value
from apps.mockbets.models import MockBet
from apps.mockbets.services.clv import (
    _SPORT_CONFIG,
    _closing_ml_for_selection,
    _closing_snapshot,
)


logger = logging.getLogger(__name__)


def _start_field_for_sport(sport: str):
    for s, _, start_field in _SPORT_CONFIG:
        if s == sport:
            return start_field
    return None


def _backfill_closing_odds_and_clv(bet) -> tuple:
    """Try to fill closing_odds_american + clv_cents on this bet.

    Returns (filled_closing: bool, computed_clv: bool). Both False when no
    pre-game OddsSnapshot exists or the bet's selection can't be matched
    to a side of the market.

    Only operates on bets where the relevant field is currently None — never
    overwrites. Mutates `bet` but does NOT call .save() so the caller can
    decide whether to persist (dry-run vs commit).
    """
    filled_closing = False
    computed_clv = False

    if bet.bet_type != 'moneyline':
        return False, False
    if bet.sport not in ('cfb', 'cbb', 'mlb', 'college_baseball'):
        return False, False

    start_field = _start_field_for_sport(bet.sport)
    game = bet.game
    if start_field is None or game is None:
        return False, False

    snap = _closing_snapshot(game, start_field)
    if snap is None:
        return False, False

    # Closing odds — only fill if currently null.
    if bet.closing_odds_american is None:
        closing_ml = _closing_ml_for_selection(bet, snap)
        if closing_ml is None:
            return False, False
        bet.closing_odds_american = closing_ml
        filled_closing = True

    # CLV — fill if null AND we now have closing odds (existing or just-set).
    if bet.clv_cents is None and bet.closing_odds_american is not None:
        clv = closing_line_value(bet.odds_american, bet.closing_odds_american)
        bet.clv_cents = clv
        bet.clv_direction = 'positive' if clv > 0 else 'negative' if clv < 0 else ''
        computed_clv = True

    return filled_closing, computed_clv


def _backfill_recommendation_snapshot(bet) -> bool:
    """Copy snapshot fields from a still-linked BettingRecommendation row.

    NEVER recomputes from the current model — today's recommendation could
    disagree with what was true at bet time, and that would be a fabrication
    by another name. Only copies when:
      - bet.recommendation FK is still set (the row exists)
      - the snapshot field is currently empty/null

    Returns True iff anything was copied.
    """
    rec = bet.recommendation
    if rec is None:
        return False
    changed = False
    if not bet.recommendation_status and rec.status:
        bet.recommendation_status = rec.status
        changed = True
    if not bet.recommendation_tier and rec.tier:
        bet.recommendation_tier = rec.tier
        changed = True
    if bet.recommendation_confidence is None and rec.confidence_score is not None:
        bet.recommendation_confidence = rec.confidence_score
        changed = True
    if not bet.status_reason and rec.status_reason:
        bet.status_reason = rec.status_reason
        changed = True
    if bet.expected_edge is None and rec.model_edge is not None:
        bet.expected_edge = rec.model_edge
        changed = True
    return changed


def backfill_mockbet_data(dry_run: bool = True, queryset=None) -> dict:
    """Walk every MockBet (or a filtered queryset) and backfill what we can.

    Returns a structured summary. With `dry_run=True` (default), no DB writes
    happen — the counts reflect what *would* be filled if you ran with
    `dry_run=False`.
    """
    bets = queryset if queryset is not None else MockBet.objects.all()
    bets = bets.select_related(
        'cfb_game__home_team', 'cfb_game__away_team',
        'cbb_game__home_team', 'cbb_game__away_team',
        'mlb_game__home_team', 'mlb_game__away_team',
        'college_baseball_game__home_team', 'college_baseball_game__away_team',
        'recommendation',
    )

    processed = 0
    closing_odds_filled = 0
    clv_computed = 0
    rec_snapshot_filled = 0
    skipped_no_odds = 0
    skipped_no_snapshot = 0

    # Each bet is independent; per-bet save() inside an atomic block keeps
    # partial-failure recovery cheap. A single overarching transaction would
    # roll back ALL backfills on a single failure, which is too aggressive
    # for an opt-in maintenance script.
    for bet in bets:
        processed += 1
        bet_changed = False

        filled_closing, computed_clv = _backfill_closing_odds_and_clv(bet)
        if filled_closing:
            closing_odds_filled += 1
            bet_changed = True
        if computed_clv:
            clv_computed += 1
            bet_changed = True

        # Counters for "we tried but couldn't" so operators understand why
        # certain bets stay null. Only count a bet as "no_odds" when CLV is
        # the missing thing AND we couldn't fill closing — otherwise this
        # double-counts bets that had everything fine.
        if (
            bet.bet_type == 'moneyline'
            and bet.sport in ('cfb', 'cbb', 'mlb', 'college_baseball')
            and bet.clv_cents is None
            and bet.closing_odds_american is None
        ):
            skipped_no_odds += 1

        rec_filled = _backfill_recommendation_snapshot(bet)
        if rec_filled:
            rec_snapshot_filled += 1
            bet_changed = True
        elif (
            not bet.recommendation_status
            and bet.recommendation is None
        ):
            skipped_no_snapshot += 1

        if bet_changed and not dry_run:
            with transaction.atomic():
                bet.save()

    return {
        'processed': processed,
        'closing_odds_filled': closing_odds_filled,
        'clv_computed': clv_computed,
        'rec_snapshot_filled': rec_snapshot_filled,
        'skipped_no_odds': skipped_no_odds,
        'skipped_no_snapshot': skipped_no_snapshot,
        'dry_run': dry_run,
    }
