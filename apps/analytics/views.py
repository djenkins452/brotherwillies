"""Backtest Analytics control page.

Single staff-only page that:
  - Shows the latest Static + Elo backtest runs side-by-side.
  - Lets the user trigger new runs from the UI (no CLI needed).
  - Displays the last 10 runs with status (running / completed / failed).

Background execution: trigger views start a daemon thread so the request
returns immediately. Concurrency is protected by checking whether any
BacktestRun row is currently `status='running'` before kicking off a new
one. This is staff-only and rare, so a small TOCTOU race window is
acceptable — the worst case is two Elo runs fighting over the
`force_use_dynamic` override, which is mitigated by holding the override
for the duration of one run only.

NO CHANGES to backtesting logic, recommendation logic, or odds ingestion
— this layer only orchestrates existing services.
"""
import logging
import threading

from django.contrib.auth.decorators import login_required
from django.http import HttpResponseForbidden, HttpResponseNotAllowed, JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.analytics.models import BacktestRun


logger = logging.getLogger(__name__)


def _staff_required(request):
    """Return None when allowed, an HttpResponse when not."""
    if not request.user.is_authenticated:
        from django.shortcuts import redirect as _redirect
        return _redirect('accounts:login')
    if not request.user.is_staff:
        return HttpResponseForbidden('Staff access required.')
    return None


def backtest_analytics(request):
    """Control page — buttons + comparison + history."""
    forbidden = _staff_required(request)
    if forbidden is not None:
        return forbidden

    static_run = (
        BacktestRun.objects.filter(rating_mode='static', status='completed').first()
    )
    elo_run = (
        BacktestRun.objects.filter(rating_mode='elo', status='completed').first()
    )
    is_running = BacktestRun.objects.filter(status='running').exists()
    running_run = BacktestRun.objects.filter(status='running').first() if is_running else None
    recent_runs = list(BacktestRun.objects.all()[:10])

    return render(request, 'analytics/backtest.html', {
        'static_run': static_run,
        'elo_run': elo_run,
        'is_running': is_running,
        'running_run': running_run,
        'recent_runs': recent_runs,
        'nav_active': '',
        # When a run is in progress we want the page to refresh so the
        # user sees the result without a manual reload. Auto-refresh
        # interval matches the background thread's expected runtime
        # — short enough to feel responsive, long enough to not hammer
        # the DB on a slow run.
        'auto_refresh_seconds': 5 if is_running else 0,
    })


@require_POST
def trigger_backtest(request):
    """POST endpoint that kicks off a backtest in a background thread.

    Params:
      elo=true|false   — force dynamic Elo (true) or static (false). Default false.
      sport=all|cfb|cbb|mlb|college_baseball — default 'all'.

    Idempotency: refuses to start a new run if any BacktestRun is
    currently `status='running'`. Returns the page with an error flash
    in that case.
    """
    forbidden = _staff_required(request)
    if forbidden is not None:
        return forbidden

    if BacktestRun.objects.filter(status='running').exists():
        # Soft fail — render the page with a flash. Don't 409 because
        # the user clicked from the page itself.
        from django.contrib import messages
        messages.warning(request, 'A backtest is already running. Please wait for it to finish.')
        return redirect('analytics:backtest')

    elo = request.POST.get('elo', 'false').lower() in ('true', '1', 'yes')
    sport = request.POST.get('sport', 'all')
    if sport not in ('all', 'cfb', 'cbb', 'mlb', 'college_baseball'):
        sport = 'all'

    rating_mode = 'elo' if elo else 'static'

    # Create the row up front so the page can show "Running..." even
    # before the thread does any work. The thread fills in summary +
    # status when it finishes.
    run = BacktestRun.objects.create(
        sport=sport,
        rating_mode=rating_mode,
        status='running',
        started_at=timezone.now(),
    )

    threading.Thread(
        target=_run_backtest_in_background,
        args=(str(run.id), elo, sport),
        daemon=True,
    ).start()

    from django.contrib import messages
    messages.success(
        request,
        f'Backtest started ({rating_mode}, sport={sport}). Refresh in a few seconds for results.',
    )
    return redirect('analytics:backtest')


