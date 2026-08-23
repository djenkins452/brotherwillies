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

from apps.analytics.models import BacktestRun, BullpenBackfillRun


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
    from apps.analytics.models import BacktestRun, BullpenBackfillRun
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
# Bullpen backfill control (v3.3 SHADOW, 2026-08-22)
#
# Staff-only page that lets the operator trigger and observe the
# historical bullpen backfill (~11 minutes for 6 months of MLB) from
# the browser without needing Railway shell access. Mirrors the
# BacktestRun control-page pattern: background thread flips
# BullpenBackfillRun.status running → completed/failed; the page
# auto-refreshes while a run is in flight so progress is visible live.
#
# Zero production side effects: only writes to RelieverAppearance
# (upsert), StartingPitcher (create-if-new), TeamBullpenSnapshot
# (append), and the BullpenBackfillRun row itself. Cannot activate
# any bullpen scoring — the flag settings are not touched.

def bullpen_backfill(request):
    """Control page — trigger a historical backfill + observe status."""
    forbidden = _staff_required(request)
    if forbidden is not None:
        return forbidden

    is_running = BullpenBackfillRun.objects.filter(status='running').exists()
    current = (
        BullpenBackfillRun.objects.filter(status='running').first()
        if is_running else None
    )
    recent_runs = list(BullpenBackfillRun.objects.all()[:10])

    # Suggest default backfill window — 180 days back from yesterday.
    from datetime import date as _d, timedelta as _td
    default_end = _d.today() - _td(days=1)
    default_start = default_end - _td(days=180)

    return render(request, 'analytics/bullpen_backfill.html', {
        'is_running': is_running,
        'current_run': current,
        'recent_runs': recent_runs,
        'default_start': default_start.isoformat(),
        'default_end': default_end.isoformat(),
        'nav_active': '',
        # Auto-refresh only while running so idle pages don't hammer
        # the server. 8s balances responsive progress with light load.
        'auto_refresh_seconds': 8 if is_running else 0,
    })


@require_POST
def trigger_bullpen_backfill(request):
    """POST endpoint that kicks off a historical backfill in a background
    thread. Idempotent — refuses to start when any run is already
    `status='running'`. Never touches bullpen feature flags."""
    forbidden = _staff_required(request)
    if forbidden is not None:
        return forbidden

    if BullpenBackfillRun.objects.filter(status='running').exists():
        from django.contrib import messages
        messages.warning(
            request,
            'A bullpen backfill is already running. Please wait for it to finish.',
        )
        return redirect('analytics:bullpen_backfill')

    from datetime import datetime as _dt, date as _d, timedelta as _td

    def _parse_date(s, default):
        try:
            return _dt.strptime(s, '%Y-%m-%d').date()
        except (TypeError, ValueError):
            return default

    default_end = _d.today() - _td(days=1)
    default_start = default_end - _td(days=180)
    date_from = _parse_date(request.POST.get('date_from'), default_start)
    date_to = _parse_date(request.POST.get('date_to'), default_end)
    if date_to < date_from:
        date_from, date_to = date_to, date_from

    run = BullpenBackfillRun.objects.create(
        kind='historical',
        status='running',
        phase='starting',
        date_from=date_from,
        date_to=date_to,
        started_at=timezone.now(),
    )

    from apps.analytics.services.bullpen_backfill_service import (
        run_backfill_in_background,
    )
    threading.Thread(
        target=run_backfill_in_background,
        args=(str(run.id),),
        daemon=True,
    ).start()

    from django.contrib import messages
    messages.success(
        request,
        f'Bullpen backfill started ({date_from}..{date_to}). '
        f'Page auto-refreshes every ~8s while it runs.',
    )
    return redirect('analytics:bullpen_backfill')


def bullpen_api_check(request):
    """v3.3 SHADOW — MLB Stats API connectivity diagnostic. Read-only.

    Exercises the three endpoints the backfill depends on (teams,
    one-day schedule, known historical boxscore) and reports
    PASS/FAIL per step. Uses the same statsapi_client the production
    code uses, so a FAIL here means a backfill will also fail —
    and the diagnostic detail tells us the exact cause.

    Staff-only. Never writes data. Safe to hit as often as needed.
    """
    forbidden = _staff_required(request)
    if forbidden is not None:
        return forbidden

    from django.http import HttpResponse
    from apps.analytics.services.bullpen_api_check import render, run_api_check

    try:
        body = render(run_api_check())
    except Exception:
        import traceback
        body = (
            'API CHECK — STAFF DIAGNOSTIC (the diagnostic itself raised)\n'
            + '=' * 78 + '\n\n' + traceback.format_exc()
        )
    return HttpResponse(body, content_type='text/plain; charset=utf-8')


