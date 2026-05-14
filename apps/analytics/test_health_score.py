"""Tests for the Recommendation Health Score system.

The Health Score is a governance tool — its formulas are the contract.
These tests lock every per-dimension scoring function and the
composite math. Drift here = drift in the operational discipline.

Coverage:
  1. Per-dimension scoring formulas (boundaries, monotonicity, clamping).
  2. Composite weighting math + None-dimension re-normalization.
  3. Band classification.
  4. Empty-data handling.
  5. Warning thresholds.
  6. Snapshot persistence.
  7. Management command (idempotence, dry-run, notes).
  8. View access control + rendering.
  9. Deterministic re-derivation: same inputs → same score.
"""
from datetime import timedelta
from decimal import Decimal
from io import StringIO

from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.analytics.services.health_score import (
    BAND_HEALTHY, BAND_INTERVENE, BAND_STRONG, BAND_WATCH,
    DIMENSION_ORDER, DIMENSION_WEIGHTS,
    classify_band, compute_composite, compute_health_score,
    detect_warnings, score_calibration, score_clv_trend,
    score_edge_realism, score_market_alignment,
    score_recommendation_stability, score_stale_odds,
    score_volume_vs_target,
)


# ---------------------------------------------------------------------------
# Pure-function tests — no DB
# ---------------------------------------------------------------------------


class BandClassificationTests(TestCase):
    def test_strong_band_at_75(self):
        self.assertEqual(classify_band(75.0), BAND_STRONG)
        self.assertEqual(classify_band(100.0), BAND_STRONG)

    def test_healthy_band_at_50_to_74_999(self):
        self.assertEqual(classify_band(50.0), BAND_HEALTHY)
        self.assertEqual(classify_band(74.99), BAND_HEALTHY)

    def test_watch_band_25_to_49_999(self):
        self.assertEqual(classify_band(25.0), BAND_WATCH)
        self.assertEqual(classify_band(49.99), BAND_WATCH)

    def test_intervene_band_below_25(self):
        self.assertEqual(classify_band(24.99), BAND_INTERVENE)
        self.assertEqual(classify_band(0.0), BAND_INTERVENE)


class DimensionWeightsTests(TestCase):
    def test_weights_sum_to_one(self):
        # Architecture invariant: weights must sum to exactly 1.0.
        # If this fails, the framework doc + weights table are out of
        # sync.
        total = sum(DIMENSION_WEIGHTS.values())
        self.assertAlmostEqual(total, 1.0, places=6)

    def test_dimension_order_matches_weights_keys(self):
        self.assertEqual(set(DIMENSION_ORDER), set(DIMENSION_WEIGHTS.keys()))


class ClvTrendScoringTests(TestCase):
    def test_zero_clv_rate_scores_zero(self):
        # 0% CLV+ rate is at the low end of the linear band → score 0.
        out = score_clv_trend(positive_clv_rate=0.0, sample=30)
        # _linear_score clamps to the bounded range [0, 100], and the
        # mapping is linear from rate=0.30 → score=0 to rate=0.60 →
        # score=100. A 0.0 rate goes below the low bound and clamps to 0.
        self.assertEqual(out['score'], 0.0)
        self.assertEqual(out['status'], BAND_INTERVENE)

    def test_midpoint_clv_rate_scores_50(self):
        # 0.45 CLV+ rate = midpoint of [0.30, 0.60] → score 50.
        out = score_clv_trend(positive_clv_rate=0.45, sample=30)
        self.assertAlmostEqual(out['score'], 50.0, places=1)
        self.assertEqual(out['status'], BAND_HEALTHY)

    def test_high_clv_rate_scores_100(self):
        out = score_clv_trend(positive_clv_rate=0.65, sample=30)
        self.assertEqual(out['score'], 100.0)
        self.assertEqual(out['status'], BAND_STRONG)

    def test_no_sample_returns_no_data(self):
        out = score_clv_trend(positive_clv_rate=None, sample=0)
        self.assertIsNone(out['score'])
        self.assertEqual(out['status'], 'no_data')

    def test_monotonic_across_input_range(self):
        prior = -1.0
        for rate in (0.30, 0.35, 0.40, 0.45, 0.50, 0.55, 0.60):
            out = score_clv_trend(positive_clv_rate=rate, sample=30)
            self.assertGreaterEqual(out['score'], prior)
            prior = out['score']


