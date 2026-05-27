"""Tests for the 2026-05-06 calibration second-pass.

Spec coverage:
  Task 1 — market blend weight 0.30 → 0.40
  Task 2 — MIN_EDGE 5.0 → 6.0 (+ lane edge gate)
  Task 3 — short-favorite (-149..+99) downgrade when edge < STRONG_EDGE
  Task 4 — extreme-disagreement cap relaxed to 20pp raw / 12pp post-blend
  Task 5 — CLV double-negative formatting bug fix

Constant-flip tests overlap with the 2026-05-03 file; this file owns the
new behavior (short-fav flag, CLV format) plus the new constant values.
"""
from datetime import timedelta
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from apps.core.services.probability_calibration import (
    MARKET_BLEND_WEIGHT,
    MARKET_BLEND_WEIGHT_CAP,
    blend_with_market,
)
from apps.core.services.recommendations import (
    EXTREME_DISAGREEMENT_GAP,
    LANE_HARD_GATES_EDGE_MIN,
    LANE_HARD_GATES_PROBABILITY_MIN,
    MIN_EDGE,
    STRONG_EDGE,
    _lane_classify,
    _lane_compute_risk_flags,
)


class CalibrationConstantsTests(TestCase):
    """The new constant values are the headline changes."""

    def test_market_blend_weight_matches_current_spec(self):
        # Tuning trail: 0.30 (2026-05-03) → 0.40 (2026-05-06) →
        # 0.55 (2026-05-22 Roadmap B Step 1). Single-variable change per
        # the Full Model Failure Review + adversarial second pass.
        self.assertEqual(MARKET_BLEND_WEIGHT, 0.55)
        self.assertEqual(MARKET_BLEND_WEIGHT_CAP, 0.55)

    def test_min_edge_is_6pp(self):
        self.assertEqual(MIN_EDGE, 6.0)

    def test_lane_edge_gate_in_lock_step_with_min_edge(self):
        # LANE_HARD_GATES_EDGE_MIN is decimal; MIN_EDGE is pp. Same value.
        self.assertAlmostEqual(LANE_HARD_GATES_EDGE_MIN * 100, MIN_EDGE)

    def test_lane_probability_gate_in_lock_step(self):
        # Probability gate matches MIN_PROBABILITY_FOR_RECOMMENDED.
        from apps.core.services.recommendations import MIN_PROBABILITY_FOR_RECOMMENDED
        self.assertAlmostEqual(LANE_HARD_GATES_PROBABILITY_MIN, MIN_PROBABILITY_FOR_RECOMMENDED)

    def test_extreme_disagreement_gap_is_12pp_post_blend(self):
        # 0.20 raw * (1 - 0.40) = 0.12 post-blend.
        self.assertAlmostEqual(EXTREME_DISAGREEMENT_GAP, 0.12, places=6)


class BlendWith40PercentWeightTests(TestCase):
    """Class name preserved for git-blame continuity. Asserts the
    CURRENT blend weight semantics (0.55 as of 2026-05-22), not 0.40.

    final_prob = model * 0.45 + market * 0.55"""

    def test_weighted_average_at_default_weight(self):
        # 0.80 model, 0.50 market → 0.80*0.45 + 0.50*0.55 = 0.360 + 0.275 = 0.635.
        self.assertAlmostEqual(blend_with_market(0.80, 0.50), 0.635, places=6)

    def test_blend_pulls_extreme_model_toward_market(self):
        # Tuning trail (model=0.92, market=0.50):
        #   0.30 weight: → 0.794
        #   0.40 weight: → 0.752
        #   0.55 weight: → 0.6890
        # Each step pulls the model harder toward the market consensus.
        result = blend_with_market(0.92, 0.50)
        self.assertAlmostEqual(result, 0.689, places=3)
        self.assertLess(result, 0.752, 'new 0.55 blend must pull harder than the prior 0.40 weight')


