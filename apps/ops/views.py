"""Ops Command Center views.

The dashboard is read-only and reflects the durable record in OddsApiUsage +
CronRunLog. The three trigger views (refresh-data, refresh-scores, test-odds-
api) are POST-only, superuser-only, and guarded against duplicate concurrent
runs. They use subprocess.Popen() rather than call_command() in-process so
the spawned management command runs asynchronously and the HTTP request
returns immediately — Railway's request timeout would otherwise kill long
refreshes.
"""
import os
import subprocess
import sys
import time

import requests
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required, user_passes_test
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.ops.services.api_logging import record_call
from apps.ops.services.cron_logging import is_command_running
from apps.ops.services.command_center import build_snapshot


def _is_superuser(user):
    return user.is_authenticated and user.is_superuser


superuser_required = user_passes_test(_is_superuser, login_url='/accounts/login/')


@login_required
@superuser_required
def command_center(request):
    """Render the Ops Command Center dashboard.

    Always renders, even on an empty database — the dashboard's whole point
    is to be the first place to look when something is wrong, so it must
    not crash if telemetry is missing. build_snapshot() returns 'unknown'
    health and zero counts in that case and the template handles those.
    """
    snapshot = build_snapshot()
    return render(request, 'ops/command_center.html', {
        'snapshot': snapshot,
        'live_data_enabled': settings.LIVE_DATA_ENABLED,
        'odds_api_key_set': bool(settings.ODDS_API_KEY),
    })


# --- Manual triggers ---------------------------------------------------------

def _spawn_management_command(command, user_id, *extra_args):
    """Fire-and-forget subprocess.Popen of `manage.py <command>`.

    Runs in the same project root as the web process. We pass --trigger=manual
    and the requesting user's id so the resulting CronRunLog row credits the
    right person. Stdout/stderr inherit so Railway captures them.
    """
    project_root = settings.BASE_DIR
    manage = os.path.join(project_root, 'manage.py')
    args = [sys.executable, manage, command, '--trigger=manual']
    if user_id:
        args.append(f'--triggered-by-user-id={user_id}')
    args.extend(extra_args)
    # Detached stdin so a Railway request lifecycle exit doesn't kill the child.
    subprocess.Popen(args, cwd=project_root, stdin=subprocess.DEVNULL)


@require_POST
@login_required
@superuser_required
def trigger_refresh_data(request):
    """Spawn refresh_data in a background subprocess. Anti-overlap guard."""
    if is_command_running('refresh_data'):
        messages.warning(request, 'A refresh_data run is already in progress — try again in a few minutes.')
        return redirect(reverse('ops:command_center'))
    _spawn_management_command('refresh_data', request.user.id)
    messages.success(
        request,
        'Triggered: Full Data Refresh. Watch the Recent Runs panel — a new row appears within seconds.',
    )
    return redirect(reverse('ops:command_center'))


@require_POST
@login_required
@superuser_required
def trigger_refresh_scores(request):
    if is_command_running('refresh_scores_and_settle'):
        messages.warning(
            request,
            'A score refresh is already in progress — try again in a moment.',
        )
        return redirect(reverse('ops:command_center'))
    _spawn_management_command('refresh_scores_and_settle', request.user.id)
    messages.success(
        request,
        'Triggered: Score Refresh + Settle. Should complete in under a minute.',
    )
    return redirect(reverse('ops:command_center'))


@require_POST
@login_required
@superuser_required
def trigger_test_odds_api(request):
    """Synchronous one-shot probe of the Odds API.

    Unlike the other triggers we run this inline (it's a single HTTP GET to
    /v4/sports) so we can return the result immediately to the dashboard.
    Result is also written to OddsApiUsage so the test counts toward the
    health view.
    """
    if not settings.ODDS_API_KEY:
        messages.error(
            request,
            'Cannot test: ODDS_API_KEY is not configured. Set it in Railway → Variables.',
        )
        return redirect(reverse('ops:command_center'))

    url = 'https://api.the-odds-api.com/v4/sports'
    start = time.time()
    try:
        resp = requests.get(url, params={'apiKey': settings.ODDS_API_KEY}, timeout=15)
        duration_ms = int((time.time() - start) * 1000)
        success = 200 <= resp.status_code < 300
        record_call(
            url=url,
            status_code=resp.status_code,
            success=success,
            response_time_ms=duration_ms,
            error_message='' if success else f'HTTP {resp.status_code}: {resp.text[:200]}',
            headers=resp.headers,
        )
        if success:
            messages.success(
                request,
                f'Odds API healthy — {resp.status_code} in {duration_ms}ms.'
                f' Quota remaining: {resp.headers.get("x-requests-remaining", "unknown")}.',
            )
        elif resp.status_code == 401:
            messages.error(
                request,
                'Odds API returned 401 Unauthorized — the key is invalid or expired.'
                ' Rotate the key in Railway → Variables → ODDS_API_KEY.',
            )
        elif resp.status_code == 429:
            messages.error(
                request,
                'Odds API returned 429 Too Many Requests — quota exhausted.'
                ' Check usage at https://the-odds-api.com.',
            )
        else:
            messages.error(
                request,
                f'Odds API returned HTTP {resp.status_code}. See Recent Failures panel for details.',
            )
    except requests.RequestException as e:
        duration_ms = int((time.time() - start) * 1000)
        record_call(
            url=url, status_code=None, success=False,
            response_time_ms=duration_ms, error_message=str(e),
            headers=None,
        )
        messages.error(request, f'Odds API test failed: {e}')

    return redirect(reverse('ops:command_center'))
