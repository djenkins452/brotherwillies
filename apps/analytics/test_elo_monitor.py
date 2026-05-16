"""Tests for Phase 2A Task 4 Elo activation + Elo Activation Monitor.

Coverage:
  1. USE_DYNAMIC_RATINGS defaults to True (post-2026-05-16 activation).
  2. Setting USE_DYNAMIC_RATINGS=False rolls back to static path
     (rollback procedure verified).
  3. Monitor renders activation state correctly.
  4. Monitor finds pre-Elo baseline snapshot by 'pre-elo' note convention.
  5. Score-delta computed correctly when both snapshots exist.
  6. Rollback triggers evaluate against the documented thresholds.
  7. View access control (staff-only).
"""
from datetime import timedelta

from django.conf import settings
from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.analytics.models import RecommendationHealthSnapshot
from apps.analytics.services.elo_monitor import (
    ROLLBACK_CLV_DROP_PP,
    ROLLBACK_HEALTH_DROP_POINTS,
    ROLLBACK_INTERVENE_THRESHOLD,
    build_monitor,
)


# ---------------------------------------------------------------------------
# Activation defaults
# ---------------------------------------------------------------------------


class ElaActivationDefaultsTests(TestCase):
    """The Phase 2A Task 4 activation: USE_DYNAMIC_RATINGS=True is now
    the repo default. Rollback is a one-step env var override."""

    def test_default_is_true(self):
        """Production behavior: with no env var, Elo is active."""
        # Read the setting directly from the loaded settings module.
        # (This is what's in effect for the running process.)
        self.assertTrue(
            getattr(settings, 'USE_DYNAMIC_RATINGS', None),
            'Phase 2A Task 4 sets USE_DYNAMIC_RATINGS=True as the repo default.',
        )

    @override_settings(USE_DYNAMIC_RATINGS=False)
    def test_rollback_via_override_returns_static_path(self):
        """When the env var override is set to False, team_rating_for_model
        falls back to the static rating regardless of elo_rating value.

        This locks the rollback procedure: one env-var change reverts the
        activation, no code revert needed.
        """
        from apps.core.services.elo_service import (
            is_dynamic_active, team_rating_for_model,
        )
        from apps.cfb.models import Conference, Team
        conf = Conference.objects.create(name='X', slug=f'x-{timezone.now().timestamp()}')
        team = Team.objects.create(
            name='T', slug=f't-{id(self)}',
            conference=conf, rating=55.0,
        )
        # Set Elo to something wildly different so a mistake would show up.
        team.elo_rating = 1900.0
        team.save()

        self.assertFalse(is_dynamic_active())
        # Static path wins under override.
        self.assertAlmostEqual(team_rating_for_model(team), 55.0)


# ---------------------------------------------------------------------------
# Monitor service
# ---------------------------------------------------------------------------


class MonitorBuildTests(TestCase):
    """Service-level behavior — pure functions, no view rendering."""

    def _snapshot(self, *, score, band, notes='', mode='static', age_days=0,
                  dim_scores=None):
        snap = RecommendationHealthSnapshot.objects.create(
            overall_score=score,
            band=band,
            dimension_scores=dim_scores or {},
            supporting_data={},
            rating_mode_active=mode,
            calibration_state={},
            notes=notes,
        )
        # Backdate captured_at if requested (auto_now_add forces now;
        # we update after create).
        if age_days:
            snap.captured_at = timezone.now() - timedelta(days=age_days)
            snap.save(update_fields=['captured_at'])
        return snap

    def test_no_snapshots_yields_no_baseline_no_current(self):
        monitor = build_monitor()
        self.assertIsNone(monitor.pre_elo_baseline)
        self.assertIsNone(monitor.current_health)
        self.assertIsNone(monitor.score_delta)

    def test_baseline_detected_by_note_convention(self):
        """A snapshot tagged 'pre-elo baseline' wins as the baseline."""
        self._snapshot(score=60.0, band='healthy', notes='pre-elo baseline',
                       mode='static', age_days=2)
        # A more-recent snapshot without the tag — should NOT become baseline.
        self._snapshot(score=70.0, band='healthy', notes='daily check',
                       mode='elo')
        monitor = build_monitor()
        self.assertIsNotNone(monitor.pre_elo_baseline)
        self.assertEqual(monitor.pre_elo_baseline['overall_score'], 60.0)
        # Current is the most-recent NON-baseline snapshot.
        self.assertEqual(monitor.current_health['overall_score'], 70.0)

    def test_baseline_case_insensitive(self):
        self._snapshot(score=55.0, band='healthy',
                       notes='Pre-Elo Baseline (Day-Of)', age_days=1)
        monitor = build_monitor()
        self.assertIsNotNone(monitor.pre_elo_baseline)

    def test_score_delta_computed(self):
        self._snapshot(score=60.0, band='healthy', notes='pre-elo baseline',
                       mode='static', age_days=2)
        self._snapshot(score=75.0, band='strong', notes='post-elo day 7',
                       mode='elo')
        monitor = build_monitor()
        self.assertEqual(monitor.score_delta, 15.0)

    def test_score_delta_negative_when_current_below_baseline(self):
        self._snapshot(score=60.0, band='healthy', notes='pre-elo baseline',
                       age_days=2)
        self._snapshot(score=40.0, band='watch', notes='post-elo day 3',
                       mode='elo')
        monitor = build_monitor()
        self.assertEqual(monitor.score_delta, -20.0)

    def test_only_baseline_present_is_treated_as_no_current(self):
        """When only the baseline exists, current_health should be None —
        we never use the baseline as both baseline AND current."""
        self._snapshot(score=60.0, band='healthy', notes='pre-elo baseline')
        monitor = build_monitor()
        self.assertIsNotNone(monitor.pre_elo_baseline)
        self.assertIsNone(monitor.current_health)


