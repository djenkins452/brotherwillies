"""Ops Command Center tests.

Coverage:
  - record_call() writes rows for the-odds-api URLs and ignores others
  - Sport extraction from path
  - cron_run_log() context manager: success, failure, partial, exception
  - is_command_running() guard
  - build_snapshot(): empty DB, healthy DB, failure DB
  - Dashboard view: anon redirect, non-superuser 302, superuser 200, no-data render
  - Manual trigger views: auth gating + anti-overlap
  - Test Odds API trigger: missing key error path

The trigger tests don't actually spawn subprocesses (we monkeypatch
subprocess.Popen) — we only verify the auth + overlap behavior. The
subprocess shape is exercised in production deploys.
"""
from datetime import timedelta
from unittest.mock import patch, MagicMock

from django.contrib.auth.models import User
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from apps.ops.models import CronRunLog, OddsApiUsage
from apps.ops.services.api_logging import (
    extract_sport, is_odds_api_url, record_call,
)
from apps.ops.services.command_center import build_snapshot
from apps.ops.services.cron_logging import cron_run_log, is_command_running


# -------------------- API logging --------------------

class ApiLoggingTests(TestCase):

    def test_is_odds_api_url_matches_known_host(self):
        self.assertTrue(is_odds_api_url('https://api.the-odds-api.com/v4/sports'))
        self.assertTrue(is_odds_api_url(
            'https://api.the-odds-api.com/v4/sports/baseball_mlb/odds/?apiKey=abc'
        ))

    def test_is_odds_api_url_rejects_others(self):
        self.assertFalse(is_odds_api_url('https://site.api.espn.com/baseball'))
        self.assertFalse(is_odds_api_url('https://statsapi.mlb.com/api/v1'))
        self.assertFalse(is_odds_api_url(''))
        self.assertFalse(is_odds_api_url(None))

    def test_extract_sport_from_path(self):
        self.assertEqual(
            extract_sport('https://api.the-odds-api.com/v4/sports/baseball_mlb/odds/'),
            'mlb',
        )
        self.assertEqual(
            extract_sport('https://api.the-odds-api.com/v4/sports/basketball_ncaab/odds/'),
            'cbb',
        )
        self.assertEqual(
            extract_sport('https://api.the-odds-api.com/v4/sports/americanfootball_ncaaf/odds/'),
            'cfb',
        )
        self.assertEqual(
            extract_sport('https://api.the-odds-api.com/v4/sports/baseball_ncaa/odds/'),
            'college_baseball',
        )
        self.assertEqual(
            extract_sport('https://api.the-odds-api.com/v4/sports/golf_pga_championship_winner/odds/'),
            'golf',
        )
        self.assertEqual(
            extract_sport('https://api.the-odds-api.com/v4/sports'),
            'unknown',
        )

    def test_record_call_writes_row_for_odds_api(self):
        record_call(
            url='https://api.the-odds-api.com/v4/sports/baseball_mlb/odds/?apiKey=secret',
            status_code=200,
            success=True,
            response_time_ms=150,
            error_message='',
            headers={'x-requests-used': '42', 'x-requests-remaining': '958'},
        )
        row = OddsApiUsage.objects.get()
        self.assertEqual(row.sport, 'mlb')
        self.assertEqual(row.status_code, 200)
        self.assertTrue(row.success)
        self.assertEqual(row.credits_used, 42)
        self.assertEqual(row.credits_remaining, 958)
        # Secret should be redacted in stored endpoint
        self.assertIn('REDACTED', row.endpoint)
        self.assertNotIn('secret', row.endpoint)

    def test_record_call_skips_non_odds_api(self):
        record_call(
            url='https://site.api.espn.com/baseball/scoreboard',
            status_code=200, success=True, response_time_ms=100,
            error_message='', headers={},
        )
        self.assertEqual(OddsApiUsage.objects.count(), 0)

    def test_record_call_records_401_failure(self):
        record_call(
            url='https://api.the-odds-api.com/v4/sports/baseball_mlb/odds/',
            status_code=401, success=False, response_time_ms=120,
            error_message='401 Client Error: Unauthorized',
            headers={},
        )
        row = OddsApiUsage.objects.get()
        self.assertFalse(row.success)
        self.assertEqual(row.status_code, 401)
        self.assertIn('401', row.error_message)
        self.assertIsNone(row.credits_used)


