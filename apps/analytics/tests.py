"""Tests for the Backtest Analytics control page.

Coverage targets:
  - Auth/permission gates (staff-only access).
  - Trigger endpoint creates a BacktestRun in 'running' state.
  - Concurrent-run guard blocks a second trigger while one is running.
  - rating_mode tagging follows the elo param + force_use_dynamic.
  - Background thread writes 'completed' / 'failed' status correctly.
  - The page surfaces the latest static + elo runs for comparison.
"""
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.analytics.models import BacktestRun
from apps.analytics.views import _run_backtest_in_background


# Test environment doesn't run collectstatic, so the manifest backend
# can't resolve static URLs. Swap to the simple backend for tests that
# render templates — this matches the workaround used elsewhere in the
# suite for the same root cause.
_NON_MANIFEST_STATICFILES = override_settings(
    STORAGES={
        'default': {
            'BACKEND': 'django.core.files.storage.FileSystemStorage',
        },
        'staticfiles': {
            'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage',
        },
    },
)


@_NON_MANIFEST_STATICFILES
class AccessControlTests(TestCase):
    """Both URLs require an authenticated staff user."""

    def setUp(self):
        self.staff = User.objects.create_user(
            username='staff', password='x', is_staff=True,
        )
        self.regular = User.objects.create_user(username='reg', password='x')

    def test_anonymous_redirected_to_login(self):
        # Anonymous traffic redirects to login (302), not 403.
        resp = self.client.get('/analytics/backtest/')
        self.assertEqual(resp.status_code, 302)
        self.assertIn('login', resp['Location'])

    def test_non_staff_user_gets_403(self):
        self.client.force_login(self.regular)
        resp = self.client.get('/analytics/backtest/')
        self.assertEqual(resp.status_code, 403)

    def test_staff_user_gets_200(self):
        self.client.force_login(self.staff)
        resp = self.client.get('/analytics/backtest/')
        self.assertEqual(resp.status_code, 200)


@_NON_MANIFEST_STATICFILES
class PageContentTests(TestCase):
    """Latest static + elo runs render side-by-side."""

    def setUp(self):
        self.staff = User.objects.create_user(
            username='s', password='x', is_staff=True,
        )
        self.client.force_login(self.staff)

    def _make_run(self, *, rating_mode, status='completed', summary=None):
        return BacktestRun.objects.create(
            sport='all',
            rating_mode=rating_mode,
            status=status,
            summary=summary or {'overall': {'sample': 0, 'roi_pct': None}},
        )

    def test_renders_static_and_elo_in_context(self):
        static_run = self._make_run(rating_mode='static')
        elo_run = self._make_run(rating_mode='elo')
        resp = self.client.get('/analytics/backtest/')
        self.assertEqual(resp.context['static_run'], static_run)
        self.assertEqual(resp.context['elo_run'], elo_run)
        self.assertFalse(resp.context['is_running'])

    def test_renders_running_state_when_run_is_active(self):
        self._make_run(rating_mode='static', status='running')
        resp = self.client.get('/analytics/backtest/')
        self.assertTrue(resp.context['is_running'])
        self.assertEqual(resp.context['auto_refresh_seconds'], 5)

    def test_recent_runs_capped_at_ten(self):
        for i in range(15):
            self._make_run(rating_mode='static')
        resp = self.client.get('/analytics/backtest/')
        self.assertEqual(len(resp.context['recent_runs']), 10)


class TriggerEndpointTests(TestCase):
    """POST /analytics/backtest/run/ creates a 'running' BacktestRun."""

    def setUp(self):
        self.staff = User.objects.create_user(
            username='s', password='x', is_staff=True,
        )
        self.client.force_login(self.staff)

    def test_get_method_not_allowed(self):
        resp = self.client.get('/analytics/backtest/run/')
        self.assertEqual(resp.status_code, 405)

    def test_non_staff_cannot_trigger(self):
        self.client.logout()
        regular = User.objects.create_user(username='r', password='x')
        self.client.force_login(regular)
        resp = self.client.post('/analytics/backtest/run/', {'elo': 'false'})
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(BacktestRun.objects.count(), 0)

    def test_post_creates_running_static_row(self):
        # Patch threading so the background work doesn't actually run
        # during the test — we only care that the row is created.
        with patch('apps.analytics.views.threading.Thread') as fake_thread:
            resp = self.client.post(
                '/analytics/backtest/run/', {'elo': 'false', 'sport': 'all'},
            )
        self.assertEqual(resp.status_code, 302)
        run = BacktestRun.objects.first()
        self.assertEqual(run.status, 'running')
        self.assertEqual(run.rating_mode, 'static')
        self.assertEqual(run.sport, 'all')
        self.assertIsNotNone(run.started_at)
        # The background thread is started exactly once.
        fake_thread.assert_called_once()
        fake_thread.return_value.start.assert_called_once()

    def test_post_with_elo_param_creates_elo_row(self):
        with patch('apps.analytics.views.threading.Thread'):
            self.client.post('/analytics/backtest/run/', {'elo': 'true'})
        run = BacktestRun.objects.first()
        self.assertEqual(run.rating_mode, 'elo')

    def test_concurrent_run_blocked(self):
        # Pre-existing 'running' row must block a new trigger.
        BacktestRun.objects.create(
            sport='all', rating_mode='static', status='running',
            started_at=timezone.now(),
        )
        with patch('apps.analytics.views.threading.Thread') as fake_thread:
            resp = self.client.post('/analytics/backtest/run/', {'elo': 'false'})
        self.assertEqual(resp.status_code, 302)
        # Only the original row exists; the trigger refused to create a second.
        self.assertEqual(BacktestRun.objects.count(), 1)
        fake_thread.assert_not_called()

    def test_invalid_sport_collapses_to_all(self):
        with patch('apps.analytics.views.threading.Thread'):
            self.client.post(
                '/analytics/backtest/run/', {'elo': 'false', 'sport': 'invalid'},
            )
        run = BacktestRun.objects.first()
        self.assertEqual(run.sport, 'all')