@require_POST
def retry_bullpen_backfill(request):
    """POST endpoint: start a new backfill run using the window of the
    most recent failed/completed_with_errors run. skip-existing carries
    forward everything the prior run completed successfully so this is
    a fast, safe re-drive rather than starting over."""
    forbidden = _staff_required(request)
    if forbidden is not None:
        return forbidden

    if BullpenBackfillRun.objects.filter(status='running').exists():
        from django.contrib import messages
        messages.warning(request, 'A run is already in progress.')
        return redirect('analytics:bullpen_backfill')

    prior = (
        BullpenBackfillRun.objects
        .filter(status__in=('failed', 'completed_with_errors'))
        .order_by('-created_at')
        .first()
    )
    if prior is None:
        from django.contrib import messages
        messages.warning(request, 'No prior failed run to retry.')
        return redirect('analytics:bullpen_backfill')

    new_run = BullpenBackfillRun.objects.create(
        kind='historical',
        status='running',
        phase='starting',
        date_from=prior.date_from,
        date_to=prior.date_to,
        started_at=timezone.now(),
    )
    from apps.analytics.services.bullpen_backfill_service import (
        run_backfill_in_background,
    )
    threading.Thread(
        target=run_backfill_in_background,
        args=(str(new_run.id),),
        daemon=True,
    ).start()
    from django.contrib import messages
    messages.success(
        request,
        f'Retrying backfill for {prior.date_from}..{prior.date_to}. '
        f'skip-existing carries forward the {prior.appearances_created} '
        f'appearances the prior run wrote.',
    )
    return redirect('analytics:bullpen_backfill')


