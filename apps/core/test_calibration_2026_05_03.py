"""Tests for the 2026-05-03 calibration tighten.

Spec coverage:
  1. Probability shrink: final_prob between model and market, weighted average
  2. Edge calculation uses final_prob (post-blend), not raw model_prob
  3. Threshold filter: tighter MIN_EDGE / MIN_PROBABILITY produces fewer recs
  4. Disagreement cap: triggers when post-blend gap > EXTREME_DISAGREEMENT_GAP
  5. Snapshot fields populated on the Recommendation dataclass
"""
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from apps.core.services.probability_calibration import (
    MARKET_BLEND_WEIGHT,
    MARKET_BLEND_WEIGHT_CAP,
    blend_with_market,
    finalize_win_prob,
)
from apps.core.services.recommendations import (
    EXTREME_DISAGREEMENT_GAP,
    MIN_EDGE,
    MIN_PROBABILITY_FOR_RECOMMENDED,
    LANE_HARD_GATES_EDGE_MIN,
    LANE_HARD_GATES_PROBABILITY_MIN,
)


class ProbabilityShrinkTests(TestCase):
    """Spec Task 1: model probability is shrunk toward market at 30% weight.
    final_prob = model * 0.7 + market * 0.3"""

    def test_constants_match_spec(self):
        self.assertEqual(MARKET_BLEND_WEIGHT, 0.30)
        self.assertEqual(MARKET_BLEND_WEIGHT_CAP, 0.30)

    def test_final_prob_is_weighted_average(self):
        # Spec: final_prob = model * 0.7 + market * 0.3
        result = blend_with_market(0.80, 0.50)
        self.assertAlmostEqual(result, 0.80 * 0.70 + 0.50 * 0.30, places=6)

    def test_final_prob_lies_between_model_and_market(self):
        for model, market in [(0.80, 0.50), (0.40, 0.55), (0.65, 0.65), (0.30, 0.45)]:
            result = blend_with_market(model, market)
            lo, hi = min(model, market), max(model, market)
            # Use almostEqual-style bounds — float arithmetic gives values
            # that can land 1e-16 below the lo bound (e.g. 0.65 input
            # rounding to 0.6499999999999999) which is still semantically
            # within the band.
            self.assertGreaterEqual(result, lo - 1e-9)
            self.assertLessEqual(result, hi + 1e-9)

    def test_no_market_passes_through(self):
        # No-odds slate → blend is a no-op (preserves model output).
        self.assertAlmostEqual(blend_with_market(0.72, None), 0.72)


class ThresholdsTightenedTests(TestCase):
    """Spec Task 3: MIN_EDGE and MIN_PROBABILITY_FOR_RECOMMENDED bumped.
    Plus: lane hard-gates kept in lock-step so a pick never clears the
    lane while failing the recommendation-status gates."""

    def test_min_edge_bumped(self):
        self.assertEqual(MIN_EDGE, 5.0)

    def test_min_probability_bumped(self):
        self.assertEqual(MIN_PROBABILITY_FOR_RECOMMENDED, 0.60)

    def test_lane_edge_gate_matches_min_edge(self):
        # Lane edge gate is decimal; MIN_EDGE is pp. 0.05 == 5pp.
        self.assertAlmostEqual(LANE_HARD_GATES_EDGE_MIN * 100, MIN_EDGE)

    def test_lane_probability_gate_matches_min_probability(self):
        self.assertAlmostEqual(
            LANE_HARD_GATES_PROBABILITY_MIN, MIN_PROBABILITY_FOR_RECOMMENDED,
        )


