"""MLB views."""
import json

from django.shortcuts import render, get_object_or_404
from django.utils import timezone

from apps.mockbets.services.prefill import prefill_from_signals

from .models import Game, Team
from .services.prioritization import (
    get_focus_game, mark_top_opportunities, prioritize, sort_live, sort_today,
)


def _attach_prefill(signals_list, *, authenticated: bool):
    """Attach a JSON-encoded prefill payload to each signal for the tile button.

    Only attached for authenticated users — anonymous viewers won't see the
    Place Mock Bet button, so we skip the work entirely.
    """
    if not authenticated:
        for s in signals_list:
            s.prefill_json = ''
        return signals_list
    for s in signals_list:
        s.prefill_json = json.dumps(prefill_from_signals(s))
    return signals_list


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

    authed = request.user.is_authenticated
    live_tiles = _attach_prefill(sort_live(prioritize(live_qs, user=request.user)), authenticated=authed)
    today_tiles = _attach_prefill(sort_today(prioritize(today_upcoming, user=request.user)), authenticated=authed)

    # Scarcity: only the single highest-conviction Best Bet across the whole
    # page (live + today) is tagged `is_top_opportunity`. mark_* mutates in
    # place and reads `settings.MLB_MAX_TOP_OPPORTUNITIES` (default 1).
    all_tiles = live_tiles + today_tiles
    mark_top_opportunities(all_tiles)

    # Focus Engine: single "do this right now" surface. None when no game
    # meets the bar — the banner is simply omitted rather than forced.
    focus = get_focus_game(all_tiles)

    return render(request, 'mlb/hub.html', {
        'live_tiles': live_tiles,
        'today_tiles': today_tiles,
        'future_games': future_upcoming,
        'focus': focus,
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
    from apps.core.services.recommendations import get_recommendation
    data = compute_game_data(game, request.user)
    rec = get_recommendation('mlb', game, request.user)
    return render(request, 'mlb/game_detail.html', {
        'game': game,
        'data': data,
        'recommendation': rec,
        'nav_active': 'mlb',
        'help_key': 'mlb_game',
    })