# -------------------- Cron logging --------------------

class CronLoggingTests(TestCase):

    def test_context_manager_success_path(self):
        with cron_run_log('refresh_data', trigger='cron') as log:
            log.summary = 'all good'
            log.stdout_tail = 'line1\nline2'
        row = CronRunLog.objects.get()
        self.assertEqual(row.status, 'success')
        self.assertEqual(row.command, 'refresh_data')
        self.assertEqual(row.summary, 'all good')
        self.assertIn('line1', row.stdout_tail)
        self.assertIsNotNone(row.completed_at)
        self.assertIsNotNone(row.duration_seconds)

    def test_context_manager_partial(self):
        with cron_run_log('refresh_data') as log:
            log.summary = '3 of 4 sports'
            log.mark_partial('golf failed')
        row = CronRunLog.objects.get()
        self.assertEqual(row.status, 'partial')
        self.assertIn('golf failed', row.error_message)

    def test_context_manager_records_exception(self):
        with self.assertRaises(ValueError):
            with cron_run_log('refresh_data') as log:
                log.summary = 'started'
                raise ValueError('boom')
        row = CronRunLog.objects.get()
        self.assertEqual(row.status, 'failure')
        self.assertIn('ValueError', row.error_message)
        self.assertIn('boom', row.error_message)

    def test_is_command_running_detects_in_flight_row(self):
        CronRunLog.objects.create(command='refresh_data', status='running')
        self.assertTrue(is_command_running('refresh_data'))

    def test_is_command_running_ignores_completed(self):
        CronRunLog.objects.create(
            command='refresh_data', status='success',
            completed_at=timezone.now(), duration_seconds=1.0,
        )
        self.assertFalse(is_command_running('refresh_data'))

    def test_is_command_running_ignores_stuck_row_past_max_age(self):
        # A row stuck running >10min is treated as crashed and DOES NOT block.
        old_row = CronRunLog.objects.create(command='refresh_data', status='running')
        CronRunLog.objects.filter(pk=old_row.pk).update(
            started_at=timezone.now() - timedelta(minutes=20)
        )
        self.assertFalse(is_command_running('refresh_data', max_age_seconds=600))


# -------------------- Snapshot health calculation --------------------

class SnapshotHealthTests(TestCase):

    def test_empty_db_returns_unknown(self):
        snap = build_snapshot()
        self.assertEqual(snap.overall.health, 'unknown')
        self.assertEqual(snap.api.health, 'unknown')
        self.assertEqual(snap.cron.health, 'unknown')
        self.assertEqual(snap.api_stats.last_24h, 0)

    def test_recent_failure_marks_api_red(self):
        OddsApiUsage.objects.create(
            sport='mlb', endpoint='/v4/sports/baseball_mlb/odds/',
            status_code=401, success=False,
        )
        snap = build_snapshot()
        self.assertEqual(snap.api.health, 'red')
        self.assertEqual(snap.overall.health, 'red')

    def test_only_old_failure_marks_api_yellow(self):
        # Older than 1 hour — yellow not red
        old = OddsApiUsage.objects.create(
            sport='mlb', endpoint='/v4/sports/baseball_mlb/odds/',
            status_code=401, success=False,
        )
        OddsApiUsage.objects.filter(pk=old.pk).update(
            timestamp=timezone.now() - timedelta(hours=2)
        )
        snap = build_snapshot()
        self.assertEqual(snap.api.health, 'yellow')

    def test_all_success_marks_api_green(self):
        for _ in range(3):
            OddsApiUsage.objects.create(
                sport='mlb', endpoint='/v4/sports/baseball_mlb/odds/',
                status_code=200, success=True, response_time_ms=120,
                credits_used=10, credits_remaining=990,
            )
        snap = build_snapshot()
        self.assertEqual(snap.api.health, 'green')
        self.assertEqual(snap.api_stats.last_24h, 3)
        self.assertEqual(snap.api_stats.last_24h_failures, 0)
        # Quota: 10 used out of 1000 → green
        self.assertEqual(snap.quota.health, 'green')
        self.assertAlmostEqual(snap.quota.pct_used, 1.0, places=1)

    def test_quota_red_at_high_usage(self):
        OddsApiUsage.objects.create(
            sport='mlb', endpoint='/x', status_code=200, success=True,
            credits_used=950, credits_remaining=50,
        )
        snap = build_snapshot()
        self.assertEqual(snap.quota.health, 'red')

    def test_cron_failure_marks_red(self):
        CronRunLog.objects.create(
            command='refresh_data', status='failure',
            completed_at=timezone.now(), duration_seconds=1.0,
            error_message='boom',
        )
        snap = build_snapshot()
        self.assertEqual(snap.cron.health, 'red')

    def test_cron_partial_marks_yellow(self):
        CronRunLog.objects.create(
            command='refresh_data', status='partial',
            completed_at=timezone.now(), duration_seconds=1.0,
        )
        snap = build_snapshot()
        self.assertEqual(snap.cron.health, 'yellow')

    def test_cron_stuck_running_detected(self):
        row = CronRunLog.objects.create(command='refresh_data', status='running')
        CronRunLog.objects.filter(pk=row.pk).update(
            started_at=timezone.now() - timedelta(minutes=30)
        )
        snap = build_snapshot()
        self.assertEqual(snap.cron.health, 'red')
        # The per-command card should also know it's stuck.
        cmd = next(c for c in snap.cron_commands if c.command == 'refresh_data')
        self.assertTrue(cmd.is_stuck)


