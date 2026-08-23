"""v3.3 SHADOW — tests for the canonical MLB Stats API client.

Locks the resilience contract that the initial Railway backfill
failure exposed the need for. All HTTP is mocked — tests never hit
the live MLB Stats API.

Covers:
  * successful JSON response
  * permanent 4xx (400, 404) → no retry, immediate rich error
  * 429 with Retry-After → sleeps, retries, eventually succeeds
  * transient 5xx (500/502/503/504) → retries then succeeds
  * exhausted retries → StatsApiError with attempt=max
  * timeout → StatsApiError with cause='timeout'
  * non-JSON 2xx body → StatsApiError with cause='non_json_body'
  * User-Agent header is set on every request
  * schedule chunking: 30-day request splits into ceil(30/7) chunks
  * schedule chunk failure → StatsApiError propagates with the failing
    chunk's params intact
"""
from datetime import date
from unittest.mock import MagicMock, patch

from django.test import TestCase

from apps.datahub.providers.mlb.statsapi_client import (
    DEFAULT_SCHEDULE_CHUNK_DAYS,
    StatsApiError,
    _iter_windows,
    fetch_boxscore,
    fetch_json,
    fetch_schedule,
    fetch_teams,
)


def _mock_response(*, status=200, json_data=None, text='', headers=None):
    m = MagicMock()
    m.status_code = status
    m.ok = 200 <= status < 300
    m.text = text or (str(json_data) if json_data is not None else '')
    m.headers = headers or {}
    if json_data is not None:
        m.json = MagicMock(return_value=json_data)
    else:
        m.json = MagicMock(side_effect=ValueError('no json body'))
    return m


# ---------------------------------------------------------------------------
# _iter_windows


class WindowChunkingTests(TestCase):

    def test_seven_day_chunks_over_thirty_days(self):
        chunks = list(_iter_windows(date(2026, 1, 1), date(2026, 1, 30), 7))
        # 30 days / 7 = 5 chunks (each 7 days wide, last one may be shorter).
        self.assertEqual(len(chunks), 5)
        # First chunk starts on the start_date.
        self.assertEqual(chunks[0][0], date(2026, 1, 1))
        # Last chunk ends on the end_date.
        self.assertEqual(chunks[-1][1], date(2026, 1, 30))
        # No overlap: every chunk_start is one day after the prior chunk_end.
        for (s, e), (ns, ne) in zip(chunks[:-1], chunks[1:]):
            self.assertEqual((e - date(2026, 1, 1)).days + 1,
                             (ns - date(2026, 1, 1)).days)

    def test_single_day_window_produces_one_chunk(self):
        chunks = list(_iter_windows(date(2026, 5, 5), date(2026, 5, 5), 7))
        self.assertEqual(chunks, [(date(2026, 5, 5), date(2026, 5, 5))])

    def test_reversed_dates_yield_nothing(self):
        chunks = list(_iter_windows(date(2026, 5, 5), date(2026, 5, 1), 7))
        self.assertEqual(chunks, [])


# ---------------------------------------------------------------------------
# fetch_json — response handling


class FetchJsonSuccessTests(TestCase):

    @patch('apps.datahub.providers.mlb.statsapi_client.requests.request')
    def test_success_returns_parsed_json(self, mock_req):
        mock_req.return_value = _mock_response(json_data={'k': 'v'})
        result = fetch_json('/v1/teams')
        self.assertEqual(result, {'k': 'v'})
        self.assertEqual(mock_req.call_count, 1)

    @patch('apps.datahub.providers.mlb.statsapi_client.requests.request')
    def test_user_agent_header_set(self, mock_req):
        mock_req.return_value = _mock_response(json_data={})
        fetch_json('/v1/teams')
        call_kwargs = mock_req.call_args.kwargs
        self.assertIn('User-Agent', call_kwargs['headers'])
        self.assertIn('BrotherWillies', call_kwargs['headers']['User-Agent'])

    @patch('apps.datahub.providers.mlb.statsapi_client.requests.request')
    def test_timeout_tuple_is_connect_read(self, mock_req):
        mock_req.return_value = _mock_response(json_data={})
        fetch_json('/v1/teams', connect_timeout=5.0, read_timeout=20.0)
        self.assertEqual(mock_req.call_args.kwargs['timeout'], (5.0, 20.0))

    @patch('apps.datahub.providers.mlb.statsapi_client.requests.request')
    def test_url_does_not_double_the_api_prefix(self, mock_req):
        """Regression lock for the 2026-08-22 Railway backfill failure.

        `settings.MLB_STATSAPI_BASE_URL` is `https://statsapi.mlb.com/api`
        — the `/api` is BAKED INTO THE BASE. Client paths must NOT
        redundantly include `/api`. Original bug: caller passed
        `/api/v1/schedule` yielding `https://statsapi.mlb.com/api/api/v1/schedule`
        which returns HTTP 404 `{"path":"api/api/v1/schedule"}`.
        """
        mock_req.return_value = _mock_response(json_data={})
        fetch_json('/v1/schedule', params={'sportId': 1})
        called_url = mock_req.call_args.args[1]
        self.assertNotIn('/api/api/', called_url,
                         msg=f'Doubled /api in URL: {called_url}')
        # And the URL DOES include exactly one /api/v1/ segment.
        self.assertIn('/api/v1/schedule', called_url)


