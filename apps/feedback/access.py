from functools import wraps
from django.shortcuts import render, redirect
from django.contrib import messages

PARTNER_USERNAMES = {'djenkins', 'jsnyder', 'msnyder'}


def is_partner(user):
    """Return True only for authorized partner users."""
    return user.is_authenticated and user.username in PARTNER_USERNAMES


def partner_required(view_func):
    """Decorator that shows access denied message for non-partner users."""
    @wraps(view_func)
    def _wrapped(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect('/accounts/login/')
        if not is_partner(request.user):
            return render(request, 'feedback/denied.html', status=403)
        return view_func(request, *args, **kwargs)
    return _wrapped