# -------------------- Dashboard view --------------------

class CommandCenterViewTests(TestCase):

    def setUp(self):
        self.client = Client()
        self.url = reverse('ops:command_center')

    def test_anonymous_redirected_to_login(self):
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, 302)
        self.assertIn('/accounts/login/', resp.url)

    def test_normal_user_redirected(self):
        joe = User.objects.create_user('joe', password='pw')
        self.client.force_login(joe)
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, 302)

    def test_staff_user_can_access(self):
        # Broadened on 2026-04-26 so the new profile-dropdown link doesn't
        # dead-end for non-superuser staff. is_staff alone is sufficient.
        staff = User.objects.create_user('staffer', password='pw', is_staff=True)
        self.client.force_login(staff)
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, 200)

    def test_superuser_renders_with_no_data(self):
        admin = User.objects.create_superuser('admin', 'a@a.com', 'pw')
        self.client.force_login(admin)
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Ops Command Center')
        # Empty-state copy must appear so we know the no-data path is hit.
        self.assertContains(resp, 'No run history captured yet')

    def test_superuser_renders_with_data(self):
        admin = User.objects.create_superuser('admin', 'a@a.com', 'pw')
        self.client.force_login(admin)
        OddsApiUsage.objects.create(
            sport='mlb', endpoint='/v4/sports/baseball_mlb/odds/',
            status_code=200, success=True, response_time_ms=100,
            credits_used=5, credits_remaining=995,
        )
        CronRunLog.objects.create(
            command='refresh_data', status='success',
            completed_at=timezone.now(), duration_seconds=42.0,
            summary='all good',
        )
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'refresh_data')
        self.assertContains(resp, 'all good')


# -------------------- Manual triggers --------------------

