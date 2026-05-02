"""Master feature-flag config.

Single source of truth for the Moneyline-Only Mode master switch and any
flags that must AND-compose with it. Every other module reads through these
helpers — never `getattr(settings, 'MONEYLINE_ONLY_MODE', ...)` directly.
That discipline is what lets us flip the master switch back to False later
and have every dependent surface restore in one place.

Default: True. The mode is a temporary system-wide focus, not a permanent
deletion. Setting it False (in settings.py or via override_settings in tests)
restores full spread/total behavior — gated, of course, by the per-feature
flags it AND-composes with.
"""
from __future__ import annotations

from django.conf import settings


def is_moneyline_only_mode() -> bool:
    """Master switch. When True:
      - the recommendation pipeline ignores spread/total
      - the placement layer rejects non-moneyline bets
      - analytics + system-tuning operate on moneyline rows only
      - all spread/total UI is hidden
    """
    return getattr(settings, 'MONEYLINE_ONLY_MODE', True)


def is_spread_total_enabled() -> bool:
    """Phase-1 spread/total opportunity signals (the lobby/hub badge layer).

    Active iff master is OFF *and* the per-feature flag is ON. The master
    flag wins regardless of the per-feature value — this is the primary
    centralization point referenced by the spec ("Apply control at the
    highest level possible. Avoid scattered logic.").
    """
    if is_moneyline_only_mode():
        return False
    return bool(getattr(settings, 'SPREAD_TOTAL_SIGNALS_ENABLED', False))


def is_spread_total_leans_enabled() -> bool:
    """Phase-2 lean intelligence (yellow 🟡 Lean badge layer)."""
    if is_moneyline_only_mode():
        return False
    return bool(getattr(settings, 'SPREAD_TOTAL_LEANS_ENABLED', False))


def is_spread_total_recommendations_enabled() -> bool:
    """Phase-3 promoted recommendations (green 🟢 Recommended layer + bulk
    Bet All Spread/Total buttons)."""
    if is_moneyline_only_mode():
        return False
    return bool(getattr(settings, 'SPREAD_TOTAL_RECOMMENDATIONS_ENABLED', False))
