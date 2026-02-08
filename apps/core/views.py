from django.shortcuts import render, get_object_or_404
from django.http import JsonResponse
from django.contrib.auth.decorators import login_required
from django.utils import timezone
from apps.cfb.models import Game as CFBGame
from apps.cfb.services.model_service import compute_game_data as cfb_compute
from apps.cbb.models import Game as CBBGame
from apps.cbb.services.model_service import compute_game_data as cbb_compute

# Sports seasons by month (inclusive)
# CFB: August through January
# CBB: November through April
SPORT_SEASONS = {
    'cfb': [8, 9, 10, 11, 12, 1],
    'cbb': [11, 12, 1, 2, 3, 4],
}


def _is_in_season(sport):
    return timezone.now().month in SPORT_SEASONS.get(sport, [])


def home(request):
    user = request.user if request.user.is_authenticated else None
    now = timezone.now()

    # Live games (currently in progress)
    cfb_live_data = []
    cbb_live_data = []

    if _is_in_season('cfb'):
        cfb_live = CFBGame.objects.filter(
            status='live'
        ).select_related('home_team', 'away_team').order_by('kickoff')
        cfb_live_data = [cfb_compute(g, user) for g in cfb_live]

    if _is_in_season('cbb'):
        cbb_live = CBBGame.objects.filter(
            status='live'
        ).select_related('home_team', 'away_team').order_by('tipoff')
        cbb_live_data = [cbb_compute(g, user) for g in cbb_live]

    # Upcoming scheduled games (top value)
    cfb_games_data = []
    if _is_in_season('cfb'):
        cfb_upcoming = CFBGame.objects.filter(
            kickoff__gte=now, status='scheduled'
        ).select_related('home_team', 'away_team').order_by('kickoff')[:5]
        cfb_games_data = [cfb_compute(g, user) for g in cfb_upcoming]
        cfb_games_data.sort(key=lambda g: abs(g.get('house_edge', 0)), reverse=True)

    cbb_games_data = []
    if _is_in_season('cbb'):
        cbb_upcoming = CBBGame.objects.filter(
            tipoff__gte=now, status='scheduled'
        ).select_related('home_team', 'away_team').order_by('tipoff')[:5]
        cbb_games_data = [cbb_compute(g, user) for g in cbb_upcoming]
        cbb_games_data.sort(key=lambda g: abs(g.get('house_edge', 0)), reverse=True)

    return render(request, 'core/home.html', {
        'cfb_live_data': cfb_live_data,
        'cbb_live_data': cbb_live_data,
        'cfb_games_data': cfb_games_data[:5],
        'cbb_games_data': cbb_games_data[:5],
        'help_key': 'home',
        'nav_active': 'home',
    })


@login_required
def ai_insight_view(request, sport, game_id):
    """AJAX endpoint that returns AI insight for a game as JSON."""
    if sport not in ('cfb', 'cbb'):
        return JsonResponse({'error': 'Invalid sport.'}, status=400)

    if sport == 'cfb':
        game = get_object_or_404(
            CFBGame.objects.select_related(
                'home_team', 'away_team',
                'home_team__conference', 'away_team__conference'
            ),
            id=game_id
        )
        data = cfb_compute(game, request.user)
    else:
        game = get_object_or_404(
            CBBGame.objects.select_related(
                'home_team', 'away_team',
                'home_team__conference', 'away_team__conference'
            ),
            id=game_id
        )
        data = cbb_compute(game, request.user)

    # Get user persona
    persona = 'analyst'
    try:
        persona = request.user.profile.ai_persona or 'analyst'
    except Exception:
        pass

    from apps.core.services.ai_insights import generate_insight
    result = generate_insight(game, data, sport, persona)

    if result['error']:
        return JsonResponse({
            'error': result['error'],
            'meta': result['meta'],
        }, status=200)

    return JsonResponse({
        'content': result['content'],
        'meta': result['meta'],
    })
