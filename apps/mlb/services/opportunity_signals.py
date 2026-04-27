"""Tiered Intelligence — Phase 1 Opportunity Signals (rule-based).

This module is the single source of truth for spread and total
"opportunity signals" — rule-based market observations that surface
in the UI as informational chips, NEVER as recommendations.

Hard contract (enforced by review + tests):
    1. Spread/Total signals are NEVER labeled "recommended".
    2. Spread/Total signals are NEVER included in "Bet All" actions.
    3. Spread/Total signals do NOT affect the Moneyline pipeline —
       BettingRecommendation, edge math, tier assignment, confidence,
       and the moneyline bulk-bet flow are all unaware this module exists.

Signal vocabulary (MLB Phase 1):
    Spread:
        tight_spread       — |spread| <= 1.5
        large_favorite     — |spread| >= 2.5
    Total:
        high_scoring       — total >= 9.5
        low_scoring        — total <=  7.5

These thresholds intentionally leave a no-signal gap between 1.5 and 2.5
(spread) and between 7.5 and 9.5 (total). A snapshot landing in the gap
gets no signal — silence is correct, not a bug.

Idempotency:
    Each (game, odds_snapshot, signal_type) tuple is unique by DB
    constraint. Running the generators twice on the same snapshot
    creates zero rows the second time. The post_save hook fires on
    every snapshot insert (primary, ESPN fallback, manual, future
    sources), so this idempotency is load-bearing — without it, ESPN
    gap-fill runs on top of fresh primary would double-write signals.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from apps.mlb.models import Game, OddsSnapshot, SpreadOpportunity, TotalOpportunity


logger = logging.getLogger(__name__)


# Thresholds — single canonical place to change them. Tests pin these
# values explicitly so a silent threshold drift would break the suite.
SPREAD_TIGHT_THRESHOLD = 1.5      # |spread| <= 1.5  → tight_spread
SPREAD_LARGE_THRESHOLD = 2.5      # |spread| >= 2.5  → large_favorite
TOTAL_HIGH_THRESHOLD = 9.5        # total >= 9.5     → high_scoring
TOTAL_LOW_THRESHOLD = 7.5         # total <= 7.5     → low_scoring


def _classify_spread(spread: float | None) -> list[str]:
    """Return the list of spread signal types that fire for a value.

    Mutually exclusive by construction (the gap between 1.5 and 2.5
    means at most one fires), but we return a list so the caller's
    iteration shape is uniform with totals and future signals.
    """
    if spread is None:
        return []
    abs_spread = abs(spread)
    signals: list[str] = []
    if abs_spread <= SPREAD_TIGHT_THRESHOLD:
        signals.append('tight_spread')
    if abs_spread >= SPREAD_LARGE_THRESHOLD:
        signals.append('large_favorite')
    return signals


def _classify_total(total: float | None) -> list[str]:
    """Return the list of total signal types that fire for a value."""
    if total is None:
        return []
    signals: list[str] = []
    if total >= TOTAL_HIGH_THRESHOLD:
        signals.append('high_scoring')
    if total <= TOTAL_LOW_THRESHOLD:
        signals.append('low_scoring')
    return signals


def generate_spread_opportunities(game, snapshot) -> list:
    """Generate SpreadOpportunity rows for one (game, snapshot) pair.

    Returns the list of rows that were CREATED (not the existing ones).
    Rows that already exist for this (game, snapshot, signal_type) are
    skipped — caller can safely retry.

    Why we link to the snapshot rather than just the game: the same
    game gets multiple snapshots over time (different bookmakers,
    different captures). Linking lets us answer "which snapshot
    generated this signal" for analytics + lets us age out old signals
    by joining on snapshot.captured_at.
    """
    if snapshot is None or getattr(snapshot, 'spread', None) is None:
        return []

    from apps.mlb.models import SpreadOpportunity

    signal_types = _classify_spread(snapshot.spread)
    if not signal_types:
        return []

    # Determine favorite/underdog from spread sign (home perspective).
    if snapshot.spread < 0:
        favorite_name = game.home_team.name
        underdog_name = game.away_team.name
    elif snapshot.spread > 0:
        favorite_name = game.away_team.name
        underdog_name = game.home_team.name
    else:  # spread == 0 — pick'em, no favorite
        favorite_name = ''
        underdog_name = ''

    created: list = []
    for signal_type in signal_types:
        obj, was_created = SpreadOpportunity.objects.get_or_create(
            game=game,
            odds_snapshot=snapshot,
            signal_type=signal_type,
            defaults={
                'spread': snapshot.spread,
                'favorite_team_name': favorite_name,
                'underdog_team_name': underdog_name,
                'source': getattr(snapshot, 'odds_source', 'odds_api'),
                'source_quality': getattr(snapshot, 'source_quality', 'primary'),
            },
        )
        if was_created:
            created.append(obj)
    return created


def generate_total_opportunities(game, snapshot) -> list:
    """Generate TotalOpportunity rows for one (game, snapshot) pair.

    Same shape as generate_spread_opportunities — see that docstring.
    """
    if snapshot is None or getattr(snapshot, 'total', None) is None:
        return []

    from apps.mlb.models import TotalOpportunity

    signal_types = _classify_total(snapshot.total)
    if not signal_types:
        return []

    created: list = []
    for signal_type in signal_types:
        obj, was_created = TotalOpportunity.objects.get_or_create(
            game=game,
            odds_snapshot=snapshot,
            signal_type=signal_type,
            defaults={
                'total': snapshot.total,
                'source': getattr(snapshot, 'odds_source', 'odds_api'),
                'source_quality': getattr(snapshot, 'source_quality', 'primary'),
            },
        )
        if was_created:
            created.append(obj)
    return created


def generate_opportunities_for_snapshot(snapshot) -> dict:
    """Single entry point used by the post_save signal handler.

    Returns a small summary dict so the caller (signal handler / tests
    / future cron job) can introspect what fired without re-querying.
    """
    if snapshot is None or getattr(snapshot, 'game', None) is None:
        return {'spread_created': 0, 'total_created': 0}
    spread = generate_spread_opportunities(snapshot.game, snapshot)
    total = generate_total_opportunities(snapshot.game, snapshot)
    return {'spread_created': len(spread), 'total_created': len(total)}


# --------------------------------------------------------------------- #
# Read-side helpers — used by the hub view + game-detail view to surface
# the most-recent active signals for a game without exposing the raw
# table to template authors.
# --------------------------------------------------------------------- #

def latest_spread_opportunity_for_game(game):
    """The most-recent SpreadOpportunity for this game, or None."""
    return game.spread_opportunities.order_by('-created_at').first()


def latest_total_opportunity_for_game(game):
    """The most-recent TotalOpportunity for this game, or None."""
    return game.total_opportunities.order_by('-created_at').first()


# Human-readable labels for the UI — kept here next to the choices so
# the template doesn't have to know the model's internal vocabulary.
SPREAD_SIGNAL_LABELS = {
    'tight_spread': 'Tight Spread',
    'large_favorite': 'Large Favorite',
}
TOTAL_SIGNAL_LABELS = {
    'high_scoring': 'High Scoring',
    'low_scoring': 'Low Scoring',
}