class ManualTriggerTests(TestCase):

    def setUp(self):
        self.client = Client()
        self.refresh_url = reverse('ops:trigger_refresh_data')
        self.scores_url = reverse('ops:trigger_refresh_scores')
        self.test_url = reverse('ops:trigger_test_odds_api')

    def test_anonymous_blocked(self):
        for url in (self.refresh_url, self.scores_url, self.test_url):
            resp = self.client.post(url)
            self.assertEqual(resp.status_code, 302)

    def test_non_superuser_blocked(self):
        joe = User.objects.create_user('joe', password='pw')
        self.client.force_login(joe)
        for url in (self.refresh_url, self.scores_url, self.test_url):
            resp = self.client.post(url)
            self.assertEqual(resp.status_code, 302)

    def test_get_method_rejected(self):
        admin = User.objects.create_superuser('admin', 'a@a.com', 'pw')
        self.client.force_login(admin)
        for url in (self.refresh_url, self.scores_url, self.test_url):
            resp = self.client.get(url)
            self.assertEqual(resp.status_code, 405)

    @patch('apps.ops.views.subprocess.Popen')
    def test_superuser_can_trigger_refresh_data(self, mock_popen):
        admin = User.objects.create_superuser('admin', 'a@a.com', 'pw')
        self.client.force_login(admin)
        resp = self.client.post(self.refresh_url, follow=True)
        self.assertEqual(resp.status_code, 200)
        mock_popen.assert_called_once()
        # Args should include the management command name and trigger flag
        args = mock_popen.call_args[0][0]
        self.assertIn('refresh_data', args)
        self.assertIn('--trigger=manual', args)

    @patch('apps.ops.views.subprocess.Popen')
    def test_overlap_blocks_trigger(self, mock_popen):
        admin = User.objects.create_superuser('admin', 'a@a.com', 'pw')
        self.client.force_login(admin)
        # Pre-existing running row blocks the trigger.
        CronRunLog.objects.create(command='refresh_data', status='running')
        resp = self.client.post(self.refresh_url, follow=True)
        self.assertEqual(resp.status_code, 200)
        mock_popen.assert_not_called()

    def test_test_odds_api_missing_key(self):
        # No ODDS_API_KEY in test settings — the view should error out cleanly.
        with self.settings(ODDS_API_KEY=''):
            admin = User.objects.create_superuser('admin', 'a@a.com', 'pw')
            self.client.force_login(admin)
            resp = self.client.post(self.test_url, follow=True)
            self.assertEqual(resp.status_code, 200)
            # No row should be written when key is missing
            self.assertEqual(OddsApiUsage.objects.count(), 0)

    @patch('apps.ops.views.requests.get')
    def test_test_odds_api_records_success(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {
            'Content-Type': 'application/json',
            'x-requests-used': '12',
            'x-requests-remaining': '988',
        }
        mock_resp.text = '[]'
        mock_get.return_value = mock_resp

        with self.settings(ODDS_API_KEY='test-key'):
            admin = User.objects.create_superuser('admin', 'a@a.com', 'pw')
            self.client.force_login(admin)
            resp = self.client.post(self.test_url, follow=True)
            self.assertEqual(resp.status_code, 200)
            row = OddsApiUsage.objects.get()
            self.assertTrue(row.success)
            self.assertEqual(row.status_code, 200)
            self.assertEqual(row.credits_remaining, 988)

    @patch('apps.ops.views.requests.get')
    def test_test_odds_api_records_401(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 401
        mock_resp.headers = {'Content-Type': 'application/json'}
        mock_resp.text = '{"message":"Invalid key"}'
        mock_get.return_value = mock_resp

        with self.settings(ODDS_API_KEY='bad-key'):
            admin = User.objects.create_superuser('admin', 'a@a.com', 'pw')
            self.client.force_login(admin)
            resp = self.client.post(self.test_url, follow=True)
            self.assertEqual(resp.status_code, 200)
            row = OddsApiUsage.objects.get()
            self.assertFalse(row.success)
            self.assertEqual(row.status_code, 401)


class ProfileDropdownLinkTests(TestCase):
    """The Ops Command Center link in the header profile dropdown should
    appear for staff/superusers and stay hidden for everyone else.

    We render any authenticated page (here: the user_guide page, which is
    login-required and uses base.html) and assert against the rendered
    HTML — keeps the test focused on the dropdown markup, not the Ops
    page itself.
    """

    LINK_HREF = '/ops/command-center/'
    LINK_LABEL = 'Command Center'
    AUTHED_PAGE = '/profile/user-guide/'

    def setUp(self):
        self.client = Client()

    def test_link_hidden_from_normal_user(self):
        joe = User.objects.create_user('joe', password='pw')
        self.client.force_login(joe)
        resp = self.client.get(self.AUTHED_PAGE)
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, self.LINK_HREF)

    def test_link_visible_for_staff(self):
        staff = User.objects.create_user('s', password='pw', is_staff=True)
        self.client.force_login(staff)
        resp = self.client.get(self.AUTHED_PAGE)
        self.assertContains(resp, self.LINK_HREF)
        self.assertContains(resp, self.LINK_LABEL)

    def test_link_visible_for_superuser(self):
        su = User.objects.create_superuser('a', 'a@a.com', 'pw')
        self.client.force_login(su)
        resp = self.client.get(self.AUTHED_PAGE)
        self.assertContains(resp, self.LINK_HREF)

    def test_link_hidden_from_anonymous(self):
        # The login page renders base.html and is guaranteed public.
        resp = self.client.get('/accounts/login/')
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, self.LINK_HREF)

    def test_link_active_state_on_ops_page(self):
        su = User.objects.create_superuser('a', 'a@a.com', 'pw')
        self.client.force_login(su)
        resp = self.client.get('/ops/command-center/')
        self.assertEqual(resp.status_code, 200)
        # Active marker class present on the dropdown item when on /ops/.
        self.assertContains(resp, 'profile-dropdown-item--active')


