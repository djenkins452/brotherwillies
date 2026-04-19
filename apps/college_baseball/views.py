"""College Baseball views."""
from django.db.models import Q
from django.shortcuts import render, get_object_or_404
from django.utils import timezone
from .models import Conference, Game, Team


def hub(request):
    now = timezone.now()
    conferences = Conference.objects.prefetch_related('teams').all()
    upcoming = (
        Game.objects.filter(first_pitch__gte=now, status='scheduled')
        .select_related('home_team', 'away_team')
        .order_by('first_pitch')[:20]
    )
    live = (
        Game.objects.filter(status='live')
        .select_related('home_team', 'away_team')
    )
    return render(request, 'college_baseball/hub.html', {
        'conferences': conferences,
        'upcoming_games': upcoming,
        'live_games': live,
    })


def conference_detail(request, slug):
    conference = get_object_or_404(Conference, slug=slug)
    teams = conference.teams.all()
    now = timezone.now()
    games = (
        Game.objects.filter(
            first_pitch__gte=now,
            status='scheduled',
        )
        .filter(Q(home_team__in=teams) | Q(away_team__in=teams))
        .select_related('home_team', 'away_team')
        .order_by('first_pitch')[:30]
    )
    return render(request, 'college_baseball/conference.html', {
        'conference': conference,
        'teams': teams,
        'games': games,
    })


def game_detail(request, game_id):
    game = get_object_or_404(
        Game.objects.select_related('home_team', 'away_team', 'home_pitcher', 'away_pitcher'),
        id=game_id,
    )
    from apps.college_baseball.services.model_service import compute_game_data
    data = compute_game_data(game, request.user)
    return render(request, 'college_baseball/game_detail.html', {
        'game': game,
        'data': data,
    })
