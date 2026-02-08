import json
from decimal import Decimal, InvalidOperation

from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import render, get_object_or_404
from django.utils import timezone
from django.views.decorators.http import require_POST

from .models import MockBet


@login_required
def my_bets(request):
    """List view showing user's mock bets with filters."""
    bets = MockBet.objects.filter(user=request.user)

    # Apply filters
    sport = request.GET.get('sport')
    if sport in ('cfb', 'cbb', 'golf'):
        bets = bets.filter(sport=sport)

    result = request.GET.get('result')
    if result in ('pending', 'win', 'loss', 'push'):
        bets = bets.filter(result=result)

    bet_type = request.GET.get('bet_type')
    if bet_type:
        bets = bets.filter(bet_type=bet_type)

    confidence = request.GET.get('confidence')
    if confidence in ('low', 'medium', 'high'):
        bets = bets.filter(confidence_level=confidence)

    model_source = request.GET.get('model_source')
    if model_source in ('house', 'user'):
        bets = bets.filter(model_source=model_source)

    bets = bets.select_related(
        'cfb_game__home_team', 'cfb_game__away_team',
        'cbb_game__home_team', 'cbb_game__away_team',
        'golf_event', 'golf_golfer',
    )

    # Compute summary stats for settled bets
    settled = [b for b in bets if b.is_settled]
    total_stake = sum(b.stake_amount for b in settled) if settled else Decimal('0')
    total_return = sum(
        (b.simulated_payout or Decimal('0')) + (b.stake_amount if b.result == 'win' else Decimal('0'))
        for b in settled
    ) if settled else Decimal('0')
    # For wins: you get your stake back + payout. For push: stake back. For loss: nothing.
    total_return = Decimal('0')
    for b in settled:
        if b.result == 'win':
            total_return += b.stake_amount + (b.simulated_payout or Decimal('0'))
        elif b.result == 'push':
            total_return += b.stake_amount
    net_pl = total_return - total_stake
    wins = sum(1 for b in settled if b.result == 'win')
    win_pct = (wins / len(settled) * 100) if settled else 0
    roi = (float(net_pl) / float(total_stake) * 100) if total_stake else 0

    return render(request, 'mockbets/my_bets.html', {
        'bets': bets[:100],
        'total_bets': bets.count(),
        'settled_count': len(settled),
        'total_stake': total_stake,
        'total_return': total_return,
        'net_pl': net_pl,
        'win_pct': win_pct,
        'roi': roi,
        'wins': wins,
        'current_sport': sport or '',
        'current_result': result or '',
        'current_bet_type': bet_type or '',
        'current_confidence': confidence or '',
        'current_model_source': model_source or '',
        'help_key': 'mock_bets',
        'nav_active': 'mockbets',
    })