class HeaderStatusIndicatorTests(TestCase):
    """The colored dot in the header is superuser-only. Coverage:
        - Hidden for anonymous users
        - Hidden for regular logged-in users
        - Hidden for staff (is_staff but NOT is_superuser)
        - Visible for superusers, color matches snapshot health
        - Tooltip carries the API + Cron summary lines
        - Falls back to 'unknown' on a snapshot exception (header survives)
    """

    AUTHED_PAGE = '/profile/user-guide/'
    PUBLIC_PAGE = '/accounts/login/'

    def setUp(self):
        self.client = Client()

    def test_anonymous_does_not_see_indicator(self):
        resp = self.client.get(self.PUBLIC_PAGE)
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, 'system-status-indicator')

    def test_regular_user_does_not_see_indicator(self):
        u = User.objects.create_user('joe', password='pw')
        self.client.force_login(u)
        resp = self.client.get(self.AUTHED_PAGE)
        self.assertNotContains(resp, 'system-status-indicator')

    def test_staff_only_user_does_not_see_indicator(self):
        # The dropdown link is visible to staff; the status dot is NOT.
        # Two different surfaces with two different audiences — keep them
        # separate so we don't surface system-health red flags to anyone
        # who shouldn't be acting on them.
        u = User.objects.create_user('staffer', password='pw', is_staff=True)
        self.client.force_login(u)
        resp = self.client.get(self.AUTHED_PAGE)
        self.assertNotContains(resp, 'system-status-indicator')

    def test_superuser_sees_unknown_dot_when_no_data(self):
        u = User.objects.create_superuser('a', 'a@a.com', 'pw')
        self.client.force_login(u)
        resp = self.client.get(self.AUTHED_PAGE)
        self.assertContains(resp, 'system-status-indicator')
        # Empty DB → unknown (no API calls, no cron rows).
        self.assertContains(resp, 'status-dot--unknown')
        # Tooltip mentions System Status.
        self.assertContains(resp, 'System Status:')

    def test_superuser_sees_green_dot_when_healthy(self):
        u = User.objects.create_superuser('a', 'a@a.com', 'pw')
        self.client.force_login(u)
        # Seed a healthy state.
        OddsApiUsage.objects.create(
            sport='mlb', endpoint='/v4/sports/baseball_mlb/odds/',
            status_code=200, success=True, response_time_ms=100,
            credits_used=5, credits_remaining=995,
        )
        CronRunLog.objects.create(
            command='refresh_data', status='success',
            completed_at=timezone.now(), duration_seconds=42.0,
            summary='all good',
        )
        CronRunLog.objects.create(
            command='refresh_scores_and_settle', status='success',
            completed_at=timezone.now(), duration_seconds=2.0,
        )
        resp = self.client.get(self.AUTHED_PAGE)
        self.assertContains(resp, 'status-dot--green')

    def test_superuser_sees_red_dot_on_recent_api_failure(self):
        u = User.objects.create_superuser('a', 'a@a.com', 'pw')
        self.client.force_login(u)
        OddsApiUsage.objects.create(
            sport='mlb', endpoint='/v4/sports/baseball_mlb/odds/',
            status_code=401, success=False,
            error_message='Unauthorized',
        )
        resp = self.client.get(self.AUTHED_PAGE)
        self.assertContains(resp, 'status-dot--red')

    def test_indicator_links_to_command_center(self):
        u = User.objects.create_superuser('a', 'a@a.com', 'pw')
        self.client.force_login(u)
        resp = self.client.get(self.AUTHED_PAGE)
        # The wrapper anchor must point at the Ops dashboard.
        self.assertContains(resp, 'href="/ops/command-center/"')

    def test_context_processor_survives_snapshot_failure(self):
        # If build_snapshot blows up, the header must not crash. The
        # context processor catches and falls back to 'unknown'. We patch
        # the source module (the lazy import inside the processor resolves
        # there) so the patch is visible at call time.
        u = User.objects.create_superuser('a', 'a@a.com', 'pw')
        self.client.force_login(u)
        with patch(
            'apps.ops.services.command_center.build_snapshot',
            side_effect=RuntimeError('boom'),
        ):
            resp = self.client.get(self.AUTHED_PAGE)
            self.assertEqual(resp.status_code, 200)
            self.assertContains(resp, 'status-dot--unknown')


