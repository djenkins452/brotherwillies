"""Tests for the application-owned V3.2 forward-validation scheduler.

Locks:
  * Gating: skipped for test/migrate/one-off commands and env
    override; started for gunicorn / runserver.
  * Tick fires when last capture is > INTERVAL_SECONDS old.
  * Tick SKIPS when last capture is < INTERVAL_SECONDS old.
  * Refresh_data failure does not stop the scheduler tick.
  * Broad exception handling — one bad tick never propagates.
  * Idempotence: double-firing the tick doesn't create a second
    snapshot for the same (game, engine_version).
  * V3.2 methodology unchanged (all shadow flags remain false).
"""
from __future__ import annotations

import datetime as dt
import sys
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from apps.analytics.services import dedicated_capture_scheduler as sched
from apps.ops.models import CronRunLog


class GatingTests(TestCase):
    def test_skip_when_disabled_by_env(self):
        with patch.dict('os.environ', {'DISABLE_V3_2_SCHEDULER': 'true'}):
            self.assertFalse(sched.should_start_scheduler())

    def test_skip_for_manage_py_test(self):
        with patch.object(sys, 'argv', ['manage.py', 'test']):
            self.assertFalse(sched.should_start_scheduler())

    def test_skip_for_manage_py_migrate(self):
        with patch.object(sys, 'argv', ['manage.py', 'migrate']):
            self.assertFalse(sched.should_start_scheduler())

    def test_skip_for_manage_py_capture_command(self):
        """One-off invocation of capture_v3_2_validation itself must
        NOT spawn the scheduler (would create a nested loop)."""
        with patch.object(sys, 'argv',
                          ['manage.py', 'capture_v3_2_validation']):
            self.assertFalse(sched.should_start_scheduler())

    def test_start_under_gunicorn(self):
        with patch.object(sys, 'argv',
                          ['/usr/local/bin/gunicorn', 'brotherwillies.wsgi']):
            self.assertTrue(sched.should_start_scheduler())

    def test_start_under_runserver(self):
        with patch.object(sys, 'argv', ['manage.py', 'runserver']):
            self.assertTrue(sched.should_start_scheduler())


class IntervalCheckTests(TestCase):
    def test_interval_elapsed_true_when_no_prior_runs(self):
        self.assertTrue(sched._interval_elapsed())

    def test_interval_elapsed_true_when_last_run_older_than_interval(self):
        r = CronRunLog.objects.create(
            command='capture_v3_2_validation',
            trigger='cron', status='success',
        )
        # Force started_at back 20 minutes.
        CronRunLog.objects.filter(id=r.id).update(
            started_at=timezone.now() - dt.timedelta(minutes=20),
        )
        self.assertTrue(sched._interval_elapsed())

    def test_interval_elapsed_false_when_last_run_recent(self):
        r = CronRunLog.objects.create(
            command='capture_v3_2_validation',
            trigger='cron', status='success',
        )
        # Force started_at back 2 minutes — well inside the 10-min
        # interval.
        CronRunLog.objects.filter(id=r.id).update(
            started_at=timezone.now() - dt.timedelta(minutes=2),
        )
        self.assertFalse(sched._interval_elapsed())


class TickFiringTests(TestCase):
    def test_tick_fires_when_no_prior_runs(self):
        """SQLite dev path (no advisory lock) — first tick invokes
        capture_v3_2_validation."""
        with patch.object(sched, '_invoke_capture') as mock_invoke:
            fired = sched.scheduler_tick()
        # SQLite path returns True even without advisory lock (single
        # worker is safe in dev).
        self.assertTrue(fired)
        mock_invoke.assert_called_once()

    def test_tick_skips_when_recent_run_exists(self):
        r = CronRunLog.objects.create(
            command='capture_v3_2_validation',
            trigger='cron', status='success',
        )
        CronRunLog.objects.filter(id=r.id).update(
            started_at=timezone.now() - dt.timedelta(minutes=3),
        )
        with patch.object(sched, '_invoke_capture') as mock_invoke:
            fired = sched.scheduler_tick()
        self.assertFalse(fired)
        mock_invoke.assert_not_called()

    def test_broad_exception_does_not_propagate(self):
        """A broken _invoke_capture must never crash the scheduler
        thread — the tick returns False and the loop continues."""
        with patch.object(sched, '_invoke_capture',
                          side_effect=RuntimeError('boom')):
            # Should NOT raise.
            fired = sched.scheduler_tick()
        # _invoke_capture is called but wrapped internally; tick
        # returns True because the outer sequence completed.
        # Either way, no exception escapes.


