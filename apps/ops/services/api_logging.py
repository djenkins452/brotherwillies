"""API call logging — writes one OddsApiUsage row per outbound Odds API request.

Called from `apps.datahub.providers.client.APIClient.get()`. Detects the Odds
API by base URL (`api.the-odds-api.com`) so we don't double-log calls to ESPN,
CFBD, statsapi.mlb.com, etc. Those providers don't have quotas and aren't on
the same single-vendor failure mode that triggered this whole project.

Why a service module instead of a method on the model:
  - Logging must NEVER raise. A DB hiccup writing telemetry should not break
    the actual ingestion call. We swallow exceptions here and emit a warning.
  - Keeping detection logic (sport from path, success from status_code, etc.)
    out of model code keeps `models.py` describing data shape, not behavior.
"""
import logging
import re

logger = logging.getLogger(__name__)

# The base URL test is intentionally loose — different providers use slightly
# different schemes/subdomains in different code paths but they all hit
# api.the-odds-api.com. Anything matching this fragment is "ours."
ODDS_API_HOSTS = ('the-odds-api.com',)

# The Odds API URL pattern is /v4/sports/<sport_key>/odds/ — pull the key.
# baseball_mlb → mlb, basketball_ncaab → cbb, americanfootball_ncaaf → cfb,
# baseball_ncaa → college_baseball, golf_pga_championship_winner → golf, etc.
SPORT_KEY_RE = re.compile(r'/v4/sports/([^/]+)/')

# Map the Odds API sport_key prefix → our internal sport short-code. We use
# our short-code in the model so dashboards group cleanly by sport.
SPORT_PREFIX_MAP = (
    ('basketball_ncaab', 'cbb'),
    ('americanfootball_ncaaf', 'cfb'),
    ('baseball_mlb', 'mlb'),
    ('baseball_ncaa', 'college_baseball'),
    ('golf_', 'golf'),
)


def is_odds_api_url(url: str) -> bool:
    """True iff this URL is an Odds API endpoint we should bill against quota."""
    if not url:
        return False
    return any(host in url for host in ODDS_API_HOSTS)


def extract_sport(url: str) -> str:
    """Best-effort sport tag from an Odds API URL. Falls back to 'unknown'."""
    if not url:
        return 'unknown'
    m = SPORT_KEY_RE.search(url)
    if not m:
        return 'unknown'
    sport_key = m.group(1)
    for prefix, code in SPORT_PREFIX_MAP:
        if sport_key.startswith(prefix):
            return code
    return 'unknown'


def _to_int(value):
    """Coerce an Odds API header value to int. Returns None if not numeric."""
    if value is None:
        return None
    try:
        return int(value)
    except (ValueError, TypeError):
        return None


def record_call(*, url, status_code, success, response_time_ms,
                error_message='', headers=None):
    """Write one OddsApiUsage row. Never raises.

    Called from the success path AND from the failure paths in APIClient.get().
    `headers` is the raw response.headers mapping when available; we read the
    Odds API quota headers from it (`x-requests-used`, `x-requests-remaining`).
    On a connection error we won't have headers, and that's fine — those
    fields stay null and the dashboard handles missing values.
    """
    if not is_odds_api_url(url):
        return
    try:
        # Local import — apps.ops.models can't be imported at module-load
        # time from APIClient because Django apps may not be ready yet during
        # certain test/management-command bootstrap paths.
        from apps.ops.models import OddsApiUsage

        credits_used = None
        credits_remaining = None
        if headers:
            credits_used = _to_int(headers.get('x-requests-used'))
            credits_remaining = _to_int(headers.get('x-requests-remaining'))

        # Truncate endpoint to the URL path/query — full URL with key would
        # be a security risk if leaked into logs/UI. Strip the apiKey param
        # if it appears in the path.
        endpoint = url
        if 'apiKey=' in endpoint:
            endpoint = re.sub(r'apiKey=[^&]+', 'apiKey=REDACTED', endpoint)
        if len(endpoint) > 500:
            endpoint = endpoint[:500]

        OddsApiUsage.objects.create(
            sport=extract_sport(url),
            endpoint=endpoint,
            status_code=status_code,
            success=success,
            response_time_ms=response_time_ms,
            error_message=error_message[:2000] if error_message else '',
            credits_used=credits_used,
            credits_remaining=credits_remaining,
        )
    except Exception as exc:  # noqa: BLE001 — logging must never break callers
        logger.warning('Failed to write OddsApiUsage row: %s', exc)
