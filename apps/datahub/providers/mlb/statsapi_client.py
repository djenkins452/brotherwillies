"""Canonical MLB Stats API HTTP client.

Single-source client used by:
  * apps/datahub/management/commands/ingest_reliever_appearances.py
  * apps/analytics/services/bullpen_backfill_service.py
  * apps/analytics/services/bullpen_api_check.py (diagnostic)
  * apps/datahub/management/commands/bullpen_daily_refresh.py

Consolidating the HTTP layer here means all callers share:
  * one User-Agent (avoids the default `python-requests/x.y` which
    some CDN/WAF layers rate-limit or block)
  * one timeout policy
  * one retry-with-backoff policy for transient failures
  * one exception type (StatsApiError) that carries URL, params,
    status code, response body preview, and attempt number
  * one canonical schedule-chunking strategy (small daily/weekly
    windows so a 6-month backfill never issues one giant request)

WHY THE PRODUCTION FAILURE

  The initial v3.3 operationalization used a single
  `/v1/schedule?startDate=..&endDate=..` call spanning 180 days.
  That call succeeded from developer/CI environments but failed on
  Railway with an HTTPError before a single boxscore was fetched.

  Common Railway-vs-dev asymmetries that would cause exactly this:
    * shared egress IP has hit MLB's rate limit (429)
    * WAF blocks or rewrites requests with the default requests UA
      (403 / 400)
    * response body of 3+ MB triggers infrastructure limits

  All three go away if we (a) issue smaller-window requests, (b)
  present a real User-Agent, and (c) retry transient responses.
  This module implements all three.

  On the FIRST failure this module raises a `StatsApiError` whose
  string form includes the exact status code + first N bytes of
  the response body — so the next production run's failure tells
  us the actual cause without needing another investigation cycle.
"""
from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional

import requests
from django.conf import settings


logger = logging.getLogger(__name__)


# --- HTTP config -------------------------------------------------------------
DEFAULT_USER_AGENT = (
    'BrotherWillies/1.0 (+https://brotherwillies.com; danny@brotherwillies.com)'
)
DEFAULT_CONNECT_TIMEOUT_S = 10.0
DEFAULT_READ_TIMEOUT_S = 30.0
DEFAULT_RETRIES = 3
BACKOFF_BASE_S = 0.75
BACKOFF_MAX_S = 15.0

# Retry these status codes as transient — network/server issues that
# frequently resolve on the next attempt.
RETRYABLE_STATUS_CODES = frozenset({408, 425, 429, 500, 502, 503, 504})

# --- Schedule chunking -------------------------------------------------------
#
# Weekly is a good balance:
#   * ~26 requests for a 180-day backfill (was 1 giant request)
#   * ~150 KB per response (was 3 MB)
#   * failure of one week doesn't destroy the whole backfill
#   * still small enough that boxscore fetches dominate the runtime
DEFAULT_SCHEDULE_CHUNK_DAYS = 7


# ---------------------------------------------------------------------------
# Exception


class StatsApiError(Exception):
    """Rich exception with everything needed to diagnose a Stats API
    failure. Never contains credentials — only URL, params, status,
    a bounded body preview, and the attempt number."""

    def __init__(
        self,
        message: str,
        *,
        url: str = '',
        params: Optional[Dict[str, Any]] = None,
        method: str = 'GET',
        status_code: Optional[int] = None,
        content_type: str = '',
        body_preview: str = '',
        attempt: int = 0,
        max_attempts: int = 0,
        cause: str = '',
    ) -> None:
        super().__init__(message)
        self.message = message
        self.url = url
        self.params = params or {}
        self.method = method
        self.status_code = status_code
        self.content_type = content_type
        self.body_preview = body_preview
        self.attempt = attempt
        self.max_attempts = max_attempts
        self.cause = cause

    def human_summary(self) -> str:
        """One-line, no-Python-traceback summary suitable for a UI card
        and for the BullpenBackfillRun.failure_summary field."""
        parts = []
        if self.status_code is not None:
            parts.append(f'HTTP {self.status_code}')
        else:
            parts.append(self.cause or 'network error')
        parts.append(f'{self.method} {self.url}')
        if self.params:
            parts.append(f'params={self.params}')
        if self.attempt and self.max_attempts:
            parts.append(f'attempt {self.attempt}/{self.max_attempts}')
        if self.body_preview:
            parts.append(f'body="{self.body_preview[:200]}"')
        return ' — '.join(parts)


