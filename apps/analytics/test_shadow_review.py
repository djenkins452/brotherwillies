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