# =========================================================================
# Provider Health / Circuit Breaker — Auto-failover Commit 1
# =========================================================================

from apps.ops.models import ProviderHealth
from apps.ops.services import provider_health


class ProviderHealthStateTests(TestCase):
    """Pure state-machine coverage. The service is the single source of
    truth for "should I call this provider?" and "should I open the
    breaker?" — every transition must be explicit and reversible."""

    def test_get_creates_row_on_first_use(self):
        self.assertEqual(ProviderHealth.objects.count(), 0)
        provider_health.get('odds_api')
        self.assertEqual(ProviderHealth.objects.count(), 1)
        # Idempotent — second call doesn't create a duplicate.
        provider_health.get('odds_api')
        self.assertEqual(ProviderHealth.objects.count(), 1)

    def test_default_state_is_healthy(self):
        row = provider_health.get('odds_api')
        self.assertEqual(row.state, 'healthy')
        self.assertFalse(row.is_circuit_open)
        self.assertEqual(row.consecutive_failures, 0)

    def test_record_success_sets_last_success_and_clears_failures(self):
        # Pre-seed with some failures + an open circuit.
        provider_health.record_failure('odds_api', status_code=500, error_message='boom')
        provider_health.record_failure('odds_api', status_code=500, error_message='boom')
        row = provider_health.get('odds_api')
        self.assertEqual(row.consecutive_failures, 2)

        # Success should reset everything.
        provider_health.record_success('odds_api', status_code=200)
        row.refresh_from_db()
        self.assertEqual(row.consecutive_failures, 0)
        self.assertIsNone(row.circuit_open_until)
        self.assertEqual(row.last_status_code, 200)
        self.assertIsNotNone(row.last_success_at)
        self.assertEqual(row.state, 'healthy')

    def test_record_failure_increments_counter(self):
        for n in (1, 2):
            provider_health.record_failure('odds_api', status_code=500, error_message='boom')
            row = provider_health.get('odds_api')
            self.assertEqual(row.consecutive_failures, n)
        # 2 failures → 'degraded' but circuit not yet open.
        self.assertEqual(row.state, 'degraded')
        self.assertFalse(row.is_circuit_open)


class CircuitBreakerTriggerTests(TestCase):
    """The three auto-open paths must each fire the breaker on the
    correct event."""

    def test_401_opens_circuit_immediately(self):
        # First and only failure; status_code=401 → open without waiting
        # for the 3-failure threshold.
        provider_health.record_failure(
            'odds_api', status_code=401, error_message='Unauthorized',
        )
        row = provider_health.get('odds_api')
        self.assertTrue(row.is_circuit_open)
        self.assertEqual(row.state, 'circuit_open')
        self.assertIn('401', row.last_open_reason)
        self.assertIn('key', row.last_open_reason.lower())

    def test_429_opens_circuit_immediately(self):
        provider_health.record_failure(
            'odds_api', status_code=429, error_message='quota exceeded',
        )
        row = provider_health.get('odds_api')
        self.assertTrue(row.is_circuit_open)
        self.assertIn('429', row.last_open_reason)
        self.assertIn('quota', row.last_open_reason.lower())

    def test_three_consecutive_failures_opens_circuit(self):
        # Two failures: degraded but not open.
        provider_health.record_failure('odds_api', status_code=500, error_message='boom')
        provider_health.record_failure('odds_api', status_code=503, error_message='boom')
        row = provider_health.get('odds_api')
        self.assertFalse(row.is_circuit_open)

        # Third failure crosses the threshold.
        provider_health.record_failure('odds_api', status_code=500, error_message='boom')
        row.refresh_from_db()
        self.assertTrue(row.is_circuit_open)
        self.assertIn('3 consecutive', row.last_open_reason)

    def test_500_alone_does_not_open_circuit(self):
        # 1-2 server errors are exactly what the retry layer is for —
        # don't open the breaker on a transient blip.
        provider_health.record_failure('odds_api', status_code=500, error_message='boom')
        provider_health.record_failure('odds_api', status_code=500, error_message='boom')
        row = provider_health.get('odds_api')
        self.assertFalse(row.is_circuit_open)
        self.assertEqual(row.state, 'degraded')

    def test_consecutive_counter_resets_on_success(self):
        # 2 failures, then a success, then 2 more failures: should be
        # at 2/3, NOT 4/3 (no breaker).
        provider_health.record_failure('odds_api', status_code=500, error_message='boom')
        provider_health.record_failure('odds_api', status_code=500, error_message='boom')
        provider_health.record_success('odds_api', status_code=200)
        provider_health.record_failure('odds_api', status_code=500, error_message='boom')
        provider_health.record_failure('odds_api', status_code=500, error_message='boom')
        row = provider_health.get('odds_api')
        self.assertEqual(row.consecutive_failures, 2)
        self.assertFalse(row.is_circuit_open)


