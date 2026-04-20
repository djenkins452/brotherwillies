"""MLB views."""
from django.shortcuts import render, get_object_or_404
from django.utils import timezone

from .models import Game, Team
from .services.prioritization import prioritize, sort_live, sort_today


def mlb_hub(request):
    now = timezone.now()
    # "Today" respects the viewer's timezone (UserTimezoneMiddleware activates it).
    today_local = timezone.localdate()

    base_qs = Game.objects.select_related(
        'home_team', 'away_team', 'home_pitcher', 'away_pitcher'
    ).prefetch_related('odds_snapshots', 'injuries')

    live_qs = base_qs.filter(status='live')
    upcoming_qs = base_qs.filter(first_pitch__gte=now, status='scheduled').order_by('first_pitch')

    today_upcoming = [g for g in upcoming_qs if timezone.localtime(g.first_pitch).date() == today_local]
    future_upcoming = [g for g in upcoming_qs if timezone.localtime(g.first_pitch).date() != today_local][:30]

    live_tiles = sort_live(prioritize(live_qs, user=request.user))
    today_tiles = sort_today(prioritize(today_upcoming, user=request.user))

    return render(request, 'mlb/hub.html', {
        'live_tiles': live_tiles,
        'today_tiles': today_tiles,
        'future_games': future_upcoming,
        'teams': Team.objects.select_related('conference').all(),
        'nav_active': 'mlb',
        'help_key': 'mlb_hub',
    })


def game_detail(request, game_id):
    game = get_object_or_404(
        Game.objects.select_related('home_team', 'away_team', 'home_pitcher', 'away_pitcher'),
        id=game_id,
    )
    from apps.mlb.services.model_service import compute_game_data
    data = compute_game_data(game, request.user)
    return render(request, 'mlb/game_detail.html', {
        'game': game,
        'data': data,
        'nav_active': 'mlb',
        'help_key': 'mlb_game',
    })