def _run_backtest_in_background(run_id: str, use_elo: bool, sport: str):
    """Background thread body. Wrapped in try/except so a failure ends
    up as `status='failed'` with the error message persisted, not a
    permanently-running row.
    """
    from apps.analytics.models import BacktestRun
    from apps.core.services.backtesting_service import run_backtest
    from apps.core.services.elo_service import force_use_dynamic

    try:
        with force_use_dynamic(use_elo):
            # `persist=False` keeps run_backtest from creating a NEW row
            # — we already have one and just need to copy its computed
            # fields in. This preserves the existing run row's id,
            # created_at, status='running', started_at — everything the
            # control page already showed the user.
            computed = run_backtest(sport=sport, persist=False)

        run = BacktestRun.objects.get(id=run_id)
        run.summary = computed.summary
        run.games_evaluated = computed.games_evaluated
        run.games_skipped = computed.games_skipped
        run.is_approximate = computed.is_approximate
        run.notes = computed.notes
        run.status = 'completed'
        run.finished_at = timezone.now()
        run.save()
    except Exception as exc:  # noqa: BLE001 — must catch broadly to set 'failed'
        logger.exception('backtest_run_failed run_id=%s', run_id)
        try:
            run = BacktestRun.objects.get(id=run_id)
            run.status = 'failed'
            run.error_message = repr(exc)[:1000]
            run.finished_at = timezone.now()
            run.save()
        except Exception:
            # If even saving the failure record fails, we've lost the
            # run. The traceback is logged above; the row stays in
            # 'running' until manually cleaned up.
            logger.exception('backtest_run_failed_save run_id=%s', run_id)


# ---------------------------------------------------------------------------
# Model Input Inventory (Phase 1A — staff diagnostic)
#
# Surface that answers "what is the model actually consuming for this game,
# and which gate is binding the recommendation?". Re-runs the live pipeline
# (no persisted state mutated) so it always reflects current DB + settings.
# Wired to the same _staff_required guard as the backtest page.

def model_inventory_index(request):
    """Slate picker — choose an MLB game to inspect."""
    forbidden = _staff_required(request)
    if forbidden is not None:
        return forbidden

    from apps.analytics.services.model_inventory import todays_mlb_games

    games = todays_mlb_games()
    return render(request, 'analytics/model_inventory_index.html', {
        'games': games,
        'nav_active': '',
    })


def model_inventory_detail(request, game_id: str):
    """Full input/score/calibration/edge/gate trace for one MLB game."""
    forbidden = _staff_required(request)
    if forbidden is not None:
        return forbidden

    from django.shortcuts import get_object_or_404

    from apps.analytics.services.model_inventory import build_mlb_inventory
    from apps.mlb.models import Game as MLBGame

    game = get_object_or_404(
        MLBGame.objects.select_related(
            'home_team', 'away_team', 'home_pitcher', 'away_pitcher',
        ),
        id=game_id,
    )
    inventory = build_mlb_inventory(game)

    # Template-friendly orderings. Pairing into tuples keeps the template
    # body small (one for-loop per side instead of two near-duplicate
    # blocks). Gate rows carry a 'kind' so the template can colour
    # compute_status gates differently from lane gates without exposing
    # the underlying dataclass structure to template logic.
    side_pairs = [('Home', inventory.home), ('Away', inventory.away)]
    pitcher_pairs = [
        ('Home Pitcher', inventory.home_pitcher),
        ('Away Pitcher', inventory.away_pitcher),
    ]
    gate_rows = []
    if inventory.gates is not None:
        g = inventory.gates
        gate_rows = [
            ('hard_min_probability (< HARD_MIN_PROBABILITY)', g.hard_min_probability_failed, 'status'),
            ('longshot (|odds| > MAX_ABS_ODDS_FOR_RECOMMENDED)', g.longshot_failed, 'status'),
            ('secondary_source (ESPN fallback)', g.secondary_source_failed, 'status'),
            ('recommended_probability (< MIN_PROBABILITY_FOR_RECOMMENDED)', g.recommended_probability_failed, 'status'),
            ('min_edge (< MIN_EDGE)', g.min_edge_failed, 'status'),
            ('heavy_favorite_juice (odds ≤ HEAVY_FAVORITE_ODDS, edge < STRONG_EDGE)', g.heavy_favorite_juice_failed, 'status'),
            ('extreme_disagreement (|final − fair| > EXTREME_DISAGREEMENT_GAP)', g.extreme_disagreement_fired, 'status'),
            ('lane: probability (< LANE_HARD_GATES_PROBABILITY_MIN)', g.lane_probability_failed, 'lane'),
            ('lane: edge (< LANE_HARD_GATES_EDGE_MIN)', g.lane_edge_failed, 'lane'),
            ('lane: odds (|odds| > LANE_HARD_GATES_MAX_ABS_ODDS)', g.lane_odds_failed, 'lane'),
            ('lane: source quality != primary', g.lane_source_failed, 'lane'),
        ]

    return render(request, 'analytics/model_inventory_detail.html', {
        'inventory': inventory,
        'game': game,
        'side_pairs': side_pairs,
        'pitcher_pairs': pitcher_pairs,
        'gate_rows': gate_rows,
        'nav_active': '',
    })