class BackgroundExecutionTests(TestCase):
    """The thread body writes status + summary correctly."""

    def setUp(self):
        # Create a 'running' row that the background function will fill in.
        self.run = BacktestRun.objects.create(
            sport='all', rating_mode='static', status='running',
            started_at=timezone.now(),
        )

    def test_successful_run_writes_completed(self):
        # Patch run_backtest to return a stand-in dataclass-like object.
        class _Computed:
            summary = {'overall': {'sample': 1}}
            games_evaluated = 1
            games_skipped = 0
            is_approximate = False
            notes = ''

        with patch(
            'apps.core.services.backtesting_service.run_backtest',
            return_value=_Computed(),
        ):
            _run_backtest_in_background(str(self.run.id), use_elo=False, sport='all')

        self.run.refresh_from_db()
        self.assertEqual(self.run.status, 'completed')
        self.assertEqual(self.run.summary, {'overall': {'sample': 1}})
        self.assertEqual(self.run.games_evaluated, 1)
        self.assertIsNotNone(self.run.finished_at)

    def test_failure_marks_run_as_failed_with_error(self):
        with patch(
            'apps.core.services.backtesting_service.run_backtest',
            side_effect=ValueError('boom'),
        ):
            _run_backtest_in_background(str(self.run.id), use_elo=False, sport='all')

        self.run.refresh_from_db()
        self.assertEqual(self.run.status, 'failed')
        self.assertIn('boom', self.run.error_message)
        self.assertIsNotNone(self.run.finished_at)


class ForceUseDynamicTests(TestCase):
    """The override toggles team_rating_for_model independent of settings."""

    def setUp(self):
        from apps.cfb.models import Conference, Team
        conf = Conference.objects.create(
            name='X', slug=f'x-{timezone.now().timestamp()}',
        )
        self.team = Team.objects.create(
            name='T', slug=f't-{id(self)}', conference=conf, rating=55.0,
        )
        self.team.elo_rating = 1700.0  # projects to legacy ~65.38 (divisor 13)
        self.team.save()
        # Centralize the expected projection so changes to the divisor
        # only need updating once in the test file.
        self.elo_legacy_expected = 50 + 200 / 13  # ≈ 65.38

    @override_settings(USE_DYNAMIC_RATINGS=False)
    def test_override_true_uses_elo_even_with_setting_off(self):
        from apps.core.services.elo_service import (
            force_use_dynamic, team_rating_for_model,
        )
        self.assertAlmostEqual(team_rating_for_model(self.team), 55.0)
        with force_use_dynamic(True):
            self.assertAlmostEqual(
                team_rating_for_model(self.team), self.elo_legacy_expected, places=3,
            )
        # Override cleared on exit.
        self.assertAlmostEqual(team_rating_for_model(self.team), 55.0)

    @override_settings(USE_DYNAMIC_RATINGS=True)
    def test_override_false_uses_static_even_with_setting_on(self):
        from apps.core.services.elo_service import (
            force_use_dynamic, team_rating_for_model,
        )
        self.assertAlmostEqual(
            team_rating_for_model(self.team), self.elo_legacy_expected, places=3,
        )
        with force_use_dynamic(False):
            self.assertAlmostEqual(team_rating_for_model(self.team), 55.0)


class BackCompatRedirectTests(TestCase):
    """The legacy /backtest/ URL redirects to the new analytics page."""

    def setUp(self):
        self.staff = User.objects.create_user(
            username='s', password='x', is_staff=True,
        )
        self.client.force_login(self.staff)

    def test_legacy_url_redirects(self):
        resp = self.client.get('/backtest/')
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp['Location'], '/analytics/backtest/')