# ---------------------------------------------------------------------------
# Low-level fetch with retry


def _sleep_for_backoff(attempt: int, retry_after: Optional[float] = None) -> None:
    """Sleep before the next retry. Honors Retry-After when supplied by
    the server (429); otherwise applies exponential backoff with jitter
    to avoid thundering-herd retries when multiple workers hit a
    transient outage simultaneously."""
    if retry_after is not None:
        # Cap Retry-After at BACKOFF_MAX_S so a hostile/misbehaving
        # header can't stall the caller for 5 minutes.
        wait = max(0.1, min(retry_after, BACKOFF_MAX_S))
    else:
        # Exp backoff: 0.75s, 1.5s, 3s, ... capped at 15s. Small jitter
        # so parallel retriers desync naturally.
        wait = min(BACKOFF_BASE_S * (2 ** (attempt - 1)), BACKOFF_MAX_S)
        wait += random.uniform(0, 0.5)  # noqa: S311 — jitter, not crypto
    time.sleep(wait)


def _parse_retry_after(header_value: str) -> Optional[float]:
    """Parse a Retry-After header per RFC 7231. Value can be integer
    seconds or an HTTP-date. We only handle the seconds form — the
    HTTP-date form is rare in practice and safely falls through to
    exponential backoff."""
    if not header_value:
        return None
    try:
        return float(header_value.strip())
    except (TypeError, ValueError):
        return None


def fetch_json(
    path: str,
    *,
    params: Optional[Dict[str, Any]] = None,
    method: str = 'GET',
    max_attempts: int = DEFAULT_RETRIES,
    connect_timeout: float = DEFAULT_CONNECT_TIMEOUT_S,
    read_timeout: float = DEFAULT_READ_TIMEOUT_S,
    user_agent: str = DEFAULT_USER_AGENT,
) -> Any:
    """Fetch `path` from the MLB Stats API and return parsed JSON.

    Raises StatsApiError with rich diagnostic context on:
      * exhausted retries after transient failures
      * permanent 4xx (except 429, which is retried)
      * network / timeout errors after retries
      * non-JSON response body after a 2xx status

    Never raises requests.HTTPError — always wraps.
    """
    base = settings.MLB_STATSAPI_BASE_URL.rstrip('/')
    url = f'{base}{path}'
    headers = {
        'User-Agent': user_agent,
        'Accept': 'application/json',
        # gzip is on by default in requests; naming it here is
        # informational so a debugger can see the intent.
        'Accept-Encoding': 'gzip, deflate',
    }

    last_error: Optional[StatsApiError] = None
    for attempt in range(1, max_attempts + 1):
        try:
            resp = requests.request(
                method, url,
                params=params or {},
                headers=headers,
                timeout=(connect_timeout, read_timeout),
            )
        except requests.Timeout as e:
            last_error = StatsApiError(
                f'Timeout on attempt {attempt}: {e!r}',
                url=url, params=params, method=method,
                attempt=attempt, max_attempts=max_attempts,
                cause='timeout',
            )
            logger.warning('statsapi: %s', last_error.human_summary())
            if attempt < max_attempts:
                _sleep_for_backoff(attempt)
                continue
            raise last_error
        except requests.RequestException as e:
            last_error = StatsApiError(
                f'Network error on attempt {attempt}: {e!r}',
                url=url, params=params, method=method,
                attempt=attempt, max_attempts=max_attempts,
                cause='network',
            )
            logger.warning('statsapi: %s', last_error.human_summary())
            if attempt < max_attempts:
                _sleep_for_backoff(attempt)
                continue
            raise last_error

        # Got a response. Decide retry vs error vs success.
        content_type = resp.headers.get('Content-Type', '')
        body_preview = (resp.text or '')[:400]
        if resp.status_code in RETRYABLE_STATUS_CODES:
            retry_after = _parse_retry_after(resp.headers.get('Retry-After', ''))
            last_error = StatsApiError(
                f'Transient HTTP {resp.status_code} on attempt {attempt}',
                url=url, params=params, method=method,
                status_code=resp.status_code,
                content_type=content_type,
                body_preview=body_preview,
                attempt=attempt, max_attempts=max_attempts,
                cause=f'http_{resp.status_code}',
            )
            logger.warning('statsapi: %s', last_error.human_summary())
            if attempt < max_attempts:
                _sleep_for_backoff(attempt, retry_after=retry_after)
                continue
            raise last_error
        if not resp.ok:
            # Permanent 4xx — do NOT retry. Raise immediately with full
            # context so the operator sees exactly what went wrong.
            raise StatsApiError(
                f'Permanent HTTP {resp.status_code}',
                url=url, params=params, method=method,
                status_code=resp.status_code,
                content_type=content_type,
                body_preview=body_preview,
                attempt=attempt, max_attempts=max_attempts,
                cause=f'http_{resp.status_code}',
            )
        # 2xx.
        try:
            return resp.json()
        except ValueError as e:
            raise StatsApiError(
                f'Response was 2xx but body was not JSON: {e!r}',
                url=url, params=params, method=method,
                status_code=resp.status_code,
                content_type=content_type,
                body_preview=body_preview,
                attempt=attempt, max_attempts=max_attempts,
                cause='non_json_body',
            )

    # Defensive — the loop above always returns or raises.
    if last_error:
        raise last_error
    raise StatsApiError(
        'Retry loop exited without result',
        url=url, params=params, method=method,
        attempt=max_attempts, max_attempts=max_attempts,
        cause='logic_error',
    )


