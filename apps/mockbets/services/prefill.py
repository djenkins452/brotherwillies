"""Mock-bet pre-fill helpers.

Given an enriched `GameSignals` object (from `apps.mlb.services.prioritization`)
return a dict the existing `openMockBetModal(...)` JS function consumes. The
goal is: one click → modal opens with the smart-default selection, bet type,
and odds already populated. The user can still override anything.

Design rules:
    - Pure function. No DB writes, no HTTP.
    - Never fabricate odds. If no snapshot exists we emit bet_type='moneyline'
      with no pre-filled odds; the user types them.
    - Default selection side is the team with the higher-rated starter —
      pitching is the model's dominant driver. Tie or TBD → home team.
    - If the market already has a tight spread (<=1.5), prefer a spread bet
      since that's where live MLB value most often shows up.
"""
from __future__ import annotations

from typing import Any


def _default_team_side(game, signals) -> str:
    """'home' or 'away' — whichever has the stronger inferred edge."""
    hp, ap = game.home_pitcher, game.away_pitcher
    if hp is not None and ap is not None and ap.rating - hp.rating > 5:
        return 'away'
    return 'home'


def _team_label(game, side: str) -> str:
    team = game.home_team if side == 'home' else game.away_team
    return team.name


def _spread_selection(game, side: str, spread_home_pov: float) -> str:
    """Build 'TeamName -1.5' / '+1.5' from a home-POV spread."""
    # OddsSnapshot.spread is stored home-POV: negative means home favored.
    if side == 'home':
        n = spread_home_pov
    else:
        n = -spread_home_pov
    sign = '-' if n < 0 else '+'
    return f"{_team_label(game, side)} {sign}{abs(n):g}"


# Default per-side juice for spread + total markets when the snapshot
# doesn't expose individual prices. OddsSnapshot stores only the line
# value (spread / total) — sportsbooks almost universally price both
# sides of the run line / O-U at -110, so this is the right default.
# If we later persist actual per-side prices, swap this for the row's
# value where present and fall back to -110 only when missing.
SPREAD_TOTAL_DEFAULT_ODDS = -110


def _build_selections(game, odds, *, moneyline_only: bool = False) -> dict[str, list[dict]]:
    """Per-bet-type selection options for the modal dropdown.

    Shape: {bet_type: [{'value': str, 'label': str, 'odds': int|None}, ...]}.

    2026-04-29 fix: each option now carries its `odds` so the modal can
    auto-populate the Odds field when the user picks a selection. Before
    this, the form's Odds field could be visually pre-filled with one
    side's odds while the user picked the other side, causing the bet
    to submit with the wrong number — or, worse, with a stale
    placeholder of "-110" that wasn't actually in form state, leading
    to validation rejection.

    2026-05-04: when `moneyline_only` is True, the spread/total entries
    are omitted from the dict regardless of snapshot data. Belt and
    suspenders alongside the master switch's other gates — even if a
    stale client retains an old selection list, server-side place_bet
    enforcement is the final safety bar.

    Odds source per bet type:
        moneyline   → snapshot.moneyline_home / moneyline_away
        spread      → SPREAD_TOTAL_DEFAULT_ODDS (-110); we don't persist
                      per-side run-line prices yet
        total       → SPREAD_TOTAL_DEFAULT_ODDS (-110); same reason

    Only bet types with meaningful options are included — if the market
    doesn't expose a spread, the 'spread' key is omitted and the modal
    falls back to the free-text input for that bet type.
    """
    home_label = game.home_team.name
    away_label = game.away_team.name

    home_ml = (
        int(odds.moneyline_home)
        if odds is not None and odds.moneyline_home is not None
        else None
    )
    away_ml = (
        int(odds.moneyline_away)
        if odds is not None and odds.moneyline_away is not None
        else None
    )

    out: dict[str, list[dict]] = {
        'moneyline': [
            {'value': away_label, 'label': away_label, 'odds': away_ml},
            {'value': home_label, 'label': home_label, 'odds': home_ml},
        ],
    }

    # Under MONEYLINE_ONLY_MODE the modal hides spread/total options at
    # the template layer, but a stale client could still consume this
    # list. Omitting the keys here means even a hand-crafted client
    # never sees a spread/total selection from the prefill.
    if not moneyline_only and odds is not None and odds.spread is not None:
        # OddsSnapshot.spread is home-POV (negative = home favored).
        home_num = odds.spread
        away_num = -odds.spread
        home_sign = '-' if home_num < 0 else '+'
        away_sign = '-' if away_num < 0 else '+'
        home_sel = f"{home_label} {home_sign}{abs(home_num):g}"
        away_sel = f"{away_label} {away_sign}{abs(away_num):g}"
        out['spread'] = [
            {'value': away_sel, 'label': away_sel, 'odds': SPREAD_TOTAL_DEFAULT_ODDS},
            {'value': home_sel, 'label': home_sel, 'odds': SPREAD_TOTAL_DEFAULT_ODDS},
        ]

    if not moneyline_only and odds is not None and odds.total is not None:
        total = odds.total
        out['total'] = [
            {'value': f"Over {total:g}", 'label': f"Over {total:g}",
             'odds': SPREAD_TOTAL_DEFAULT_ODDS},
            {'value': f"Under {total:g}", 'label': f"Under {total:g}",
             'odds': SPREAD_TOTAL_DEFAULT_ODDS},
        ]

    return out


def prefill_from_signals(signals) -> dict[str, Any]:
    """Return a JSON-serializable dict for `openMockBetModal(opts)`.

    The returned shape is keyed to match the modal's existing contract
    (sport, game_id, bet_type, selection, odds) plus `selections_by_type`
    for the dynamic dropdown.

    2026-05-04: under MONEYLINE_ONLY_MODE the prefill ALWAYS defaults to
    a moneyline bet — the legacy "tight spread → prefer spread" branch
    would otherwise pre-fill a spread selection that the place_bet view
    rejects with `Invalid bet type`. The fix gates the spread-default at
    the source rather than masking it at the form layer.
    """
    from apps.core.config import is_moneyline_only_mode
    moneyline_only = is_moneyline_only_mode()

    game = signals.game
    odds = signals.latest_odds
    side = _default_team_side(game, signals)

    bet_type = 'moneyline'
    selection = _team_label(game, side)
    odds_american = None

    if odds is not None:
        # Tight spread → prefer a spread bet — but only when the master
        # ML-only flag is OFF. With ML-only on, we always stay on
        # moneyline so the modal can submit cleanly.
        if (
            not moneyline_only
            and odds.spread is not None
            and abs(odds.spread) <= 1.5
        ):
            bet_type = 'spread'
            selection = _spread_selection(game, side, odds.spread)
        else:
            ml = odds.moneyline_home if side == 'home' else odds.moneyline_away
            if ml is not None:
                odds_american = int(ml)

    result: dict[str, Any] = {
        'sport': 'mlb',
        'game_id': str(game.id),
        'bet_type': bet_type,
        'selection': selection,
        'selections_by_type': _build_selections(game, odds, moneyline_only=moneyline_only),
    }
    if odds_american is not None:
        result['odds'] = odds_american
    return result