class DisagreementCapTests(TestCase):
    """Spec Task 4: when |model - market| > 0.15 (raw), downgrade tier
    but DO NOT block. Implemented as |final - market| > 0.105 post-blend
    since the 30% blend compresses the raw gap by a factor of 0.70."""

    def test_cap_threshold_value(self):
        # 0.15 * (1 - 0.30) = 0.105
        self.assertAlmostEqual(EXTREME_DISAGREEMENT_GAP, 0.105, places=6)

    def test_cap_does_not_fire_inside_band(self):
        # Build a recommendation with a moderate model-market disagreement
        # that should leave tier untouched.
        from apps.mlb.models import Conference, Team, Game, OddsSnapshot
        from apps.core.services.recommendations import get_recommendation
        conf = Conference.objects.create(name='AL-cap1', slug='al-cap1')
        h = Team.objects.create(name='H', slug='cap1-h', conference=conf,
                                rating=70, source='mlb_stats_api', external_id='cap1-1')
        a = Team.objects.create(name='A', slug='cap1-a', conference=conf,
                                rating=50, source='mlb_stats_api', external_id='cap1-2')
        g = Game.objects.create(
            home_team=h, away_team=a,
            first_pitch=timezone.now() + timedelta(hours=2),
            status='scheduled',
            source='mlb_stats_api', external_id='cap1-g',
        )
        OddsSnapshot.objects.create(
            game=g, captured_at=timezone.now(),
            market_home_win_prob=0.55, moneyline_home=-130, moneyline_away=120,
        )
        rec = get_recommendation('mlb', g)
        self.assertIsNotNone(rec)
        # extreme_disagreement is the recorded flag — verify it didn't fire.
        self.assertFalse(
            rec.extreme_disagreement,
            f'cap should not fire inside the band; gap was '
            f'{abs(rec.final_model_prob - rec.market_prob):.4f}',
        )

    def test_cap_fires_on_extreme_disagreement(self):
        # Set up a market that strongly disagrees with the model.
        # Rating gap 90 vs 10 → high model prob; market priced as a coin
        # flip → blend pulls toward market but residual gap > threshold.
        from apps.mlb.models import Conference, Team, Game, OddsSnapshot
        from apps.core.services.recommendations import get_recommendation
        conf = Conference.objects.create(name='AL-cap2', slug='al-cap2')
        h = Team.objects.create(name='H', slug='cap2-h', conference=conf,
                                rating=90, source='mlb_stats_api', external_id='cap2-1')
        a = Team.objects.create(name='A', slug='cap2-a', conference=conf,
                                rating=10, source='mlb_stats_api', external_id='cap2-2')
        g = Game.objects.create(
            home_team=h, away_team=a,
            first_pitch=timezone.now() + timedelta(hours=2),
            status='scheduled',
            source='mlb_stats_api', external_id='cap2-g',
        )
        OddsSnapshot.objects.create(
            game=g, captured_at=timezone.now(),
            market_home_win_prob=0.50, moneyline_home=-110, moneyline_away=-110,
        )
        rec = get_recommendation('mlb', g)
        self.assertIsNotNone(rec)
        self.assertTrue(rec.extreme_disagreement)
        # Cap downgrades elite/strong → standard but does NOT block.
        # (We can't always assert tier because clamp may keep prob in
        # range below the elite edge band — but we CAN assert the bet
        # was not blocked: status remains 'recommended' or 'value',
        # not 'high_juice' or other rejection reasons.)
        self.assertNotEqual(rec.tier, 'elite')
        self.assertNotEqual(rec.tier, 'strong')

    def test_cap_does_not_block_the_recommendation(self):
        """Spec: 'Do NOT block the bet'. Even when the cap fires, the
        recommendation row is still produced; only the tier is downgraded."""
        from apps.mlb.models import Conference, Team, Game, OddsSnapshot
        from apps.core.services.recommendations import get_recommendation
        conf = Conference.objects.create(name='AL-cap3', slug='al-cap3')
        h = Team.objects.create(name='H', slug='cap3-h', conference=conf,
                                rating=90, source='mlb_stats_api', external_id='cap3-1')
        a = Team.objects.create(name='A', slug='cap3-a', conference=conf,
                                rating=10, source='mlb_stats_api', external_id='cap3-2')
        g = Game.objects.create(
            home_team=h, away_team=a,
            first_pitch=timezone.now() + timedelta(hours=2),
            status='scheduled',
            source='mlb_stats_api', external_id='cap3-g',
        )
        OddsSnapshot.objects.create(
            game=g, captured_at=timezone.now(),
            market_home_win_prob=0.50, moneyline_home=-110, moneyline_away=-110,
        )
        rec = get_recommendation('mlb', g)
        # Returned, not None — the cap downgrades but never blocks.
        self.assertIsNotNone(rec)


class SnapshotFieldsTests(TestCase):
    """Spec Task 5: store final_prob, market_prob, edge alongside the
    existing confidence_score / model_edge. raw_model_prob is None in v1
    (requires sport-service plumbing as a follow-up)."""

    def test_recommendation_carries_snapshot_fields(self):
        from apps.mlb.models import Conference, Team, Game, OddsSnapshot
        from apps.core.services.recommendations import get_recommendation
        conf = Conference.objects.create(name='AL-snap', slug='al-snap')
        h = Team.objects.create(name='H', slug='snap-h', conference=conf,
                                rating=85, source='mlb_stats_api', external_id='snap-1')
        a = Team.objects.create(name='A', slug='snap-a', conference=conf,
                                rating=25, source='mlb_stats_api', external_id='snap-2')
        g = Game.objects.create(
            home_team=h, away_team=a,
            first_pitch=timezone.now() + timedelta(hours=2),
            status='scheduled',
            source='mlb_stats_api', external_id='snap-g',
        )
        OddsSnapshot.objects.create(
            game=g, captured_at=timezone.now(),
            market_home_win_prob=0.45, moneyline_home=-110, moneyline_away=-110,
        )
        rec = get_recommendation('mlb', g)
        self.assertIsNotNone(rec)
        # Final prob: decimal mirror of confidence_score (which is rounded
        # to 1 decimal place — so we compare at places=1 not places=2).
        self.assertIsNotNone(rec.final_model_prob)
        self.assertAlmostEqual(rec.final_model_prob * 100.0, rec.confidence_score, places=1)
        # Market prob: decimal, picked-side de-vigged.
        self.assertIsNotNone(rec.market_prob)
        self.assertGreaterEqual(rec.market_prob, 0.0)
        self.assertLessEqual(rec.market_prob, 1.0)
        # extreme_disagreement is a bool, always set.
        self.assertIn(rec.extreme_disagreement, (True, False))
        # raw_model_prob is None in v1 (sport plumbing follow-up).
        self.assertIsNone(rec.raw_model_prob)


