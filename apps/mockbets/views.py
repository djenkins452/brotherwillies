import json
from decimal import Decimal, InvalidOperation

from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import render, get_object_or_404
from django.utils import timezone
from django.views.decorators.http import require_POST

from .models import MockBet
from .services.analytics import (
    compute_kpis, compute_chart_data, compute_comparison,
    compute_confidence_calibration, compute_edge_analysis,
    compute_flat_bet_simulation, compute_variance_stats,
)


@login_required
def my_bets(request):
    """List view showing user's mock bets with filters."""
    # Settle stragglers for this user before we render — guarantees the
    # page never shows a stale 'pending' badge for a game that already
    # finalized, even if the cron is behind. Idempotent by design.
    from .services.settlement import settle_user_pending_bets
    try:
        settle_user_pending_bets(request.user)
    except Exception:
        pass  # Never block the page; cron is the authoritative path.

    bets = MockBet.objects.filter(user=request.user)

    # Apply filters
    sport = request.GET.get('sport')
    if sport in ('cfb', 'cbb', 'golf', 'mlb', 'college_baseball'):
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
        'mlb_game__home_team', 'mlb_game__away_team',
        'college_baseball_game__home_team', 'college_baseball_game__away_team',
        'golf_event', 'golf_golfer',
    )

    # Bankroll KPIs come from the canonical analytics helper — same math as the
    # analytics dashboard, no duplicate accounting logic on this page.
    kpis = compute_kpis(bets)

    return render(request, 'mockbets/my_bets.html', {
        'bets': bets[:100],
        'kpis': kpis,
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
    if sport not in ('cfb', 'cbb', 'golf', 'mlb', 'college_baseball'):
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
    elif sport == 'mlb' and game_id:
        from apps.mlb.models import Game as MLBGame
        try:
            bet.mlb_game = MLBGame.objects.get(id=game_id)
        except MLBGame.DoesNotExist:
            return JsonResponse({'error': 'MLB game not found'}, status=404)
    elif sport == 'college_baseball' and game_id:
        from apps.college_baseball.models import Game as CBGame
        try:
            bet.college_baseball_game = CBGame.objects.get(id=game_id)
        except CBGame.DoesNotExist:
            return JsonResponse({'error': 'College Baseball game not found'}, status=404)
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

    # Snapshot the current model pick alongside the bet (team sports only).
    # Non-fatal on failure — the bet is already saved and valid without it.
    #
    # We denormalize status/tier/confidence onto the MockBet row so analytics
    # queries don't depend on the linked BettingRecommendation staying intact,
    # and so "what the system believed at bet time" is preserved forever even
    # if decision rules change later.
    if sport in ('cfb', 'cbb', 'mlb', 'college_baseball') and bet.game is not None:
        try:
            from apps.core.services.recommendations import persist_recommendation
            rec = persist_recommendation(sport, bet.game, request.user)
            if rec is not None:
                bet.recommendation = rec
                bet.recommendation_status = rec.status or ''
                bet.recommendation_tier = getattr(rec, 'tier', '') or ''
                bet.recommendation_confidence = rec.confidence_score
                bet.status_reason = rec.status_reason or ''
                bet.save(update_fields=[
                    'recommendation', 'recommendation_status',
                    'recommendation_tier', 'recommendation_confidence',
                    'status_reason',
                ])
        except Exception:
            pass

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
            'mlb_game__home_team', 'mlb_game__away_team',
            'college_baseball_game__home_team', 'college_baseball_game__away_team',
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


@login_required
@require_POST
def cancel_bet(request, bet_id):
    """Cancel (delete) a pending mock bet — only allowed pre-game.

    The `can_cancel` property is the single source of truth for eligibility
    (pending result AND underlying game hasn't started). We re-check here
    rather than trusting any client-side state.
    """
    bet = get_object_or_404(MockBet, id=bet_id, user=request.user)
    if not bet.can_cancel:
        return JsonResponse(
            {
                'error': 'This bet can no longer be cancelled — the game has '
                         'started or the bet has already settled.',
            },
            status=400,
        )
    bet.delete()
    return JsonResponse({'success': True, 'message': 'Mock bet cancelled.'})


@login_required
def analytics_dashboard(request):
    """Phase 2-4 analytics dashboard with charts, comparison, and advanced analytics."""
    bets = MockBet.objects.filter(user=request.user).select_related(
        'cfb_game__home_team', 'cfb_game__away_team',
        'cbb_game__home_team', 'cbb_game__away_team',
        'mlb_game__home_team', 'mlb_game__away_team',
        'college_baseball_game__home_team', 'college_baseball_game__away_team',
        'golf_event', 'golf_golfer',
    )

    # Apply filters
    sport = request.GET.get('sport')
    if sport in ('cfb', 'cbb', 'golf', 'mlb', 'college_baseball'):
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

    # Quick time filter — translates a named window into date_from/date_to.
    # Always wins over manual dates so toggling a chip resets cleanly.
    quick_range = request.GET.get('range', '')
    today = timezone.localdate()
    if quick_range == 'today':
        date_from = date_to = today.isoformat()
    elif quick_range == 'yesterday':
        from datetime import timedelta as _td
        y = (today - _td(days=1)).isoformat()
        date_from = date_to = y
    elif quick_range == '7d':
        from datetime import timedelta as _td
        date_from = (today - _td(days=6)).isoformat()
        date_to = today.isoformat()
    elif quick_range == '30d':
        from datetime import timedelta as _td
        date_from = (today - _td(days=29)).isoformat()
        date_to = today.isoformat()
    elif quick_range == 'all':
        date_from = ''
        date_to = ''
    else:
        # Fall back to manual date filter form values
        date_from = request.GET.get('date_from')
        date_to = request.GET.get('date_to')
    if date_from:
        bets = bets.filter(placed_at__date__gte=date_from)
    if date_to:
        bets = bets.filter(placed_at__date__lte=date_to)

    # "Current rules only" — excludes bets placed before the decision-layer
    # snapshot migration landed. `recommendation_status` was added by
    # mockbets.0004; pre-migration bets have it blank. Using that as the
    # marker lets us scope analytics to the post-rules era without a hard
    # date cutoff that drifts with re-deploys.
    current_rules_only = request.GET.get('current_rules_only') == '1'
    if current_rules_only:
        bets = bets.exclude(recommendation_status='')

    all_bets = list(bets)
    kpis = compute_kpis(all_bets)
    chart_data = compute_chart_data(all_bets)
    comparison = compute_comparison(all_bets)
    calibration = compute_confidence_calibration(all_bets)
    edge = compute_edge_analysis(all_bets)
    variance = compute_variance_stats(all_bets)
    # Recommendation performance — proves the selection engine is actually
    # picking winners vs just making guesses.
    from .services.recommendation_performance import compute_all as compute_rec_perf
    rec_performance = compute_rec_perf(all_bets)
    # Command-center facade: single structured analytics object the new
    # dashboard sections + AI summary read from. Reuses kpis/perf above.
    from .services.command_center import build_command_center
    cc = build_command_center(all_bets)

    return render(request, 'mockbets/analytics.html', {
        'kpis': kpis,
        'chart_data_json': json.dumps(chart_data),
        'comparison': comparison,
        'calibration': calibration,
        'edge': edge,
        'variance': variance,
        'rec_performance': rec_performance,
        'cc': cc,
        'current_quick_range': quick_range,
        'current_sport': sport or '',
        'current_bet_type': bet_type or '',
        'current_confidence': confidence or '',
        'current_model_source': model_source or '',
        'current_date_from': date_from or '',
        'current_date_to': date_to or '',
        'current_rules_only': current_rules_only,
        'help_key': 'mock_analytics',
        'nav_active': 'mockbets',
    })


@login_required
@require_POST
def flat_bet_sim(request):
    """AJAX endpoint for flat-bet simulation what-if."""
    try:
        data = json.loads(request.body)
        flat_stake = Decimal(str(data.get('flat_stake', '100')))
        if flat_stake <= 0 or flat_stake > Decimal('10000'):
            return JsonResponse({'error': 'Stake must be between $0.01 and $10,000'}, status=400)
    except (json.JSONDecodeError, InvalidOperation, TypeError):
        return JsonResponse({'error': 'Invalid input'}, status=400)

    bets = list(MockBet.objects.filter(user=request.user))
    result = compute_flat_bet_simulation(bets, flat_stake)
    if result is None:
        return JsonResponse({'error': 'No settled bets to simulate'}, status=400)
    return JsonResponse(result)


@login_required
def ai_summary(request):
    """AJAX endpoint for the postgame AI summary on the analytics dashboard.

    Reads the same query string as analytics_dashboard so the summary
    reflects the user's current filter state. Always returns JSON, even
    when OpenAI is unavailable (deterministic fallback baked into the
    service).
    """
    from .services.command_center import build_command_center
    from .services.ai_summary import generate_mockbet_analytics_summary

    bets = MockBet.objects.filter(user=request.user).select_related(
        'cfb_game__home_team', 'cfb_game__away_team',
        'cbb_game__home_team', 'cbb_game__away_team',
        'mlb_game__home_team', 'mlb_game__away_team',
        'college_baseball_game__home_team', 'college_baseball_game__away_team',
        'golf_event', 'golf_golfer',
    )

    # Apply the same filter taxonomy as the analytics view. Re-doing the
    # parse rather than refactoring into a shared helper keeps this endpoint
    # independently testable and avoids a refactor right when the
    # dashboard rewrite is landing.
    sport = request.GET.get('sport')
    if sport in ('cfb', 'cbb', 'golf', 'mlb', 'college_baseball'):
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
    quick_range = request.GET.get('range', '')
    today = timezone.localdate()
    if quick_range == 'today':
        bets = bets.filter(placed_at__date=today)
    elif quick_range == 'yesterday':
        from datetime import timedelta as _td
        bets = bets.filter(placed_at__date=today - _td(days=1))
    elif quick_range == '7d':
        from datetime import timedelta as _td
        bets = bets.filter(placed_at__date__gte=today - _td(days=6))
    elif quick_range == '30d':
        from datetime import timedelta as _td
        bets = bets.filter(placed_at__date__gte=today - _td(days=29))
    elif quick_range != 'all':
        date_from = request.GET.get('date_from')
        date_to = request.GET.get('date_to')
        if date_from:
            bets = bets.filter(placed_at__date__gte=date_from)
        if date_to:
            bets = bets.filter(placed_at__date__lte=date_to)
    if request.GET.get('current_rules_only') == '1':
        bets = bets.exclude(recommendation_status='')

    cc = build_command_center(list(bets))
    result = generate_mockbet_analytics_summary(cc)
    return JsonResponse({
        'content': result['content'],
        'source': result['source'],
        'meta': result['meta'],
        'error': result['error'],
    })


@login_required
@require_POST
def ai_commentary(request):
    """AJAX endpoint for AI performance commentary."""
    from .services.ai_commentary import generate_commentary

    bets = list(MockBet.objects.filter(user=request.user))
    kpis = compute_kpis(bets)
    comparison = compute_comparison(bets)
    calibration = compute_confidence_calibration(bets)
    edge = compute_edge_analysis(bets)
    variance = compute_variance_stats(bets)

    # Get user's persona preference
    persona = 'analyst'
    try:
        persona = request.user.profile.ai_persona or 'analyst'
    except Exception:
        pass

    result = generate_commentary(kpis, comparison, calibration, edge, variance, persona)
    if result['error']:
        return JsonResponse({'error': result['error']}, status=400)
    return JsonResponse({
        'content': result['content'],
        'meta': result['meta'],
    })