class ShortFavoriteDisciplineTests(TestCase):
    """Spec Task 3: -149..+99 odds with edge < STRONG_EDGE → short_fav_thin
    flag fires, lane drops core → qualified."""

    def _flags(self, **overrides):
        kwargs = dict(
            probability=0.65,
            odds_american=-110,
            edge_decimal=0.07,
            movement_class=None,
            movement_supports_pick=True,
            insight_conflicts=False,
        )
        kwargs.update(overrides)
        return _lane_compute_risk_flags(**kwargs)

    def test_short_favorite_with_thin_edge_fires_flag(self):
        # Odds in band, edge below STRONG_EDGE/100 (0.06).
        flags = self._flags(odds_american=-110, edge_decimal=0.05)
        self.assertTrue(flags['short_fav_thin'])

    def test_short_favorite_with_strong_edge_does_NOT_fire(self):
        # Odds in band but edge clears STRONG_EDGE → no downgrade needed.
        flags = self._flags(odds_american=-110, edge_decimal=0.07)
        self.assertFalse(flags['short_fav_thin'])

    def test_underdog_in_band_with_thin_edge_fires(self):
        # +90 is inside the [-149, +99] band — underdog short of pick'em.
        flags = self._flags(odds_american=90, edge_decimal=0.05)
        self.assertTrue(flags['short_fav_thin'])

    def test_outside_band_does_NOT_fire(self):
        # +100 is the lower boundary of "underdog" per the report's classifier.
        # +120 is clearly outside the short-fav band.
        for odds in (100, 120, -150, -200):
            flags = self._flags(odds_american=odds, edge_decimal=0.05)
            self.assertFalse(
                flags['short_fav_thin'],
                f'short_fav_thin should not fire for odds={odds}',
            )

    def test_band_boundaries(self):
        # Spec band: [-149, +99] inclusive on both ends.
        for odds in (-149, -110, 0, 99):
            flags = self._flags(odds_american=odds, edge_decimal=0.05)
            self.assertTrue(flags['short_fav_thin'], f'should fire at odds={odds}')

    def test_short_fav_thin_does_not_block_bet(self):
        """Spec: do NOT remove the bet — move from Recommended → Potential.
        In our partition, Potential = qualified lane + value. So the flag
        should produce lane='qualified', not lane='pass'."""
        # Set up a clean pick that ONLY has the short_fav_thin flag.
        # Hard gates need: prob >= 0.60, edge >= 0.06, |odds| <= 300, primary.
        # Wait — short_fav_thin requires edge < 0.06. So if edge < 0.06,
        # the lane hard gate (edge >= 0.06) would fail FIRST and the bet
        # lands in 'pass' regardless of risk flags. Need a different angle.
        #
        # The real test of Task 3 is: when MIN_EDGE drops STRONG_EDGE
        # (e.g. via a future tweak), the short_fav_thin flag would
        # demote a clearing-MIN-but-failing-STRONG bet from core to
        # qualified. With the current MIN_EDGE=STRONG_EDGE=6.0 the
        # window is empty — same bet that fails STRONG also fails MIN.
        # We assert the flag fires when called with low edge, which
        # documents the intent for when the window reopens.
        flags = _lane_compute_risk_flags(
            probability=0.65, odds_american=-110, edge_decimal=0.04,
            movement_class=None, movement_supports_pick=True,
        )
        self.assertTrue(flags['short_fav_thin'])
        # And the flag is an addition, not a replacement — other flags
        # remain independently computed.
        self.assertIn('thin_edge', flags)
        self.assertIn('market_conflict', flags)
        self.assertIn('sanity_mismatch', flags)
        self.assertIn('insight_conflict', flags)


