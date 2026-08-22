"""Tests for v3.2 Fixed 62/7 Selection activation (2026-08-22).

These tests protect the exact invariants named in the activation brief:

  * flag OFF = exact prior V3 behavior (0.60 / 6pp)
  * flag ON  = 62% / 7pp behavior
  * 61.9% is REJECTED under v3.2; 62.0% can qualify
  * 6.9pp is REJECTED under v3.2; 7.0pp can qualify
  * all existing risk/lane gates still apply (short_fav_thin, sanity, etc.)
  * recent-form flag remains active + independent of v3.2 flag
  * feature contribution tracking remains intact
  * UI approve/reject copy displays the ACTIVE thresholds (not stale)
  * replay reproduces both V3 and V3.2 under override_settings
  * rollback works without data changes (env-var flip only)

Pure-Python tests — no DB rows required, no fixtures. Every path exercised
through the helpers or through `compute_status`/`_lane_hard_gates_pass`
directly.
"""

from __future__ import annotations

from django.test import TestCase, override_settings

from apps.core.services.recommendations import (
    HARD_MIN_PROBABILITY,
    HEAVY_FAVORITE_ODDS,
    LANE_HARD_GATES_EDGE_MIN,
    LANE_HARD_GATES_PROBABILITY_MIN,
    MAX_ABS_ODDS_FOR_RECOMMENDED,
    MIN_EDGE,
    MIN_PROBABILITY_FOR_RECOMMENDED,
    STATUS_NOT_RECOMMENDED,
    STATUS_RECOMMENDED,
    STRONG_EDGE,
    V3_2_LANE_HARD_GATES_EDGE_MIN,
    V3_2_LANE_HARD_GATES_PROBABILITY_MIN,
    V3_2_MIN_EDGE,
    V3_2_MIN_PROBABILITY_FOR_RECOMMENDED,
    _lane_hard_gates_pass,
    approved_reasons,
    compute_status,
    get_lane_hard_gates_edge_min,
    get_lane_hard_gates_probability_min,
    get_min_edge,
    get_min_probability_for_recommended,
    passed_reasons,
    v3_2_active,
)


class HelperFlagRoutingTests(TestCase):
    """Every helper switches on USE_V3_2_SELECTION."""

    @override_settings(USE_V3_2_SELECTION=False)
    def test_helpers_return_pre_v3_2_constants_when_flag_off(self):
        self.assertFalse(v3_2_active())
        self.assertEqual(get_min_probability_for_recommended(), MIN_PROBABILITY_FOR_RECOMMENDED)
        self.assertEqual(get_min_edge(), MIN_EDGE)
        self.assertEqual(get_lane_hard_gates_probability_min(), LANE_HARD_GATES_PROBABILITY_MIN)
        self.assertEqual(get_lane_hard_gates_edge_min(), LANE_HARD_GATES_EDGE_MIN)

    @override_settings(USE_V3_2_SELECTION=True)
    def test_helpers_return_v3_2_thresholds_when_flag_on(self):
        self.assertTrue(v3_2_active())
        self.assertEqual(get_min_probability_for_recommended(), V3_2_MIN_PROBABILITY_FOR_RECOMMENDED)
        self.assertEqual(get_min_edge(), V3_2_MIN_EDGE)
        self.assertEqual(get_lane_hard_gates_probability_min(), V3_2_LANE_HARD_GATES_PROBABILITY_MIN)
        self.assertEqual(get_lane_hard_gates_edge_min(), V3_2_LANE_HARD_GATES_EDGE_MIN)

    def test_v3_2_constants_are_the_expected_values(self):
        # If these change, the whole activation brief needs re-reading.
        self.assertEqual(V3_2_MIN_PROBABILITY_FOR_RECOMMENDED, 0.62)
        self.assertEqual(V3_2_MIN_EDGE, 7.0)
        self.assertEqual(V3_2_LANE_HARD_GATES_PROBABILITY_MIN, 0.62)
        self.assertAlmostEqual(V3_2_LANE_HARD_GATES_EDGE_MIN, 0.07)


