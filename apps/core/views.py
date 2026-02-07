from django.shortcuts import render
from apps.cfb.models import Game
from apps.cfb.services.model_service import compute_game_data
from django.utils import timezone


def home(request):
    upcoming = Game.objects.filter(
        kickoff__gte=timezone.now(), status='scheduled'
    ).select_related('home_team', 'away_team').order_by('kickoff')[:5]

    games_data = []
    for game in upcoming:
        data = compute_game_data(game, request.user if request.user.is_authenticated else None)
        games_data.append(data)

    games_data.sort(key=lambda g: abs(g.get('house_edge', 0)), reverse=True)

    return render(request, 'core/home.html', {
        'games_data': games_data[:5],
        'help_key': 'home',
        'nav_active': 'home',
    })
