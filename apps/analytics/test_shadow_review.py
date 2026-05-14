"""Tests for the Phase 1B shadow-review aggregator."""
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.analytics.services.shadow_review import build_shadow_review
from apps.core.models import BettingRecommendation


def _make_mlb_team_pair(suffix=''):
    from apps.mlb.models import Conference, Team
    league = Conference.objects.create(
        name=f'AL-{suffix}', slug=f'al-{timezone.now().timestamp()}-{suffix}',
    )
    home = Team.objects.create(
        name=f'Home{suffix}', slug=f'h-{timezone.now().timestamp()}-{suffix}',
        conference=league, rating=70.0,
    )
    away = Team.objects.create(
        name=f'Away{suffix}', slug=f'a-{timezone.now().timestamp()}-{suffix}',
        conference=league, rating=40.0,
    )
    return home, away


def _make_rec(
    *,
    pick='Home',
    pick_side_alt='away',
    active_mode='static',
    alt_status='not_recommended',
    alt_tier='standard',
    alt_lane='qualified',
    alt_edge_pp=2.5,
    alt_final_prob=0.55,
    elo_available=True,
    home=None, away=None,
    game=None,
    suffix='',
):
    """Build a saved BettingRecommendation row with a chosen shadow blob."""
    if home is None or away is None:
        home, away = _make_mlb_team_pair(suffix=suffix)
    if game is None:
        from apps.mlb.models import Game
        game = Game.objects.create(
            home_team=home, away_team=away,
            first_pitch=timezone.now() + timedelta(hours=2),
        )
    # Note: BettingRecommendation.tier is a @property derived from
    # model_edge — not a field, so we don't pass it to .create().
    rec = BettingRecommendation.objects.create(
        sport='mlb',
        bet_type='moneyline',
        pick=pick if pick else home.name,
        line='-150',
        odds_american=-150,
        confidence_score=Decimal('64.5'),
        model_edge=Decimal('5.0'),
        model_source='house',
        status='recommended',
        lane='core',
        shadow_active_mode=active_mode,
        shadow_alt_mode='elo' if active_mode == 'static' else 'static',
        shadow_alt_data={
            'pick': away.name if pick_side_alt == 'away' else home.name,
            'pick_side': pick_side_alt,
            'pick_odds': 130 if pick_side_alt == 'away' else -150,
            'final_prob': alt_final_prob,
            'edge_pp': alt_edge_pp,
            'status': alt_status,
            'status_reason': '',
            'tier': alt_tier,
            'lane': alt_lane,
            'home_static_rating': 70.0,
            'home_elo_rating': 1700.0 if elo_available else None,
            'away_static_rating': 40.0,
            'away_elo_rating': 1300.0 if elo_available else None,
            'elo_available': elo_available,
        },
        mlb_game=game,
    )
    return rec


class EmptyDataTests(TestCase):
    def test_empty_queryset_yields_zero_sample(self):
        review = build_shadow_review(BettingRecommendation.objects.none())
        self.assertEqual(review.sample, 0)
        self.assertEqual(review.sample_total, 0)
        self.assertIsNone(review.active_mode)

    def test_rows_with_elo_unavailable_are_excluded_from_sample(self):
        _make_rec(elo_available=False, suffix='1')
        _make_rec(elo_available=False, suffix='2')
        review = build_shadow_review(
            BettingRecommendation.objects.filter(sport='mlb'),
        )
        self.assertEqual(review.sample, 0)
        # But sample_total includes them so the operator can see why.
        self.assertEqual(review.sample_total, 2)


class AggregationTests(TestCase):
    def test_pick_agreement_counts(self):
        # Three same-side picks, one different.
        # active pick is 'Home<suffix>' (we set pick=home.name via _make_rec defaults).
        for i in range(3):
            home, away = _make_mlb_team_pair(suffix=f'sm{i}')
            _make_rec(pick=home.name, pick_side_alt='home',
                      home=home, away=away, suffix=f'sm{i}')
        home4, away4 = _make_mlb_team_pair(suffix='diff')
        _make_rec(pick=home4.name, pick_side_alt='away',
                  home=home4, away=away4, suffix='diff')

        review = build_shadow_review(
            BettingRecommendation.objects.filter(sport='mlb'),
        )
        self.assertEqual(review.pick_same_side, 3)
        self.assertEqual(review.pick_different_side, 1)
        self.assertAlmostEqual(review.pick_agreement_rate, 0.75, places=4)

    def test_status_flip_counters(self):
        # Recommended in active, not_recommended in alt → active_only
        h1, a1 = _make_mlb_team_pair(suffix='aoa')
        _make_rec(home=h1, away=a1, suffix='aoa', alt_status='not_recommended')
        # Both recommended
        h2, a2 = _make_mlb_team_pair(suffix='both')
        _make_rec(home=h2, away=a2, suffix='both', alt_status='recommended')

        review = build_shadow_review(
            BettingRecommendation.objects.filter(sport='mlb'),
        )
        self.assertEqual(review.status_recommended_active_only, 1)
        self.assertEqual(review.status_recommended_both, 1)
        self.assertEqual(review.status_recommended_alt_only, 0)
        self.assertEqual(review.status_recommended_neither, 0)

    def test_distributions_have_expected_means(self):
        # Two rows with edge_pp 5.0 active and 7.0 alt.
        for i in range(2):
            h, a = _make_mlb_team_pair(suffix=f'd{i}')
            _make_rec(home=h, away=a, suffix=f'd{i}', alt_edge_pp=7.0)
        review = build_shadow_review(
            BettingRecommendation.objects.filter(sport='mlb'),
        )
        self.assertAlmostEqual(review.active_edge_pp.mean, 5.0, places=4)
        self.assertAlmostEqual(review.alt_edge_pp.mean, 7.0, places=4)

    def test_disagreement_examples_capped_at_5(self):
        # Make 7 disagreements; only first 5 should land in examples.
        for i in range(7):
            h, a = _make_mlb_team_pair(suffix=f'dis{i}')
            _make_rec(home=h, away=a, suffix=f'dis{i}',
                      pick=h.name, pick_side_alt='away')
        review = build_shadow_review(
            BettingRecommendation.objects.filter(sport='mlb'),
        )
        self.assertEqual(review.pick_different_side, 7)
        self.assertEqual(len(review.disagreement_examples), 5)