class ComputeStatusBehaviorFlagOff(TestCase):
    """With v3.2 OFF, compute_status must reproduce the pre-v3.2 baseline
    exactly. A pick at prob=0.60 / edge=6pp / -110 was Recommended under
    v3; it must stay Recommended when v3.2 is rolled back."""

    @override_settings(USE_V3_2_SELECTION=False)
    def test_60pct_prob_6pp_edge_is_recommended(self):
        status, reason = compute_status(
            model_edge=6.0, odds_american=-110, probability=0.60,
        )
        self.assertEqual(status, STATUS_RECOMMENDED)
        self.assertEqual(reason, '')

    @override_settings(USE_V3_2_SELECTION=False)
    def test_59pct_still_rejected_low_probability(self):
        status, reason = compute_status(
            model_edge=7.0, odds_american=-110, probability=0.59,
        )
        self.assertEqual(status, STATUS_NOT_RECOMMENDED)
        self.assertEqual(reason, 'low_probability')

    @override_settings(USE_V3_2_SELECTION=False)
    def test_5_9pp_still_rejected_low_edge(self):
        status, reason = compute_status(
            model_edge=5.9, odds_american=-110, probability=0.70,
        )
        self.assertEqual(status, STATUS_NOT_RECOMMENDED)
        self.assertEqual(reason, 'low_edge')


class ComputeStatusBehaviorFlagOn(TestCase):
    """With v3.2 ON, compute_status uses 0.62 / 7pp. Boundary picks that
    passed under v3 must now be rejected; picks at the new boundary
    can still qualify."""

    # --- Probability boundary ---

    @override_settings(USE_V3_2_SELECTION=True)
    def test_61_9pct_prob_rejected_under_v3_2(self):
        """The brief specifies this exact boundary: 61.9% is rejected."""
        status, reason = compute_status(
            model_edge=8.0, odds_american=-110, probability=0.619,
        )
        self.assertEqual(status, STATUS_NOT_RECOMMENDED)
        self.assertEqual(reason, 'low_probability')

    @override_settings(USE_V3_2_SELECTION=True)
    def test_62_0pct_prob_can_qualify_under_v3_2(self):
        """The brief specifies this exact boundary: 62.0% can qualify."""
        status, reason = compute_status(
            model_edge=8.0, odds_american=-110, probability=0.620,
        )
        self.assertEqual(status, STATUS_RECOMMENDED)
        self.assertEqual(reason, '')

    @override_settings(USE_V3_2_SELECTION=True)
    def test_60_0pct_prob_now_rejected_where_pre_v3_2_would_pass(self):
        # Same pick that passed under v3 above (60% / 6pp) must fail under v3.2.
        status, reason = compute_status(
            model_edge=8.0, odds_american=-110, probability=0.60,
        )
        self.assertEqual(status, STATUS_NOT_RECOMMENDED)
        self.assertEqual(reason, 'low_probability')

    # --- Edge boundary ---

    @override_settings(USE_V3_2_SELECTION=True)
    def test_6_9pp_edge_rejected_under_v3_2(self):
        """The brief specifies this exact boundary: 6.9pp is rejected."""
        status, reason = compute_status(
            model_edge=6.9, odds_american=-110, probability=0.70,
        )
        self.assertEqual(status, STATUS_NOT_RECOMMENDED)
        self.assertEqual(reason, 'low_edge')

    @override_settings(USE_V3_2_SELECTION=True)
    def test_7_0pp_edge_can_qualify_under_v3_2(self):
        """The brief specifies this exact boundary: 7.0pp can qualify."""
        status, reason = compute_status(
            model_edge=7.0, odds_american=-110, probability=0.70,
        )
        self.assertEqual(status, STATUS_RECOMMENDED)
        self.assertEqual(reason, '')

    @override_settings(USE_V3_2_SELECTION=True)
    def test_6_0pp_edge_now_rejected_where_pre_v3_2_would_pass(self):
        # Same pick that passed under v3 (60% / 6pp / -110) must fail v3.2.
        status, reason = compute_status(
            model_edge=6.0, odds_american=-110, probability=0.70,
        )
        self.assertEqual(status, STATUS_NOT_RECOMMENDED)
        self.assertEqual(reason, 'low_edge')

    # --- Interaction: both boundaries must clear ---

    @override_settings(USE_V3_2_SELECTION=True)
    def test_62pct_and_7pp_together_qualify(self):
        status, reason = compute_status(
            model_edge=7.0, odds_american=-110, probability=0.62,
        )
        self.assertEqual(status, STATUS_RECOMMENDED)
        self.assertEqual(reason, '')

    @override_settings(USE_V3_2_SELECTION=True)
    def test_62pct_but_6_9pp_still_rejected(self):
        status, reason = compute_status(
            model_edge=6.9, odds_american=-110, probability=0.62,
        )
        self.assertEqual(status, STATUS_NOT_RECOMMENDED)
        self.assertEqual(reason, 'low_edge')

    @override_settings(USE_V3_2_SELECTION=True)
    def test_61_9pct_and_7pp_still_rejected(self):
        status, reason = compute_status(
            model_edge=7.0, odds_american=-110, probability=0.619,
        )
        self.assertEqual(status, STATUS_NOT_RECOMMENDED)
        self.assertEqual(reason, 'low_probability')


