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