class TaskThreeExtensionsTests(TestCase):
    """Phase 2A Task 3 extensions to ShadowReview.

    Locks the new aggregations: giant-edge frequency, overconfidence
    frequency, market disagreement bands, short-favorite scoped review.
    """

    def test_empty_data_yields_empty_band_counts(self):
        review = build_shadow_review(BettingRecommendation.objects.none())
        # All band-count fields populated with zero, not None — the
        # template depends on attribute access without defensive .get().
        self.assertEqual(review.edge_ge_6pp.active, 0)
        self.assertEqual(review.edge_ge_6pp.alt, 0)
        self.assertEqual(review.prob_ge_85.active, 0)
        self.assertEqual(review.prob_ge_85.alt, 0)
        self.assertEqual(review.disagreement_gt_10pp.active, 0)
        self.assertEqual(review.short_fav.sample, 0)
        self.assertIsNone(review.avg_disagreement_active)

    def test_edge_band_counts_threshold_inclusive(self):
        # Active edge defaults to 5.0 in _make_rec. Alt edge defaults to
        # 2.5. So:
        #   edge_ge_6pp.active == 0, edge_ge_6pp.alt == 0
        # Now bump active to 8.0 and alt to 6.0; should bump counts.
        h, a = _make_mlb_team_pair(suffix='eb1')
        rec = _make_rec(home=h, away=a, suffix='eb1', alt_edge_pp=6.0)
        rec.model_edge = Decimal('8.0')
        rec.save()

        review = build_shadow_review(
            BettingRecommendation.objects.filter(sport='mlb'),
        )
        self.assertEqual(review.edge_ge_6pp.active, 1)
        self.assertEqual(review.edge_ge_6pp.alt, 1)
        self.assertEqual(review.edge_ge_8pp.active, 1)
        self.assertEqual(review.edge_ge_8pp.alt, 0)  # 6.0 < 8.0
        self.assertEqual(review.edge_ge_10pp.active, 0)
        self.assertEqual(review.edge_ge_10pp.alt, 0)

    def test_prob_band_counts(self):
        # Active confidence_score=64.5 (decimal 0.645) — clears 0.60, not 0.70.
        # Alt final_prob=0.55 (test factory default) — clears nothing.
        h, a = _make_mlb_team_pair(suffix='pb1')
        _make_rec(home=h, away=a, suffix='pb1', alt_final_prob=0.55)

        review = build_shadow_review(
            BettingRecommendation.objects.filter(sport='mlb'),
        )
        self.assertEqual(review.prob_ge_60.active, 1)
        self.assertEqual(review.prob_ge_60.alt, 0)
        self.assertEqual(review.prob_ge_70.active, 0)
        self.assertEqual(review.prob_ge_70.alt, 0)
        self.assertEqual(review.prob_ge_85.active, 0)

    def test_prob_band_counts_high_alt(self):
        # Set alt high enough to clear 0.80 threshold.
        h, a = _make_mlb_team_pair(suffix='pb2')
        _make_rec(home=h, away=a, suffix='pb2', alt_final_prob=0.82)

        review = build_shadow_review(
            BettingRecommendation.objects.filter(sport='mlb'),
        )
        self.assertEqual(review.prob_ge_80.alt, 1)
        self.assertEqual(review.prob_ge_85.alt, 0)  # 0.82 < 0.85

    def test_disagreement_bands_use_market_prob(self):
        # _make_rec doesn't set market_prob on the row, so disagreement
        # is None for that row → counts stay 0. Let's explicitly set it.
        # Using alt_final_prob=0.53 so alt disagreement is unambiguously
        # ~0.03 — clearly below the 0.05 threshold (avoiding the
        # 0.55-0.50 = 0.0500…0444 floating-point trap).
        h, a = _make_mlb_team_pair(suffix='dis1')
        rec = _make_rec(home=h, away=a, suffix='dis1', alt_final_prob=0.53)
        rec.market_prob = 0.50  # active confidence 0.645 → disagreement 0.145
        rec.save()

        review = build_shadow_review(
            BettingRecommendation.objects.filter(sport='mlb'),
        )
        # Active disagreement 0.145 → > 0.05, > 0.10 yes; > 0.15 no
        self.assertEqual(review.disagreement_gt_5pp.active, 1)
        self.assertEqual(review.disagreement_gt_10pp.active, 1)
        self.assertEqual(review.disagreement_gt_15pp.active, 0)
        # Alt: pick_side=away vs active_side. The factory's pick='Home'
        # default doesn't match the suffixed home.name, so _pick_side_from_rec
        # returns 'away' for active. Both sides are 'away' → direct
        # comparison: |0.53 - 0.50| ≈ 0.03, well under 0.05.
        self.assertEqual(review.disagreement_gt_5pp.alt, 0)

    def test_disagreement_avg_is_computed(self):
        h, a = _make_mlb_team_pair(suffix='dis2')
        rec = _make_rec(home=h, away=a, suffix='dis2')
        rec.market_prob = 0.50
        rec.save()

        review = build_shadow_review(
            BettingRecommendation.objects.filter(sport='mlb'),
        )
        # active confidence 0.645 vs market 0.50 → 0.145
        self.assertAlmostEqual(review.avg_disagreement_active, 0.145, places=3)

    def test_short_fav_segment_includes_in_band_rows_only(self):
        # _make_rec default odds_american = -150. That's NOT in [-149,+99]
        # (just outside on the favorite side). So short_fav should be empty.
        h, a = _make_mlb_team_pair(suffix='sf1')
        _make_rec(home=h, away=a, suffix='sf1')

        review = build_shadow_review(
            BettingRecommendation.objects.filter(sport='mlb'),
        )
        self.assertEqual(review.short_fav.sample, 0)

    def test_short_fav_segment_captures_in_band_rows(self):
        # Manually create a row with odds_american in [-149, +99].
        h, a = _make_mlb_team_pair(suffix='sf2')
        from apps.mlb.models import Game
        game = Game.objects.create(
            home_team=h, away_team=a,
            first_pitch=timezone.now() + timedelta(hours=2),
        )
        BettingRecommendation.objects.create(
            sport='mlb',
            bet_type='moneyline',
            pick=h.name,
            line='-130',
            odds_american=-130,  # IN the short-fav band
            confidence_score=Decimal('60.0'),
            model_edge=Decimal('5.5'),
            model_source='house',
            status='recommended',
            lane='core',
            shadow_active_mode='static',
            shadow_alt_mode='elo',
            shadow_alt_data={
                'pick': h.name,
                'pick_side': 'home',
                'pick_odds': -130,
                'final_prob': 0.58,
                'edge_pp': 3.0,
                'status': 'not_recommended',
                'status_reason': 'low_edge',
                'tier': 'standard',
                'lane': 'qualified',
                'home_static_rating': 60.0,
                'home_elo_rating': 1550.0,
                'away_static_rating': 40.0,
                'away_elo_rating': 1450.0,
                'elo_available': True,
            },
            mlb_game=game,
        )

        review = build_shadow_review(
            BettingRecommendation.objects.filter(sport='mlb'),
        )
        self.assertEqual(review.short_fav.sample, 1)
        self.assertAlmostEqual(review.short_fav.active_final_prob.mean, 0.60, places=3)
        self.assertAlmostEqual(review.short_fav.alt_final_prob.mean, 0.58, places=3)
        # Active recommends; alt does not.
        self.assertEqual(review.short_fav.status_recommended_active, 1)
        self.assertEqual(review.short_fav.status_recommended_alt, 0)


class ViewAccessTests(TestCase):
    def test_anonymous_redirected(self):
        resp = self.client.get(reverse('analytics:shadow_review'))
        self.assertEqual(resp.status_code, 302)

    def test_non_staff_forbidden(self):
        u = User.objects.create_user(username='r', password='x')
        self.client.force_login(u)
        resp = self.client.get(reverse('analytics:shadow_review'))
        self.assertEqual(resp.status_code, 403)

    def test_staff_can_access_empty(self):
        u = User.objects.create_user(username='s', password='x', is_staff=True)
        self.client.force_login(u)
        resp = self.client.get(reverse('analytics:shadow_review'))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'No usable shadow data yet')

    def test_staff_can_access_with_data(self):
        u = User.objects.create_user(username='s2', password='x', is_staff=True)
        self.client.force_login(u)
        h, a = _make_mlb_team_pair(suffix='vw')
        _make_rec(home=h, away=a, suffix='vw')
        resp = self.client.get(reverse('analytics:shadow_review'))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Distributions')
        self.assertContains(resp, 'Recommendation Status Flips')