class FetchJsonPermanentErrorTests(TestCase):

    @patch('apps.datahub.providers.mlb.statsapi_client.requests.request')
    def test_permanent_400_raises_with_context(self, mock_req):
        mock_req.return_value = _mock_response(
            status=400, text='Bad Request: invalid startDate',
        )
        with self.assertRaises(StatsApiError) as ctx:
            fetch_json('/v1/schedule', params={'sportId': 1})
        e = ctx.exception
        self.assertEqual(e.status_code, 400)
        self.assertEqual(e.attempt, 1)
        self.assertIn('Bad Request', e.body_preview)
        self.assertIn('sportId', str(e.params))
        # Did NOT retry — permanent 4xx.
        self.assertEqual(mock_req.call_count, 1)

    @patch('apps.datahub.providers.mlb.statsapi_client.requests.request')
    def test_permanent_404_raises_with_context(self, mock_req):
        mock_req.return_value = _mock_response(
            status=404, text='Game not found',
        )
        with self.assertRaises(StatsApiError) as ctx:
            fetch_boxscore(9999999999)
        self.assertEqual(ctx.exception.status_code, 404)
        # Rich human_summary is what the UI shows.
        s = ctx.exception.human_summary()
        self.assertIn('HTTP 404', s)
        self.assertIn('9999999999', s)


class FetchJsonRetryableTests(TestCase):

    @patch('apps.datahub.providers.mlb.statsapi_client.time.sleep')
    @patch('apps.datahub.providers.mlb.statsapi_client.requests.request')
    def test_500_retries_and_succeeds(self, mock_req, mock_sleep):
        mock_req.side_effect = [
            _mock_response(status=500, text='oops'),
            _mock_response(status=503, text='oops2'),
            _mock_response(json_data={'ok': 1}),
        ]
        result = fetch_json('/v1/teams', max_attempts=3)
        self.assertEqual(result, {'ok': 1})
        self.assertEqual(mock_req.call_count, 3)
        # Slept between retries.
        self.assertEqual(mock_sleep.call_count, 2)

    @patch('apps.datahub.providers.mlb.statsapi_client.time.sleep')
    @patch('apps.datahub.providers.mlb.statsapi_client.requests.request')
    def test_429_with_retry_after_honors_header(self, mock_req, mock_sleep):
        mock_req.side_effect = [
            _mock_response(status=429, text='slow down', headers={'Retry-After': '2'}),
            _mock_response(json_data={'ok': 1}),
        ]
        result = fetch_json('/v1/teams', max_attempts=3)
        self.assertEqual(result, {'ok': 1})
        # sleep called with something >= 2.0 (the Retry-After value).
        self.assertGreaterEqual(mock_sleep.call_args_list[0].args[0], 2.0)

    @patch('apps.datahub.providers.mlb.statsapi_client.time.sleep')
    @patch('apps.datahub.providers.mlb.statsapi_client.requests.request')
    def test_exhausted_retries_raises_with_attempt_equal_max(self, mock_req, mock_sleep):
        mock_req.side_effect = [
            _mock_response(status=502), _mock_response(status=502),
            _mock_response(status=502),
        ]
        with self.assertRaises(StatsApiError) as ctx:
            fetch_json('/v1/teams', max_attempts=3)
        e = ctx.exception
        self.assertEqual(e.attempt, 3)
        self.assertEqual(e.max_attempts, 3)
        self.assertEqual(e.status_code, 502)


