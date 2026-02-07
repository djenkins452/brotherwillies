from django import template
from django.utils import timezone

register = template.Library()


@register.simple_tag
def tz_abbr():
    """Return the abbreviation for the currently active timezone (e.g. CST, EST)."""
    current_tz = timezone.get_current_timezone()
    now = timezone.now()
    return now.astimezone(current_tz).strftime('%Z')