# ---------------------------------------------------------------------------
# Phase 1B Elo shadow-mode review (staff diagnostic)


def shadow_review(request):
    """Side-by-side: how does the active rating mode differ from the alt
    on the recently-emitted MLB recommendation slate?

    Cheap real-time complement to the backtest harness — works the
    moment shadow data is captured (no need for games to settle).
    """
    forbidden = _staff_required(request)
    if forbidden is not None:
        return forbidden

    from apps.analytics.services.shadow_review import recent_mlb_shadow_review

    days = 14
    try:
        days = int(request.GET.get('days', '14'))
    except (TypeError, ValueError):
        pass
    days = max(1, min(days, 90))

    review = recent_mlb_shadow_review(days=days)
    return render(request, 'analytics/shadow_review.html', {
        'review': review,
        'days': days,
        'nav_active': '',
    })


# ---------------------------------------------------------------------------
# Recommendation Health Score — staff diagnostic
#
# Single composite score (0–100) across seven dimensions answering "is
# the engine behaving like a disciplined predictive system?". Designed
# to prevent emotional tuning and threshold churn — see
# docs/recommendation_quality_framework.md.

def health_score(request):
    """Composite Health Score + dimension breakdown + warnings + history."""
    forbidden = _staff_required(request)
    if forbidden is not None:
        return forbidden

    from apps.analytics.services.health_score import (
        DIMENSION_LABELS, DIMENSION_ORDER, DIMENSION_WEIGHTS,
        compute_health_score, detect_warnings,
    )
    from apps.analytics.services.health_snapshot import recent_snapshots

    days = 14
    try:
        days = int(request.GET.get('days', '14'))
    except (TypeError, ValueError):
        pass
    days = max(1, min(days, 90))

    health = compute_health_score(window_days=days)
    warnings = detect_warnings(health)

    # Order the dimensions for display per DIMENSION_ORDER.
    ordered_dimensions = []
    for key in DIMENSION_ORDER:
        info = health.dimension_scores.get(key, {})
        ordered_dimensions.append({
            'key': key,
            'label': DIMENSION_LABELS[key],
            'weight': DIMENSION_WEIGHTS[key],
            'info': info,
        })

    history = recent_snapshots(limit=20)

    return render(request, 'analytics/health_score.html', {
        'health': health,
        'warnings': warnings,
        'ordered_dimensions': ordered_dimensions,
        'history': history,
        'days': days,
        'nav_active': '',
    })


# ---------------------------------------------------------------------------
# Elo Activation Monitor (Phase 2A Task 4 — 2026-05-16)
#
# Focused observation surface for the 2-3 week post-activation window.
# Reads the latest Health Score snapshot, compares to the pre-Elo
# baseline, and evaluates rollback triggers. Cannot influence
# recommendation behavior; never writes anything.

def elo_monitor(request):
    """Pre/post Elo cutover monitor + rollback-trigger status."""
    forbidden = _staff_required(request)
    if forbidden is not None:
        return forbidden

    from apps.analytics.services.elo_monitor import build_monitor

    monitor = build_monitor()
    return render(request, 'analytics/elo_monitor.html', {
        'monitor': monitor,
        'nav_active': '',
    })


# ---------------------------------------------------------------------------
# Method Replay — retrospective MLB moneyline backtest (2026-05-22)
#
# Answers "what would BW have recommended over the past N days under
# blend weight W?" Strict no-future-leakage. Staff-only. Read-only.

