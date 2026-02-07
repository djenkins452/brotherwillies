import zoneinfo
from django.utils import timezone


class UserTimezoneMiddleware:
    """Activate the user's timezone for each request so Django's template
    date filters render times in the user's local timezone."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.user.is_authenticated:
            try:
                tz_name = request.user.profile.timezone
                if tz_name:
                    timezone.activate(zoneinfo.ZoneInfo(tz_name))
                else:
                    timezone.deactivate()
            except Exception:
                timezone.deactivate()
        else:
            timezone.deactivate()

        response = self.get_response(request)
        return response