class RefreshDataFailureDoesNotStopSchedulerTests(TestCase):
    def test_refresh_data_failure_rows_do_not_block_tick(self):
        """A stack of failed refresh_data rows is unrelated to the
        dedicated capture scheduler — the tick still fires."""
        for _ in range(4):
            CronRunLog.objects.create(
                command='refresh_data', trigger='cron', status='failure',
                summary='mlb failed: something',
            )
        with patch.object(sched, '_invoke_capture') as mock_invoke:
            fired = sched.scheduler_tick()
        self.assertTrue(fired)
        mock_invoke.assert_called_once()


class IdempotenceTests(TestCase):
    def test_double_invoke_does_not_duplicate_snapshot(self):
        """The command's (mlb_game, engine_version) unique constraint
        makes double-firing safe. If the scheduler races itself (which
        the advisory lock prevents but defense-in-depth matters), no
        duplicate row appears."""
        from apps.mlb.models import Conference, Game, Team
        from apps.analytics.models import ForwardValidationSnapshot
        conf = Conference.objects.create(name='MLB', slug='mlb')
        home = Team.objects.create(
            name='Yankees', slug='nyy', conference=conf,
            source='mlb_stats_api', external_id='nyy',
        )
        away = Team.objects.create(
            name='Red Sox', slug='bos', conference=conf,
            source='mlb_stats_api', external_id='bos',
        )
        now = timezone.now()
        Game.objects.create(
            source='mlb_stats_api', external_id='g-race',
            home_team=home, away_team=away,
            first_pitch=now + dt.timedelta(minutes=60),
            status='scheduled',
        )

        class _FakeRec:
            status = 'recommended'
            status_reason = ''
            lane = 'core'
            pick = 'Yankees'
            odds_american = -150
            raw_model_prob = 0.65
            final_model_prob = 0.65
            market_prob = 0.57
            model_edge = 8.0
            confidence_score = 65.0
            tier = 'standard'
            risk_flags = {}
            risk_score = 0
            is_secondary = False
            movement_class = None
            movement_score = None
            movement_supports_pick = False
            market_warning = False
            feature_contributions = {}

        with patch(
            'apps.core.services.recommendations.get_recommendation',
            return_value=_FakeRec(),
        ):
            # Fire twice — second should be a no-op via idempotence.
            sched._invoke_capture()
            # Fake advancing time to bypass the interval check.
            for r in CronRunLog.objects.all():
                CronRunLog.objects.filter(id=r.id).update(
                    started_at=timezone.now() - dt.timedelta(minutes=20),
                )
            sched._invoke_capture()

        self.assertEqual(ForwardValidationSnapshot.objects.count(), 1)


class ProductionFlagsFrozenTests(TestCase):
    def test_shadow_flags_default_false(self):
        from brotherwillies import settings as s
        self.assertFalse(getattr(s, 'USE_BULLPEN_QUALITY', False))
        self.assertFalse(getattr(s, 'USE_BULLPEN_FATIGUE', False))
        self.assertFalse(getattr(s, 'USE_LINEUP_QUALITY', False))
        self.assertFalse(getattr(s, 'USE_TEAM_OFFENSE', False))


class SchedulerStartupIsIdempotentTests(TestCase):
    def test_second_start_returns_existing_thread(self):
        """Guard: AppConfig.ready() may fire twice (test discovery
        edge cases). The second call must NOT spawn a duplicate
        scheduler thread."""
        # Reset module state.
        sched.stop_scheduler()
        sched._scheduler_thread = None
        with patch.object(sys, 'argv',
                          ['/usr/local/bin/gunicorn', 'brotherwillies.wsgi']):
            t1 = sched.start_scheduler_if_appropriate()
            t2 = sched.start_scheduler_if_appropriate()
        self.assertIsNotNone(t1)
        self.assertIs(t1, t2)
        # Cleanup — stop the thread so tearDown doesn't leak it.
        sched.stop_scheduler()
        t1.join(timeout=2)


class CanonicalWindowUnchangedTests(TestCase):
    def test_canonical_window_locked(self):
        """The scheduler must NEVER widen the canonical capture
        window as a workaround for scheduling weakness."""
        from apps.analytics.services import v3_2_capture
        self.assertEqual(v3_2_capture.MIN_WINDOW_MIN, 45)
        self.assertEqual(v3_2_capture.MAX_WINDOW_MIN, 75)

    def test_scheduler_interval_is_below_canonical_window_width(self):
        """The whole point of this scheduler is to guarantee coverage.
        Its interval must be comfortably below the 30-min window."""
        canonical_width = 75 - 45  # 30 min
        # INTERVAL_SECONDS is 10 min = 600s < 30 min → guarantees.
        self.assertLess(sched.INTERVAL_SECONDS, canonical_width * 60)
