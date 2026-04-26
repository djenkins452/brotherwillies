"""Provider health tracking + circuit breaker.

Single source of truth for "should I call this provider right now?" and
"should I open the breaker after this response?". Read by the odds router,
written by the providers (or the router on their behalf).

Public surface:

    record_success(provider, status_code=200)
        Resets consecutive_failures, sets last_success_at, clears any open
        circuit. The success is what closes the breaker after cooldown.

    record_failure(provider, status_code=None, error_message='')
        Increments consecutive_failures. Auto-opens the circuit on:
          - HTTP 401 (key invalid / expired)
          - HTTP 429 (quota exhausted)
          - 3+ consecutive failures
        Returns the updated row.

    is_circuit_open(provider) -> bool
        True iff circuit_open_until is in the future. The router uses this
        to skip the call entirely when the breaker is open.

    open_circuit(provider, reason='')
        Force-open. Used by tests and by manual ops actions.

    reset_circuit(provider)
        Force-close + zero out consecutive_failures. Used by the manual
        "Reset Circuit Breaker" button on the Ops dashboard.

    get(provider) -> ProviderHealth
        Convenience get-or-create.

Design notes:
  - Every mutating call is wrapped in a broad try/except + log so a DB
    hiccup writing health state CANNOT break the upstream provider call.
    The router still routes; it just routes blind for that one call.
  - All times are recorded with django.utils.timezone for TZ correctness.
  - No raw SQL — keeps the module portable across SQLite (dev) and
    Postgres (prod).
"""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Optional

from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)

# 3 consecutive failures opens the breaker. Calibrated so a single transient
# blip (network glitch, brief 503) doesn't trigger fallback — the retries in
# APIClient.get already absorb most of those. 3 in a row is "this looks real."
CONSECUTIVE_FAILURE_THRESHOLD = 3

# Status codes that open the breaker on the FIRST occurrence rather than
# waiting for the consecutive-failure threshold. Both signal something that
# won't fix itself within seconds:
#   401 — invalid / expired key. Will keep failing until rotated.
#   429 — quota exhausted. Will keep failing until usage resets / upgraded.
INSTANT_OPEN_STATUS_CODES = (401, 429)


def _cooldown_minutes() -> int:
    """Read the cooldown from settings each call so tests can override it
    via @override_settings without restarting the process."""
    return int(getattr(settings, 'ODDS_PROVIDER_CIRCUIT_COOLDOWN_MINUTES', 60))


def get(provider: str):
    """Return the ProviderHealth row for `provider`, creating it on first use."""
    from apps.ops.models import ProviderHealth
    row, _ = ProviderHealth.objects.get_or_create(provider=provider)
    return row


def is_circuit_open(provider: str) -> bool:
    """True if the breaker is currently blocking calls. Reads-only — never
    mutates state. Cheap; safe to call before every provider request."""
    try:
        return get(provider).is_circuit_open
    except Exception as exc:  # noqa: BLE001 — never break the caller on a read
        logger.warning('is_circuit_open(%s) failed: %s', provider, exc)
        return False


def record_success(provider: str, status_code: int = 200):
    """Mark a successful call. Closes any open circuit and zeros the failure
    counter — a single success is sufficient evidence to recover."""
    from apps.ops.models import ProviderHealth
    try:
        row, _ = ProviderHealth.objects.get_or_create(provider=provider)
        row.last_success_at = timezone.now()
        row.last_status_code = status_code
        row.last_error_message = ''
        row.consecutive_failures = 0
        row.circuit_open_until = None
        row.last_open_reason = ''
        row.save()
        return row
    except Exception as exc:  # noqa: BLE001 — never break the caller
        logger.warning('record_success(%s) failed: %s', provider, exc)
        return None


def record_failure(provider: str, *, status_code: Optional[int] = None,
                   error_message: str = ''):
    """Mark a failed call. Increments consecutive_failures; opens the breaker
    on auto-open status codes or the consecutive-failure threshold.

    Returns the updated row (or None if the write itself failed)."""
    from apps.ops.models import ProviderHealth
    try:
        row, _ = ProviderHealth.objects.get_or_create(provider=provider)
        row.last_failure_at = timezone.now()
        row.consecutive_failures = (row.consecutive_failures or 0) + 1
        row.last_status_code = status_code
        row.last_error_message = (error_message or '')[:2000]

        reason = _open_reason(status_code, row.consecutive_failures)
        if reason:
            cooldown = _cooldown_minutes()
            row.circuit_open_until = timezone.now() + timedelta(minutes=cooldown)
            row.last_open_reason = reason
            logger.warning(
                'Circuit OPEN for %s (reason=%s, cooldown=%dmin, '
                'consecutive_failures=%d)',
                provider, reason, cooldown, row.consecutive_failures,
            )
        row.save()
        return row
    except Exception as exc:  # noqa: BLE001 — never break the caller
        logger.warning('record_failure(%s) failed: %s', provider, exc)
        return None


def open_circuit(provider: str, reason: str = 'manual'):
    """Force the breaker open. Used by tests + manual ops actions. The
    cooldown is the same as the auto-open path."""
    from apps.ops.models import ProviderHealth
    row, _ = ProviderHealth.objects.get_or_create(provider=provider)
    row.circuit_open_until = timezone.now() + timedelta(minutes=_cooldown_minutes())
    row.last_open_reason = reason
    row.save()
    logger.warning('Circuit force-opened for %s (reason=%s)', provider, reason)
    return row


def reset_circuit(provider: str):
    """Manual reset — clears circuit_open_until and zeros consecutive_failures.
    Used by the "Reset Circuit Breaker" button on the Ops dashboard. Does NOT
    touch last_success_at / last_failure_at — those still reflect history."""
    from apps.ops.models import ProviderHealth
    row, _ = ProviderHealth.objects.get_or_create(provider=provider)
    row.circuit_open_until = None
    row.consecutive_failures = 0
    row.last_open_reason = ''
    row.save()
    logger.info('Circuit manually reset for %s', provider)
    return row


def _open_reason(status_code: Optional[int], consecutive_failures: int) -> str:
    """Decide whether this failure should open the breaker. Returns the
    reason string ('' = don't open)."""
    if status_code == 401:
        return '401 Unauthorized — key invalid or expired'
    if status_code == 429:
        return '429 Too Many Requests — quota exhausted'
    if consecutive_failures >= CONSECUTIVE_FAILURE_THRESHOLD:
        return f'{consecutive_failures} consecutive failures'
    return ''


def state_summary(provider: str) -> dict:
    """Cheap read-only snapshot of provider state for the Ops dashboard.

    Returns a dict the template can consume directly without poking at
    timezone-naive vs aware datetimes or remembering field names."""
    row = get(provider)
    return {
        'provider': row.provider,
        'state': row.state,
        'last_success_at': row.last_success_at,
        'last_failure_at': row.last_failure_at,
        'consecutive_failures': row.consecutive_failures,
        'last_status_code': row.last_status_code,
        'last_error_message': row.last_error_message,
        'circuit_open_until': row.circuit_open_until,
        'is_circuit_open': row.is_circuit_open,
        'last_open_reason': row.last_open_reason,
    }
