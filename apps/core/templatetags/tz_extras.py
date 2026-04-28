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