# ---------------------------------------------------------------------------
# Schedule (chunked)


def _iter_windows(
    start: date, end: date, chunk_days: int,
) -> Iterable[tuple]:
    """Yield (start_date, end_date) tuples covering [start, end]
    inclusive, each at most `chunk_days` wide. Deterministic ordering:
    earliest chunk first."""
    if end < start:
        return
    cursor = start
    step = timedelta(days=chunk_days - 1)
    one_day = timedelta(days=1)
    while cursor <= end:
        chunk_end = min(cursor + step, end)
        yield cursor, chunk_end
        cursor = chunk_end + one_day


def fetch_schedule(
    start_date: date, end_date: date,
    *,
    sport_id: int = 1,
    chunk_days: int = DEFAULT_SCHEDULE_CHUNK_DAYS,
    max_attempts: int = DEFAULT_RETRIES,
) -> List[dict]:
    """Fetch /v1/schedule for a date range, chunking into small
    windows internally. Returns a flat list of game dicts (each from
    a `dates[].games[]` block) — filtering happens in the caller.

    Chunking is why an 11-minute Railway backfill can survive a
    transient outage: a single 14-day chunk failing costs at most that
    chunk, not the whole 6-month schedule.
    """
    all_games: List[dict] = []
    for chunk_start, chunk_end in _iter_windows(start_date, end_date, chunk_days):
        data = fetch_json(
            '/v1/schedule',
            params={
                'sportId': sport_id,
                'startDate': chunk_start.isoformat(),
                'endDate': chunk_end.isoformat(),
            },
            max_attempts=max_attempts,
        )
        for dblk in data.get('dates', []) or []:
            for g in dblk.get('games', []) or []:
                all_games.append(g)
    return all_games


def fetch_boxscore(gamepk: int, *, max_attempts: int = DEFAULT_RETRIES) -> dict:
    """Fetch /v1/game/{gamepk}/boxscore. Raises StatsApiError on
    permanent failure so the caller can decide to skip-and-record vs
    fail-the-run."""
    return fetch_json(f'/v1/game/{gamepk}/boxscore', max_attempts=max_attempts)


def fetch_teams(*, season: int, sport_id: int = 1,
                max_attempts: int = DEFAULT_RETRIES) -> List[dict]:
    """Fetch /v1/teams (used only by the connectivity diagnostic)."""
    data = fetch_json(
        '/v1/teams',
        params={'sportId': sport_id, 'season': season},
        max_attempts=max_attempts,
    )
    return data.get('teams', []) or []
