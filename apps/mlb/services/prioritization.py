"""MLB game prioritization signals.

Transforms raw Game rows into enriched `GameSignals` objects with a
`priority` bucket ('high' | 'medium' | 'low'), a numeric `priority_score`
for stable sorting, and a list of human-readable `reasons` explaining
*why* a game was elevated.

Design:
    - Pure functions. No view coupling, no request object.
    - Each signal is a small function returning (score_contribution, reason_or_none).
    - A single WEIGHTS table makes signals individually tunable and easy
      to extend (user favorites, odds movement, playoff importance, etc.).
    - Unknown / partial data never raises — missing inputs contribute 0.

Bucket thresholds (after summing weighted contributions):
    score >= 3.0  -> 'high'
    1.5 <= score  -> 'medium'
    else          -> 'low'

Sort keys the view uses:
    Live:       (priority_score desc)                # inning data not yet ingested
    Today:      (priority_score desc, first_pitch asc)
    Future:     (first_pitch asc)   [unchanged; list format]
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Optional

from django.utils import timezone


# --- tunable weights ---------------------------------------------------------

WEIGHTS = {
    'tight_spread': 2.0,        # within 1.5 runs
    'moderate_spread': 1.0,     # within 2.5 runs
    'close_live_score': 2.0,    # within 2 runs while live
    'blowout_live_score': -1.5, # 6+ run margin while live
    'high_injury': 1.5,
    'med_injury': 0.5,
    'ace_matchup': 1.2,         # both starters rating >= 65
    'pitcher_tbd': -1.0,        # confidence penalty
    # Future extension seams (return 0 today):
    'favorite_team': 2.0,
    'odds_movement': 1.5,
    'game_importance': 2.0,
}

HIGH_CUTOFF = 3.0
MEDIUM_CUTOFF = 1.5


@dataclass
class GameSignals:
    """Enriched game context for the MLB hub tiles.

    `game` is the original Game model — templates may still reach into
    `game.home_team`, `game.first_pitch`, etc. for rendering.
    """
    game: object
    priority: str                   # 'high' | 'medium' | 'low'
    priority_score: float
    reasons: list[str] = field(default_factory=list)
    latest_odds: object | None = None
    injury_summary: dict = field(default_factory=dict)  # {'home': 'high'|None, 'away': ...}
    ace_matchup: bool = False
    pitchers_known: bool = True


# --- individual signals ------------------------------------------------------

def _spread_signal(odds) -> tuple[float, Optional[str]]:
    if odds is None or odds.spread is None:
        return 0.0, None
    mag = abs(odds.spread)
    if mag <= 1.5:
        return WEIGHTS['tight_spread'], f"Tight spread ({mag:g})"
    if mag <= 2.5:
        return WEIGHTS['moderate_spread'], f"Close spread ({mag:g})"
    return 0.0, None


def _live_score_signal(game) -> tuple[float, Optional[str]]:
    if game.status != 'live' or game.home_score is None or game.away_score is None:
        return 0.0, None
    margin = abs(game.home_score - game.away_score)
    if margin <= 2:
        return WEIGHTS['close_live_score'], f"Close game ({game.away_score}-{game.home_score})"
    if margin >= 6:
        return WEIGHTS['blowout_live_score'], None  # no reason — negative
    return 0.0, None


def _injury_signal(injuries) -> tuple[float, Optional[str], dict]:
    summary = {'home': None, 'away': None}
    contrib = 0.0
    reason = None
    for inj in injuries:
        team_side = 'home' if inj.team_id == inj.game.home_team_id else 'away'
        current = summary[team_side]
        if inj.impact_level == 'high' and current != 'high':
            summary[team_side] = 'high'
        elif inj.impact_level == 'med' and current is None:
            summary[team_side] = 'med'
        elif inj.impact_level == 'low' and current is None:
            summary[team_side] = 'low'
    has_high = 'high' in summary.values()
    has_med = 'med' in summary.values()
    if has_high:
        contrib = WEIGHTS['high_injury']
        reason = "High injury impact"
    elif has_med:
        contrib = WEIGHTS['med_injury']
        reason = "Medium injury impact"
    return contrib, reason, summary


def _pitcher_signal(game) -> tuple[float, Optional[str], bool, bool]:
    """Returns (contribution, reason, ace_matchup, both_known)."""
    hp, ap = game.home_pitcher, game.away_pitcher
    if hp is None or ap is None:
        return WEIGHTS['pitcher_tbd'], None, False, False
    if hp.rating >= 65 and ap.rating >= 65:
        return WEIGHTS['ace_matchup'], "Ace pitching matchup", True, True
    return 0.0, None, False, True


# --- future extension seams (no-op today) -----------------------------------

def _favorite_team_signal(game, user) -> tuple[float, Optional[str]]:
    """Boost games featuring the user's favorite teams.

    Placeholder — hook up when a UserPreferences.favorite_mlb_teams field
    exists. Returning 0 keeps the sort stable for anonymous users.
    """
    return 0.0, None


def _odds_movement_signal(game) -> tuple[float, Optional[str]]:
    """Boost games where the spread or total has moved meaningfully.

    Placeholder — requires at least two OddsSnapshots spaced in time to
    compute delta. Returns 0 until we start retaining historical snapshots.
    """
    return 0.0, None


def _game_importance_signal(game) -> tuple[float, Optional[str]]:
    """Boost rivalry / division / playoff games.

    Placeholder — no importance tagging in the data model yet.
    """
    return 0.0, None


# --- bucketing ---------------------------------------------------------------

def _bucket(score: float) -> str:
    if score >= HIGH_CUTOFF:
        return 'high'
    if score >= MEDIUM_CUTOFF:
        return 'medium'
    return 'low'


# --- public API --------------------------------------------------------------

def build_signals(game, *, user=None) -> GameSignals:
    """Compute signals for a single game. Pure function — no DB writes."""
    latest_odds = game.odds_snapshots.order_by('-captured_at').first()
    # Load injuries with .game set for the FK lookup below. Callers that
    # prefetch injuries pay no extra query here.
    injuries = list(game.injuries.all())
    for inj in injuries:
        # Avoid extra queries inside _injury_signal — attach the parent ref.
        inj.game = game

    score = 0.0
    reasons: list[str] = []

    s, r = _spread_signal(latest_odds)
    score += s
    if r:
        reasons.append(r)

    s, r = _live_score_signal(game)
    score += s
    if r:
        reasons.append(r)

    s, r, injury_summary = _injury_signal(injuries)
    score += s
    if r:
        reasons.append(r)

    s, r, ace, known = _pitcher_signal(game)
    score += s
    if r:
        reasons.append(r)

    # Future seams — contribute 0 today.
    score += _favorite_team_signal(game, user)[0]
    score += _odds_movement_signal(game)[0]
    score += _game_importance_signal(game)[0]

    return GameSignals(
        game=game,
        priority=_bucket(score),
        priority_score=round(score, 3),
        reasons=reasons,
        latest_odds=latest_odds,
        injury_summary=injury_summary,
        ace_matchup=ace,
        pitchers_known=known,
    )


def prioritize(games: Iterable, *, user=None) -> list[GameSignals]:
    """Enrich a list of games with signals. Order is preserved — caller sorts."""
    return [build_signals(g, user=user) for g in games]


def sort_live(signals: list[GameSignals]) -> list[GameSignals]:
    """Live sort: priority desc only. Inning/progression not yet ingested."""
    return sorted(signals, key=lambda s: -s.priority_score)


def sort_today(signals: list[GameSignals]) -> list[GameSignals]:
    """Today sort: priority desc, then earliest first_pitch first."""
    return sorted(signals, key=lambda s: (-s.priority_score, s.game.first_pitch))
