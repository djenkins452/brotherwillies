"""Tests for the moneyline probability calibration helpers.

Coverage:
  - blend_with_market: weight cap, None-market passthrough, math.
  - clamp_probability: picked-side bounds, side preservation, exact-0.5.
  - finalize_win_prob: order-of-operations (blend then clamp).
"""
from django.test import TestCase

from apps.core.services.probability_calibration import (
    MARKET_BLEND_WEIGHT,
    MARKET_BLEND_WEIGHT_CAP,
    PROB_MAX,
    PROB_MIN,
    blend_with_market,
    clamp_probability,
    finalize_win_prob,
)


class BlendWithMarketTests(TestCase):

    def test_returns_model_unchanged_when_market_is_none(self):
        # No-odds slate: nothing to anchor to, model wins.
        self.assertAlmostEqual(blend_with_market(0.70, None), 0.70)

    def test_default_weight_is_55_percent(self):
        # 2026-05-22 Roadmap B Step 1: weight bumped 0.40 → 0.55 per the
        # Full Model Failure Review evidence base (29-bet Model Clean,
        # CLV+ 31%, 8+pp bucket collapse, market disagreeing 75%).
        # 0.70 * 0.45 + 0.50 * 0.55 = 0.315 + 0.275 = 0.590
        self.assertAlmostEqual(
            blend_with_market(0.70, 0.50),
            0.590,
            places=4,
        )

    def test_explicit_weight_override(self):
        # 0.80 * 0.80 + 0.50 * 0.20 = 0.640 + 0.100 = 0.740
        self.assertAlmostEqual(
            blend_with_market(0.80, 0.50, weight=0.20),
            0.740,
            places=4,
        )

    def test_weight_capped_at_cap(self):
        # 2026-05-22: cap is now 0.55. Caller asks for 0.80 weight — that's
        # above the cap and must be clamped to MARKET_BLEND_WEIGHT_CAP.
        self.assertAlmostEqual(
            blend_with_market(0.80, 0.50, weight=0.80),
            blend_with_market(0.80, 0.50, weight=MARKET_BLEND_WEIGHT_CAP),
            places=6,
        )

    def test_negative_weight_clamped_to_zero(self):
        # Defensive: negative weight degrades to no blend.
        self.assertAlmostEqual(
            blend_with_market(0.70, 0.40, weight=-0.5),
            0.70,
            places=6,
        )


class ClampProbabilityTests(TestCase):

    def test_caps_strong_home_favorite_at_max(self):
        self.assertAlmostEqual(clamp_probability(0.92), PROB_MAX)

    def test_caps_strong_away_favorite_at_mirrored_min(self):
        # home=0.05 → away picked at 0.95 → clamped to 0.85 → home=0.15.
        self.assertAlmostEqual(clamp_probability(0.05), 1.0 - PROB_MAX)

    def test_pushes_weak_home_favorite_up_to_min(self):
        # home=0.51 picks home at 0.51 → below MIN, pushed to 0.52.
        self.assertAlmostEqual(clamp_probability(0.51), PROB_MIN)

    def test_pushes_weak_away_favorite_down_to_mirrored_min(self):
        # home=0.49 picks away at 0.51 → away below MIN → home=0.48.
        self.assertAlmostEqual(clamp_probability(0.49), 1.0 - PROB_MIN)

    def test_preserves_in_range_values(self):
        for p in (0.55, 0.60, 0.70, 0.80, 0.84):
            self.assertAlmostEqual(clamp_probability(p), p)
        for p in (0.45, 0.40, 0.30, 0.20, 0.16):
            self.assertAlmostEqual(clamp_probability(p), p)

    def test_exact_05_unchanged(self):
        # Coin-flip: no clamp applied (the side is genuinely undetermined).
        self.assertAlmostEqual(clamp_probability(0.5), 0.5)

    def test_clamp_never_crosses_50_50(self):
        # A 0.51 home favorite never becomes an away pick by clamping.
        self.assertGreater(clamp_probability(0.51), 0.5)
        # A 0.49 away favorite (home_prob 0.49) stays on its side.
        self.assertLess(clamp_probability(0.49), 0.5)

    def test_custom_bounds(self):
        # Used by future tuning — bounds are arguments, not constants.
        self.assertAlmostEqual(clamp_probability(0.95, lo=0.55, hi=0.90), 0.90)
        self.assertAlmostEqual(clamp_probability(0.51, lo=0.55, hi=0.90), 0.55)


class FinalizeWinProbTests(TestCase):

    def test_blend_then_clamp_order_matters(self):
        # 2026-05-22 (weight=0.55): blend 0.92*0.45 + 0.55*0.55 = 0.7165.
        # 0.7165 is within [PROB_MIN, PROB_MAX] so the clamp leaves it
        # unchanged — proving the blend already pulled the prob in range
        # without needing the clamp to fire.
        self.assertAlmostEqual(finalize_win_prob(0.92, 0.55), 0.7165, places=4)

    def test_no_market_clamp_only(self):
        # Without market data the function reduces to clamp.
        self.assertAlmostEqual(finalize_win_prob(0.92, None), PROB_MAX)
        self.assertAlmostEqual(finalize_win_prob(0.05, None), 1.0 - PROB_MAX)

    def test_blend_can_keep_in_range(self):
        # 2026-05-22 (weight=0.55): blend 0.86*0.45 + 0.55*0.55 = 0.6895.
        # Below MAX so no clamp applies — the blend alone pulled it back
        # into range.
        result = finalize_win_prob(0.86, 0.55)
        self.assertAlmostEqual(result, 0.86 * 0.45 + 0.55 * 0.55, places=4)
        self.assertLess(result, PROB_MAX)

    def test_aligned_inputs_pass_through_in_range(self):
        # Model and market agree at 0.65 → blend stays at 0.65, in range.
        self.assertAlmostEqual(finalize_win_prob(0.65, 0.65), 0.65)


class ConstantsTests(TestCase):
    """Sanity-check the published constants match the spec."""

    def test_market_blend_weight_within_spec(self):
        # 2026-05-22 Roadmap B Step 1: bumped 0.40 → 0.55. Single-variable
        # change per the Full Model Failure Review + adversarial second
        # pass. See probability_calibration.py for the full Evidence block.
        self.assertEqual(MARKET_BLEND_WEIGHT, 0.55)
        self.assertEqual(MARKET_BLEND_WEIGHT_CAP, 0.55)

    def test_prob_bounds_within_spec(self):
        self.assertEqual(PROB_MIN, 0.52)
        self.assertEqual(PROB_MAX, 0.85)