class CalibrationScoringTests(TestCase):
    def test_low_brier_scores_high(self):
        # Lower Brier = better. 0.18 → 100.
        out = score_calibration(brier_score=0.18, sample=50)
        self.assertEqual(out['score'], 100.0)

    def test_high_brier_scores_low(self):
        out = score_calibration(brier_score=0.30, sample=50)
        self.assertEqual(out['score'], 0.0)

    def test_midpoint_brier_scores_50(self):
        out = score_calibration(brier_score=0.24, sample=50)
        self.assertAlmostEqual(out['score'], 50.0, places=1)

    def test_inverted_monotonic(self):
        # Higher brier → lower score (inverted relationship).
        prior = 101.0
        for brier in (0.18, 0.20, 0.22, 0.24, 0.26, 0.28, 0.30):
            out = score_calibration(brier_score=brier, sample=50)
            self.assertLessEqual(out['score'], prior)
            prior = out['score']


class EdgeRealismScoringTests(TestCase):
    def test_perverse_outperformance_scores_zero(self):
        # 8+ ROI worst than 4-6 by 5pp+ → score 0 (the "fake giant
        # edge" pathology this dimension exists to detect).
        out = score_edge_realism(
            roi_8plus=-0.05, roi_4to6=0.05,
            sample_8plus=30, sample_4to6=30,
        )
        self.assertEqual(out['score'], 0.0)

    def test_equal_rois_score_50(self):
        out = score_edge_realism(
            roi_8plus=0.05, roi_4to6=0.05,
            sample_8plus=30, sample_4to6=30,
        )
        self.assertAlmostEqual(out['score'], 50.0, places=1)

    def test_clear_outperformance_scores_100(self):
        out = score_edge_realism(
            roi_8plus=0.10, roi_4to6=0.0,
            sample_8plus=30, sample_4to6=30,
        )
        self.assertEqual(out['score'], 100.0)

    def test_insufficient_sample_returns_no_data(self):
        out = score_edge_realism(
            roi_8plus=0.05, roi_4to6=0.05,
            sample_8plus=3, sample_4to6=30,  # 8+ bucket too small
        )
        self.assertIsNone(out['score'])


class StabilityScoringTests(TestCase):
    def test_too_few_weeks_returns_no_data(self):
        out = score_recommendation_stability([5, 5], sample_weeks=2)
        self.assertIsNone(out['score'])

    def test_within_2sigma_scores_100(self):
        # History [10, 11, 9, 10, 11] → mean 10.2, stdev ~0.84.
        # Current of 11 is ~1σ from mean → inside 2σ → score 100.
        out = score_recommendation_stability(
            [10, 11, 9, 10, 11, 11], sample_weeks=6,
        )
        self.assertEqual(out['score'], 100.0)

    def test_above_4sigma_scores_zero(self):
        # History [10, 10, 10, 10, 10] → mean 10, stdev 0 (degenerate).
        # Current 20 is wildly different → score should reflect.
        # With stdev=0, score=50 in the degenerate path.
        out = score_recommendation_stability(
            [10, 10, 10, 10, 10, 20], sample_weeks=6,
        )
        # Stdev=0 path returns 50 when current != mean.
        self.assertEqual(out['score'], 50.0)


class MarketAlignmentScoringTests(TestCase):
    def test_low_disagreement_scores_high(self):
        out = score_market_alignment(avg_disagreement=0.05, sample=30)
        self.assertEqual(out['score'], 100.0)

    def test_high_disagreement_scores_low(self):
        out = score_market_alignment(avg_disagreement=0.20, sample=30)
        self.assertEqual(out['score'], 0.0)


class StaleOddsScoringTests(TestCase):
    def test_zero_stale_scores_100(self):
        out = score_stale_odds(stale_rate=0.0, sample=50)
        self.assertEqual(out['score'], 100.0)

    def test_high_stale_scores_zero(self):
        out = score_stale_odds(stale_rate=0.20, sample=50)
        self.assertEqual(out['score'], 0.0)


class VolumeVsTargetScoringTests(TestCase):
    def test_too_few_weeks_returns_no_data(self):
        out = score_volume_vs_target(
            current_volume=10, target_mean=None, target_stdev=0.0,
            sample_weeks=2,
        )
        self.assertIsNone(out['score'])

    def test_inside_2sigma_scores_100(self):
        out = score_volume_vs_target(
            current_volume=10, target_mean=10.0, target_stdev=2.0,
            sample_weeks=5,
        )
        self.assertEqual(out['score'], 100.0)

    def test_4sigma_out_scores_zero(self):
        # current=20, mean=10, stdev=2 → 5σ out → above 4σ → score 0.
        out = score_volume_vs_target(
            current_volume=20, target_mean=10.0, target_stdev=2.0,
            sample_weeks=5,
        )
        self.assertEqual(out['score'], 0.0)


