"""Automatic settlement for ForwardValidationSnapshot rows.

Runs from `refresh_data` alongside `settle_mockbets`. For every
unsettled snapshot whose linked game is now final, attaches the
outcome fields (won, profit_per_dollar, closing_market_prob, clv_pp,
scores) WITHOUT touching the immutable decision fields.

Idempotent — filters `settled_at__isnull=True` and only writes to
settlement fields. Repeated runs after a game is settled are no-ops.

No user activity required. Never depends on Danny placing a bet —
the forward-validation sample is MODEL performance, not betting
behavior.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from django.utils import timezone


logger = logging.getLogger(__name__)


def _american_to_decimal_return(odds: int) -> float:
    """American → decimal return per $1 stake (payout, not profit).
    -150 → 1.667; +150 → 2.5. Profit = return - 1."""
    if odds is None:
        return 1.0
    if odds > 0:
        return 1.0 + odds / 100.0
    return 1.0 + 100.0 / abs(odds)


def _american_to_implied(odds: int) -> float:
    if odds is None:
        return 0.5
    if odds > 0:
        return 100.0 / (odds + 100.0)
    return abs(odds) / (abs(odds) + 100.0)


def _closing_market_prob_for(game) -> Optional[float]:
    """Same source used by v3_2_forward_health CLV calc — the
    ModelResultSnapshot row updated by resolve_outcomes."""
    from apps.analytics.models import ModelResultSnapshot
    try:
        snap = (
            ModelResultSnapshot.objects
            .filter(mlb_game=game)
            .exclude(closing_market_prob__isnull=True)
            .order_by('-captured_at')
            .first()
        )
    except Exception:
        return None
    return snap.closing_market_prob if snap else None


def _settle_one(snap) -> Optional[Dict[str, Any]]:
    """Apply settlement to one snapshot. Returns a summary dict or
    None if the game isn't final yet."""
    game = snap.mlb_game
    if game is None:
        return None
    if game.status != 'final':
        return None
    if game.home_score is None or game.away_score is None:
        return None

    # Push handling — MLB regulation rarely ties, but defensive.
    if game.home_score == game.away_score:
        won = None
        profit = 0.0
    else:
        home_won = game.home_score > game.away_score
        if snap.pick_side == 'home':
            won = home_won
        elif snap.pick_side == 'away':
            won = not home_won
        else:
            # decision_class in ('not_recommended', 'no_signal') — no
            # side taken. Still settle so the row records the outcome
            # of the game, but no P/L or W/L to attach.
            won = None
            profit = None

    if won is True:
        profit = _american_to_decimal_return(snap.odds_american) - 1.0
    elif won is False:
        profit = -1.0
    # else profit already set above (None or 0.0).

    closing = _closing_market_prob_for(game)
    clv_pp = None
    if closing is not None and snap.odds_american is not None and snap.pick_side:
        open_implied = _american_to_implied(snap.odds_american)
        if snap.pick_side == 'home':
            clv_pp = (open_implied - closing) * 100.0
        else:
            clv_pp = (open_implied - (1.0 - closing)) * 100.0

    # settled_at protects against re-settlement; only write settlement
    # fields, NEVER decision fields.
    from apps.analytics.models import ForwardValidationSnapshot
    ForwardValidationSnapshot.objects.filter(id=snap.id).update(
        settled_at=timezone.now(),
        won=won,
        profit_per_dollar=(round(profit, 4) if profit is not None else None),
        closing_market_prob=closing,
        clv_pp=(round(clv_pp, 3) if clv_pp is not None else None),
        home_score_at_settlement=game.home_score,
        away_score_at_settlement=game.away_score,
    )
    return {
        'snapshot_id': str(snap.id),
        'game_id': str(game.id),
        'won': won,
        'profit_per_dollar': profit,
        'clv_pp': clv_pp,
    }


def settle_pending() -> Dict[str, Any]:
    """Settle every unsettled ForwardValidationSnapshot whose game is
    now final. Returns a summary."""
    from apps.analytics.models import ForwardValidationSnapshot

    pending = list(
        ForwardValidationSnapshot.objects
        .filter(settled_at__isnull=True,
                mlb_game__status='final')
        .select_related('mlb_game')
    )
    settled = 0
    skipped = 0
    for s in pending:
        r = _settle_one(s)
        if r is not None:
            settled += 1
        else:
            skipped += 1
    return {'attempted': len(pending), 'settled': settled, 'skipped': skipped}
