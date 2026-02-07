from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.utils import timezone
from .models import Parlay, ParlayLeg
from apps.cfb.models import Game
from apps.cfb.services.model_service import compute_game_data
from apps.analytics.models import UserGameInteraction


@login_required
def parlay_hub(request):
    parlays = Parlay.objects.filter(user=request.user).prefetch_related('legs', 'legs__game')
    return render(request, 'parlays/hub.html', {
        'parlays': parlays,
        'help_key': 'parlays',
        'nav_active': 'profile',
    })


@login_required
def parlay_new(request):
    games = Game.objects.filter(
        kickoff__gte=timezone.now(), status='scheduled'
    ).select_related('home_team', 'away_team').order_by('kickoff')

    if request.method == 'POST':
        selected_games = request.POST.getlist('games')
        selections = request.POST.getlist('selections')
        market_types = request.POST.getlist('market_types')
        odds_list = request.POST.getlist('odds')

        if len(selected_games) < 2:
            messages.error(request, 'A parlay needs at least 2 legs.')
        else:
            parlay = Parlay.objects.create(user=request.user, sportsbook=request.POST.get('sportsbook', ''))

            game_ids_in_parlay = []
            implied_probs = []

            for i, game_id in enumerate(selected_games):
                game = Game.objects.get(id=game_id)
                selection = selections[i] if i < len(selections) else ''
                market_type = market_types[i] if i < len(market_types) else 'moneyline'
                leg_odds = odds_list[i] if i < len(odds_list) else ''

                game_data = compute_game_data(game, request.user)
                market_prob = game_data['market_prob'] / 100.0
                house_prob = game_data['house_prob'] / 100.0
                user_prob = (game_data['user_prob'] / 100.0) if game_data['user_prob'] else None

                # Use home/away prob based on selection
                if 'away' in selection.lower() or 'under' in selection.lower():
                    market_prob = 1.0 - market_prob
                    house_prob = 1.0 - house_prob
                    if user_prob:
                        user_prob = 1.0 - user_prob

                same_game_group = None
                if game_id in game_ids_in_parlay:
                    same_game_group = game_ids_in_parlay.index(game_id) + 1
                game_ids_in_parlay.append(game_id)

                ParlayLeg.objects.create(
                    parlay=parlay,
                    game=game,
                    market_type=market_type,
                    selection=selection,
                    odds=leg_odds,
                    market_prob=market_prob,
                    house_prob=house_prob,
                    user_prob=user_prob,
                    same_game_group=same_game_group,
                )
                implied_probs.append(market_prob)

                UserGameInteraction.objects.create(
                    user=request.user, game=game, action='parlay_leg_added', page_key='parlay_new'
                )

            # Calculate parlay probabilities
            import math
            parlay_implied = math.prod(implied_probs)
            parlay_house = math.prod([l.house_prob for l in parlay.legs.all() if l.house_prob])
            parlay_user = None
            user_probs = [l.user_prob for l in parlay.legs.all() if l.user_prob is not None]
            if user_probs:
                parlay_user = math.prod(user_probs)

            # Correlation detection
            has_same_game = parlay.legs.filter(same_game_group__isnull=False).exists()
            if has_same_game:
                parlay.correlation_risk = 'high'
                # Apply haircut
                if parlay_house:
                    parlay_house *= 0.90
                if parlay_user:
                    parlay_user *= 0.90
            else:
                # Check for multiple legs from same game
                game_counts = {}
                for leg in parlay.legs.all():
                    gid = str(leg.game_id)
                    game_counts[gid] = game_counts.get(gid, 0) + 1
                if any(c > 1 for c in game_counts.values()):
                    parlay.correlation_risk = 'high'
                    if parlay_house:
                        parlay_house *= 0.90
                    if parlay_user:
                        parlay_user *= 0.90

            parlay.implied_probability = parlay_implied
            parlay.house_probability = parlay_house
            parlay.user_probability = parlay_user
            parlay.save()

            messages.success(request, 'Parlay created.')
            return redirect('parlays:detail', parlay_id=parlay.id)

    return render(request, 'parlays/new.html', {
        'games': games,
        'help_key': 'parlays',
        'nav_active': 'profile',
    })


@login_required
def parlay_detail(request, parlay_id):
    parlay = get_object_or_404(Parlay, id=parlay_id, user=request.user)
    legs = parlay.legs.select_related('game', 'game__home_team', 'game__away_team').all()

    return render(request, 'parlays/detail.html', {
        'parlay': parlay,
        'legs': legs,
        'help_key': 'parlays',
        'nav_active': 'profile',
    })