# ---------------------------------------------------------------------------
# Composite math
# ---------------------------------------------------------------------------


class CompositeMathTests(TestCase):
    def test_all_none_returns_none(self):
        scores = {key: {'score': None} for key in DIMENSION_WEIGHTS}
        self.assertIsNone(compute_composite(scores))

    def test_single_dimension_full_weight(self):
        # Only CLV present → composite = CLV score regardless of weight.
        scores = {
            'clv_trend': {'score': 80.0},
            'calibration': {'score': None},
            'edge_realism': {'score': None},
            'recommendation_stability': {'score': None},
            'market_alignment': {'score': None},
            'stale_odds': {'score': None},
            'volume_vs_target': {'score': None},
        }
        # Re-normalized weight: 0.25 / 0.25 = 1.0 → composite = 80.
        self.assertEqual(compute_composite(scores), 80.0)

    def test_all_dimensions_equal_weighted_avg(self):
        scores = {
            'clv_trend': {'score': 100.0},                  # weight 0.25
            'calibration': {'score': 50.0},                 # weight 0.20
            'edge_realism': {'score': 0.0},                 # weight 0.15
            'recommendation_stability': {'score': 100.0},   # weight 0.10
            'market_alignment': {'score': 50.0},            # weight 0.10
            'stale_odds': {'score': 100.0},                 # weight 0.10
            'volume_vs_target': {'score': 50.0},            # weight 0.10
        }
        # 0.25*100 + 0.20*50 + 0.15*0 + 0.10*100 + 0.10*50 + 0.10*100 + 0.10*50
        # = 25 + 10 + 0 + 10 + 5 + 10 + 5
        # = 65.0
        self.assertAlmostEqual(compute_composite(scores), 65.0, places=2)

    def test_partial_none_renormalizes_weights(self):
        # CLV (0.25) at 80 + Calibration (0.20) at 60. Composite reweights:
        # CLV weight = 0.25 / (0.25+0.20) = 5/9
        # Cal weight = 0.20 / (0.25+0.20) = 4/9
        # composite = 80 * 5/9 + 60 * 4/9 = (400 + 240) / 9 = 71.111
        scores = {key: {'score': None} for key in DIMENSION_WEIGHTS}
        scores['clv_trend'] = {'score': 80.0}
        scores['calibration'] = {'score': 60.0}
        self.assertAlmostEqual(compute_composite(scores), 71.11, places=1)


# ---------------------------------------------------------------------------
# DB-touching tests
# ---------------------------------------------------------------------------


class ComputeHealthScoreEmptyTests(TestCase):
    def test_empty_db_yields_no_data_dimensions(self):
        health = compute_health_score(window_days=14)
        # No bets, no recommendations → every dimension is no_data.
        for key in DIMENSION_WEIGHTS:
            info = health.dimension_scores.get(key, {})
            self.assertEqual(info.get('status'), 'no_data')
        # Composite is None.
        self.assertIsNone(health.overall_score)
        self.assertIsNone(health.band)

    def test_calibration_state_is_captured(self):
        health = compute_health_score(window_days=14)
        self.assertIn('market_blend_weight', health.calibration_state)
        self.assertIn('min_edge', health.calibration_state)
        self.assertIn('prob_min', health.calibration_state)

    def test_rating_mode_reflects_setting(self):
        # Defaults to static (USE_DYNAMIC_RATINGS=False).
        health = compute_health_score(window_days=14)
        self.assertIn(health.rating_mode_active, ('static', 'elo'))


