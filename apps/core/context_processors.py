"""Template context processors.

The moneyline-only flag goes through every template that conditionally hides
spread/total UI. Wiring it as a context processor avoids per-view boilerplate
and guarantees the value comes from the same `is_moneyline_only_mode()`
helper as the server-side gates — no drift between "what the server enforces"
and "what the template renders."
"""
from apps.core.config import (
    is_moneyline_only_mode,
    is_spread_total_enabled,
    is_spread_total_recommendations_enabled,
)


def feature_flags(request):
    """Expose master + composed flags to every template."""
    return {
        'MONEYLINE_ONLY_MODE': is_moneyline_only_mode(),
        'SPREAD_TOTAL_ENABLED': is_spread_total_enabled(),
        'SPREAD_TOTAL_RECOMMENDATIONS_ENABLED': is_spread_total_recommendations_enabled(),
    }
