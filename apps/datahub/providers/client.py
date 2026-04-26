import logging
import time

import requests

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 30
MAX_RETRIES = 3
BACKOFF_BASE = 2  # seconds
BODY_SNIPPET_LEN = 200  # chars of body to include in non-JSON error messages


class NonJSONResponseError(ValueError):
    """Raised when an upstream API returned a non-JSON body (e.g., HTML
    landing page for rate limits, auth redirects, or upstream incidents).

    Treated as a hard error: a non-JSON response is almost never a valid
    state for our ingestion pipeline and silently .json()-ing would either
    raise an opaque JSONDecodeError or, worse, succeed on junk and poison
    the DB.
    """


class APIClient:
    """Shared HTTP client with rate limiting, retries, and logging."""

    def __init__(self, base_url, headers=None, rate_limit_delay=1.0):
        self.base_url = base_url.rstrip('/')
        self.headers = headers or {}
        self.rate_limit_delay = rate_limit_delay
        self._last_request_time = 0

    def _wait_for_rate_limit(self):
        elapsed = time.time() - self._last_request_time
        if elapsed < self.rate_limit_delay:
            time.sleep(self.rate_limit_delay - elapsed)

    @staticmethod
    def _validate_json_response(resp, url):
        """Guardrail against HTML/error-page responses served with 2xx.

        Many providers return a 200 HTML page for rate limits, expired keys,
        WAF challenges, etc. Our pipelines MUST fail loudly in that case
        rather than poison the DB or silently skip.
        """
        ct = resp.headers.get('Content-Type', '')
        body_head = (resp.text or '').lstrip()[:BODY_SNIPPET_LEN]
        if 'json' not in ct.lower():
            raise NonJSONResponseError(
                f"Non-JSON response from {url} "
                f"(status {resp.status_code}, Content-Type {ct!r}): {body_head!r}"
            )
        if body_head[:10].lower().startswith(('<html', '<!doctype', '<?xml')):
            raise NonJSONResponseError(
                f"HTML body from {url} (status {resp.status_code}): {body_head!r}"
            )

    def get(self, path, params=None):
        """GET request with retries and rate limiting. Returns parsed JSON.

        Raises NonJSONResponseError if the server returns a non-JSON body
        (including HTML pages served with 2xx). Raises for HTTP errors after
        exhausting retries. Does NOT retry NonJSONResponseError — an HTML
        landing page rarely fixes itself within seconds.

        Side effect: every Odds API call (success or failure) is logged to
        the OddsApiUsage table via apps.ops.services.api_logging. Logging
        never raises — a DB hiccup writing telemetry must not break ingestion.
        """
        url = f"{self.base_url}/{path.lstrip('/')}" if path else self.base_url
        for attempt in range(1, MAX_RETRIES + 1):
            self._wait_for_rate_limit()
            start = time.time()
            resp = None
            try:
                resp = requests.get(
                    url,
                    params=params,
                    headers=self.headers,
                    timeout=DEFAULT_TIMEOUT,
                )
                duration = time.time() - start
                self._last_request_time = time.time()
                logger.info(
                    f"GET {url} [{resp.status_code}] {duration:.1f}s"
                    f" (attempt {attempt})"
                )
                resp.raise_for_status()
                self._validate_json_response(resp, url)
                # Successful path — log before returning so the row reflects
                # the same status code that the caller sees.
                _log_odds_api_call(
                    url=url, status_code=resp.status_code, success=True,
                    duration_s=duration, error_message='',
                    response_headers=resp.headers,
                )
                return resp.json()
            except requests.exceptions.HTTPError as e:
                duration = time.time() - start
                status = resp.status_code if resp is not None else None
                # Always log the failed attempt — even if we retry, the
                # individual attempt was a real outbound call.
                _log_odds_api_call(
                    url=url, status_code=status, success=False,
                    duration_s=duration, error_message=str(e),
                    response_headers=resp.headers if resp is not None else None,
                )
                if status in (429, 500, 502, 503, 504) and attempt < MAX_RETRIES:
                    wait = BACKOFF_BASE ** attempt
                    logger.warning(
                        f"Retryable error {status} on {url}, "
                        f"waiting {wait}s (attempt {attempt}/{MAX_RETRIES})"
                    )
                    time.sleep(wait)
                    continue
                raise
            except NonJSONResponseError as e:
                duration = time.time() - start
                status = resp.status_code if resp is not None else None
                _log_odds_api_call(
                    url=url, status_code=status, success=False,
                    duration_s=duration, error_message=str(e),
                    response_headers=resp.headers if resp is not None else None,
                )
                raise
            except requests.exceptions.RequestException as e:
                duration = time.time() - start
                _log_odds_api_call(
                    url=url, status_code=None, success=False,
                    duration_s=duration, error_message=str(e),
                    response_headers=None,
                )
                if attempt < MAX_RETRIES:
                    wait = BACKOFF_BASE ** attempt
                    logger.warning(
                        f"Request error on {url}: {e}, "
                        f"waiting {wait}s (attempt {attempt}/{MAX_RETRIES})"
                    )
                    time.sleep(wait)
                    continue
                raise


def _log_odds_api_call(*, url, status_code, success, duration_s,
                       error_message, response_headers):
    """Wrapper that imports the ops service lazily and never raises.

    Lazy import: this module is loaded by Django at app-startup time and
    apps.ops may not be ready in some bootstrap orderings. The import lives
    inside the function so the cost is paid only on outbound calls.
    """
    try:
        from apps.ops.services.api_logging import record_call
        record_call(
            url=url,
            status_code=status_code,
            success=success,
            response_time_ms=int(duration_s * 1000) if duration_s else None,
            error_message=error_message,
            headers=response_headers,
        )
    except Exception as exc:  # noqa: BLE001 — never break ingestion on telemetry
        logger.warning('Ops api logging failed: %s', exc)
