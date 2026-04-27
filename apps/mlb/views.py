"""MLB views."""
import json

from django.shortcuts import render, get_object_or_404
from django.utils import timezone

from apps.mockbets.services.prefill import prefill_from_signals

from .models import Game, Team
from .services.prioritization import (
    get_focus_game, mark_top_opportunities, partition_games_by_decision,
    prioritize, sort_live, sort_today,
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

    # Slate-level elite cap (MAX_ELITE_PER_SLATE = 2). Mutates rec.tier in
    # place; we must call this before partitioning so Top Plays never shows
    # more than the allowed number.
    from apps.core.services.recommendations import assign_tiers
    all_recs = [s.recommendation for s in all_tiles if s.recommendation is not None]
    assign_tiers(all_recs)

    # Decision-driven partition. Each game appears in exactly one section;
    # games without a recommendation (no odds yet) fall into not_recommended.
    decision_sections = partition_games_by_decision(all_tiles)

    # Focus Engine: single "do this right now" surface. None when no game
    # meets the bar — the banner is simply omitted rather than forced.
    focus = get_focus_game(all_tiles)

    # Staff diagnostic — ?diag=1 dumps per-game rec state so operators can
    # see why sections are empty without shelling into the DB.
    diag_rows = None
    if request.GET.get('diag') == '1' and request.user.is_staff:
        diag_rows = _build_diag_rows(all_tiles)

    # Pre-compute bulk-bet button counts so the template can show
    # "Bet All Verified Plays (8)" and the confirm modal can say "you
    # are about to place bets on 8 games". Doing this in Python avoids
    # the {% with foo|length|add:bar|length %} filter-parsing footgun
    # where Django takes the first post-colon token as the add arg and
    # a trailing |length is then applied to the wrong intermediate.
    verified_bulk_count = (
        len(decision_sections['elite']) + len(decision_sections['recommended'])
    )
    espn_bulk_count = len(decision_sections.get('recommended_espn', []))

    return render(request, 'mlb/hub.html', {
        'live_tiles': live_tiles,
        'today_tiles': today_tiles,
        'elite_games': decision_sections['elite'],
        'recommended_games': decision_sections['recommended'],
        'verified_bulk_count': verified_bulk_count,
        'espn_bulk_count': espn_bulk_count,
        # Source-Aware Betting (Commit B): ESPN-secondary recommendeds
        # render in their own section under the verified Recommended
        # bets, with a "secondary market — lower confidence" note.
        'recommended_espn_games': decision_sections.get('recommended_espn', []),
        'not_recommended_games': decision_sections['not_recommended'],
        'unrated_games': decision_sections.get('unrated', []),
        # Blocked recommendations are derived-odds rows. They render
        # ONLY when ?diag=1 is on; the public hub never shows them.
        'blocked_games': decision_sections.get('blocked', []),
        'future_games': future_upcoming,
        'focus': focus,
        'diag_rows': diag_rows,
        'teams': Team.objects.select_related('conference').all(),
        'nav_active': 'mlb',
        'help_key': 'mlb_hub',
    })


def _build_diag_rows(signals):
    """Per-game recommendation diagnostics for the staff-only ?diag=1 panel.

    Emits one row per today's-slate game with: whether odds exist, whether
    moneyline prices exist, raw market/house probs, edge, tier, status, reason.
    Helps pinpoint why the decision sections are empty on any given slate.
    """
    rows = []
    for s in signals:
        game = s.game
        odds = getattr(s, 'latest_odds', None)
        rec = s.recommendation
        row = {
            'matchup': f"{game.away_team.name} @ {game.home_team.name}",
            'status_label': game.status,
            'has_odds': odds is not None,
            'has_moneyline': bool(
                odds and odds.moneyline_home is not None
                and odds.moneyline_away is not None
            ),
            'ml_home': odds.moneyline_home if odds else None,
            'ml_away': odds.moneyline_away if odds else None,
            'market_prob': round(odds.market_home_win_prob * 100, 1) if odds else None,
            'house_prob': round(s.house_prob * 100, 1) if s.house_prob is not None else None,
            'rec_pick': rec.pick if rec else '—',
            'rec_edge': float(rec.model_edge) if rec else None,
            'rec_confidence': float(rec.confidence_score) if rec else None,
            'rec_tier': rec.tier if rec else '—',
            'rec_status': rec.status if rec else 'no_rec',
            'rec_reason': rec.status_reason if rec else 'no_odds_or_no_moneyline',
        }
        rows.append(row)
    return rows


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
