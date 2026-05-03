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


def command_center_home(request):
    """New homepage (2026-05-03) — staff/user landing page that answers:
      - What should I bet on today?
      - How did yesterday go?
      - Is the system healthy?
      - Where do I go next?

    Pulls top-edge moneyline recommendations + reuses the moneyline
    evaluation service for yesterday's numbers. Light query — no charts,
    no per-user analytics computation. Falls through to the legacy
    analytics dashboard for backwards-compat under `/home-analytics/`.
    """
    from datetime import timedelta as _td
    from apps.core.models import BettingRecommendation
    from apps.core.config import is_moneyline_only_mode
    from apps.mockbets.services.moneyline_evaluation import build_evaluation_report
    from apps.mockbets.models import MockBet

    today_local = timezone.localdate()
    yesterday = today_local - _td(days=1)

    # --- Section 1: today's recommended bets -------------------------------
    # Pull BettingRecommendation rows captured today, status=recommended,
    # bet_type=moneyline (the only emitted type today). Order by edge DESC,
    # cap at 5. select_related the per-sport game so the template can read
    # team names without N+1 lookups. The `created_at` field defaults to
    # `timezone.now()` so a today's-local-date filter on it gives us the
    # current slate's recs without requiring a per-game-time join.
    today_recs_qs = (
        BettingRecommendation.objects
        .filter(
            bet_type='moneyline',
            status='recommended',
            created_at__date=today_local,
        )
        .select_related(
            'cfb_game__home_team', 'cfb_game__away_team',
            'cbb_game__home_team', 'cbb_game__away_team',
            'mlb_game__home_team', 'mlb_game__away_team',
            'college_baseball_game__home_team', 'college_baseball_game__away_team',
        )
        .order_by('-model_edge')
    )
    # Dedupe by (sport, game.id) — the engine writes a fresh row each time
    # the rec mutates so the same game can have several rows in the day.
    # Keep the highest-edge one per game (already first due to ordering).
    seen_games = set()
    today_recs = []
    for rec in today_recs_qs:
        game = rec.game  # uses the per-sport FK helper on the model
        key = (rec.sport, getattr(game, 'id', None))
        if key in seen_games or game is None:
            continue
        seen_games.add(key)
        today_recs.append(rec)
        if len(today_recs) >= 5:
            break

    # --- Section 2: yesterday's evaluation ---------------------------------
    # Reuse the moneyline_evaluation service so the homepage and the
    # /mockbets/moneyline-evaluation/ page show the same numbers. Engine-
    # performance evaluation = system-generated only, all users.
    eval_report = build_evaluation_report(
        MockBet.objects.all(),
        date_from=yesterday,
        date_to=yesterday,
        include_manual=False,
    )
    yesterday_summary = eval_report['executive_summary']

    # --- Section 3: system health ------------------------------------------
    # Spec rule: clv_positive_rate >= 50% → healthy, else warning.
    # Special case: empty CLV sample → 'unknown' rather than fake-warning.
    if yesterday_summary['clv_sample'] == 0:
        health_status = 'unknown'
        health_label = 'No CLV data yesterday'
    elif yesterday_summary['positive_clv_rate'] >= 50.0:
        health_status = 'healthy'
        health_label = f"CLV+ {yesterday_summary['positive_clv_rate']:.1f}% — healthy"
    else:
        health_status = 'warning'
        health_label = f"CLV+ {yesterday_summary['positive_clv_rate']:.1f}% — below 50%"

    return render(request, 'core/command_center.html', {
        'today_recs': today_recs,
        'yesterday_summary': yesterday_summary,
        'yesterday_label': yesterday.isoformat(),
        'health_status': health_status,
        'health_label': health_label,
        'is_moneyline_only_mode': is_moneyline_only_mode(),
        'nav_active': 'home',
    })


