"""Game Timing helpers for the recommendation-card betting-window panel.

Pure presentation logic — converts a game's start time + a small set of
already-stored recommendation signals into:

    - status:    'good_to_bet' / 'review_later' / 'hold' / 'unavailable'
    - countdown: human-readable "Starts in 47m" / "Starts in 2h 14m" /
                 "Tomorrow at 1:10 PM"
    - best review window: 45–90 minutes before first pitch

INTENTIONAL DESIGN NOTES (do NOT change without owner approval):
  - This is BEHAVIORAL guidance for the user, NOT a predictive timing
    engine. It must never influence model probability, recommendation
    status, edge, tier, lane, or any decision rule.
  - HOLD is signal-driven (market_warning), not predictive. We don't
    try to forecast line movement.
  - Thresholds are intentionally simple round numbers.
  - Cross-sport: works for MLB (first_pitch), CFB (kickoff), CBB (tipoff)
    via a duck-typed game_start_time().

The recommendation engine is NOT touched by this module. The render layer
only reads stored fields.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional, Tuple

from django.utils import timezone


# --- Thresholds (minutes) ---------------------------------------------------
GOOD_TO_BET_WINDOW_MIN = 90      # within 90 min of first pitch
BEST_REVIEW_EARLIEST_MIN = 90    # window starts 90 min before
BEST_REVIEW_LATEST_MIN = 45      # window ends 45 min before


# --- Status constants ------------------------------------------------------
STATUS_GOOD_TO_BET = 'good_to_bet'
STATUS_REVIEW_LATER = 'review_later'
STATUS_HOLD = 'hold'
STATUS_UNAVAILABLE = 'unavailable'

STATUS_LABELS = {
    STATUS_GOOD_TO_BET:  '🟢 Good to Bet Now',
    STATUS_REVIEW_LATER: '🟡 Review Later',
    STATUS_HOLD:         '🔴 Hold / Wait',
    STATUS_UNAVAILABLE:  'Game time unavailable',
}

STATUS_REASONS = {
    STATUS_GOOD_TO_BET:  'Lineups are likely available and major uncertainty is reduced.',
    STATUS_REVIEW_LATER: 'MLB lineup and injury information is still evolving.',
    STATUS_HOLD:         'Pitching or lineup uncertainty may materially impact the recommendation.',
    STATUS_UNAVAILABLE:  '',
}


# ---------------------------------------------------------------------------
# Game-start resolution (cross-sport)

def game_start_time(game) -> Optional[datetime]:
    """Return the game's scheduled start datetime regardless of sport.

    MLB → first_pitch; CFB → kickoff; CBB → tipoff. Returns None for
    games without any of those (golf events, unscheduled, etc.)."""
    if game is None:
        return None
    return (
        getattr(game, 'first_pitch', None)
        or getattr(game, 'kickoff', None)
        or getattr(game, 'tipoff', None)
    )


# ---------------------------------------------------------------------------
# Timing math

def minutes_until_first_pitch(game_start, now=None) -> Optional[int]:
    """Return whole minutes until `game_start`. None if game_start is None.
    Negative when the game is already underway."""
    if game_start is None:
        return None
    if now is None:
        now = timezone.now()
    return int((game_start - now).total_seconds() // 60)


def timing_status(game_start, *, market_warning: bool = False, now=None) -> str:
    """Decide which timing-status badge to show.

    Rules (intentionally simple):
      - No game_start, or game already started → UNAVAILABLE
      - market_warning is True               → HOLD  (signal-driven)
      - ≤ 90 minutes to first pitch          → GOOD_TO_BET
      - otherwise                             → REVIEW_LATER

    HOLD is intentionally signal-gated (not predictive). The only signal
    that triggers it in v1 is the persisted `market_warning` flag — a real,
    already-computed signal of sharp money moving against the pick. We do
    NOT try to forecast line movement here.
    """
    if game_start is None:
        return STATUS_UNAVAILABLE
    if now is None:
        now = timezone.now()
    minutes = (game_start - now).total_seconds() / 60.0
    if minutes < 0:
        return STATUS_UNAVAILABLE  # game already in progress / over
    if market_warning:
        return STATUS_HOLD
    if minutes <= GOOD_TO_BET_WINDOW_MIN:
        return STATUS_GOOD_TO_BET
    return STATUS_REVIEW_LATER


# ---------------------------------------------------------------------------
# Formatting

def format_countdown(game_start, now=None) -> str:
    """Human-readable countdown to first pitch.

    Examples (assuming now is fixed):
      < 60 min:    "Starts in 47m"
      < 24h:       "Starts in 2h 14m"
      ≥ 24h:       "Tomorrow at 1:10 PM" / "Mon at 7:10 PM"
      game past:   "Game in progress"
      no time:     "Game time unavailable"
    """
    if game_start is None:
        return 'Game time unavailable'
    if now is None:
        now = timezone.now()
    delta = game_start - now
    total_seconds = int(delta.total_seconds())
    if total_seconds < 0:
        return 'Game in progress'
    total_min = total_seconds // 60
    if total_min < 60:
        return f'Starts in {total_min}m'
    if total_min < 24 * 60:
        hours, mins = divmod(total_min, 60)
        return f'Starts in {hours}h {mins:02d}m'

    # Same calendar day in the user's local TZ is handled above (it would
    # be < 24h). Beyond 24h: render "Tomorrow at 1:10 PM" when exactly the
    # next calendar day; otherwise "Sat at 7:10 PM".
    local_now = timezone.localtime(now)
    local_start = timezone.localtime(game_start)
    days_ahead = (local_start.date() - local_now.date()).days
    time_part = local_start.strftime('%I:%M %p').lstrip('0')
    if days_ahead == 1:
        return f'Tomorrow at {time_part}'
    return f"{local_start.strftime('%a')} at {time_part}"


def best_review_window(game_start) -> Optional[Tuple[datetime, datetime]]:
    """Return the (earliest, latest) datetimes of the best review window.
    None when game_start is None."""
    if game_start is None:
        return None
    earliest = game_start - timedelta(minutes=BEST_REVIEW_EARLIEST_MIN)
    latest = game_start - timedelta(minutes=BEST_REVIEW_LATEST_MIN)
    return (earliest, latest)


def format_best_review_window(game_start) -> str:
    """Human-readable best review window: '6:05–6:25 PM ET'."""
    window = best_review_window(game_start)
    if window is None:
        return ''
    earliest, latest = window
    e = timezone.localtime(earliest).strftime('%I:%M').lstrip('0')
    l_dt = timezone.localtime(latest)
    l = l_dt.strftime('%I:%M %p').lstrip('0')
    return f'{e}–{l}'


# ---------------------------------------------------------------------------
# Top-level builder used by the template tag

def build_timing_context(recommendation, *, now=None) -> dict:
    """Build everything the panel template needs from a Recommendation
    (dataclass or BettingRecommendation model). Pure read — never mutates
    `recommendation`.

    Returns a dict with: game_start, status_key, status_label, reason,
    countdown, best_window, has_betting_window (False for non-recommended —
    avoids re-introducing the "engine says no but UI says good to bet"
    contradiction we just fixed in the banner).
    """
    if recommendation is None:
        return _empty_context()

    game = getattr(recommendation, 'game', None)
    game_start = game_start_time(game)
    market_warning = bool(getattr(recommendation, 'market_warning', False))

    status_key = timing_status(game_start, market_warning=market_warning, now=now)
    countdown = format_countdown(game_start, now=now)
    best_window = format_best_review_window(game_start)

    # Only show the BETTING WINDOW status row on recommended picks. On a
    # not_recommended card, showing "🟢 Good to Bet Now" would contradict
    # the engine's decision — same class of mistake the banner UX
    # correction was built to eliminate. The countdown stays (informational).
    is_recommended = bool(getattr(recommendation, 'is_recommended', False))

    return {
        'game_start': game_start,
        'status_key': status_key,
        'status_label': STATUS_LABELS.get(status_key, ''),
        'reason': STATUS_REASONS.get(status_key, ''),
        'countdown': countdown,
        'best_window': best_window,
        'has_betting_window': is_recommended and game_start is not None,
        'has_game_start': game_start is not None,
    }


def _empty_context() -> dict:
    return {
        'game_start': None,
        'status_key': STATUS_UNAVAILABLE,
        'status_label': STATUS_LABELS[STATUS_UNAVAILABLE],
        'reason': '',
        'countdown': 'Game time unavailable',
        'best_window': '',
        'has_betting_window': False,
        'has_game_start': False,
    }


# ---------------------------------------------------------------------------
# Chronological section sort (2026-05-30 UX spec)
#
# After the Game Timing panel surfaced first-pitch times on every card, the
# natural user expectation flipped from "show me the engine's ranking" to
# "show me the slate in chronological order — I act on the next game first."
#
# Rules per spec:
#   1. Primary:    game_start_time ASC
#   2. Secondary:  recommendation tier priority ASC (elite > strong > standard)
#   3. Games with no start time render at the bottom of their section
#   4. Sort is INTRA-section only — never collapse sections together
#
# This helper is the single source of truth. Every section partitioner /
# view that builds card lists should call it (or factor through it).

def sort_by_game_start_then_priority(items, *, game_getter, tier_getter=None):
    """Sort `items` chronologically by game start time, with recommendation
    tier as the tiebreaker. Items with no resolvable game start go LAST
    (never crash). Returns a new list — does not mutate input.

    Args:
        items: list of card-like things (dicts, dataclasses, MLB signals).
        game_getter: callable(item) → game-like object (with first_pitch /
                     kickoff / tipoff) or None.
        tier_getter: optional callable(item) → tier string or None. When
                     omitted, the secondary key is constant — sort is
                     purely chronological.

    Sort is stable: Python's sort preserves original ordering on ties beyond
    the supplied keys. That means in practice a `tier_getter` is only
    necessary when two cards share the same start time AND the input order
    is undefined (e.g. coming out of a set).
    """
    from datetime import datetime as _dt, timezone as _utc
    from apps.core.services.recommendations import TIER_ORDER

    # Sentinel for None-game-start so those items reliably land at the
    # bottom while still allowing a stable tier secondary sort.
    LAST_TIER_PRIORITY = max(TIER_ORDER.values()) + 1   # 4 — past 'None': 3
    FAR_FUTURE = _dt.max.replace(tzinfo=_utc.utc)

    def _key(item):
        game = game_getter(item)
        start = game_start_time(game) if game is not None else None
        tier = tier_getter(item) if tier_getter is not None else None
        tier_priority = TIER_ORDER.get(tier, LAST_TIER_PRIORITY)
        if start is None:
            # None bucket = 1, far-future timestamp keeps the secondary key
            # comparison well-defined even if Python ever changes how None
            # compares in mixed tuples.
            return (1, FAR_FUTURE, tier_priority)
        return (0, start, tier_priority)

    return sorted(items, key=_key)
