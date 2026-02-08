from functools import wraps
from django.http import Http404

PARTNER_USERNAMES = {'djenkins', 'jsnyder', 'msnyder'}


def is_partner(user):
    """Return True only for authorized partner users."""
    return user.is_authenticated and user.username in PARTNER_USERNAMES


def partner_required(view_func):
    """Decorator that returns 404 for non-partner users."""
    @wraps(view_func)
    def _wrapped(request, *args, **kwargs):
        if not is_partner(request.user):
            raise Http404
        return view_func(request, *args, **kwargs)
    return _wrapped
