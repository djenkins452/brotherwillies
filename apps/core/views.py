import json
from datetime import timedelta

from django.shortcuts import render, redirect, get_object_or_404
from django.http import JsonResponse
from django.contrib.auth.decorators import login_required
from django.utils import timezone
from apps.cfb.models import Game as CFBGame
from apps.cfb.services.model_service import compute_game_data as cfb_compute
from apps.cbb.models import Game as CBBGame
from apps.cbb.services.model_service import compute_game_data as cbb_compute
from apps.core.sport_registry import SPORT_REGISTRY, all_team_sports, get_sport


def _is_in_season(sport):
    entry = get_sport(sport)
    if not entry:
        return False
    return timezone.now().month in entry['season_months']


def home(request):
    """Home page — mock bet analytics dashboard."""
    if not request.user.is_authenticated:
        return redirect('accounts:login')

    from apps.mockbets.models import MockBet
    from apps.mockbets.services.analytics import (
        compute_kpis, compute_chart_data, compute_comparison,
        compute_confidence_calibration, compute_edge_analysis,
        compute_variance_stats,
    )

    bets = MockBet.objects.filter(user=request.user).select_related(
        'cfb_game__home_team', 'cfb_game__away_team',
        'cbb_game__home_team', 'cbb_game__away_team',
        'golf_event', 'golf_golfer',
    )

    # Apply filters
    sport = request.GET.get('sport')
    if sport in ('cfb', 'cbb', 'golf'):
        bets = bets.filter(sport=sport)

    bet_type = request.GET.get('bet_type')
    if bet_type:
        bets = bets.filter(bet_type=bet_type)

    confidence = request.GET.get('confidence')
    if confidence in ('low', 'medium', 'high'):
        bets = bets.filter(confidence_level=confidence)

    model_source = request.GET.get('model_source')
    if model_source in ('house', 'user'):
        bets = bets.filter(model_source=model_source)

    date_from = request.GET.get('date_from')
    date_to = request.GET.get('date_to')
    if date_from:
        bets = bets.filter(placed_at__date__gte=date_from)
    if date_to:
        bets = bets.filter(placed_at__date__lte=date_to)

    all_bets = list(bets)
    kpis = compute_kpis(all_bets)
    chart_data = compute_chart_data(all_bets)
    comparison = compute_comparison(all_bets)
    calibration = compute_confidence_calibration(all_bets)
    edge = compute_edge_analysis(all_bets)
    variance = compute_variance_stats(all_bets)

    return render(request, 'mockbets/analytics.html', {
        'kpis': kpis,
        'chart_data_json': json.dumps(chart_data),
        'comparison': comparison,
        'calibration': calibration,
        'edge': edge,
        'variance': variance,
        'current_sport': sport or '',
        'current_bet_type': bet_type or '',
        'current_confidence': confidence or '',
        'current_model_source': model_source or '',
        'current_date_from': date_from or '',
        'current_date_to': date_to or '',
        'help_key': 'mock_analytics',
        'nav_active': 'home',
    })



# ── Lobby (formerly Value Board) ─────────────────────────────────────────────────

def _get_available_sports():
    """Return list of sports that have upcoming games/events, ordered by relevance.

    Team sports come from SPORT_REGISTRY; Golf is appended separately since it
    has an entirely different model shape (events + golfers, not games).
    """
    now = timezone.now()
    sports = []

    for key, entry in all_team_sports():
        model = entry['game_model']
        time_field = entry['time_field']
        count = model.objects.filter(
            **{f'{time_field}__gte': now},
            status='scheduled',
        ).count()
        if count > 0 or _is_in_season(key):
            sports.append({'key': key, 'label': entry['label'], 'count': count})

    # Golf — separate structure (events, not games)
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


