"""Source-aware trust tier for an OddsSnapshot.

Single canonical function that the recommendation engine, the MLB hub,
and any future surface call to ask "how much should I trust this row?"

Tiers:
    primary    The row came from the paid Odds API. Trust as authoritative.
    secondary  The row came from ESPN's free fallback feed. Trust for
               display + lower-confidence betting; flagged in the UI.
    invalid    The row's moneyline was synthesized (symmetric inversion
               in the ESPN provider). NEVER feeds a recommendation, never
               appears in bulk-bet actions, only visible in staff diags.
    unknown    Defensive default for shapes we don't recognize.

Backward-compatibility note: snapshots written before is_derived existed
return is_derived=False (the field default). A snapshot with
odds_source='espn' and is_derived=False is treated as 'secondary', which
matches the pre-derivation reality (ESPN was either both-sided or skipped).
"""
from __future__ import annotations

from typing import Optional


# UI badge / label copy. Centralised so the hub template, the game-detail
# page, and any future surface stay in sync.
TRUST_TIER_BADGE = {
    'primary':   {'icon': '🟢', 'label': 'Verified Odds'},
    'secondary': {'icon': '🟡', 'label': 'ESPN Odds'},
    'invalid':   {'icon': '🔴', 'label': 'Derived Odds'},
    'unknown':   {'icon': '⚪', 'label': 'Unknown Source'},
}

# Confidence multiplier applied to the *displayed* confidence for
# secondary-tier recommendations. Spec value (v1). The model's edge math
# is unaffected — only the user-facing display number scales down.
SECONDARY_CONFIDENCE_MULTIPLIER = 0.85


def get_odds_trust_tier(snapshot) -> str:
    """Classify a snapshot. Defensive against missing fields."""
    if snapshot is None:
        return 'unknown'
    # Derived takes precedence over source: an ESPN row with one side
    # synthesized is 'invalid' even though odds_source == 'espn'.
    if getattr(snapshot, 'is_derived', False):
        return 'invalid'
    source = getattr(snapshot, 'odds_source', None)
    if source == 'odds_api':
        return 'primary'
    if source == 'espn':
        return 'secondary'
    return 'unknown'


def trust_badge(tier: Optional[str]) -> dict:
    """UI-ready badge dict for a tier. Returns the 'unknown' fallback for
    any tier the helper doesn't know — never raises."""
    return TRUST_TIER_BADGE.get(tier or 'unknown', TRUST_TIER_BADGE['unknown'])


def secondary_confidence_multiplier() -> float:
    return SECONDARY_CONFIDENCE_MULTIPLIER