@login_required
@require_POST
def place_bet(request):
    """AJAX endpoint for placing a mock bet."""
    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({'error': 'Invalid JSON'}, status=400)

    sport = data.get('sport')
    if sport not in ('cfb', 'cbb', 'golf'):
        return JsonResponse({'error': 'Invalid sport'}, status=400)

    bet_type = data.get('bet_type')
    valid_types = [c[0] for c in MockBet.BET_TYPE_CHOICES]
    if bet_type not in valid_types:
        return JsonResponse({'error': 'Invalid bet type'}, status=400)

    selection = data.get('selection', '').strip()
    if not selection:
        return JsonResponse({'error': 'Selection is required'}, status=400)

    try:
        odds_american = int(data.get('odds_american', 0))
    except (ValueError, TypeError):
        return JsonResponse({'error': 'Invalid odds'}, status=400)

    if odds_american == 0 or odds_american < -10000 or odds_american > 50000:
        return JsonResponse({'error': 'Odds must be non-zero and reasonable'}, status=400)

    try:
        stake = Decimal(str(data.get('stake_amount', '100')))
        if stake <= 0 or stake > Decimal('10000'):
            return JsonResponse({'error': 'Stake must be between $0.01 and $10,000'}, status=400)
    except (InvalidOperation, TypeError):
        return JsonResponse({'error': 'Invalid stake amount'}, status=400)

    # Calculate implied probability
    if odds_american > 0:
        implied_prob = Decimal('100') / (Decimal(odds_american) + Decimal('100'))
    else:
        implied_prob = Decimal(abs(odds_american)) / (Decimal(abs(odds_american)) + Decimal('100'))

    confidence = data.get('confidence_level', 'medium')
    if confidence not in ('low', 'medium', 'high'):
        confidence = 'medium'

    model_source = data.get('model_source', 'house')
    if model_source not in ('house', 'user'):
        model_source = 'house'

    edge_str = data.get('expected_edge')
    expected_edge = None
    if edge_str:
        try:
            expected_edge = Decimal(str(edge_str))
        except (InvalidOperation, TypeError):
            pass

    notes = data.get('notes', '').strip()[:500]

    # Build the bet
    bet = MockBet(
        user=request.user,
        sport=sport,
        bet_type=bet_type,
        selection=selection,
        odds_american=odds_american,
        implied_probability=implied_prob,
        stake_amount=stake,
        confidence_level=confidence,
        model_source=model_source,
        expected_edge=expected_edge,
        notes=notes,
    )

    # Set the appropriate game/event FK
    game_id = data.get('game_id')
    if sport == 'cfb' and game_id:
        from apps.cfb.models import Game as CFBGame
        try:
            bet.cfb_game = CFBGame.objects.get(id=game_id)
        except CFBGame.DoesNotExist:
            return JsonResponse({'error': 'CFB game not found'}, status=404)
    elif sport == 'cbb' and game_id:
        from apps.cbb.models import Game as CBBGame
        try:
            bet.cbb_game = CBBGame.objects.get(id=game_id)
        except CBBGame.DoesNotExist:
            return JsonResponse({'error': 'CBB game not found'}, status=404)
    elif sport == 'golf':
        event_id = data.get('event_id')
        golfer_id = data.get('golfer_id')
        if event_id:
            from apps.golf.models import GolfEvent
            try:
                bet.golf_event = GolfEvent.objects.get(id=event_id)
            except GolfEvent.DoesNotExist:
                return JsonResponse({'error': 'Golf event not found'}, status=404)
        if golfer_id:
            from apps.golf.models import Golfer
            try:
                bet.golf_golfer = Golfer.objects.get(id=golfer_id)
            except Golfer.DoesNotExist:
                return JsonResponse({'error': 'Golfer not found'}, status=404)

    bet.save()

    return JsonResponse({
        'success': True,
        'bet_id': str(bet.id),
        'message': 'Mock bet placed successfully',
    })


@login_required
def bet_detail(request, bet_id):
    """Detail view for a single mock bet."""
    bet = get_object_or_404(
        MockBet.objects.select_related(
            'cfb_game__home_team', 'cfb_game__away_team',
            'cbb_game__home_team', 'cbb_game__away_team',
            'golf_event', 'golf_golfer',
        ),
        id=bet_id,
        user=request.user,
    )

    settlement_logs = bet.settlement_logs.all()

    return render(request, 'mockbets/bet_detail.html', {
        'bet': bet,
        'settlement_logs': settlement_logs,
        'help_key': 'mock_bet_detail',
        'nav_active': 'mockbets',
    })


@login_required
@require_POST
def review_bet(request, bet_id):
    """AJAX endpoint for flagging a bet with review."""
    bet = get_object_or_404(MockBet, id=bet_id, user=request.user)

    if not bet.is_settled:
        return JsonResponse({'error': 'Can only review settled bets'}, status=400)

    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({'error': 'Invalid JSON'}, status=400)

    review_flag = data.get('review_flag', '')
    if review_flag and review_flag not in ('repeat', 'avoid'):
        return JsonResponse({'error': 'Invalid review flag'}, status=400)

    bet.review_flag = review_flag
    bet.review_notes = data.get('review_notes', '').strip()[:500]
    bet.save(update_fields=['review_flag', 'review_notes'])

    return JsonResponse({'success': True})
