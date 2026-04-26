"""Context processor for the header system-status dot.

Why a context processor: the dot lives in `base.html`, which is rendered by
every view. Threading the snapshot through every view's context dict would
be invasive. A context processor is the standard Django escape hatch.

Cost control:
  - Returns an empty dict for non-superusers, so the snapshot is never
    computed for the 99%+ of requests that won't show the dot.
  - For superusers, calls build_snapshot() once per request. Each snapshot
    is ~5–6 indexed lookups against bounded recent windows (24h / 7d) on
    append-only tables — fast in practice. If profiling shows otherwise
    we can add a short-TTL cache here without touching the template.
  - Wrapped in a broad try/except so a snapshot failure can NEVER take
    down the header. On error we silently fall back to "unknown" status,
    same as the dot would render before any data has been captured.
"""
import logging

logger = logging.getLogger(__name__)

# Map snapshot health → glyph used by hovering screen-reader text + tooltip.
# The colored dot is drawn in CSS; the glyph is for accessibility / tooltips.
_HEALTH_GLYPH = {
    'green': '🟢',
    'yellow': '🟡',
    'red': '🔴',
    'unknown': '⚪',
}

_HEALTH_LABEL = {
    'green': 'Healthy',
    'yellow': 'Warnings',
    'red': 'Needs Attention',
    'unknown': 'No Data',
}


def ops_status(request):
    """Inject ops_status_color / ops_status_tooltip into the template context
    for superusers. Empty dict for everyone else."""
    user = getattr(request, 'user', None)
    if not user or not user.is_authenticated or not user.is_superuser:
        return {}

    try:
        # Local import — context processors load at app startup; importing
        # apps.ops.services.command_center at module level can race with
        # Django's app registry on cold boot.
        from apps.ops.services.command_center import build_snapshot

        snap = build_snapshot()
        color = snap.overall.health
        # Tooltip kept tight (3 lines, under 200 chars) so browsers don't
        # truncate. Title attributes word-wrap inconsistently across OSes,
        # so we keep each line short and informative.
        api_label = snap.api.label or 'API: no data yet'
        cron_label = snap.cron.label or 'Cron: no run history yet'
        tooltip = (
            f'System Status: {_HEALTH_LABEL.get(color, color.title())}\n'
            f'Odds API — {api_label}\n'
            f'Cron — {cron_label}'
        )

        return {
            'ops_status_color': color,
            'ops_status_glyph': _HEALTH_GLYPH.get(color, '⚪'),
            'ops_status_tooltip': tooltip,
        }
    except Exception as exc:  # noqa: BLE001 — header must never crash
        logger.warning('ops_status context processor failed: %s', exc)
        return {
            'ops_status_color': 'unknown',
            'ops_status_glyph': '⚪',
            'ops_status_tooltip': 'System Status: unavailable',
        }
