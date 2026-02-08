import logging
import time

import requests

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 30
MAX_RETRIES = 3
BACKOFF_BASE = 2  # seconds


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

    def get(self, path, params=None):
        """GET request with retries and rate limiting. Returns parsed JSON."""
        url = f"{self.base_url}/{path.lstrip('/')}" if path else self.base_url
        for attempt in range(1, MAX_RETRIES + 1):
            self._wait_for_rate_limit()
            start = time.time()
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
                return resp.json()
            except requests.exceptions.HTTPError as e:
                if resp.status_code in (429, 500, 502, 503, 504) and attempt < MAX_RETRIES:
                    wait = BACKOFF_BASE ** attempt
                    logger.warning(
                        f"Retryable error {resp.status_code} on {url}, "
                        f"waiting {wait}s (attempt {attempt}/{MAX_RETRIES})"
                    )
                    time.sleep(wait)
                    continue
                raise
            except requests.exceptions.RequestException as e:
                if attempt < MAX_RETRIES:
                    wait = BACKOFF_BASE ** attempt
                    logger.warning(
                        f"Request error on {url}: {e}, "
                        f"waiting {wait}s (attempt {attempt}/{MAX_RETRIES})"
                    )
                    time.sleep(wait)
                    continue
                raise