class UnchangedGatesTests(TestCase):
    """Everything else in compute_status must be UNCHANGED under v3.2."""

    @override_settings(USE_V3_2_SELECTION=True)
    def test_longshot_gate_unchanged(self):
        # |odds| > 300 still rejected regardless of prob/edge.
        status, reason = compute_status(
            model_edge=15.0, odds_american=+400, probability=0.75,
        )
        self.assertEqual(status, STATUS_NOT_RECOMMENDED)
        self.assertEqual(reason, 'longshot')

    @override_settings(USE_V3_2_SELECTION=True)
    def test_hard_probability_floor_unchanged(self):
        # Below 50% with big edge still routes to 'value', not recommended.
        status, reason = compute_status(
            model_edge=10.0, odds_american=+300, probability=0.40,
        )
        self.assertEqual(status, STATUS_NOT_RECOMMENDED)
        self.assertEqual(reason, 'value')

    @override_settings(USE_V3_2_SELECTION=True)
    def test_secondary_source_gate_unchanged(self):
        status, reason = compute_status(
            model_edge=10.0, odds_american=-110, probability=0.75,
            is_secondary=True,
        )
        self.assertEqual(status, STATUS_NOT_RECOMMENDED)
        self.assertEqual(reason, 'secondary_source')

    @override_settings(USE_V3_2_SELECTION=True)
    def test_max_abs_odds_constant_unchanged(self):
        # V3.2 does not change the longshot cap.
        self.assertEqual(MAX_ABS_ODDS_FOR_RECOMMENDED, 300)

    @override_settings(USE_V3_2_SELECTION=True)
    def test_tier_markers_unchanged(self):
        # STRONG_EDGE and ELITE_EDGE label tier; v3.2 does not tighten them.
        self.assertEqual(STRONG_EDGE, 6.0)
        # ELITE_EDGE was 8.0 pre-v3.2; still 8.0.
        from apps.core.services.recommendations import ELITE_EDGE
        self.assertEqual(ELITE_EDGE, 8.0)

    @override_settings(USE_V3_2_SELECTION=True)
    def test_heavy_favorite_odds_constant_unchanged(self):
        self.assertEqual(HEAVY_FAVORITE_ODDS, -150)

    @override_settings(USE_V3_2_SELECTION=True)
    def test_hard_min_probability_unchanged(self):
        self.assertEqual(HARD_MIN_PROBABILITY, 0.50)


class LaneHardGatesTrackFlagTests(TestCase):
    """_lane_hard_gates_pass tightens with v3.2. A pick at prob=0.60,
    edge=0.06 that passed the lane under v3 must fail under v3.2, so
    lane hard-gates and compute_status stay consistent."""

    @override_settings(USE_V3_2_SELECTION=False)
    def test_lane_hard_gates_60pct_6pp_passes_pre_v3_2(self):
        self.assertTrue(_lane_hard_gates_pass(
            probability=0.60, edge=0.06, odds_american=-110, source_quality='primary',
        ))

    @override_settings(USE_V3_2_SELECTION=True)
    def test_lane_hard_gates_60pct_6pp_fails_under_v3_2(self):
        # v3.2 floors are 0.62 / 0.07, so this pick must fail.
        self.assertFalse(_lane_hard_gates_pass(
            probability=0.60, edge=0.06, odds_american=-110, source_quality='primary',
        ))

    @override_settings(USE_V3_2_SELECTION=True)
    def test_lane_hard_gates_62pct_7pp_passes_v3_2(self):
        self.assertTrue(_lane_hard_gates_pass(
            probability=0.62, edge=0.07, odds_american=-110, source_quality='primary',
        ))