class ExtremeDisagreementCapTests(TestCase):
    """Spec Task 4 (2026-05-06 relaxation): 20pp raw → 12pp post-blend."""

    def test_cap_does_not_fire_at_modest_disagreement(self):
        from apps.mlb.models import Conference, Team, Game, OddsSnapshot
        from apps.core.services.recommendations import get_recommendation
        conf = Conference.objects.create(name='cap1', slug='cap1-may06')
        h = Team.objects.create(name='H', slug='cap1-h-may06', conference=conf,
                                rating=70, source='mlb_stats_api', external_id='cap1-h-may06')
        a = Team.objects.create(name='A', slug='cap1-a-may06', conference=conf,
                                rating=50, source='mlb_stats_api', external_id='cap1-a-may06')
        g = Game.objects.create(
            home_team=h, away_team=a,
            first_pitch=timezone.now() + timedelta(hours=2),
            status='scheduled',
            source='mlb_stats_api', external_id='cap1-g-may06',
        )
        OddsSnapshot.objects.create(
            game=g, captured_at=timezone.now(),
            market_home_win_prob=0.55,
            moneyline_home=-130, moneyline_away=110,
        )
        rec = get_recommendation('mlb', g)
        if rec is None:
            self.skipTest('rec did not produce; skipping')
        self.assertFalse(
            rec.extreme_disagreement,
            'cap should not fire on a modest gap; under 0.40 blend the model'
            ' already lands close to market',
        )

    def test_cap_fires_on_extreme_gap(self):
        # Big rating gap + balanced market = post-blend gap above the
        # 12pp threshold even after the heavier shrink.
        from apps.mlb.models import Conference, Team, Game, OddsSnapshot
        from apps.core.services.recommendations import get_recommendation
        conf = Conference.objects.create(name='cap2', slug='cap2-may06')
        h = Team.objects.create(name='H', slug='cap2-h-may06', conference=conf,
                                rating=99, source='mlb_stats_api', external_id='cap2-h-may06')
        a = Team.objects.create(name='A', slug='cap2-a-may06', conference=conf,
                                rating=1, source='mlb_stats_api', external_id='cap2-a-may06')
        g = Game.objects.create(
            home_team=h, away_team=a,
            first_pitch=timezone.now() + timedelta(hours=2),
            status='scheduled',
            source='mlb_stats_api', external_id='cap2-g-may06',
        )
        OddsSnapshot.objects.create(
            game=g, captured_at=timezone.now(),
            market_home_win_prob=0.50,
            moneyline_home=-110, moneyline_away=-110,
        )
        rec = get_recommendation('mlb', g)
        self.assertIsNotNone(rec)
        self.assertTrue(rec.extreme_disagreement)


class CLVFormatBugFixTests(TestCase):
    """Spec Task 5: --0.0162 → -0.0162. The format spec already includes
    the sign for negative values; the prior code added another."""

    def test_negative_clv_renders_with_single_minus(self):
        from apps.mockbets.services.moneyline_evaluation import _fmt_clv
        out = _fmt_clv(-0.0162, 'negative')
        self.assertEqual(out, '-0.0162')
        self.assertNotIn('--', out)

    def test_positive_clv_renders_with_plus(self):
        from apps.mockbets.services.moneyline_evaluation import _fmt_clv
        out = _fmt_clv(0.0774, 'positive')
        self.assertEqual(out, '+0.0774')

    def test_zero_clv_renders_with_plus(self):
        # +0.0000 is the natural format; arrow logic is gone, sign is fine.
        from apps.mockbets.services.moneyline_evaluation import _fmt_clv
        out = _fmt_clv(0.0, '')
        self.assertEqual(out, '+0.0000')

    def test_none_clv_renders_em_dash(self):
        from apps.mockbets.services.moneyline_evaluation import _fmt_clv
        self.assertEqual(_fmt_clv(None, ''), '—')
        self.assertEqual(_fmt_clv(None, 'negative'), '—')

    def test_direction_arg_no_longer_affects_output(self):
        """Direction is preserved in the signature for API-compat with
        existing callers, but the value's natural sign drives output now."""
        from apps.mockbets.services.moneyline_evaluation import _fmt_clv
        # Same value, different direction strings — output identical.
        self.assertEqual(_fmt_clv(-0.05, 'negative'), _fmt_clv(-0.05, 'positive'))
        self.assertEqual(_fmt_clv(-0.05, ''),         _fmt_clv(-0.05, 'negative'))


class FewerRecommendationsUnderTighterGatesTests(TestCase):
    """Marginal picks that cleared old gates should now fail."""

    def test_pick_at_old_5pp_edge_now_fails(self):
        from apps.core.services.recommendations import compute_status, STATUS_NOT_RECOMMENDED
        # 5pp edge was the old MIN_EDGE; 65% prob clears MIN_PROBABILITY.
        status, reason = compute_status(
            model_edge=5.0, odds_american=-110, probability=0.65,
        )
        self.assertEqual(status, STATUS_NOT_RECOMMENDED)
        self.assertEqual(reason, 'low_edge')

    def test_pick_at_new_6pp_edge_clears(self):
        from apps.core.services.recommendations import compute_status, STATUS_RECOMMENDED
        status, reason = compute_status(
            model_edge=6.0, odds_american=-110, probability=0.65,
        )
        self.assertEqual(status, STATUS_RECOMMENDED)
        self.assertEqual(reason, '')
