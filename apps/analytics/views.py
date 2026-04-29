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