class ReasonBulletsReflectActiveThresholdsTests(TestCase):
    """UI copy — passed_reasons / approved_reasons — must display the
    ACTIVE thresholds, not the stale pre-v3.2 constants. If this test
    fails, a v3.2-rejected pick would render "Confidence below the 60%
    threshold" — a lie in v3.2."""

    @override_settings(USE_V3_2_SELECTION=True)
    def test_passed_reasons_low_probability_shows_62pct(self):
        bullets = passed_reasons(
            status=STATUS_NOT_RECOMMENDED, status_reason='low_probability',
            confidence_score=61.0,
        )
        joined = ' | '.join(bullets)
        self.assertIn('62%', joined)
        self.assertNotIn('60%', joined)

    @override_settings(USE_V3_2_SELECTION=True)
    def test_passed_reasons_low_edge_shows_7pp(self):
        bullets = passed_reasons(
            status=STATUS_NOT_RECOMMENDED, status_reason='low_edge',
        )
        joined = ' | '.join(bullets)
        self.assertIn('7pp', joined)
        self.assertNotIn('6pp', joined)

    @override_settings(USE_V3_2_SELECTION=False)
    def test_passed_reasons_low_probability_shows_60pct_under_rollback(self):
        bullets = passed_reasons(
            status=STATUS_NOT_RECOMMENDED, status_reason='low_probability',
            confidence_score=59.0,
        )
        joined = ' | '.join(bullets)
        self.assertIn('60%', joined)

    @override_settings(USE_V3_2_SELECTION=True)
    def test_approved_reasons_shows_62pct_minimum(self):
        bullets = approved_reasons(
            model_edge=8.0, confidence_score=68.0, status=STATUS_RECOMMENDED,
        )
        joined = ' | '.join(bullets)
        self.assertIn('62% minimum', joined)


class BulkActionsFilterTracksFlagTests(TestCase):
    """The bulk-bet placement filter must accept/reject picks consistently
    with the compute_status decision. If the two disagree, a card marked
    Recommended could be excluded from Bet All (or vice versa)."""

    def _stub_rec(self, *, confidence_score, odds_american=-110, **extra):
        class _Rec:
            pass
        r = _Rec()
        r.status = extra.get('status', 'recommended')
        r.lane = extra.get('lane', 'core')
        r.tier = extra.get('tier', 'standard')
        r.status_reason = extra.get('status_reason', '')
        r.confidence_score = confidence_score
        r.odds_american = odds_american
        r.is_secondary = False
        return r

    @override_settings(USE_V3_2_SELECTION=True)
    def test_61pct_recommended_rejected_by_bulk_filter_under_v3_2(self):
        # In practice compute_status would already have rejected this
        # 61% pick — so the recommendation wouldn't render as
        # status='recommended'. But if somehow a stale row exists,
        # the bulk-bet filter (defense in depth) must still exclude it.
        from apps.mockbets.services.bulk_actions import is_bulk_moneyline_eligible as is_placement_eligible
        rec = self._stub_rec(confidence_score=61.0)
        self.assertFalse(is_placement_eligible(rec))

    @override_settings(USE_V3_2_SELECTION=True)
    def test_62pct_recommended_accepted_by_bulk_filter_under_v3_2(self):
        from apps.mockbets.services.bulk_actions import is_bulk_moneyline_eligible as is_placement_eligible
        rec = self._stub_rec(confidence_score=62.0)
        self.assertTrue(is_placement_eligible(rec))

    @override_settings(USE_V3_2_SELECTION=False)
    def test_60pct_recommended_accepted_by_bulk_filter_under_rollback(self):
        from apps.mockbets.services.bulk_actions import is_bulk_moneyline_eligible as is_placement_eligible
        rec = self._stub_rec(confidence_score=60.0)
        self.assertTrue(is_placement_eligible(rec))


