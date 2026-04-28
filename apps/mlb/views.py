"""MLB views."""
import json

from django.conf import settings
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

    # ----- Tiered Intelligence Phase 1: Spread + Total opportunity signals
    # Always computed but only RENDERED when settings.SPREAD_TOTAL_SIGNALS_ENABLED.
    # Computing unconditionally lets us flip the flag at runtime via env var
    # without re-deploying. The cost is one query per signal type for today's
    # slate + live tiles, scoped to the tile games we already have in memory.
    #
    # Each entry in the signal lists carries:
    #     {'signal': SpreadOpportunity|TotalOpportunity instance,
    #      'tile':   GameSignals tile (so the template can reuse the
    #                existing _tile_upcoming partial — same matchup,
    #                pitchers, badges, source attribution)}
    # We dedupe by game so the same game appearing in both spread and
    # total sections still uses the same tile context object.
    from apps.mlb.services.opportunity_signals import (
        latest_spread_opportunity_for_game,
        latest_total_opportunity_for_game,
    )
    spread_tiles = []
    total_tiles = []
    spread_total_signals_enabled = bool(
        getattr(settings, 'SPREAD_TOTAL_SIGNALS_ENABLED', False)
    )
    if spread_total_signals_enabled:
        # Phase 3: also pre-compute break-even rate (% format) for the
        # "X% vs Y% break-even" badge format. -110 standard pricing —
        # MLB run-line / total juice is rarely far enough from -110
        # for the precision of "Y%" to mislead a user.
        from apps.mlb.services.opportunity_signals import (
            calculate_break_even, DEFAULT_SPREAD_TOTAL_ODDS,
        )
        break_even_pct = round(
            calculate_break_even(DEFAULT_SPREAD_TOTAL_ODDS) * 100, 1,
        )

        def _shape_entry(sig, tile):
            wr_pct = (
                round(sig.historical_win_rate * 100, 1)
                if sig.historical_win_rate is not None
                else None
            )
            roi_pct = (
                round(sig.roi * 100, 1)
                if getattr(sig, 'roi', None) is not None
                else None
            )
            return {
                'signal': sig, 'tile': tile,
                'historical_win_rate_pct': wr_pct,
                'roi_pct': roi_pct,
                'break_even_pct': break_even_pct,
            }

        for tile in all_tiles:
            sig = latest_spread_opportunity_for_game(tile.game)
            if sig is not None:
                spread_tiles.append(_shape_entry(sig, tile))
            sig = latest_total_opportunity_for_game(tile.game)
            if sig is not None:
                total_tiles.append(_shape_entry(sig, tile))

    # Filter param — `?bet_type=moneyline|spread|total|leans|recommended|all`.
    # Default 'all'. Any other value (typo, stale link, malicious param)
    # collapses to 'all' so the page is robust to garbage input.
    bet_type_filter = request.GET.get('bet_type', 'all')
    if bet_type_filter not in ('all', 'moneyline', 'spread', 'total', 'leans', 'recommended'):
        bet_type_filter = 'all'

    # Phase 2 — Leans intelligence layer. Separate flag from Phase 1 so
    # we can keep Phase 1 in prod while iterating on the Lean copy.
    spread_total_leans_enabled = bool(
        getattr(settings, 'SPREAD_TOTAL_LEANS_ENABLED', False)
    )
    # Phase 3 — Promoted Recommendations. Independent flag from leans;
    # recommendations require both flags + Phase 3 historical data.
    spread_total_recommendations_enabled = bool(
        getattr(settings, 'SPREAD_TOTAL_RECOMMENDATIONS_ENABLED', False)
    )
    # The Leans Only filter only makes sense when the Leans layer is on.
    # If a stale URL has bet_type=leans but the flag is off, fall back
    # to 'all' so the page still renders normally.
    if bet_type_filter == 'leans' and not spread_total_leans_enabled:
        bet_type_filter = 'all'
    if bet_type_filter == 'recommended' and not spread_total_recommendations_enabled:
        bet_type_filter = 'all'

    # Phase 3 — partition spread/total tiles into "recommended" and
    # "non-recommended" buckets for separate rendering. The recommended
    # bucket gets its own green-bordered section ABOVE the existing
    # opportunity sections (which become the Lean + Signal collection).
    spread_recommended_tiles = [
        e for e in spread_tiles if e['signal'].is_recommended
    ]
    total_recommended_tiles = [
        e for e in total_tiles if e['signal'].is_recommended
    ]
    # The existing Phase 1 opportunity sections render `spread_tiles` /
    # `total_tiles` directly. When Phase 3 is on, we want the recommended
    # rows to render ONCE — in their own new section — not twice. So we
    # pass a "non-recommended" view of the same list to the Phase 1
    # template branch when Phase 3 is on, full list otherwise.
    if spread_total_recommendations_enabled:
        spread_signal_tiles = [e for e in spread_tiles if not e['signal'].is_recommended]
        total_signal_tiles = [e for e in total_tiles if not e['signal'].is_recommended]
    else:
        spread_signal_tiles = spread_tiles
        total_signal_tiles = total_tiles

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
        # Tiered Intelligence Phase 1 — passed always so the template
        # can use them gated by `spread_total_signals_enabled`. When
        # the flag is off the lists are empty AND the gate is False
        # so nothing renders. (Belt + suspenders: gating on the list
        # alone would render an empty section header.)
        'spread_total_signals_enabled': spread_total_signals_enabled,
        'spread_total_leans_enabled': spread_total_leans_enabled,
        'spread_total_recommendations_enabled': spread_total_recommendations_enabled,
        'spread_tiles': spread_tiles,
        'total_tiles': total_tiles,
        # Phase 3 partition: recommended (green Proven-Edge section)
        # vs the rest. The existing Phase 1 opportunity sections now
        # iterate `*_signal_tiles` (non-recommended) when Phase 3 is on
        # so the same row doesn't render twice. When Phase 3 is off,
        # `*_signal_tiles` == `*_tiles` (current behavior preserved).
        'spread_recommended_tiles': spread_recommended_tiles,
        'total_recommended_tiles': total_recommended_tiles,
        'spread_signal_tiles': spread_signal_tiles,
        'total_signal_tiles': total_signal_tiles,
        # Counts for the bulk-bet button labels.
        'spread_rec_bulk_count': len(spread_recommended_tiles),
        'total_rec_bulk_count': len(total_recommended_tiles),
        'bet_type_filter': bet_type_filter,
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
