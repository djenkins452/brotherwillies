"""MLB views. Filled in during Phase 4."""
from django.shortcuts import render, get_object_or_404
from django.utils import timezone
from .models import Game, Team


def mlb_hub(request):
    now = timezone.now()
    upcoming = (
        Game.objects.filter(first_pitch__gte=now, status='scheduled')
        .select_related('home_team', 'away_team', 'home_pitcher', 'away_pitcher')
        .order_by('first_pitch')[:20]
    )
    live = (
        Game.objects.filter(status='live')
        .select_related('home_team', 'away_team', 'home_pitcher', 'away_pitcher')
    )
    return render(request, 'mlb/hub.html', {
        'upcoming_games': upcoming,
        'live_games': live,
        'teams': Team.objects.select_related('conference').all(),
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
    })