class CircuitBreakerCooldownTests(TestCase):
    """The circuit's time-based reset is the recovery path. Verify both
    sides: still open before cooldown, automatically closed after."""

    def test_circuit_remains_open_during_cooldown(self):
        provider_health.record_failure('odds_api', status_code=401, error_message='bad key')
        self.assertTrue(provider_health.is_circuit_open('odds_api'))

    def test_circuit_auto_closes_after_cooldown(self):
        # 3 failures (the consecutive-failure path) so post-cooldown state
        # reflects 'failed', not just 'degraded'. The 401 path opens the
        # breaker on a SINGLE failure, which would leave us at
        # consecutive_failures=1 and state='degraded' after cooldown —
        # also correct, just a different scenario.
        provider_health.record_failure('odds_api', status_code=500, error_message='boom')
        provider_health.record_failure('odds_api', status_code=500, error_message='boom')
        provider_health.record_failure('odds_api', status_code=500, error_message='boom')
        self.assertTrue(provider_health.is_circuit_open('odds_api'))
        # Backdate the open-until so we're past the cooldown.
        row = provider_health.get('odds_api')
        row.circuit_open_until = timezone.now() - timedelta(minutes=1)
        row.save()
        self.assertFalse(provider_health.is_circuit_open('odds_api'))
        # 3 failures still on the books → 'failed' (timer expired so not
        # 'circuit_open' anymore, but consecutive_failures >= threshold).
        row.refresh_from_db()
        self.assertEqual(row.state, 'failed')

    def test_success_after_cooldown_returns_to_healthy(self):
        # Simulate the half-open → closed transition: breaker opens, time
        # passes, a probe succeeds, we're healthy again.
        provider_health.record_failure('odds_api', status_code=401, error_message='bad key')
        row = provider_health.get('odds_api')
        row.circuit_open_until = timezone.now() - timedelta(minutes=1)
        row.save()

        provider_health.record_success('odds_api', status_code=200)
        row.refresh_from_db()
        self.assertEqual(row.state, 'healthy')
        self.assertEqual(row.consecutive_failures, 0)
        self.assertIsNone(row.circuit_open_until)


class CircuitBreakerManualControlsTests(TestCase):
    """Manual reset is the operator's escape hatch — clears state without
    waiting for cooldown or successful probe."""

    def test_reset_circuit_clears_open_and_failures(self):
        provider_health.record_failure('odds_api', status_code=401, error_message='bad key')
        self.assertTrue(provider_health.is_circuit_open('odds_api'))

        provider_health.reset_circuit('odds_api')
        row = provider_health.get('odds_api')
        self.assertFalse(row.is_circuit_open)
        self.assertEqual(row.consecutive_failures, 0)
        self.assertEqual(row.last_open_reason, '')

    def test_open_circuit_force_opens(self):
        provider_health.open_circuit('odds_api', reason='manual smoke test')
        row = provider_health.get('odds_api')
        self.assertTrue(row.is_circuit_open)
        self.assertEqual(row.state, 'circuit_open')
        self.assertIn('manual', row.last_open_reason)

    def test_state_summary_returns_dict(self):
        provider_health.record_failure('odds_api', status_code=401, error_message='bad key')
        summary = provider_health.state_summary('odds_api')
        self.assertEqual(summary['provider'], 'odds_api')
        self.assertEqual(summary['state'], 'circuit_open')
        self.assertTrue(summary['is_circuit_open'])
        self.assertEqual(summary['last_status_code'], 401)


