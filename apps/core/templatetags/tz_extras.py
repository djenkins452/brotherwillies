from django import template
from django.utils import timezone

register = template.Library()


@register.simple_tag
def tz_abbr():
    """Return the abbreviation for the currently active timezone (e.g. CST, EST)."""
    current_tz = timezone.get_current_timezone()
    now = timezone.now()
    return now.astimezone(current_tz).strftime('%Z')


@register.filter
def decimal_pct(value, places=2):
    """Format a decimal share as a percentage string. 0.55 → "55.00%".

    Used by the backtest results template — backtest summary metrics are
    stored in decimal form (0.06 = 6%) so the JSON shape stays uniform,
    but the UI wants a human-readable percentage.
    """
    if value is None or value == '':
        return '—'
    try:
        return f"{float(value) * 100:.{int(places)}f}%"
    except (TypeError, ValueError):
        return '—'


@register.filter
def dict_get(d, key):
    """Look up a key in a dict from a template.

    Django templates can't do `dict["literal-with-dashes"]` because the
    parser treats `-` as a hyphen. This filter wraps the lookup so
    callers can fetch buckets like `0.6-0.65` from the calibration_curve
    JSON without a custom template tag per call site.

    Returns None when the key is absent or the value isn't dict-like.
    """
    if not hasattr(d, 'get'):
        return None
    return d.get(key)


@register.filter
def decimal_signed(value, places=4):
    """Format a signed decimal-odds delta. None → '—'."""
    if value is None or value == '':
        return '—'
    try:
        return f"{float(value):+.{int(places)}f}"
    except (TypeError, ValueError):
        return '—'


@register.filter
def spread_display(spread, side):
    """Format spread for home or away team. Spread is stored from home perspective.
    Usage: {{ odds.spread|spread_display:"home" }} or {{ odds.spread|spread_display:"away" }}
    """
    if spread is None:
        return ''
    val = float(spread) if side == 'home' else -float(spread)
    if val > 0:
        return '+{:.1f}'.format(val)
    elif val == 0:
        return 'PK'
    else:
        return '{:.1f}'.format(val)


@register.simple_tag
def game_timing(recommendation):
    """Build the Game Timing panel context for a Recommendation (dataclass
    or BettingRecommendation row).

    Returns a dict the panel template renders directly. Pure read —
    no recommendation-engine fields are mutated. Safe to call with None.

    Usage:
        {% game_timing recommendation as timing %}
        {% include 'core/includes/game_timing_panel.html' with timing=timing %}
    """
    from apps.core.services.game_timing import build_timing_context
    return build_timing_context(recommendation)
