from django.shortcuts import render
from django.utils import timezone
from apps.cfb.models import Game as CFBGame
from apps.cfb.services.model_service import compute_game_data as cfb_compute
from apps.cbb.models import Game as CBBGame
from apps.cbb.services.model_service import compute_game_data as cbb_compute


def home(request):
    user = request.user if request.user.is_authenticated else None

    # CFB games
    cfb_upcoming = CFBGame.objects.filter(
        kickoff__gte=timezone.now(), status='scheduled'
    ).select_related('home_team', 'away_team').order_by('kickoff')[:5]
    cfb_games_data = [cfb_compute(g, user) for g in cfb_upcoming]
    cfb_games_data.sort(key=lambda g: abs(g.get('house_edge', 0)), reverse=True)

    # CBB games
    cbb_upcoming = CBBGame.objects.filter(
        tipoff__gte=timezone.now(), status='scheduled'
    ).select_related('home_team', 'away_team').order_by('tipoff')[:5]
    cbb_games_data = [cbb_compute(g, user) for g in cbb_upcoming]
    cbb_games_data.sort(key=lambda g: abs(g.get('house_edge', 0)), reverse=True)

    return render(request, 'core/home.html', {
        'cfb_games_data': cfb_games_data[:5],
        'cbb_games_data': cbb_games_data[:5],
        'help_key': 'home',
        'nav_active': 'home',
    })