class CircuitBreakerExceptionSafetyTests(TestCase):
    """The service must never raise — a DB hiccup writing health state can
    NOT take down the upstream provider call."""

    def test_record_success_swallows_exceptions(self):
        with patch('apps.ops.models.ProviderHealth.objects.get_or_create',
                   side_effect=RuntimeError('db_down')):
            result = provider_health.record_success('odds_api')
            self.assertIsNone(result)  # Returns None, doesn't raise.

    def test_record_failure_swallows_exceptions(self):
        with patch('apps.ops.models.ProviderHealth.objects.get_or_create',
                   side_effect=RuntimeError('db_down')):
            result = provider_health.record_failure('odds_api', status_code=500)
            self.assertIsNone(result)

    def test_is_circuit_open_swallows_exceptions(self):
        with patch('apps.ops.models.ProviderHealth.objects.get_or_create',
                   side_effect=RuntimeError('db_down')):
            # Conservative: when we can't read state, return False (allow
            # the call) rather than blocking the user. Better to make a
            # call than fail closed on a transient DB issue.
            self.assertFalse(provider_health.is_circuit_open('odds_api'))


class CooldownSettingOverrideTests(TestCase):
    """The cooldown is configurable via settings — verify the service
    reads it dynamically (so override_settings works in tests)."""

    def test_cooldown_setting_is_respected(self):
        with self.settings(ODDS_PROVIDER_CIRCUIT_COOLDOWN_MINUTES=5):
            provider_health.record_failure(
                'odds_api', status_code=401, error_message='bad key',
            )
            row = provider_health.get('odds_api')
            self.assertIsNotNone(row.circuit_open_until)
            # Should be ~5 min in the future, not 60.
            delta_min = (row.circuit_open_until - timezone.now()).total_seconds() / 60
            self.assertGreater(delta_min, 4.5)
            self.assertLess(delta_min, 5.5)


class SnapshotSourceFieldDefaultsTests(TestCase):
    """The new odds_source / source_quality fields must default to the
    primary path so existing rows + new rows that don't explicitly set
    them are sane (not orphaned in a 'unavailable' state by accident)."""

    def test_mlb_snapshot_defaults(self):
        from apps.mlb.models import Conference, Game, OddsSnapshot, Team
        conf = Conference.objects.create(name='AL East', slug='al-east')
        h = Team.objects.create(name='Home', slug='home', conference=conf)
        a = Team.objects.create(name='Away', slug='away', conference=conf)
        g = Game.objects.create(
            home_team=h, away_team=a,
            first_pitch=timezone.now() + timedelta(hours=2),
        )
        snap = OddsSnapshot.objects.create(
            game=g,
            captured_at=timezone.now(),
            sportsbook='DraftKings',
            market_home_win_prob=0.5,
            moneyline_home=-110, moneyline_away=-110,
        )
        self.assertEqual(snap.odds_source, 'odds_api')
        self.assertEqual(snap.source_quality, 'primary')

    def test_mlb_snapshot_can_set_fallback(self):
        from apps.mlb.models import Conference, Game, OddsSnapshot, Team
        conf = Conference.objects.create(name='AL East', slug='al-east')
        h = Team.objects.create(name='Home', slug='home', conference=conf)
        a = Team.objects.create(name='Away', slug='away', conference=conf)
        g = Game.objects.create(
            home_team=h, away_team=a,
            first_pitch=timezone.now() + timedelta(hours=2),
        )
        snap = OddsSnapshot.objects.create(
            game=g,
            captured_at=timezone.now(),
            sportsbook='ESPN BET',
            market_home_win_prob=0.5,
            moneyline_home=-110, moneyline_away=-110,
            odds_source='espn',
            source_quality='fallback',
        )
        self.assertEqual(snap.odds_source, 'espn')
        self.assertEqual(snap.source_quality, 'fallback')