def home(request):
    """Home page — mock bet analytics dashboard."""
    if not request.user.is_authenticated:
        return redirect('accounts:login')

    from apps.mockbets.models import MockBet
    from apps.mockbets.services.analytics import (
        compute_kpis, compute_chart_data, compute_comparison,
        compute_confidence_calibration,
        compute_variance_stats,
    )

    bets = MockBet.objects.filter(user=request.user).select_related(
        'cfb_game__home_team', 'cfb_game__away_team',
        'cbb_game__home_team', 'cbb_game__away_team',
        'mlb_game__home_team', 'mlb_game__away_team',
        'college_baseball_game__home_team', 'college_baseball_game__away_team',
        'golf_event', 'golf_golfer',
    )
    # Master-switch gate at the query layer.
    from apps.core.config import is_moneyline_only_mode
    if is_moneyline_only_mode():
        bets = bets.filter(bet_type='moneyline')

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
    # 2026-04-30: dropped `edge = compute_edge_analysis(all_bets)` — the
    # template's Edge Analysis card was a duplicate of "Where Is My Edge?"
    # (cc.edge_buckets) showing different bucket schemes and totals. See
    # the analytics.html change for the full rationale.
    variance = compute_variance_stats(all_bets)

    # 2026-04-30: build the filter-preserving query string for the
    # quick-range tabs. The home-page analytics view doesn't expose a
    # `range` quick-range or current_rules_only checkbox in this form,
    # but we still pass cc_filter_query_string so the template
    # references work cleanly (empty string = no extra params).
    from urllib.parse import urlencode as _urlencode
    cc_filter_pairs = []
    if sport:
        cc_filter_pairs.append(('sport', sport))
    if bet_type:
        cc_filter_pairs.append(('bet_type', bet_type))
    if confidence:
        cc_filter_pairs.append(('confidence', confidence))
    if model_source:
        cc_filter_pairs.append(('model_source', model_source))
    if date_from:
        cc_filter_pairs.append(('date_from', date_from))
    if date_to:
        cc_filter_pairs.append(('date_to', date_to))
    cc_filter_query_string = _urlencode(cc_filter_pairs)

    # 2026-04-30: single-sport detection for ROI by Sport — see the
    # mockbets analytics_dashboard view for the same computation.
    sport_roi_data = chart_data.get('roi_by_sport') or {}
    sport_count = len(sport_roi_data)
    single_sport_label = ''
    single_sport_roi = 0
    single_sport_count = 0
    single_sport_net = 0.0
    if sport_count == 1:
        only_sport = next(iter(sport_roi_data))
        d = sport_roi_data[only_sport]
        single_sport_label = only_sport.replace('_', ' ').upper()
        single_sport_roi = d.get('roi', 0)
        single_sport_count = d.get('count', 0)
        single_sport_net = d.get('net', 0.0)

    return render(request, 'mockbets/analytics.html', {
        'kpis': kpis,
        'chart_data_json': json.dumps(chart_data),
        'chart_data_sport_count': sport_count,
        'chart_data_single_sport_label': single_sport_label,
        'chart_data_single_sport_roi': single_sport_roi,
        'chart_data_single_sport_count': single_sport_count,
        'chart_data_single_sport_net': single_sport_net,
        'chart_data_single_sport_net_abs': abs(single_sport_net),
        'cc_filter_query_string': cc_filter_query_string,
        'comparison': comparison,
        'calibration': calibration,
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
    _attach_recommendations(sport, user, games_data)
    _sort_games_by_tier_then_edge(games_data, sort_by)
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
    data = [compute_fn(g, user) for g in games]
    _attach_recommendations(sport, user, data)
    return data


def _attach_recommendations(sport, user, games_data):
    """Mutate each dict in `games_data` to add a `recommendation` key (or None)."""
    from apps.core.services.recommendations import get_recommendation
    for g in games_data:
        game_obj = g.get('game')
        if game_obj is None:
            g['recommendation'] = None
            continue
        g['recommendation'] = get_recommendation(sport, game_obj, user)


def _partition_elite(games_data, live_data):
    """Split elite-tier recommendations into their own list for the featured
    section, and return the remaining upcoming + live lists with elites removed.

    Must run AFTER assign_tiers, because tier may be demoted by the slate-level
    cap. Preserves ordering of returned lists; elites are sorted by confidence
    desc, edge desc so the strongest pick leads the featured section.
    """
    elite_ids = set()
    elite_games = []
    for g in list(live_data) + list(games_data):
        rec = g.get('recommendation')
        if rec is not None and rec.tier == 'elite':
            game_obj = g.get('game')
            gid = getattr(game_obj, 'id', None)
            if gid is None or gid in elite_ids:
                continue
            elite_ids.add(gid)
            elite_games.append(g)

    # Global ranking rule (edge DESC, confidence DESC) — edge is the primary
    # signal per the selection engine spec; confidence is the tiebreaker.
    elite_games.sort(
        key=lambda g: (
            -(g['recommendation'].model_edge or 0),
            -(g['recommendation'].confidence_score or 0),
        )
    )

    remaining_upcoming = [g for g in games_data if getattr(g.get('game'), 'id', None) not in elite_ids]
    remaining_live = [g for g in live_data if getattr(g.get('game'), 'id', None) not in elite_ids]
    return elite_games, remaining_upcoming, remaining_live


def _sort_games_by_tier_then_edge(games_data, sort_by):
    """Sort by recommendation tier first (elite > strong > standard > none),
    then the existing edge-based ordering as a tiebreaker within tier."""
    from apps.core.services.recommendations import TIER_ORDER

    def edge_magnitude(g):
        if sort_by == 'user_edge':
            return abs(g.get('user_edge', 0) or 0)
        if sort_by == 'delta':
            return abs(g.get('delta', 0) or 0)
        return abs(g.get('house_edge', 0) or 0)

    def key(g):
        rec = g.get('recommendation')
        tier_pri = TIER_ORDER[rec.tier if rec else None]
        # Tier asc (elite=0 first), magnitude desc within tier (use negative).
        return (tier_pri, -edge_magnitude(g))

    games_data.sort(key=key)


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
    elite_games = []
    golf_events = []
    is_offseason = False
    show_bye_message = False

    if sport in SPORT_REGISTRY:
        games_data = _get_value_data_for_sport(sport, user, sort_by)
        is_offseason = not _is_in_season(sport)
        live_data = _get_live_data_for_sport(sport, user)
        # Slate-level elite guardrail runs across the combined set so the
        # lobby never shows more than MAX_ELITE_PER_SLATE "high confidence"
        # picks regardless of how many games meet the raw threshold.
        # assign_tiers mutates each rec's .tier in place; re-sort after so
        # demoted elites fall into the strong bucket's ordering.
        from apps.core.services.recommendations import assign_tiers
        all_recs = [
            g['recommendation']
            for g in (games_data + live_data)
            if g.get('recommendation') is not None
        ]
        assign_tiers(all_recs)

        # Pull elite-tier games into their own featured section above the
        # main board. The slate-level guardrail already caps this at 2 —
        # we just partition the same list. `elite_games` is removed from
        # `games_data` and `live_data` so it never duplicates below.
        elite_games, games_data, live_data = _partition_elite(games_data, live_data)
        _sort_games_by_tier_then_edge(games_data, sort_by)

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
        'elite_games': elite_games,
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


def backtest_results(request):
    """Back-compat redirect for the legacy /backtest/ URL.

    The full control page has moved to /analytics/backtest/ (added
    2026-04-28). This stub preserves any bookmarks pointing at the
    original path.
    """
    return redirect('/analytics/backtest/')