def _get_value_data_for_sport(sport, user, sort_by):
    """Fetch and compute value-board data for any registered team sport.

    Generalized from the per-sport CFB/CBB helpers: behavior is identical
    but parameterized by the SPORT_REGISTRY entry.
    """
    entry = get_sport(sport)
    if not entry:
        return []

    now = timezone.now()
    time_field = entry['time_field']
    games = (
        entry['game_model'].objects
        .filter(**{f'{time_field}__gte': now}, status='scheduled')
        .select_related('home_team', 'away_team')
        .order_by(time_field)
    )

    compute_fn = entry['compute_fn']
    games_data = [compute_fn(g, user) for g in games]
    games_data = _apply_filters(games_data, user)

    if sort_by == 'user_edge':
        games_data.sort(key=lambda g: abs(g.get('user_edge', 0) or 0), reverse=True)
    elif sort_by == 'delta':
        games_data.sort(key=lambda g: abs(g.get('delta', 0) or 0), reverse=True)
    else:
        games_data.sort(key=lambda g: abs(g.get('house_edge', 0) or 0), reverse=True)

    return games_data


def _get_live_data_for_sport(sport, user):
    """Fetch live (in-progress) games for any team sport via the registry."""
    entry = get_sport(sport)
    if not entry:
        return []
    time_field = entry['time_field']
    games = (
        entry['game_model'].objects
        .filter(status='live')
        .select_related('home_team', 'away_team')
        .order_by(time_field)
    )
    compute_fn = entry['compute_fn']
    return [compute_fn(g, user) for g in games]


def _get_golf_events():
    """Fetch upcoming golf events."""
    from apps.golf.models import GolfEvent
    now = timezone.now()
    return list(GolfEvent.objects.filter(end_date__gte=now.date()).order_by('start_date')[:20])


def _group_games_by_timeframe(games_data, active_sport, live_data=None):
    """Group game data dicts into timeframe sections for accordion display.
    Only one section gets default_open=True: Live (if any), else Big Matchups, else Today, else first."""
    now = timezone.now()
    today = now.date()
    tomorrow = today + timedelta(days=1)
    days_until_sunday = (6 - today.weekday()) % 7 or 7
    end_of_week = today + timedelta(days=days_until_sunday)

    sections = []

    # Live Now section (always shown, even with 0 games)
    sections.append({
        'key': 'live',
        'label': 'Live Now',
        'games': live_data or [],
        'count': len(live_data or []),
        'default_open': False,  # set below
        'is_live': True,
    })

    big_game_ids = set()

    # "Big Games" section — top 5 by combined team rating (all registered team sports)
    if active_sport in SPORT_REGISTRY and games_data:
        big_games = sorted(
            games_data,
            key=lambda g: (g['game'].home_team.rating + g['game'].away_team.rating),
            reverse=True,
        )[:5]
        big_game_ids = {id(g) for g in big_games}
        if big_games:
            sections.append({
                'key': 'big_games',
                'label': 'Big Matchups',
                'games': big_games,
                'count': len(big_games),
                'default_open': False,
            })

    # Determine game time field from the sport registry
    entry = get_sport(active_sport)
    time_field = entry['time_field'] if entry else 'tipoff'

    # Bucket remaining games
    today_games = []
    tomorrow_games = []
    this_week_games = []
    coming_up_games = []

    for g in games_data:
        if id(g) in big_game_ids:
            continue
        game_date = getattr(g['game'], time_field).date()
        if game_date == today:
            today_games.append(g)
        elif game_date == tomorrow:
            tomorrow_games.append(g)
        elif game_date <= end_of_week:
            this_week_games.append(g)
        else:
            coming_up_games.append(g)

    if today_games:
        sections.append({
            'key': 'today',
            'label': "Today's Games",
            'games': today_games,
            'count': len(today_games),
            'default_open': False,
        })
    if tomorrow_games:
        sections.append({
            'key': 'tomorrow',
            'label': "Tomorrow's Games",
            'games': tomorrow_games,
            'count': len(tomorrow_games),
            'default_open': False,
        })
    if this_week_games:
        sections.append({
            'key': 'this_week',
            'label': 'This Week',
            'games': this_week_games,
            'count': len(this_week_games),
            'default_open': False,
        })
    if coming_up_games:
        sections.append({
            'key': 'coming_up',
            'label': 'Coming Up',
            'games': coming_up_games,
            'count': len(coming_up_games),
            'default_open': False,
        })

    # Smart default: only one section open
    # Live (only if games in progress) > Big Matchups > Today > first
    priority = ['live', 'big_games', 'today']
    opened = False
    for pkey in priority:
        for s in sections:
            if s['key'] == pkey and s['count'] > 0:
                s['default_open'] = True
                opened = True
                break
        if opened:
            break
    if not opened and sections:
        # Skip Live (index 0) if it has no games, open next section
        for s in sections:
            if s['count'] > 0:
                s['default_open'] = True
                opened = True
                break
        if not opened:
            sections[0]['default_open'] = True

    return sections


