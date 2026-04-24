"""Odds-math primitives.

Single source of truth for converting between American / decimal / implied
probability and for de-vigging two-way markets. Every service that needs
these conversions should import from here.

Why this matters: the raw "market implied probability" from American odds
includes the sportsbook's overround (vig). A -110/-110 line has implied
probabilities of 52.38% / 52.38% = 104.76% total. Computing edge against
the raw 52.38% undercounts the vig; edge against the fair (de-vigged) 50%
is what actually maps to expected value.
"""
from typing import Optional


def american_to_implied_prob(american: int) -> float:
    """Implied probability from American odds, still vig-included.

    +120 → 0.4545, -150 → 0.60. This is the SAME value sportsbooks quote —
    use devig_moneyline_prob to strip the overround before computing edge.
    """
    if american > 0:
        return 100.0 / (american + 100.0)
    return abs(american) / (abs(american) + 100.0)


def american_to_decimal(american: int) -> float:
    """American → decimal (European) odds. +120 → 2.20, -150 → 1.667."""
    if american > 0:
        return 1.0 + (american / 100.0)
    return 1.0 + (100.0 / abs(american))


def devig_moneyline_prob(implied_home: float, implied_away: float) -> float:
    """Remove vig from a two-way moneyline to get the fair home-win probability.

    Fair home prob = implied_home / (implied_home + implied_away). This
    assumes the sportsbook's overround is distributed proportionally across
    both sides (the "proportional" or "basic" de-vig method). More
    sophisticated methods (Shin, power) exist but proportional is standard
    for moneyline within 10% overrounds.

    Falls back to the raw `implied_home` when only one side is available
    (total == 0 is unreachable for real markets, but defensive).
    """
    total = implied_home + implied_away
    if total == 0:
        return implied_home
    return implied_home / total


def devig_two_way(implied_a: float, implied_b: float) -> tuple:
    """Convenience: return both fair probabilities for a two-way market."""
    total = implied_a + implied_b
    if total == 0:
        return implied_a, implied_b
    return implied_a / total, implied_b / total


def closing_line_value(bet_odds_american: int, closing_odds_american: int) -> float:
    """Closing Line Value — positive when your bet's price beat the close.

    CORRECTNESS NOTE: the incoming spec wrote `close_dec - bet_dec` with
    "positive CLV = you beat the market", which is backwards. You beat the
    market by GETTING a better price, which in decimal odds means your
    bet_dec is HIGHER than close_dec. So positive-CLV semantics require
    `bet_dec - close_dec`. Implemented that way here.

    Returns decimal-odds units. Example: bet at +120 (dec 2.20), closed at
    +110 (dec 2.10) → +0.10 (you beat the line). Bet at +120, closed at
    +130 (dec 2.30) → -0.10 (market moved against you).
    """
    return round(
        american_to_decimal(bet_odds_american) - american_to_decimal(closing_odds_american),
        4,
    )
