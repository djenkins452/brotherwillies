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

    # Two-Lane partition (2026-04-28). Orthogonal to decision_sections —
    # this slices the SAME tiles by lane (core / qualified / pass) rather
    # than by status/tier. Drives the new Core / Qualified / Pass sections
    # in the template, and the Bet All filter ensures only `core` is bulk-
    # bet eligible. We compute it on the same all_tiles list so it stays
    # consistent with whatever the existing decision_sections rendered.
    from apps.core.services.recommendations import partition_games_by_lane, TIER_ORDER
    lane_sections = partition_games_by_lane(all_tiles)

    # Decision-first 3-bucket partition (2026-05-02). Replaces the multi-
    # section today's-slate UI with the spec-mandated three groups. Each
    # tile appears in EXACTLY ONE bucket — first match wins, in
    # Recommended → Potential → Not Recommended priority order — so the
    # user never has to track the same game across two sections.
    #
    # The legacy decision_sections / lane_sections dicts above are still
    # populated for downstream tests + bulk-button counts; the 3 buckets
    # are derived from them.
    _seen_ids = set()

    def _take(tiles):
        out = []
        for t in tiles:
            gid = t.game.id
            if gid in _seen_ids:
                continue
            _seen_ids.add(gid)
            out.append(t)
        return out

    def _tier_rank(tile):
        rec = getattr(tile, 'recommendation', None)
        return TIER_ORDER.get(getattr(rec, 'tier', None), 99) if rec else 99

    def _edge_value(tile):
        rec = getattr(tile, 'recommendation', None)
        if rec is None or rec.model_edge is None:
            return 0.0
        return float(rec.model_edge)

    # 2026-05-22 trust repair: Recommended bucket membership === bulk-bet
    # eligibility. Per the master prompt's RULE 2: "If a game appears in
    # Recommended, it MUST be bettable by Bet All. Otherwise it belongs
    # in Potential."
    #
    # Previously, Recommended was built from decision_sections['elite'] +
    # decision_sections['recommended'] which checks status/tier but NOT
    # lane. A game with status='recommended' + lane='qualified' (because
    # a risk flag fired — short_fav_thin / market_conflict / etc.) would
    # appear as a Recommended card but be excluded from Bet All. The
    # operator saw "Recommended (4)" + "Bet All (2)" with no explanation
    # of the divergence.
    #
    # Fix: filter the Recommended candidate pool through
    # is_bulk_moneyline_eligible — the SAME predicate the Bet All button
    # count uses. Games that pass go to Recommended. Games that don't
    # (but were status='recommended' upstream) fall through to Potential
    # via the lane_sections['qualified'] / value bucket / explicit
    # carry-over below.
    #
    # Single source of truth: is_bulk_moneyline_eligible. No secondary
    # gates anywhere.
    from apps.mockbets.services.bulk_actions import is_bulk_moneyline_eligible

    def _is_visible_recommended(tile):
        """The canonical Recommended-bucket predicate. Identical to the
        bulk-eligibility predicate so the visible count and the Bet All
        count cannot diverge by construction."""
        return is_bulk_moneyline_eligible(
            getattr(tile, 'recommendation', None), source_filter='verified',
        )

    # Candidate pool for Recommended: status='recommended' games from
    # the decision partition (elite + recommended buckets). Filtered
    # through the canonical predicate.
    _recommended_candidate_pool = (
        decision_sections['elite'] + decision_sections['recommended']
    )
    recommended_tiles = _take([
        tile for tile in _recommended_candidate_pool
        if _is_visible_recommended(tile)
    ])
    recommended_tiles.sort(key=lambda t: (_tier_rank(t), -_edge_value(t)))

    # Potential = visible-but-not-bulk-eligible. Three sources, in
    # priority order (the `_take` dedupe means each game lands in
    # the highest-priority bucket only):
    #   1. lane='qualified' games — cleared hard gates but carry 1-2
    #      risk flags. These are the canonical "Potential" cohort.
    #   2. Value-tier picks — high edge, low probability. Visible but
    #      never bulk-eligible by design.
    #   3. Carry-overs: status='recommended' games that failed
    #      is_bulk_moneyline_eligible for any other reason (e.g.,
    #      they dropped below probability gate during the page session,
    #      odds shifted into longshot range, etc.). These would
    #      otherwise vanish silently — Potential catches them.
    _recommended_carry_overs = [
        tile for tile in _recommended_candidate_pool
        if not _is_visible_recommended(tile)
    ]
    potential_tiles = _take(
        lane_sections['qualified']
        + decision_sections.get('value', [])
        + _recommended_carry_overs
    )
    potential_tiles.sort(key=lambda t: -_edge_value(t))

    # Not Recommended = everything left from today's slate. No strict
    # sort per spec — we keep the input ordering so callers can inject
    # their own priority by feeding tiles in the desired order.
    not_recommended_tiles = _take(
        decision_sections['not_recommended']
        + decision_sections.get('recommended_espn', [])
        + lane_sections['pass']
        + decision_sections.get('unrated', [])
    )

    # Focus Engine: single "do this right now" surface. None when no game
    # meets the bar — the banner is simply omitted rather than forced.
    focus = get_focus_game(all_tiles)

    # Staff diagnostic — ?diag=1 dumps per-game rec state so operators can
    # see why sections are empty without shelling into the DB.
    diag_rows = None
    if request.GET.get('diag') == '1' and request.user.is_staff:
        diag_rows = _build_diag_rows(all_tiles)

    # Pre-compute bulk-bet button counts.
    #
    # 2026-05-22 trust repair (RULE 1: single source of truth):
    # the button count and the visible Recommended bucket BOTH come
    # from `recommended_tiles` above — which is itself filtered by
    # `_is_visible_recommended` (= `is_bulk_moneyline_eligible`).
    # By taking `verified_bulk_game_ids` directly from
    # `recommended_tiles`, the count cannot diverge from the visible
    # bucket regardless of which gates the predicate enforces.
    #
    # Per-game drift between page render and click is still possible
    # (odds movement can flip a recommendation from core to qualified),
    # but it is surfaced as `skipped_recommendation_drift` with a
    # human-readable reason, not silently dropped.
    verified_bulk_game_ids = [str(tile.game.id) for tile in recommended_tiles]
    verified_bulk_count = len(verified_bulk_game_ids)
    espn_bulk_count = len(decision_sections.get('recommended_espn', []))

    # RULE 3: defensive divergence detection. The construction above
    # makes a mismatch impossible by design, but we assert + log
    # anyway so any future refactor that re-introduces a separate
    # filter path is caught the first time a slate hits it.
    _alt_count = sum(
        1 for tile in all_tiles
        if _is_visible_recommended(tile)
    )
    if _alt_count != verified_bulk_count:
        import logging as _logging
        _div_log = _logging.getLogger(__name__)
        _div_log.warning(
            'mlb_hub bulk count divergence: recommended_tiles=%d '
            'predicate_over_all_tiles=%d. visible_ids=%s diverged_ids=%s',
            verified_bulk_count, _alt_count,
            verified_bulk_game_ids,
            [
                str(tile.game.id) for tile in all_tiles
                if _is_visible_recommended(tile)
                and str(tile.game.id) not in verified_bulk_game_ids
            ],
        )

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
    # Read through the centralized helpers — they AND-compose the per-feature
    # flags with MONEYLINE_ONLY_MODE, so a single master toggle silences
    # every spread/total surface here without per-flag conditionals.
    from apps.core.config import (
        is_spread_total_enabled,
        is_spread_total_leans_enabled,
        is_spread_total_recommendations_enabled,
    )
    spread_total_signals_enabled = is_spread_total_enabled()
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
    # Both helpers AND-compose with MONEYLINE_ONLY_MODE.
    spread_total_leans_enabled = is_spread_total_leans_enabled()
    # Phase 3 — Promoted Recommendations.
    spread_total_recommendations_enabled = is_spread_total_recommendations_enabled()
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
        # JSON list of game IDs passed to the JS so the bulk endpoint
        # processes EXACTLY the games shown in the button count (2026-05-16
        # trust repair). Encoded once in the view to keep the template
        # logic dumb — `|safe` in the template since these are UUID strings
        # which are inert.
        'verified_bulk_game_ids_json': json.dumps(verified_bulk_game_ids),
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
        # 2026-04-27 strict correction: 'value' bucket — high-edge,
        # low-probability picks (e.g., +1700 underdog with 21% prob,
        # 15pp model edge). Renders in its own Value Plays section.
        # NEVER bulk-bet eligible. Defaults to empty list so a page
        # render with the partition unchanged still works.
        'value_games': decision_sections.get('value', []),
        # Two-Lane sections (2026-04-28). Always passed; the template
        # decides whether to show the new Core/Qualified/Pass sections
        # alongside (or in place of) the existing decision sections.
        # core_games is bulk-bet eligible; qualified is visible-but-
        # excluded; pass is hidden (or in a collapsed "Not actionable"
        # bucket).
        'core_games': lane_sections['core'],
        'qualified_games': lane_sections['qualified'],
        'pass_games': lane_sections['pass'],
        'core_bulk_count': len(lane_sections['core']),
        # 3-bucket decision-first view (2026-05-02). The new MLB hub
        # renders these instead of the legacy multi-section structure.
        'recommended_tiles': recommended_tiles,
        'potential_tiles': potential_tiles,
        'not_recommended_tiles': not_recommended_tiles,
        'future_games': future_upcoming,
        'focus': focus,
        'diag_rows': diag_rows,
        'diag_stale_count': sum(1 for r in (diag_rows or []) if r.get('is_stale')),
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
    from apps.core.utils.multi_book import is_odds_stale
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
            # Stale = pre-game inside the 30-min window with no recent capture.
            # Helper returns False for already-started games and games >30min
            # away, so this column is meaningful only for the live edge of the
            # slate — exactly where staleness matters.
            'is_stale': is_odds_stale(game),
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