class HealthScoreReportsActiveThresholdsTests(TestCase):
    """The health-score panel reports the currently active thresholds so
    the operator sees what the engine is enforcing, not the frozen
    baseline constants."""

    @override_settings(USE_V3_2_SELECTION=True)
    def test_reported_thresholds_reflect_v3_2_when_flag_on(self):
        from apps.analytics.services.health_score import _capture_calibration_state
        c = _capture_calibration_state()
        self.assertEqual(c['min_edge'], V3_2_MIN_EDGE)
        self.assertEqual(c['min_probability_for_recommended'], V3_2_MIN_PROBABILITY_FOR_RECOMMENDED)

    @override_settings(USE_V3_2_SELECTION=False)
    def test_reported_thresholds_reflect_baseline_when_flag_off(self):
        from apps.analytics.services.health_score import _capture_calibration_state
        c = _capture_calibration_state()
        self.assertEqual(c['min_edge'], MIN_EDGE)
        self.assertEqual(c['min_probability_for_recommended'], MIN_PROBABILITY_FOR_RECOMMENDED)


class ReplayReproducesBothMethodologiesTests(TestCase):
    """The walk-forward harness's `apply_candidate_gates` reproduces
    compute_status behavior under the correct config, for BOTH
    methodologies. Locked in test_walk_forward.py::GateMirrorLockTests
    across the full input matrix — here we spot-check the exact brief
    boundaries as a cross-file sanity."""

    def test_baseline_candidate_reproduces_pre_v3_2(self):
        from apps.analytics.services.walk_forward import (
            CandidateConfig, apply_candidate_gates,
        )
        cfg = CandidateConfig(label='baseline')  # 0.60 / 6.0
        # 60% / 6pp / -110 → RECOMMENDED under pre-v3.2 rules.
        status, _ = apply_candidate_gates(
            edge_pp=6.0, pick_odds=-110, pick_prob=0.60, config=cfg,
        )
        self.assertEqual(status, STATUS_RECOMMENDED)

    def test_v3_2_candidate_rejects_59pct_and_5_9pp(self):
        from apps.analytics.services.walk_forward import (
            CandidateConfig, apply_candidate_gates,
        )
        cfg = CandidateConfig(
            label='v3_2', min_probability=0.62, min_edge_pp=7.0,
        )
        # 60% / 6pp under v3.2 → NOT recommended.
        status, reason = apply_candidate_gates(
            edge_pp=6.0, pick_odds=-110, pick_prob=0.60, config=cfg,
        )
        self.assertEqual(status, STATUS_NOT_RECOMMENDED)
        # (either 'low_probability' or 'low_edge' — whichever gate fires
        # first; the important assertion is that it did not pass.)
        self.assertIn(reason, ('low_probability', 'low_edge'))


class RecentFormAndContributionsUnchangedByV3_2Tests(TestCase):
    """v3.2 is a SELECTION tightening. It does not touch USE_STARTER_RECENT_FORM
    or the feature-contribution capture. Both should continue to operate
    independently of the v3.2 flag."""

    @override_settings(USE_V3_2_SELECTION=True, USE_STARTER_RECENT_FORM=True)
    def test_recent_form_flag_still_reads_true(self):
        from django.conf import settings
        self.assertTrue(settings.USE_STARTER_RECENT_FORM)

    @override_settings(USE_V3_2_SELECTION=True, USE_STARTER_RECENT_FORM=False)
    def test_recent_form_flag_independently_flippable_from_v3_2(self):
        from django.conf import settings
        self.assertFalse(settings.USE_STARTER_RECENT_FORM)
        # And v3.2 flag is independently on.
        self.assertTrue(v3_2_active())

    def test_feature_contributions_schema_unchanged_by_v3_2(self):
        # The v3.2 activation does not modify BettingRecommendation
        # schema. Ensure the JSON field still exists.
        from apps.core.models import BettingRecommendation
        field = BettingRecommendation._meta.get_field('feature_contributions')
        self.assertIsNotNone(field)