class EdgeUsesPostBlendProbTests(TestCase):
    """Spec Task 2: edge calculation uses final_prob (post-blend), not raw."""

    def test_edge_equals_final_minus_market(self):
        from apps.mlb.models import Conference, Team, Game, OddsSnapshot
        from apps.core.services.recommendations import get_recommendation
        conf = Conference.objects.create(name='AL-edge', slug='al-edge')
        h = Team.objects.create(name='H', slug='edge-h', conference=conf,
                                rating=80, source='mlb_stats_api', external_id='edge-1')
        a = Team.objects.create(name='A', slug='edge-a', conference=conf,
                                rating=30, source='mlb_stats_api', external_id='edge-2')
        g = Game.objects.create(
            home_team=h, away_team=a,
            first_pitch=timezone.now() + timedelta(hours=2),
            status='scheduled',
            source='mlb_stats_api', external_id='edge-g',
        )
        OddsSnapshot.objects.create(
            game=g, captured_at=timezone.now(),
            market_home_win_prob=0.50, moneyline_home=-110, moneyline_away=-110,
        )
        rec = get_recommendation('mlb', g)
        self.assertIsNotNone(rec)
        # edge_pp = (final_prob - market_prob) * 100, rounded to 1dp.
        # That is: edge is computed on the post-blend prob, not raw.
        expected_edge_pp = round((rec.final_model_prob - rec.market_prob) * 100.0, 1)
        self.assertAlmostEqual(rec.model_edge, expected_edge_pp, places=1)


class FewerRecommendationsTests(TestCase):
    """Spec Task 3 success criterion: tighter thresholds = fewer 'Recommended'.
    A pick that scraped through under MIN_EDGE=3 / MIN_PROB=0.55 should
    now fall short of MIN_EDGE=5 / MIN_PROB=0.60."""

    def test_marginal_pick_no_longer_recommended(self):
        from apps.core.services.recommendations import compute_status, STATUS_NOT_RECOMMENDED
        # 4pp edge + 57% prob — would have cleared the old 3pp/55% gates.
        # Under the new 5pp/60% gates it fails. Probability gate fires
        # before edge gate per compute_status ordering, so reason is
        # 'low_probability' here.
        status, reason = compute_status(
            model_edge=4.0, odds_american=-110, probability=0.57,
        )
        self.assertEqual(status, STATUS_NOT_RECOMMENDED)
        self.assertEqual(reason, 'low_probability')

    def test_marginal_edge_at_high_prob_no_longer_recommended(self):
        from apps.core.services.recommendations import compute_status, STATUS_NOT_RECOMMENDED
        # 4pp edge + 65% prob — clears the new probability gate but
        # fails the new MIN_EDGE=5.0 gate.
        status, reason = compute_status(
            model_edge=4.0, odds_american=-110, probability=0.65,
        )
        self.assertEqual(status, STATUS_NOT_RECOMMENDED)
        self.assertEqual(reason, 'low_edge')

    def test_low_probability_no_longer_recommended(self):
        from apps.core.services.recommendations import compute_status, STATUS_NOT_RECOMMENDED
        # 6pp edge but only 56% prob — fails the new 60% prob gate.
        status, reason = compute_status(
            model_edge=6.0, odds_american=-110, probability=0.56,
        )
        self.assertEqual(status, STATUS_NOT_RECOMMENDED)
        self.assertEqual(reason, 'low_probability')

    def test_pick_that_clears_new_gates_is_recommended(self):
        from apps.core.services.recommendations import compute_status, STATUS_RECOMMENDED
        # 7pp edge + 65% prob — clears all gates including the new tighter ones.
        status, reason = compute_status(
            model_edge=7.0, odds_american=-110, probability=0.65,
        )
        self.assertEqual(status, STATUS_RECOMMENDED)
        self.assertEqual(reason, '')
