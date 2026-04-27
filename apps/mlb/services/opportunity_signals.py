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

# ----- Phase 2: Lean thresholds --------------------------------------- #
# A signal becomes a "Lean" only when both bars clear:
#     win_rate     > threshold
#     sample_size  > minimum
#
# Spread bar is intentionally tighter on win rate (53%) since spread
# bets typically price at -110/+100 and 53% over a 30+ sample is the
# rough breakeven boundary. Total bar is set per the spec at 54% AND
# >2% ROI — ROI on a -110 bet at win_rate w is approximately
# (1.91*w) - 1, so win_rate ~ 0.5236 = breakeven. 54% / 2% ROI is
# comfortably above breakeven and represents a real edge.
SPREAD_LEAN_WIN_RATE_THRESHOLD = 0.53
SPREAD_LEAN_MIN_SAMPLE = 30
TOTAL_LEAN_WIN_RATE_THRESHOLD = 0.54
TOTAL_LEAN_MIN_ROI = 0.02
TOTAL_LEAN_MIN_SAMPLE = 30  # Same minimum so a single hot week can't promote.

# Standard sportsbook -110 vig — used for ROI math. 100 staked at -110
# returns 90.91 in profit on a win. ROI per unit at win_rate w:
#     ROI = w * (100/110) - (1 - w)  =  1.91 * w - 1
_AMERICAN_MINUS_110_PROFIT_FACTOR = 100.0 / 110.0