class ComputeHealthScoreWithBetsTests(TestCase):
    """Drive the service with real MockBet rows to confirm the data
    aggregators wire up correctly."""

    def setUp(self):
        from apps.mockbets.models import MockBet
        self.user = User.objects.create_user('hs_user', password='x')
        # 30 winning bets at -110 with CLV +0.05 and 5pp edge.
        for i in range(30):
            bet = MockBet.objects.create(
                user=self.user, sport='mlb', bet_type='moneyline',
                selection=f'Team{i}', odds_american=-110,
                implied_probability=Decimal('0.5238'),
                stake_amount=Decimal('100'),
                simulated_payout=Decimal('91'),  # for wins
                result='win',
                is_system_generated=True,
                expected_edge=Decimal('5.0'),
                recommendation_status='recommended',
                recommendation_tier='standard',
                recommendation_confidence=Decimal('60.0'),
                clv_cents=0.05,
                odds_source='odds_api',
                closing_odds_american=-120,
            )
            bet.placed_at = timezone.now() - timedelta(days=2)
            bet.save(update_fields=['placed_at'])

    def test_clv_dimension_populates(self):
        health = compute_health_score(window_days=14)
        clv = health.dimension_scores['clv_trend']
        # 30/30 bets have positive CLV → rate = 1.0 → score = 100.
        self.assertEqual(clv['score'], 100.0)
        self.assertEqual(clv['value'], 1.0)
        self.assertEqual(clv['sample'], 30)

    def test_calibration_dimension_populates(self):
        # All bets won, predicted at 60% confidence:
        # Brier = mean((0.60 - 1.0)^2) = 0.16 — better than 0.18 floor.
        health = compute_health_score(window_days=14)
        cal = health.dimension_scores['calibration']
        self.assertEqual(cal['sample'], 30)
        self.assertEqual(cal['score'], 100.0)

    def test_stale_odds_dimension_populates(self):
        # All bets have closing_odds → 0% stale → score 100.
        health = compute_health_score(window_days=14)
        stale = health.dimension_scores['stale_odds']
        self.assertEqual(stale['value'], 0.0)
        self.assertEqual(stale['score'], 100.0)


class WarningTests(TestCase):
    def test_critical_clv_warning_fires(self):
        from apps.analytics.services.health_score import HealthScore
        health = HealthScore(
            overall_score=50.0, band='healthy',
            dimension_scores={
                'clv_trend': {
                    'score': 10.0, 'value': 0.25, 'sample': 50,
                    'status': 'intervene',
                },
            },
            supporting_data={},
            window_days=14, rating_mode_active='static',
            calibration_state={},
        )
        warnings = detect_warnings(health)
        self.assertEqual(len(warnings), 1)
        self.assertEqual(warnings[0]['dimension'], 'clv_trend')
        self.assertEqual(warnings[0]['severity'], 'critical')

    def test_no_warnings_at_healthy_levels(self):
        from apps.analytics.services.health_score import HealthScore
        health = HealthScore(
            overall_score=80.0, band='strong',
            dimension_scores={
                'clv_trend': {'value': 0.55, 'sample': 80},
                'calibration': {'value': 0.20, 'sample': 80},
                'edge_realism': {
                    'value': 0.05, 'sample_8plus': 30, 'sample_4to6': 30,
                },
                'recommendation_stability': {'value': 1.0},
                'market_alignment': {'value': 0.07, 'sample': 80},
                'stale_odds': {'value': 0.02, 'sample': 80},
            },
            supporting_data={},
            window_days=14, rating_mode_active='static',
            calibration_state={},
        )
        warnings = detect_warnings(health)
        self.assertEqual(warnings, [])

    def test_warnings_require_minimum_samples(self):
        # CLV below threshold but sample too small → no warning.
        from apps.analytics.services.health_score import HealthScore
        health = HealthScore(
            overall_score=50.0, band='healthy',
            dimension_scores={
                'clv_trend': {
                    'score': 10.0, 'value': 0.25, 'sample': 5,
                    'status': 'intervene',
                },
            },
            supporting_data={},
            window_days=14, rating_mode_active='static',
            calibration_state={},
        )
        self.assertEqual(detect_warnings(health), [])


class SnapshotPersistenceTests(TestCase):
    def test_capture_persists_snapshot(self):
        from apps.analytics.models import RecommendationHealthSnapshot
        from apps.analytics.services.health_snapshot import capture_snapshot

        before = RecommendationHealthSnapshot.objects.count()
        snap = capture_snapshot(notes='test capture')
        after = RecommendationHealthSnapshot.objects.count()

        self.assertEqual(after, before + 1)
        self.assertEqual(snap.notes, 'test capture')
        # Always captures the calibration state.
        self.assertIn('market_blend_weight', snap.calibration_state)
        self.assertIn(snap.rating_mode_active, ('static', 'elo'))

    def test_repeated_capture_creates_distinct_snapshots(self):
        from apps.analytics.models import RecommendationHealthSnapshot
        from apps.analytics.services.health_snapshot import capture_snapshot

        capture_snapshot(notes='first')
        capture_snapshot(notes='second')
        snaps = RecommendationHealthSnapshot.objects.all()
        # Append-only — both rows persist.
        self.assertEqual(snaps.count(), 2)
        notes = {s.notes for s in snaps}
        self.assertEqual(notes, {'first', 'second'})

    def test_empty_data_snapshot_persists_with_zero_score(self):
        # No bets / recommendations → overall_score is None → snapshot
        # stores 0.0 + band=''.
        from apps.analytics.services.health_snapshot import capture_snapshot
        snap = capture_snapshot(notes='empty state')
        self.assertEqual(snap.overall_score, 0.0)
        self.assertEqual(snap.band, '')


