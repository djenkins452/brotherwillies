from apps.accounts.models import UserProfile


def user_profile(request):
    """Make the user's profile available in all templates safely."""
    if request.user.is_authenticated:
        profile, _ = UserProfile.objects.get_or_create(user=request.user)
        return {'user_profile': profile}
    return {'user_profile': None}