class FetchJsonTimeoutTests(TestCase):

    @patch('apps.datahub.providers.mlb.statsapi_client.time.sleep')
    @patch('apps.datahub.providers.mlb.statsapi_client.requests.request')
    def test_timeout_raises_with_cause_timeout(self, mock_req, mock_sleep):
        import requests as rq
        mock_req.side_effect = rq.Timeout('read timeout')
        with self.assertRaises(StatsApiError) as ctx:
            fetch_json('/v1/teams', max_attempts=2)
        self.assertEqual(ctx.exception.cause, 'timeout')
        # Retried once (max_attempts=2).
        self.assertEqual(mock_req.call_count, 2)

    @patch('apps.datahub.providers.mlb.statsapi_client.time.sleep')
    @patch('apps.datahub.providers.mlb.statsapi_client.requests.request')
    def test_generic_network_error_raises_with_cause_network(self, mock_req, mock_sleep):
        import requests as rq
        mock_req.side_effect = rq.ConnectionError('connection refused')
        with self.assertRaises(StatsApiError) as ctx:
            fetch_json('/v1/teams', max_attempts=2)
        self.assertEqual(ctx.exception.cause, 'network')


class FetchJsonBadBodyTests(TestCase):

    @patch('apps.datahub.providers.mlb.statsapi_client.requests.request')
    def test_2xx_with_non_json_body_raises(self, mock_req):
        # 200 status but body is HTML (e.g. a WAF interstitial).
        m = MagicMock()
        m.status_code = 200; m.ok = True
        m.text = '<html>Blocked by WAF</html>'
        m.headers = {'Content-Type': 'text/html'}
        m.json = MagicMock(side_effect=ValueError('no json'))
        mock_req.return_value = m
        with self.assertRaises(StatsApiError) as ctx:
            fetch_json('/v1/teams')
        self.assertEqual(ctx.exception.cause, 'non_json_body')
        self.assertIn('WAF', ctx.exception.body_preview)


# ---------------------------------------------------------------------------
# fetch_schedule chunking


class FetchScheduleChunkingTests(TestCase):

    @patch('apps.datahub.providers.mlb.statsapi_client.requests.request')
    def test_thirty_day_window_makes_five_chunk_calls(self, mock_req):
        # 30 days at 7 per chunk → 5 calls.
        mock_req.side_effect = [
            _mock_response(json_data={'dates': [{'games': [{'gamePk': i}]}]})
            for i in range(5)
        ]
        games = fetch_schedule(date(2026, 1, 1), date(2026, 1, 30))
        self.assertEqual(mock_req.call_count, 5)
        # One game per chunk in the mock → 5 total games returned.
        self.assertEqual(len(games), 5)
        # Every call was to /v1/schedule.
        for call in mock_req.call_args_list:
            self.assertIn('/v1/schedule', call.args[1])

    @patch('apps.datahub.providers.mlb.statsapi_client.requests.request')
    def test_180_day_window_defaults_to_weekly_chunks(self, mock_req):
        mock_req.return_value = _mock_response(json_data={'dates': []})
        fetch_schedule(date(2026, 2, 22), date(2026, 8, 21))
        # 181 days / 7 = 26 chunks (last is short).
        self.assertGreaterEqual(mock_req.call_count, 25)
        self.assertLessEqual(mock_req.call_count, 27)

    @patch('apps.datahub.providers.mlb.statsapi_client.time.sleep')
    @patch('apps.datahub.providers.mlb.statsapi_client.requests.request')
    def test_chunk_failure_propagates_with_chunk_params(self, mock_req, mock_sleep):
        mock_req.side_effect = [
            _mock_response(json_data={'dates': []}),
            _mock_response(status=400, text='date out of range'),
        ]
        with self.assertRaises(StatsApiError) as ctx:
            fetch_schedule(date(2026, 1, 1), date(2026, 1, 14), chunk_days=7)
        e = ctx.exception
        # The failing call's chunk params (second chunk starts 2026-01-08)
        # should be present in the exception.
        self.assertIn('2026-01-08', str(e.params))


# ---------------------------------------------------------------------------
# StatsApiError.human_summary


class HumanSummaryTests(TestCase):

    def test_summary_contains_status_and_url(self):
        e = StatsApiError(
            'x',
            url='https://statsapi.mlb.com/v1/schedule',
            params={'sportId': 1, 'startDate': '2026-01-01'},
            status_code=429,
            body_preview='rate limit exceeded',
            attempt=2, max_attempts=3,
        )
        s = e.human_summary()
        self.assertIn('HTTP 429', s)
        self.assertIn('/v1/schedule', s)
        self.assertIn('attempt 2/3', s)
        self.assertIn('rate limit', s)

    def test_summary_when_no_status_uses_cause(self):
        e = StatsApiError(
            'x', url='https://x/api', cause='timeout',
            attempt=3, max_attempts=3,
        )
        self.assertIn('timeout', e.human_summary())
