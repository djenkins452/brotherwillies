from django.shortcuts import render, get_object_or_404
from django.utils import timezone
from .models import Conference, Team, Game
from .services.model_service import compute_game_data
from apps.analytics.models import UserGameInteraction

CFB_SEASON_MONTHS = [8, 9, 10, 11, 12, 1]


def _cfb_is_offseason():
    return timezone.now().month not in CFB_SEASON_MONTHS


def cfb_hub(request):
    conferences = Conference.objects.prefetch_related('teams').all()
    upcoming = Game.objects.filter(
        kickoff__gte=timezone.now(), status='scheduled'
    ).select_related('home_team', 'away_team').order_by('kickoff')[:10]

    return render(request, 'cfb/hub.html', {
        'conferences': conferences,
        'upcoming': upcoming,
        'is_offseason': _cfb_is_offseason(),
        'help_key': 'cfb_hub',
        'nav_active': 'cfb',
    })


def conference_detail(request, slug):
    conference = get_object_or_404(Conference, slug=slug)
    teams = conference.teams.all()

    games = Game.objects.filter(
        status='scheduled',
        kickoff__gte=timezone.now(),
    ).filter(
        models.Q(home_team__conference=conference) | models.Q(away_team__conference=conference)
    ).select_related('home_team', 'away_team').order_by('kickoff')[:20]

    return render(request, 'cfb/conference.html', {
        'conference': conference,
        'teams': teams,
        'games': games,
        'help_key': 'cfb_hub',
        'nav_active': 'cfb',
    })


def game_detail(request, game_id):
    game = get_object_or_404(
        Game.objects.select_related('home_team', 'away_team', 'home_team__conference', 'away_team__conference'),
        id=game_id
    )
    data = compute_game_data(game, request.user if request.user.is_authenticated else None)

    # Log interaction
    if request.user.is_authenticated:
        UserGameInteraction.objects.create(
            user=request.user, game=game, action='viewed', page_key='game_detail'
        )

    return render(request, 'cfb/game_detail.html', {
        'game': game,
        'data': data,
        'help_key': 'game_detail',
        'nav_active': 'cfb',
    })


def value_board(request):
    games = Game.objects.filter(
        kickoff__gte=timezone.now(), status='scheduled'
    ).select_related('home_team', 'away_team').order_by('kickoff')

    sort_by = request.GET.get('sort', 'house_edge')
    games_data = []

    for game in games:
        data = compute_game_data(game, request.user if request.user.is_authenticated else None)
        games_data.append(data)

        # Log interaction for authenticated users
        if request.user.is_authenticated:
            UserGameInteraction.objects.get_or_create(
                user=request.user, game=game, action='evaluated', page_key='value_board',
                defaults={}
            )

    # Apply user preference filters
    if request.user.is_authenticated:
        try:
            from apps.accounts.models import UserProfile
            profile, _ = UserProfile.objects.get_or_create(user=request.user)
            filtered = []
            for g in games_data:
                edge = g.get('house_edge', 0)
                # Always include favorite team
                if g.get('is_favorite') and profile.always_include_favorite_team:
                    filtered.append(g)
                    continue
                # Apply min edge filter
                if abs(edge) < profile.preference_min_edge:
                    continue
                # Apply spread filters
                if profile.preference_spread_min is not None and g.get('latest_odds'):
                    spread = g['latest_odds'].spread
                    if spread is not None and spread < profile.preference_spread_min:
                        continue
                if profile.preference_spread_max is not None and g.get('latest_odds'):
                    spread = g['latest_odds'].spread
                    if spread is not None and spread > profile.preference_spread_max:
                        continue
                filtered.append(g)
            games_data = filtered
        except Exception:
            pass

    # Sort
    if sort_by == 'user_edge':
        games_data.sort(key=lambda g: abs(g.get('user_edge', 0) or 0), reverse=True)
    elif sort_by == 'delta':
        games_data.sort(key=lambda g: abs(g.get('delta', 0) or 0), reverse=True)
    else:
        games_data.sort(key=lambda g: abs(g.get('house_edge', 0)), reverse=True)

    # Gate for anonymous users
    is_gated = not request.user.is_authenticated
    if is_gated:
        visible_data = games_data[:3]
    else:
        visible_data = games_data

    # Check for favorite team "bye week" message
    fav_playing = any(g.get('is_favorite') for g in games_data)
    show_bye_message = False
    if request.user.is_authenticated:
        try:
            from apps.accounts.models import UserProfile
            _profile, _ = UserProfile.objects.get_or_create(user=request.user)
            if _profile.favorite_team and not fav_playing:
                show_bye_message = True
        except Exception:
            pass

    return render(request, 'cfb/value_board.html', {
        'games_data': visible_data,
        'total_count': len(games_data),
        'is_gated': is_gated,
        'sort_by': sort_by,
        'show_bye_message': show_bye_message,
        'is_offseason': _cfb_is_offseason(),
        'help_key': 'value_board',
        'nav_active': 'value',
    })


# Need this import for Q objects
from django.db import models