def value_board(request):
    """Lobby — unified game board with sport tabs, live games, and value analysis."""
    available_sports = _get_available_sports()
    sport = request.GET.get('sport', '')
    sort_by = request.GET.get('sort', 'house_edge')
    user = request.user if request.user.is_authenticated else None

    # Default to first available sport (CBB comes first in season)
    if not sport or not any(s['key'] == sport for s in available_sports):
        sport = available_sports[0]['key'] if available_sports else 'cbb'

    games_data = []
    live_data = []
    golf_events = []
    is_offseason = False
    show_bye_message = False

    if sport in SPORT_REGISTRY:
        games_data = _get_value_data_for_sport(sport, user, sort_by)
        is_offseason = not _is_in_season(sport)
        live_data = _get_live_data_for_sport(sport, user)

        # Bye-week / off-week detection — currently only wired for CFB & CBB
        # because only those sports expose a favorite_team on the profile.
        # Baseball favorite-team UI can plug in here without changing structure.
        if user and sport == 'cfb':
            try:
                profile = user.profile
                if profile.favorite_team:
                    fav_playing = any(g.get('is_favorite') for g in games_data)
                    if not fav_playing:
                        show_bye_message = True
            except Exception:
                pass
        elif user and sport == 'cbb':
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

    # Group games into timeframe sections (always for team sports, includes Live section)
    game_sections = []
    if sport != 'golf':
        game_sections = _group_games_by_timeframe(visible_data, sport, live_data=live_data)

    # Favorite team color
    favorite_team_color = ''
    if user:
        try:
            profile = user.profile
            if sport == 'cfb' and profile.favorite_team:
                favorite_team_color = profile.favorite_team.primary_color or ''
            elif sport == 'cbb' and profile.favorite_cbb_team:
                favorite_team_color = profile.favorite_cbb_team.primary_color or ''
        except Exception:
            pass

    return render(request, 'core/value_board.html', {
        'available_sports': available_sports,
        'active_sport': sport,
        'games_data': visible_data,
        'game_sections': game_sections,
        'golf_events': golf_events,
        'total_count': total_count,
        'is_gated': is_gated,
        'sort_by': sort_by,
        'show_bye_message': show_bye_message,
        'is_offseason': is_offseason,
        'favorite_team_color': favorite_team_color,
        'help_key': 'lobby',
        'nav_active': 'lobby',
    })


def cbb_value_redirect(request):
    """Redirect old /cbb/value/ to unified /lobby/?sport=cbb."""
    sort_by = request.GET.get('sort', '')
    url = '/lobby/?sport=cbb'
    if sort_by:
        url += f'&sort={sort_by}'
    return redirect(url)


@login_required
def ai_insight_view(request, sport, game_id):
    """AJAX endpoint that returns AI insight for a game as JSON."""
    entry = get_sport(sport)
    if not entry:
        return JsonResponse({'error': 'Invalid sport.'}, status=400)

    select_fields = ['home_team', 'away_team']
    # CFB/CBB have conference FKs; baseball has a different structure.
    # Prefetch conference if the model field exists — cheap safety.
    try:
        entry['game_model']._meta.get_field('home_team').related_model._meta.get_field('conference')
        select_fields.extend(['home_team__conference', 'away_team__conference'])
    except Exception:
        pass
    # Baseball additionally selects pitchers so AI prompt can inspect them.
    if sport in ('mlb', 'college_baseball'):
        select_fields.extend(['home_pitcher', 'away_pitcher'])

    game = get_object_or_404(
        entry['game_model'].objects.select_related(*select_fields),
        id=game_id,
    )
    data = entry['compute_fn'](game, request.user)

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