def method_replay(request):
    """Render the Method Replay comparison page."""
    forbidden = _staff_required(request)
    if forbidden is not None:
        return forbidden

    from datetime import datetime as _dt, date as _d, timedelta as _td

    from apps.analytics.services.method_replay import run_replay

    # --- Blend experiment mode (read-only counterfactual, plaintext) -----
    # 0.40 vs 0.55 on the EXACT SAME slate across multiple windows.
    if (request.GET.get('experiment') or '').lower() == 'blend':
        from django.http import HttpResponse
        from apps.analytics.services.method_replay import (
            run_blend_experiment, render_blend_experiment,
        )

        def _parse_blend(name, default):
            try:
                v = float(request.GET.get(name, default))
                return v if 0.0 <= v <= 0.80 else default
            except (TypeError, ValueError):
                return default

        blend_a = _parse_blend('a', 0.40)
        blend_b = _parse_blend('b', 0.55)

        windows_raw = request.GET.get('windows', '7,14,30,60')
        try:
            windows = tuple(
                w for w in (int(x.strip()) for x in windows_raw.split(','))
                if 1 <= w <= 120
            ) or (7, 14, 30, 60)
        except (TypeError, ValueError):
            windows = (7, 14, 30, 60)

        # Staff-only diagnostic capture. DEBUG is False in production, so an
        # uncaught exception here returns an opaque 500 whose traceback only
        # reaches logs we can't always see. Catch it and return the exact
        # exception + traceback as plaintext so the precise failure (type /
        # file / line) is visible to the staff operator. NOTE: a gunicorn
        # WORKER TIMEOUT kills the process and is NOT catchable here — the
        # simulate-once-and-slice refactor in run_blend_experiment is what
        # addresses that path.
        try:
            exp = run_blend_experiment(
                blend_a=blend_a, blend_b=blend_b, windows=windows,
            )
            body = render_blend_experiment(exp)
        except Exception:
            import traceback
            body = (
                "BLEND EXPERIMENT — STAFF DIAGNOSTIC (the experiment raised)\n"
                + "=" * 78 + "\n"
                + f"blend_a={blend_a} blend_b={blend_b} windows={windows}\n"
                + "=" * 78 + "\n\n"
                + traceback.format_exc()
            )
        return HttpResponse(body, content_type='text/plain; charset=utf-8')

    # --- Favorites-only experiment mode (read-only, plaintext) -----------
    # Standard 0.55 (A) vs 0.55 + favorites-only (B) on the EXACT SAME slate.
    if (request.GET.get('experiment') or '').lower() == 'favorites':
        from django.http import HttpResponse
        from apps.analytics.services.method_replay import (
            run_favorites_experiment, render_favorites_experiment,
        )

        try:
            blend = float(request.GET.get('blend', 0.55))
            if not (0.0 <= blend <= 0.80):
                blend = 0.55
        except (TypeError, ValueError):
            blend = 0.55

        windows_raw = request.GET.get('windows', '30,60,90')
        try:
            windows = tuple(
                w for w in (int(x.strip()) for x in windows_raw.split(','))
                if 1 <= w <= 180
            ) or (30, 60, 90)
        except (TypeError, ValueError):
            windows = (30, 60, 90)

        try:
            exp = run_favorites_experiment(blend=blend, windows=windows)
            body = render_favorites_experiment(exp)
        except Exception:
            import traceback
            body = (
                "FAVORITES EXPERIMENT — STAFF DIAGNOSTIC (the experiment raised)\n"
                + "=" * 78 + "\n"
                + f"blend={blend} windows={windows}\n"
                + "=" * 78 + "\n\n"
                + traceback.format_exc()
            )
        return HttpResponse(body, content_type='text/plain; charset=utf-8')

    today = timezone.localdate()
    quick_range = (request.GET.get('range') or '').lower()

    date_from = date_to = None
    if quick_range == '7d':
        date_from = today - _td(days=7)
        date_to = today - _td(days=1)
    elif quick_range == '30d':
        date_from = today - _td(days=30)
        date_to = today - _td(days=1)
    else:
        df_raw = request.GET.get('date_from', '')
        dt_raw = request.GET.get('date_to', '')
        try:
            date_from = _dt.strptime(df_raw, '%Y-%m-%d').date() if df_raw else None
            date_to = _dt.strptime(dt_raw, '%Y-%m-%d').date() if dt_raw else None
        except ValueError:
            date_from = date_to = None
        if date_from is None or date_to is None:
            quick_range = '7d'
            date_from = today - _td(days=7)
            date_to = today - _td(days=1)

    # Parse blend weights (defaults: 0.40 vs 0.55).
    raw_weights = request.GET.get('weights', '0.40,0.55')
    try:
        weights = []
        for w_str in raw_weights.split(','):
            w = float(w_str.strip())
            if 0.0 <= w <= 0.80:
                weights.append(w)
        if not weights:
            weights = [0.40, 0.55]
    except (ValueError, TypeError):
        weights = [0.40, 0.55]

    labels = [f'Replay {w:.2f}' for w in weights]

    result = run_replay(
        date_from=date_from,
        date_to=date_to,
        blend_weights=weights,
        method_labels=labels,
    )

    return render(request, 'analytics/method_replay.html', {
        'result': result,
        'current_range': quick_range,
        'current_date_from': date_from.isoformat(),
        'current_date_to': date_to.isoformat(),
        'current_weights': ','.join(f'{w:.2f}' for w in weights),
        'nav_active': '',
    })