class RollbackTriggerTests(TestCase):
    """Each documented rollback trigger evaluates correctly."""

    def _snapshot(self, *, score=70.0, band='healthy', notes='', mode='static',
                  age_days=0, dim_scores=None):
        snap = RecommendationHealthSnapshot.objects.create(
            overall_score=score, band=band,
            dimension_scores=dim_scores or {},
            supporting_data={}, rating_mode_active=mode,
            calibration_state={}, notes=notes,
        )
        if age_days:
            snap.captured_at = timezone.now() - timedelta(days=age_days)
            snap.save(update_fields=['captured_at'])
        return snap

    def test_clv_drop_trigger_fires_at_threshold(self):
        """CLV+ rate dropping ≥ ROLLBACK_CLV_DROP_PP from baseline fires."""
        self._snapshot(
            score=70.0, band='healthy', notes='pre-elo baseline',
            age_days=2,
            dim_scores={'clv_trend': {'value': 0.45}},
        )
        # New CLV+ rate is 0.39 (6pp drop, above the 5pp threshold).
        self._snapshot(
            score=65.0, band='healthy', notes='post-elo day 1',
            mode='elo',
            dim_scores={'clv_trend': {'value': 0.39}},
        )
        monitor = build_monitor()
        clv_trigger = next(
            t for t in monitor.rollback_triggers
            if t['name'] == 'CLV deterioration'
        )
        self.assertTrue(clv_trigger['fired'])

    def test_clv_drop_trigger_does_not_fire_below_threshold(self):
        self._snapshot(
            score=70.0, band='healthy', notes='pre-elo baseline',
            age_days=2,
            dim_scores={'clv_trend': {'value': 0.45}},
        )
        # CLV dropped only 2pp; below the trigger threshold.
        self._snapshot(
            score=68.0, band='healthy', notes='post-elo day 1',
            mode='elo',
            dim_scores={'clv_trend': {'value': 0.43}},
        )
        monitor = build_monitor()
        clv_trigger = next(
            t for t in monitor.rollback_triggers
            if t['name'] == 'CLV deterioration'
        )
        self.assertFalse(clv_trigger['fired'])

    def test_health_score_collapse_trigger(self):
        self._snapshot(
            score=70.0, band='healthy', notes='pre-elo baseline', age_days=2,
        )
        # 12-point drop > 10-point threshold.
        self._snapshot(
            score=58.0, band='healthy', notes='post-elo day 1', mode='elo',
        )
        monitor = build_monitor()
        coll = next(
            t for t in monitor.rollback_triggers
            if t['name'] == 'Health Score collapse'
        )
        self.assertTrue(coll['fired'])

    def test_intervene_band_trigger(self):
        self._snapshot(
            score=70.0, band='healthy', notes='pre-elo baseline', age_days=2,
        )
        # Current in INTERVENE band.
        self._snapshot(
            score=20.0, band='intervene', notes='post-elo day 1', mode='elo',
        )
        monitor = build_monitor()
        intv = next(
            t for t in monitor.rollback_triggers
            if 'INTERVENE' in t['name']
        )
        self.assertTrue(intv['fired'])

    def test_edge_realism_inversion_trigger(self):
        """Fake-edge pathology persisting post-Elo fires the inversion trigger."""
        self._snapshot(
            score=70.0, band='healthy', notes='pre-elo baseline', age_days=2,
        )
        self._snapshot(
            score=65.0, band='healthy', notes='post-elo day 1', mode='elo',
            dim_scores={
                'edge_realism': {
                    'value': -0.08,  # 8% below the 4-6 bucket
                    'sample_8plus': 25, 'sample_4to6': 25,
                },
            },
        )
        monitor = build_monitor()
        edge = next(
            t for t in monitor.rollback_triggers
            if 'Edge realism' in t['name']
        )
        self.assertTrue(edge['fired'])

    def test_edge_realism_does_not_fire_with_insufficient_sample(self):
        self._snapshot(
            score=70.0, band='healthy', notes='pre-elo baseline', age_days=2,
        )
        self._snapshot(
            score=65.0, band='healthy', notes='post-elo day 1', mode='elo',
            dim_scores={
                'edge_realism': {
                    'value': -0.08,
                    'sample_8plus': 5, 'sample_4to6': 25,  # 8+ too small
                },
            },
        )
        monitor = build_monitor()
        edge = next(
            t for t in monitor.rollback_triggers
            if 'Edge realism' in t['name']
        )
        self.assertFalse(edge['fired'])

    def test_all_triggers_present_even_without_data(self):
        """The four documented triggers are always evaluated, even
        when sample is insufficient. The status is 'ok' (not fired)
        and the operator sees 'no data yet' rather than a missing entry."""
        monitor = build_monitor()
        # 4 triggers always present.
        names = {t['name'] for t in monitor.rollback_triggers}
        self.assertIn('CLV deterioration', names)
        self.assertIn('Health Score collapse', names)
        self.assertIn('Composite in INTERVENE band', names)
        self.assertTrue(any('Edge realism' in n for n in names))


