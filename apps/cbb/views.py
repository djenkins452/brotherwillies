from django.shortcuts import render, get_object_or_404
from django.utils import timezone
from .models import Conference, Team, Game
from .services.model_service import compute_game_data
from apps.analytics.models import UserGameInteraction


def cbb_hub(request):
    conferences = Conference.objects.prefetch_related('teams').all()
    upcoming = Game.objects.filter(
        tipoff__gte=timezone.now(), status='scheduled'
    ).select_related('home_team', 'away_team').order_by('tipoff')[:10]

    return render(request, 'cbb/hub.html', {
        'conferences': conferences,
        'upcoming': upcoming,
        'help_key': 'cbb_hub',
        'nav_active': 'cbb',
    })


def conference_detail(request, slug):
    conference = get_object_or_404(Conference, slug=slug)
    teams = conference.teams.all()
    games = Game.objects.filter(
        status='scheduled',
        tipoff__gte=timezone.now(),
    ).filter(
        models.Q(home_team__conference=conference) | models.Q(away_team__conference=conference)
    ).select_related('home_team', 'away_team').order_by('tipoff')[:20]

    return render(request, 'cbb/conference.html', {
        'conference': conference,
        'teams': teams,
        'games': games,
        'help_key': 'cbb_hub',
        'nav_active': 'cbb',
    })


def game_detail(request, game_id):
    game = get_object_or_404(
        Game.objects.select_related(
            'home_team', 'away_team', 'home_team__conference', 'away_team__conference'
        ),
        id=game_id
    )
    data = compute_game_data(game, request.user if request.user.is_authenticated else None)

    if request.user.is_authenticated:
        UserGameInteraction.objects.create(
            user=request.user, cbb_game=game, action='viewed', page_key='game_detail'
        )

    return render(request, 'cbb/game_detail.html', {
        'game': game,
        'data': data,
        'help_key': 'game_detail',
        'nav_active': 'cbb',
    })


def value_board(request):
    upcoming = Game.objects.filter(
        tipoff__gte=timezone.now(), status='scheduled'
    ).select_related('home_team', 'away_team').order_by('tipoff')

    user = request.user if request.user.is_authenticated else None
    games_data = []

    for game in upcoming:
        data = compute_game_data(game, user)
        games_data.append(data)

    # Apply user preference filters
    if user:
        try:
            profile = user.profile
        except Exception:
            profile = None

        if profile:
            filtered = []
            for g in games_data:
                include = True
                if profile.preference_min_edge and abs(g['house_edge']) < profile.preference_min_edge:
                    if not (profile.always_include_favorite_team and g['is_favorite']):
                        include = False
                if profile.preference_spread_min is not None and g['latest_odds'] and g['latest_odds'].spread is not None:
                    if abs(g['latest_odds'].spread) < profile.preference_spread_min:
                        if not (profile.always_include_favorite_team and g['is_favorite']):
                            include = False
                if profile.preference_spread_max is not None and g['latest_odds'] and g['latest_odds'].spread is not None:
                    if abs(g['latest_odds'].spread) > profile.preference_spread_max:
                        if not (profile.always_include_favorite_team and g['is_favorite']):
                            include = False
                if include:
                    filtered.append(g)
            games_data = filtered

    # Sorting
    sort_by = request.GET.get('sort', 'house_edge')
    if sort_by == 'user_edge' and user:
        games_data.sort(key=lambda g: abs(g.get('user_edge', 0) or 0), reverse=True)
    elif sort_by == 'delta' and user:
        games_data.sort(key=lambda g: abs(g.get('delta', 0) or 0), reverse=True)
    else:
        sort_by = 'house_edge'
        games_data.sort(key=lambda g: abs(g.get('house_edge', 0)), reverse=True)

    # Log interactions
    if user:
        for g in games_data:
            UserGameInteraction.objects.create(
                user=user, cbb_game=g['game'], action='evaluated', page_key='value_board'
            )

    # Gating for anonymous
    total_count = len(games_data)
    is_gated = not (user and user.is_authenticated)
    if is_gated:
        games_data = games_data[:3]

    # Bye week detection
    show_bye_message = False
    if user:
        try:
            fav_id = user.profile.favorite_cbb_team_id
            if fav_id:
                has_fav = any(
                    g['game'].home_team_id == fav_id or g['game'].away_team_id == fav_id
                    for g in games_data
                )
                if not has_fav:
                    show_bye_message = True
        except Exception:
            pass

    return render(request, 'cbb/value_board.html', {
        'games_data': games_data,
        'total_count': total_count,
        'is_gated': is_gated,
        'sort_by': sort_by,
        'show_bye_message': show_bye_message,
        'help_key': 'value_board',
        'nav_active': 'cbb',
    })


from django.db import models
