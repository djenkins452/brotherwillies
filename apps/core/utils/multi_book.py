"""Multi-book odds helpers + stale-odds detection.

Pure functions. Sport-agnostic — accept any Game whose model exposes the
`odds_snapshots` related manager (CFB / CBB / MLB / college_baseball today).
No DB writes, no recommendation-engine integration. The engine continues to
read its single-snapshot path; these helpers are additive infrastructure for
future upgrades and the System Tuning surface.

Design note: consensus deliberately averages raw `market_home_win_prob`
without per-book de-vigging. The average across books partially neutralizes
vig, and per-book de-vig can be added later when the engine actually consumes
this output. Document the simplification rather than hide it.
"""
from __future__ import annotations

from datetime import timedelta
from typing import Optional

from django.utils import timezone

from apps.core.utils.odds import american_to_implied_prob


# Sport-key → name of the DateTimeField that marks game start. Same lookup
# table is duplicated in services/clv.py; do not reuse it from there to keep
# this module dependency-free of the mockbets app.
_START_FIELDS = ('first_pitch', 'kickoff', 'tipoff')


def _start_time(game):
    """Return the game's scheduled start datetime, or None if unknown."""
    for attr in _START_FIELDS:
        val = getattr(game, attr, None)
        if val is not None:
            return val
    return None


def get_latest_snapshots_for_game(game) -> list:
    """One snapshot per distinct sportsbook — the most recent per book.

    Excludes is_derived rows (synthesized from one-sided ESPN data) so they
    can't pollute consensus / best-price math. Returns an empty list when
    the game has no usable snapshots.
    """
    if game is None:
        return []
    qs = game.odds_snapshots.filter(is_derived=False).order_by('sportsbook', '-captured_at')
    seen: set = set()
    latest = []
    for snap in qs:
        if snap.sportsbook in seen:
            continue
        seen.add(snap.sportsbook)
        latest.append(snap)
    return latest


def get_consensus_prob(game) -> Optional[float]:
    """Average `market_home_win_prob` across the latest snapshot per book.

    Returns None when no usable snapshots exist. Single-book games return that
    book's prob unchanged (the average of one number is itself).
    """
    snaps = [s for s in get_latest_snapshots_for_game(game) if s.market_home_win_prob is not None]
    if not snaps:
        return None
    return sum(s.market_home_win_prob for s in snaps) / len(snaps)


def get_best_price(game, side: str):
    """Return (american_odds, sportsbook) with the lowest implied probability
    for the requested side — i.e. the most favorable price to the bettor.

    Implied-prob comparison sidesteps the +100/-100 American-odds discontinuity:
    +110 (47.6% implied) is unambiguously better than -110 (52.4%) on the same
    side, regardless of sign. Returns None when no book has a moneyline for the
    requested side.
    """
    if side not in ('home', 'away'):
        raise ValueError(f"side must be 'home' or 'away', got {side!r}")
    field = 'moneyline_home' if side == 'home' else 'moneyline_away'

    best_odds = None
    best_book = None
    best_implied = None
    for snap in get_latest_snapshots_for_game(game):
        odds = getattr(snap, field, None)
        if odds is None:
            continue
        implied = american_to_implied_prob(odds)
        if implied is None:
            continue
        if best_implied is None or implied < best_implied:
            best_implied = implied
            best_odds = odds
            best_book = snap.sportsbook
    if best_odds is None:
        return None
    return (best_odds, best_book)


# --- Source attribution for bet placement -----------------------------------

def get_odds_source_for_game(game) -> str:
    """Return the `odds_source` of the most recent snapshot on this game.

    Used at bet-placement time to denormalize provenance onto MockBet. Falls
    back to 'unknown' when:
      - game is None (e.g. golf bets, where we don't carry a Game FK)
      - the game has no snapshots
      - the latest snapshot's odds_source is empty/unset

    The returned string is one of OddsSnapshot.SNAPSHOT_SOURCE_CHOICES values
    (e.g. 'odds_api', 'espn'), which is intentionally a subset of the
    MockBet.ODDS_SOURCE_CHOICES vocabulary.
    """
    if game is None:
        return 'unknown'
    latest = game.odds_snapshots.order_by('-captured_at').first()
    if latest is None:
        return 'unknown'
    return getattr(latest, 'odds_source', None) or 'unknown'


# --- Stale-odds detection ---------------------------------------------------

def is_odds_stale(game, threshold_minutes: int = 30) -> bool:
    """True when a pre-game's latest snapshot is older than the threshold AND
    the game starts within the threshold window.

    "Stale" semantically means: we are inside the critical pre-game window where
    odds typically converge to the close, AND we don't have a recent enough
    capture to trust. Outside the window (e.g. 4 hours pre-game with a 6-hour-old
    snapshot) the situation is "old" but not "stale" because there's still time
    to refresh. After kickoff, the question is moot.

    Returns False — never True — when:
      - the game has no scheduled start time
      - the game has already started
      - no odds snapshots exist at all (separate signal: 'no data', not 'stale')
    """
    if game is None:
        return False
    start = _start_time(game)
    if start is None:
        return False

    now = timezone.now()
    if start <= now:
        return False  # game already started — out of scope for "stale"

    # Only flag inside the pre-game window. A 6-hour-old snapshot at 4 hours
    # pre-game is acceptable; a 6-hour-old snapshot at 5 minutes pre-game is not.
    window = timedelta(minutes=threshold_minutes)
    if start - now > window:
        return False

    latest = game.odds_snapshots.order_by('-captured_at').first()
    if latest is None:
        return False  # no data is its own signal; not "stale"

    age = now - latest.captured_at
    return age > window


def count_stale_games(games_qs, threshold_minutes: int = 30) -> int:
    """Count games in a queryset/iterable that are currently flagged stale."""
    return sum(1 for g in games_qs if is_odds_stale(g, threshold_minutes))