class ViewAccessTests(TestCase):
    def test_anonymous_redirects(self):
        resp = self.client.get(reverse('analytics:elo_monitor'))
        self.assertEqual(resp.status_code, 302)

    def test_non_staff_forbidden(self):
        u = User.objects.create_user('regular_em', password='x')
        self.client.force_login(u)
        resp = self.client.get(reverse('analytics:elo_monitor'))
        self.assertEqual(resp.status_code, 403)

    def test_staff_can_access_empty_state(self):
        u = User.objects.create_user('staff_em', password='x', is_staff=True)
        self.client.force_login(u)
        resp = self.client.get(reverse('analytics:elo_monitor'))
        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode('utf-8')
        self.assertIn('Elo Activation Monitor', body)
        # Triggers always render even with no data.
        self.assertIn('CLV deterioration', body)
        self.assertIn('Rollback Procedure', body)
        # No pre-Elo baseline yet — guidance message shown.
        self.assertIn('No pre-Elo baseline captured yet', body)

    def test_staff_can_access_with_data(self):
        u = User.objects.create_user('staff_em2', password='x', is_staff=True)
        self.client.force_login(u)
        # Seed a baseline + current snapshot.
        RecommendationHealthSnapshot.objects.create(
            overall_score=60.0, band='healthy', notes='pre-elo baseline',
            dimension_scores={}, supporting_data={},
            rating_mode_active='static', calibration_state={},
        )
        RecommendationHealthSnapshot.objects.create(
            overall_score=70.0, band='healthy', notes='post-elo day 1',
            dimension_scores={}, supporting_data={},
            rating_mode_active='elo', calibration_state={},
        )
        resp = self.client.get(reverse('analytics:elo_monitor'))
        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode('utf-8')
        # Both snapshots render with their scores.
        self.assertIn('60.0', body)
        self.assertIn('70.0', body)
        self.assertIn('Δ 10.0', body)  # The delta line
        self.assertIn('pre-elo baseline', body)


class IsolationTests(TestCase):
    """The monitor cannot influence recommendations."""

    def test_build_monitor_does_not_write(self):
        """Reading the monitor multiple times never writes any state."""
        from apps.analytics.models import RecommendationHealthSnapshot
        before = RecommendationHealthSnapshot.objects.count()
        for _ in range(5):
            build_monitor()
        after = RecommendationHealthSnapshot.objects.count()
        self.assertEqual(after, before)