def bullpen_integrity_audit(request):
    """Read-only PASS/FAIL check of the bullpen data integrity —
    duplicates, determinism (sample-based re-build), coverage, etc.
    See `apps.analytics.services.bullpen_backfill_service.integrity_audit`
    for the full check list."""
    forbidden = _staff_required(request)
    if forbidden is not None:
        return forbidden

    from django.http import HttpResponse
    from apps.analytics.services.bullpen_backfill_service import integrity_audit

    try:
        try:
            sample = int(request.GET.get('sample', 200))
        except (TypeError, ValueError):
            sample = 200
        sample = max(10, min(sample, 2000))

        report = integrity_audit(sample_size=sample)
        lines = []
        lines.append('=' * 78)
        lines.append('v3.3 BULLPEN DATA INTEGRITY AUDIT')
        lines.append('=' * 78)
        lines.append(
            f"Overall: {report['overall']}   "
            f"PASS: {report['summary']['PASS']}   "
            f"FAIL: {report['summary']['FAIL']}   "
            f"INFO: {report['summary']['INFO']}   "
            f"(sample={sample})"
        )
        lines.append('')
        for f in report['findings']:
            lines.append(f"[{f['result']:>4}] {f['check']}")
            lines.append(f"       {f['detail']}")
            lines.append('')
        body = '\n'.join(lines)
    except Exception:
        import traceback
        body = (
            'BULLPEN INTEGRITY AUDIT — STAFF DIAGNOSTIC (the audit raised)\n'
            + '=' * 78 + '\n\n' + traceback.format_exc()
        )
    return HttpResponse(body, content_type='text/plain; charset=utf-8')


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

    # --- v3.1 recent-form experiment (read-only, plaintext) --------------
    # A: production / B: production + starter recent form. Pre-registered
    # ship criteria. Verdict is mechanical.
    if (request.GET.get('experiment') or '').lower() == 'recent_form':
        from django.http import HttpResponse
        from apps.analytics.services.method_replay import (
            run_recent_form_experiment, render_recent_form_experiment,
        )

        try:
            days = int(request.GET.get('days', 90))
        except (TypeError, ValueError):
            days = 90
        days = max(7, min(days, 365))

        try:
            blend = float(request.GET.get('blend', 0.55))
            if not (0.0 <= blend <= 0.80):
                blend = 0.55
        except (TypeError, ValueError):
            blend = 0.55

        try:
            exp = run_recent_form_experiment(days=days, blend_weight=blend)
            body = render_recent_form_experiment(exp)
        except Exception:
            import traceback
            body = (
                "RECENT-FORM EXPERIMENT — STAFF DIAGNOSTIC (the experiment raised)\n"
                + "=" * 78 + "\n"
                + f"days={days} blend={blend}\n"
                + "=" * 78 + "\n\n"
                + traceback.format_exc()
            )
        return HttpResponse(body, content_type='text/plain; charset=utf-8')

    # --- Calibration audit (read-only, plaintext) ------------------------
    # For each pick_prob bucket: predicted (midpoint) vs actual win rate.
    if (request.GET.get('experiment') or '').lower() == 'calibration':
        from django.http import HttpResponse
        from datetime import datetime as _dt
        from apps.analytics.services.calibration import (
            build_calibration, render_calibration,
        )

        try:
            blend = float(request.GET.get('blend', 0.55))
            if not (0.0 <= blend <= 0.80):
                blend = 0.55
        except (TypeError, ValueError):
            blend = 0.55

        def _parse_cal(name, default):
            raw = (request.GET.get(name) or '').strip()
            if not raw:
                return default
            try:
                return _dt.strptime(raw, '%Y-%m-%d').date()
            except (TypeError, ValueError):
                return default

        _now = timezone.localdate()
        date_from = _parse_cal('since', _now - _td(days=180))
        date_to = _parse_cal('until', _now - _td(days=1))

        try:
            c = build_calibration(date_from, date_to, blend_weight=blend)
            body = render_calibration(c)
        except Exception:
            import traceback
            body = (
                "CALIBRATION AUDIT — STAFF DIAGNOSTIC (the experiment raised)\n"
                + "=" * 78 + "\n"
                + f"blend={blend} since={date_from} until={date_to}\n"
                + "=" * 78 + "\n\n"
                + traceback.format_exc()
            )
        return HttpResponse(body, content_type='text/plain; charset=utf-8')

    # --- v3.3 Bullpen replay experiment (read-only, plaintext) -----------
    # A: v3.2 baseline / B: +bullpen quality / C: +quality +fatigue on the
    # SAME historical slate. Same leakage-safe machinery as recent_form.
    # Reports data coverage explicitly — when no TeamBullpenSnapshot data
    # has been ingested (current state), the run is flagged INFRASTRUCTURE-
    # ONLY and refuses to interpret the equal numbers as validation.
    # STAFF-ONLY. READ-ONLY. NO WRITES.
    if (request.GET.get('experiment') or '').lower() == 'bullpen':
        from django.http import HttpResponse
        from apps.analytics.services.bullpen_replay import (
            run_bullpen_experiment, render_bullpen_experiment,
        )

        try:
            days = int(request.GET.get('days', 90))
        except (TypeError, ValueError):
            days = 90
        days = max(7, min(days, 365))

        try:
            blend = float(request.GET.get('blend', 0.55))
            if not (0.0 <= blend <= 0.80):
                blend = 0.55
        except (TypeError, ValueError):
            blend = 0.55

        try:
            exp = run_bullpen_experiment(days=days, blend_weight=blend)
            body = render_bullpen_experiment(exp)
        except Exception:
            import traceback
            body = (
                "BULLPEN EXPERIMENT — STAFF DIAGNOSTIC (the experiment raised)\n"
                + "=" * 78 + "\n"
                + f"days={days} blend={blend}\n"
                + "=" * 78 + "\n\n"
                + traceback.format_exc()
            )
        return HttpResponse(body, content_type='text/plain; charset=utf-8')

    # --- Walk-forward optimization study (read-only, plaintext) ----------
    # v3 → ≥60% out-of-sample. Expanding-training + forward-holdout folds
    # over the current v3 baseline (blend=0.55, use_recent_form=True).
    # Grid: probability floor, edge floor, and short-fav/heavy-fav-specific
    # tightening. Returns per-candidate held-out aggregates + true walk-
    # forward selection log + Wilson 95% CI. STAFF-ONLY. READ-ONLY. NO
    # WRITES. Does NOT modify any live decision path.
    #
    # Params (all optional):
    #   since=YYYY-MM-DD, until=YYYY-MM-DD   window (default 180 days)
    #   train_days=30, holdout_days=14, step_days=14
    #   min_sample=20     min picks in training window for a candidate
    #                     to be eligible for selection
    #   objective=win_rate_then_roi | roi | wilson_lower
    if (request.GET.get('experiment') or '').lower() == 'walk_forward':
        from django.http import HttpResponse
        from datetime import datetime as _dt
        from apps.analytics.services.walk_forward import (
            run_walk_forward, render_walk_forward,
        )

        def _parse_date(name, default):
            raw = (request.GET.get(name) or '').strip()
            if not raw:
                return default
            try:
                return _dt.strptime(raw, '%Y-%m-%d').date()
            except (TypeError, ValueError):
                return default

        def _parse_int(name, default, lo, hi):
            try:
                v = int(request.GET.get(name, default))
            except (TypeError, ValueError):
                return default
            return max(lo, min(hi, v))

        _now = timezone.localdate()
        date_from = _parse_date('since', _now - _td(days=180))
        date_to = _parse_date('until', _now - _td(days=1))
        train_days = _parse_int('train_days', 30, 7, 365)
        holdout_days = _parse_int('holdout_days', 14, 3, 60)
        step_days = _parse_int('step_days', 14, 1, 60)
        min_sample = _parse_int('min_sample', 20, 5, 500)
        objective = (request.GET.get('objective') or 'win_rate_then_roi').strip()
        if objective not in ('win_rate_then_roi', 'roi', 'wilson_lower'):
            objective = 'win_rate_then_roi'

        try:
            result = run_walk_forward(
                date_from=date_from,
                date_to=date_to,
                train_days=train_days,
                holdout_days=holdout_days,
                step_days=step_days,
                min_sample_for_selection=min_sample,
                selection_objective=objective,
            )
            body = render_walk_forward(result)
        except Exception:
            import traceback
            body = (
                "WALK-FORWARD STUDY — STAFF DIAGNOSTIC (the experiment raised)\n"
                + "=" * 78 + "\n"
                + f"since={date_from} until={date_to} train={train_days} "
                + f"hold={holdout_days} step={step_days} "
                + f"min_sample={min_sample} objective={objective}\n"
                + "=" * 78 + "\n\n"
                + traceback.format_exc()
            )
        return HttpResponse(body, content_type='text/plain; charset=utf-8')

    # --- 60–65% confidence bucket root-cause deep dive (plaintext) -------
    # Cross-tabs every baseline-recommended sim in [0.60, 0.65) against
    # odds/edge/tier/side/movement + risk flags. Answers "which subset
    # actually drives that bucket's -28.6% ROI in production?" so any
    # tightening rule can target the interaction, not blanket-raise the
    # floor.
    if (request.GET.get('experiment') or '').lower() == 'confidence_bucket_deep_dive':
        from django.http import HttpResponse
        from datetime import datetime as _dt
        from apps.analytics.services.walk_forward import (
            run_60_65_deep_dive, render_60_65_deep_dive,
        )

        def _parse_date2(name, default):
            raw = (request.GET.get(name) or '').strip()
            if not raw:
                return default
            try:
                return _dt.strptime(raw, '%Y-%m-%d').date()
            except (TypeError, ValueError):
                return default

        _now = timezone.localdate()
        date_from = _parse_date2('since', _now - _td(days=180))
        date_to = _parse_date2('until', _now - _td(days=1))

        try:
            result = run_60_65_deep_dive(
                date_from=date_from, date_to=date_to,
            )
            body = render_60_65_deep_dive(result)
        except Exception:
            import traceback
            body = (
                "60-65% DEEP DIVE — STAFF DIAGNOSTIC (the experiment raised)\n"
                + "=" * 78 + "\n"
                + f"since={date_from} until={date_to}\n"
                + "=" * 78 + "\n\n"
                + traceback.format_exc()
            )
        return HttpResponse(body, content_type='text/plain; charset=utf-8')

    # --- Replay vs Actual OVERLAP (read-only, plaintext) -----------------
    # Cross-references the lane-corrected replay against MockBet rows in the
    # same first_pitch window. Buckets: overlap / production-only / replay-only.
    # Required params: ?since=YYYY-MM-DD&until=YYYY-MM-DD. Optional: ?blend, ?user.
    if (request.GET.get('experiment') or '').lower() == 'overlap':
        from django.http import HttpResponse
        from datetime import datetime as _dt
        from apps.analytics.services.replay_overlap import (
            build_overlap, render_overlap,
        )

        try:
            blend = float(request.GET.get('blend', 0.55))
            if not (0.0 <= blend <= 0.80):
                blend = 0.55
        except (TypeError, ValueError):
            blend = 0.55

        def _parse(name, default):
            raw = (request.GET.get(name) or '').strip()
            if not raw:
                return default
            try:
                return _dt.strptime(raw, '%Y-%m-%d').date()
            except (TypeError, ValueError):
                return default

        _now = timezone.localdate()
        date_from = _parse('since', _now - _td(days=30))
        date_to = _parse('until', _now - _td(days=1))
        username = request.GET.get('user') or None

        try:
            overlap = build_overlap(date_from, date_to,
                                    blend_weight=blend, username=username)
            body = render_overlap(overlap)
        except Exception:
            import traceback
            body = (
                "OVERLAP EXPERIMENT — STAFF DIAGNOSTIC (the experiment raised)\n"
                + "=" * 78 + "\n"
                + f"blend={blend} since={date_from} until={date_to} user={username}\n"
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
