from django.shortcuts import render, redirect, get_object_or_404
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
    if not request.user.is_authenticated:
        return redirect('accounts:login')

    user = request.user
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


# ── Unified Value Board ─────────────────────────────────────────────────

def _get_available_sports():
    """Return list of sports that have upcoming games/events, ordered by relevance."""
    now = timezone.now()
    sports = []

    # CBB
    cbb_count = CBBGame.objects.filter(tipoff__gte=now, status='scheduled').count()
    if cbb_count > 0 or _is_in_season('cbb'):
        sports.append({'key': 'cbb', 'label': 'CBB', 'count': cbb_count})

    # CFB
    cfb_count = CFBGame.objects.filter(kickoff__gte=now, status='scheduled').count()
    if cfb_count > 0 or _is_in_season('cfb'):
        sports.append({'key': 'cfb', 'label': 'CFB', 'count': cfb_count})

    # Golf
    from apps.golf.models import GolfEvent
    golf_count = GolfEvent.objects.filter(end_date__gte=now.date()).count()
    if golf_count > 0:
        sports.append({'key': 'golf', 'label': 'Golf', 'count': golf_count})

    return sports


def _apply_filters(games_data, user):
    """Apply user preference filters to a list of computed game data dicts."""
    if not user or not user.is_authenticated:
        return games_data
    try:
        profile = user.profile
    except Exception:
        return games_data

    filtered = []
    for g in games_data:
        edge = g.get('house_edge', 0)
        # Always include favorite team
        if g.get('is_favorite') and profile.always_include_favorite_team:
            filtered.append(g)
            continue
        # Min edge filter
        if profile.preference_min_edge and abs(edge) < profile.preference_min_edge:
            continue
        # Spread filters
        if g.get('latest_odds') and g['latest_odds'].spread is not None:
            spread = g['latest_odds'].spread
            if profile.preference_spread_min is not None and spread < profile.preference_spread_min:
                continue
            if profile.preference_spread_max is not None and spread > profile.preference_spread_max:
                continue
        filtered.append(g)
    return filtered


def _get_cfb_value_data(user, sort_by):
    """Fetch and compute CFB value board data."""
    now = timezone.now()
    games = CFBGame.objects.filter(
        kickoff__gte=now, status='scheduled'
    ).select_related('home_team', 'away_team').order_by('kickoff')

    games_data = [cfb_compute(g, user) for g in games]
    games_data = _apply_filters(games_data, user)

    if sort_by == 'user_edge':
        games_data.sort(key=lambda g: abs(g.get('user_edge', 0) or 0), reverse=True)
    elif sort_by == 'delta':
        games_data.sort(key=lambda g: abs(g.get('delta', 0) or 0), reverse=True)
    else:
        games_data.sort(key=lambda g: abs(g.get('house_edge', 0)), reverse=True)

    return games_data


def _get_cbb_value_data(user, sort_by):
    """Fetch and compute CBB value board data."""
    now = timezone.now()
    games = CBBGame.objects.filter(
        tipoff__gte=now, status='scheduled'
    ).select_related('home_team', 'away_team').order_by('tipoff')

    games_data = [cbb_compute(g, user) for g in games]
    games_data = _apply_filters(games_data, user)

    if sort_by == 'user_edge':
        games_data.sort(key=lambda g: abs(g.get('user_edge', 0) or 0), reverse=True)
    elif sort_by == 'delta':
        games_data.sort(key=lambda g: abs(g.get('delta', 0) or 0), reverse=True)
    else:
        games_data.sort(key=lambda g: abs(g.get('house_edge', 0)), reverse=True)

    return games_data


def _get_golf_events():
    """Fetch upcoming golf events."""
    from apps.golf.models import GolfEvent
    now = timezone.now()
    return list(GolfEvent.objects.filter(end_date__gte=now.date()).order_by('start_date')[:20])


def value_board(request):
    """Unified Value Board with sport tabs."""
    available_sports = _get_available_sports()
    sport = request.GET.get('sport', '')
    sort_by = request.GET.get('sort', 'house_edge')
    user = request.user if request.user.is_authenticated else None

    # Default to first available sport (CBB comes first in season)
    if not sport or not any(s['key'] == sport for s in available_sports):
        sport = available_sports[0]['key'] if available_sports else 'cbb'

    games_data = []
    golf_events = []
    is_offseason = False
    show_bye_message = False

    if sport == 'cfb':
        games_data = _get_cfb_value_data(user, sort_by)
        is_offseason = not _is_in_season('cfb')
        # Bye week detection
        if user:
            try:
                profile = user.profile
                if profile.favorite_team:
                    fav_playing = any(g.get('is_favorite') for g in games_data)
                    if not fav_playing:
                        show_bye_message = True
            except Exception:
                pass

    elif sport == 'cbb':
        games_data = _get_cbb_value_data(user, sort_by)
        # Bye week detection
        if user:
            try:
                profile = user.profile
                fav_id = getattr(profile, 'favorite_cbb_team_id', None)
                if fav_id:
                    has_fav = any(
                        g['game'].home_team_id == fav_id or g['game'].away_team_id == fav_id
                        for g in games_data
                    )
                    if not has_fav:
                        show_bye_message = True
            except Exception:
                pass

    elif sport == 'golf':
        golf_events = _get_golf_events()

    # Gate for anonymous users
    total_count = len(games_data)
    is_gated = not (user and user.is_authenticated)
    if is_gated:
        visible_data = games_data[:3]
    else:
        visible_data = games_data

    return render(request, 'core/value_board.html', {
        'available_sports': available_sports,
        'active_sport': sport,
        'games_data': visible_data,
        'golf_events': golf_events,
        'total_count': total_count,
        'is_gated': is_gated,
        'sort_by': sort_by,
        'show_bye_message': show_bye_message,
        'is_offseason': is_offseason,
        'help_key': 'value_board',
        'nav_active': 'value',
    })


def cbb_value_redirect(request):
    """Redirect old /cbb/value/ to unified /value/?sport=cbb."""
    sort_by = request.GET.get('sort', '')
    url = '/value/?sport=cbb'
    if sort_by:
        url += f'&sort={sort_by}'
    return redirect(url)


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
