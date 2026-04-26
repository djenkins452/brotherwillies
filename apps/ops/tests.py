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