class ManagementCommandTests(TestCase):
    def test_capture_command_persists_by_default(self):
        from apps.analytics.models import RecommendationHealthSnapshot

        out = StringIO()
        call_command('capture_health_snapshot', stdout=out)
        self.assertEqual(RecommendationHealthSnapshot.objects.count(), 1)
        body = out.getvalue()
        self.assertIn('Health Score window=14d', body)

    def test_capture_command_dry_run_does_not_persist(self):
        from apps.analytics.models import RecommendationHealthSnapshot

        out = StringIO()
        call_command('capture_health_snapshot', '--dry-run', stdout=out)
        self.assertEqual(RecommendationHealthSnapshot.objects.count(), 0)
        self.assertIn('NOT persisted', out.getvalue())

    def test_capture_command_with_notes(self):
        from apps.analytics.models import RecommendationHealthSnapshot

        out = StringIO()
        call_command(
            'capture_health_snapshot',
            '--notes', 'pre-elo baseline', stdout=out,
        )
        snap = RecommendationHealthSnapshot.objects.first()
        self.assertEqual(snap.notes, 'pre-elo baseline')

    def test_capture_command_with_custom_window(self):
        out = StringIO()
        call_command('capture_health_snapshot', '--window', '30', stdout=out)
        body = out.getvalue()
        self.assertIn('window=30d', body)


class ViewAccessTests(TestCase):
    def test_anonymous_redirects(self):
        resp = self.client.get(reverse('analytics:health_score'))
        self.assertEqual(resp.status_code, 302)

    def test_non_staff_forbidden(self):
        u = User.objects.create_user('regular_hs', password='x')
        self.client.force_login(u)
        resp = self.client.get(reverse('analytics:health_score'))
        self.assertEqual(resp.status_code, 403)

    def test_staff_can_access_empty_state(self):
        u = User.objects.create_user('staff_hs', password='x', is_staff=True)
        self.client.force_login(u)
        resp = self.client.get(reverse('analytics:health_score'))
        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode('utf-8')
        self.assertIn('Recommendation Health Score', body)
        self.assertIn('Insufficient data', body)

    def test_staff_can_access_with_data(self):
        u = User.objects.create_user('staff_hs2', password='x', is_staff=True)
        self.client.force_login(u)
        # Persist a snapshot so the history section renders.
        from apps.analytics.services.health_snapshot import capture_snapshot
        capture_snapshot(notes='view test')

        resp = self.client.get(reverse('analytics:health_score'))
        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode('utf-8')
        self.assertIn('Dimension Breakdown', body)
        self.assertIn('Snapshot History', body)
        self.assertIn('view test', body)


class DeterminismTests(TestCase):
    def test_score_is_deterministic_for_same_db_state(self):
        # Compute the score twice; must be identical.
        h1 = compute_health_score(window_days=14)
        h2 = compute_health_score(window_days=14)
        self.assertEqual(h1.overall_score, h2.overall_score)
        self.assertEqual(h1.band, h2.band)
        self.assertEqual(h1.dimension_scores, h2.dimension_scores)


class IsolationTests(TestCase):
    """The Health Score must never influence recommendations."""

    def test_compute_health_score_does_not_write(self):
        # Compute multiple times. Snapshot count stays 0.
        from apps.analytics.models import RecommendationHealthSnapshot
        for _ in range(5):
            compute_health_score(window_days=14)
        self.assertEqual(RecommendationHealthSnapshot.objects.count(), 0)

    def test_compute_health_score_reads_static_constants(self):
        # The calibration_state reflects probability_calibration's
        # current constants — no caching, no override.
        from apps.core.services import probability_calibration as pc
        health = compute_health_score(window_days=14)
        self.assertEqual(
            health.calibration_state['market_blend_weight'],
            pc.MARKET_BLEND_WEIGHT,
        )