def _approx_roi_at_minus_110(win_rate: float | None) -> float | None:
    """Estimate ROI per unit at -110 odds for a given win rate.
    Returns None if win_rate is None. Excludes pushes from the inputs —
    the caller is expected to pass a push-excluded win rate."""
    if win_rate is None:
        return None
    return win_rate * _AMERICAN_MINUS_110_PROFIT_FACTOR - (1.0 - win_rate)


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
        # Snapshot the lean status from current historical performance
        # at create-time. Computing here (not in defaults={...}) so a
        # cold start with zero history doesn't double-query.
        is_lean, win_rate, sample = _spread_lean_status(signal_type)
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
                'is_lean': is_lean,
                'historical_win_rate': win_rate,
                'sample_size': sample,
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
        is_lean, win_rate, sample = _total_lean_status(signal_type)
        obj, was_created = TotalOpportunity.objects.get_or_create(
            game=game,
            odds_snapshot=snapshot,
            signal_type=signal_type,
            defaults={
                'total': snapshot.total,
                'source': getattr(snapshot, 'odds_source', 'odds_api'),
                'source_quality': getattr(snapshot, 'source_quality', 'primary'),
                'is_lean': is_lean,
                'historical_win_rate': win_rate,
                'sample_size': sample,
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


# ===================================================================== #
# Phase 2: Settlement
#
# Each opportunity row evaluates to win/loss/push once the underlying
# game finalizes. The "side we evaluated" is fixed per signal_type by
# the model's EVALUATED_DIRECTION map — see the SpreadOpportunity /
# TotalOpportunity docstrings for the conventions.
#
# Settlement is idempotent — calling it twice is a no-op. It only
# writes when:
#   1. The game has status='final' AND both scores are populated.
#   2. The opportunity row's outcome is currently empty.
# ===================================================================== #


def _spread_outcome(opp) -> str | None:
    """Win/loss/push for a SpreadOpportunity, given a final game.
    Returns None if the game isn't ready to settle.

    Convention reminder (defined on the model class):
        tight_spread     → underdog covers
        large_favorite   → favorite covers

    Math:
        spread is from home perspective; negative => home favored.
        favorite covers iff favorite's margin > |spread|.
        push when favorite's margin == |spread| (only possible at
        whole-number lines, which MLB run lines almost never use,
        but we handle it correctly anyway).
    """
    game = opp.game
    if (
        game.status != 'final'
        or game.home_score is None
        or game.away_score is None
    ):
        return None

    home_margin = game.home_score - game.away_score
    home_is_fav = opp.spread < 0
    if opp.spread == 0:
        # Pick'em — neither side is "favorite" so we can only settle
        # tight_spread if it ever fires at 0 (it does; 0 ≤ 1.5).
        # Treat the lower-rated/away side as the underdog by
        # convention; for a true coin flip this is arbitrary.
        # The signal_type tight_spread is then "underdog covers" =
        # away wins or it's a push at 0-0 which is impossible in MLB.
        # We only get here when away_team is treated as dog.
        fav_margin = abs(home_margin)  # whichever side won
        fav_covers = fav_margin > 0  # any non-tie favors "the winner"
        # For spread=0, we don't really have a favorite — collapse
        # to "tight_spread = home wins or away wins, no push"
        if opp.signal_type == 'tight_spread':
            # We're "betting the dog" but there is no dog. Mark push.
            return 'push'
        # large_favorite at spread=0 is impossible by definition
        # (|0| < 2.5), so this branch shouldn't execute. Defensive.
        return None

    if home_is_fav:
        fav_margin = home_margin
    else:
        fav_margin = -home_margin

    line = abs(opp.spread)
    if fav_margin == line:
        return 'push'
    fav_covers = fav_margin > line

    if opp.signal_type == 'tight_spread':
        # Underdog covers iff favorite does NOT cover.
        return 'win' if not fav_covers else 'loss'
    if opp.signal_type == 'large_favorite':
        return 'win' if fav_covers else 'loss'
    return None  # unknown signal_type — defensive


def _total_outcome(opp) -> str | None:
    """Win/loss/push for a TotalOpportunity, given a final game.

    Convention:
        high_scoring → over hits
        low_scoring  → under hits
    """
    game = opp.game
    if (
        game.status != 'final'
        or game.home_score is None
        or game.away_score is None
    ):
        return None

    total_runs = game.home_score + game.away_score
    if total_runs == opp.total:
        return 'push'
    over_hits = total_runs > opp.total

    if opp.signal_type == 'high_scoring':
        return 'win' if over_hits else 'loss'
    if opp.signal_type == 'low_scoring':
        return 'win' if not over_hits else 'loss'
    return None


def _settle_opportunity_row(opp, outcome_fn) -> bool:
    """Try to settle one opportunity row. Returns True iff a write
    happened. Idempotent — already-settled rows return False."""
    if opp.outcome:
        return False
    outcome = outcome_fn(opp)
    if outcome is None:
        return False
    from django.utils import timezone as _tz
    opp.outcome = outcome
    opp.settled_at = _tz.now()
    opp.save(update_fields=['outcome', 'settled_at'])
    return True


def settle_opportunities_for_game(game) -> dict:
    """Settle every unsettled opportunity row tied to one game.

    Called by the resolve_outcomes pipeline after a game's status
    flips to 'final'. Safe to call repeatedly — idempotent per row.
    Returns a counts dict for ops visibility.
    """
    spread_settled = 0
    for opp in game.spread_opportunities.filter(outcome=''):
        if _settle_opportunity_row(opp, _spread_outcome):
            spread_settled += 1
    total_settled = 0
    for opp in game.total_opportunities.filter(outcome=''):
        if _settle_opportunity_row(opp, _total_outcome):
            total_settled += 1
    return {'spread_settled': spread_settled, 'total_settled': total_settled}


def settle_all_unsettled() -> dict:
    """One-shot pass that settles every unsettled opportunity row whose
    game is final. Used as a backfill helper / ops command. The normal
    runtime path is settle_opportunities_for_game called per-game from
    the resolve_outcomes pipeline."""
    from apps.mlb.models import Game, SpreadOpportunity, TotalOpportunity

    spread = total = 0
    final_games_with_signals = (
        Game.objects.filter(status='final')
        .filter(
            home_score__isnull=False,
            away_score__isnull=False,
        )
        .distinct()
    )
    for g in final_games_with_signals:
        for opp in SpreadOpportunity.objects.filter(game=g, outcome=''):
            if _settle_opportunity_row(opp, _spread_outcome):
                spread += 1
        for opp in TotalOpportunity.objects.filter(game=g, outcome=''):
            if _settle_opportunity_row(opp, _total_outcome):
                total += 1
    return {'spread_settled': spread, 'total_settled': total}


# ===================================================================== #
# Phase 2: Performance aggregation
#
# Per-signal-type win rate over all settled rows. Push rows are
# excluded from win_rate (standard sports-betting convention) but
# included in the raw counts for transparency. Sample size = wins +
# losses (the decided rows), NOT including pushes.
# ===================================================================== #


def _aggregate(qs, signal_choices) -> dict:
    """Shared aggregation for both spread and total. Returns a dict
    keyed by signal_type with {win_rate, wins, losses, pushes,
    sample_size, roi_estimate}."""
    from django.db.models import Count, Q

    result = {}
    for signal_type, _label in signal_choices:
        sig_qs = qs.filter(signal_type=signal_type)
        wins = sig_qs.filter(outcome='win').count()
        losses = sig_qs.filter(outcome='loss').count()
        pushes = sig_qs.filter(outcome='push').count()
        decided = wins + losses
        win_rate = (wins / decided) if decided > 0 else None
        result[signal_type] = {
            'win_rate': win_rate,
            'wins': wins,
            'losses': losses,
            'pushes': pushes,
            'sample_size': decided,
            'roi_estimate': _approx_roi_at_minus_110(win_rate),
        }
    return result


def compute_spread_performance() -> dict:
    """Win rate, sample size, ROI estimate per spread signal_type."""
    from apps.mlb.models import SpreadOpportunity
    qs = SpreadOpportunity.objects.exclude(outcome='')
    return _aggregate(qs, SpreadOpportunity.SIGNAL_CHOICES)


def compute_total_performance() -> dict:
    """Win rate, sample size, ROI estimate per total signal_type."""
    from apps.mlb.models import TotalOpportunity
    qs = TotalOpportunity.objects.exclude(outcome='')
    return _aggregate(qs, TotalOpportunity.SIGNAL_CHOICES)


# ===================================================================== #
# Phase 2: Lean classification
#
# At signal-creation time, the generator stamps:
#     historical_win_rate  the current win rate for THIS signal_type
#     sample_size          the current decided-rows count
#     is_lean              True iff thresholds clear
#
# Snapshotted at create-time so threshold tweaks / data drift can
# never retroactively flip an old row's lean status.
# ===================================================================== #


def _spread_lean_status(signal_type: str) -> tuple[bool, float | None, int | None]:
    """Returns (is_lean, historical_win_rate, sample_size) for the
    NEW signal we're about to create. Reads the current historical
    performance for this signal_type and applies the spread thresholds."""
    perf = compute_spread_performance().get(signal_type) or {}
    win_rate = perf.get('win_rate')
    sample = perf.get('sample_size') or 0
    is_lean = (
        sample > SPREAD_LEAN_MIN_SAMPLE
        and win_rate is not None
        and win_rate > SPREAD_LEAN_WIN_RATE_THRESHOLD
    )
    return is_lean, win_rate, sample if sample else None


def _total_lean_status(signal_type: str) -> tuple[bool, float | None, int | None]:
    """Returns (is_lean, historical_win_rate, sample_size) for a new
    total signal. Total leans require BOTH a win-rate clear AND a
    minimum estimated ROI (per the spec) — protects against a high
    win rate that only barely covers the vig."""
    perf = compute_total_performance().get(signal_type) or {}
    win_rate = perf.get('win_rate')
    sample = perf.get('sample_size') or 0
    roi = perf.get('roi_estimate')
    is_lean = (
        sample > TOTAL_LEAN_MIN_SAMPLE
        and win_rate is not None
        and win_rate > TOTAL_LEAN_WIN_RATE_THRESHOLD
        and roi is not None
        and roi > TOTAL_LEAN_MIN_ROI
    )
    return is_lean, win_rate, sample if sample else None
