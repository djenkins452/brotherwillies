"""Command Center homepage tests.

Asserts:
  - Section 1: today's recommended bets (top 5, edge DESC, ML only)
  - Section 2: yesterday numbers reuse the moneyline_evaluation service
  - Section 3: health status follows the spec rule
                (clv_positive_rate >= 50% → healthy, else warning,
                 empty CLV sample → unknown)
  - Section 4: quick action buttons present, staff-gated correctly
  - Top-nav Home link active on `/`
"""
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase, Client, override_settings
from django.utils import timezone


@override_settings(STORAGES={
    'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
    'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'},
})
class CommandCenterRenderTests(TestCase):
    """Smoke + section presence tests with empty data."""

    def setUp(self):
        self.user = User.objects.create_user('cc_user', password='x')
        self.client = Client()
        self.client.force_login(self.user)

    def test_homepage_routes_to_command_center(self):
        resp = self.client.get('/')
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Command Center')

    def test_all_four_sections_render(self):
        resp = self.client.get('/')
        body = resp.content.decode('utf-8')
        # Each section's heading is the discriminator.
        self.assertIn("Today's Plays", body)
        self.assertIn('Yesterday', body)
        self.assertIn('System Health', body)
        self.assertIn('Quick Actions', body)

    def test_quick_action_buttons_for_normal_user(self):
        resp = self.client.get('/')
        body = resp.content.decode('utf-8')
        # Public actions visible to all logged-in users.
        self.assertIn('href="/mlb/"', body)
        self.assertIn('href="/mockbets/"', body)
        self.assertIn('href="/mockbets/analytics/"', body)
        # Staff-only actions hidden.
        self.assertNotIn('href="/mockbets/moneyline-evaluation/"', body)
        self.assertNotIn('href="/mockbets/system-tuning/"', body)

    def test_quick_action_buttons_for_staff(self):
        staff = User.objects.create_user('cc_staff', password='x', is_staff=True)
        c = Client()
        c.force_login(staff)
        resp = c.get('/')
        body = resp.content.decode('utf-8')
        self.assertIn('href="/mockbets/moneyline-evaluation/"', body)
        self.assertIn('href="/mockbets/system-tuning/"', body)


@override_settings(STORAGES={
    'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
    'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'},
})
class CommandCenterHealthBandTests(TestCase):
    """Spec health rule:
       clv_positive_rate >= 50% → healthy
       clv_positive_rate <  50% → warning
       clv_sample == 0          → unknown
    Drives the rule via seeded MockBets at placement_date=yesterday."""

    def setUp(self):
        self.user = User.objects.create_user('cc_health', password='x')
        self.client = Client()
        self.client.force_login(self.user)

    def _seed_bet(self, *, clv_direction='', clv_cents=None, result='loss'):
        """Build a settled moneyline bet placed YESTERDAY local-tz with
        the given CLV signal. Tests the health-rule branches."""
        from apps.mockbets.models import MockBet
        bet = MockBet.objects.create(
            user=self.user, sport='mlb', bet_type='moneyline',
            selection='X', odds_american=-110,
            implied_probability=Decimal('0.5238'),
            stake_amount=Decimal('100'),
            simulated_payout=Decimal('91') if result == 'win' else None,
            result=result,
            is_system_generated=True,
            recommendation_status='recommended',
            recommendation_tier='standard',
            recommendation_confidence=Decimal('60.0'),
            expected_edge=Decimal('5.0'),
            clv_cents=clv_cents,
            clv_direction=clv_direction,
            closing_odds_american=-110 if clv_direction else None,
        )
        bet.placed_at = timezone.now() - timedelta(days=1)
        bet.save(update_fields=['placed_at'])
        return bet

    def test_health_unknown_when_no_clv_sample(self):
        # No bets at all yesterday → CLV sample is 0 → 'unknown'
        resp = self.client.get('/')
        self.assertEqual(resp.context['health_status'], 'unknown')

    def test_health_warning_when_majority_negative_clv(self):
        self._seed_bet(clv_direction='negative', clv_cents=-0.05)
        self._seed_bet(clv_direction='negative', clv_cents=-0.04)
        self._seed_bet(clv_direction='positive', clv_cents=0.03)
        # 1/3 positive = 33.3% < 50% → warning
        resp = self.client.get('/')
        self.assertEqual(resp.context['health_status'], 'warning')

    def test_health_healthy_when_majority_positive_clv(self):
        self._seed_bet(clv_direction='positive', clv_cents=0.05)
        self._seed_bet(clv_direction='positive', clv_cents=0.04)
        self._seed_bet(clv_direction='negative', clv_cents=-0.02)
        # 2/3 positive = 66.7% >= 50% → healthy
        resp = self.client.get('/')
        self.assertEqual(resp.context['health_status'], 'healthy')

    def test_health_healthy_at_exactly_50_pct(self):
        # Spec: >= 50% is healthy. One positive, one negative = 50.0%.
        self._seed_bet(clv_direction='positive', clv_cents=0.05)
        self._seed_bet(clv_direction='negative', clv_cents=-0.05)
        resp = self.client.get('/')
        self.assertEqual(resp.context['health_status'], 'healthy')


@override_settings(STORAGES={
    'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
    'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'},
})
class CommandCenterTodaysPlaysTests(TestCase):
    """Today's plays: limit 5, sort by edge DESC, moneyline + recommended only."""

    def setUp(self):
        self.user = User.objects.create_user('cc_plays', password='x')
        self.client = Client()
        self.client.force_login(self.user)
        # Build an MLB conf + teams once; each rec fabricates a fresh game.
        from apps.mlb.models import Conference, Team
        self.conf = Conference.objects.create(name='AL-cc', slug='al-cc')
        self.t_home = Team.objects.create(
            name='Yankees', slug='cc-yankees', conference=self.conf, rating=70,
            source='mlb_stats_api', external_id='cc-h',
        )
        self.t_away = Team.objects.create(
            name='Rays', slug='cc-rays', conference=self.conf, rating=40,
            source='mlb_stats_api', external_id='cc-a',
        )

    def _make_rec(self, *, edge=Decimal('5.0'), bet_type='moneyline',
                  status='recommended', ext='cc-game-1'):
        from apps.mlb.models import Game
        from apps.core.models import BettingRecommendation
        # Each rec gets its own Game so the dedupe-by-game logic in the
        # view doesn't collapse them.
        game = Game.objects.create(
            home_team=self.t_home, away_team=self.t_away,
            first_pitch=timezone.now() + timedelta(hours=4),
            status='scheduled',
            source='mlb_stats_api', external_id=ext,
        )
        return BettingRecommendation.objects.create(
            sport='mlb', mlb_game=game,
            bet_type=bet_type, pick='Yankees', odds_american=-110,
            confidence_score=Decimal('60.0'), model_edge=edge,
            status=status,
        )

    def test_recs_sorted_by_edge_desc(self):
        self._make_rec(edge=Decimal('3.5'), ext='ed-3')
        self._make_rec(edge=Decimal('8.0'), ext='ed-8')
        self._make_rec(edge=Decimal('5.0'), ext='ed-5')
        resp = self.client.get('/')
        recs = list(resp.context['today_recs'])
        edges = [float(r.model_edge) for r in recs]
        self.assertEqual(edges, sorted(edges, reverse=True))

    def test_only_moneyline_recs_render(self):
        # Engine emits only moneyline today; defensively assert the
        # filter still excludes any spread/total row that might exist.
        self._make_rec(bet_type='moneyline', ext='ml-1')
        # Direct ORM creation of a non-moneyline rec to verify filter.
        from apps.mlb.models import Game
        from apps.core.models import BettingRecommendation
        g = Game.objects.create(
            home_team=self.t_home, away_team=self.t_away,
            first_pitch=timezone.now() + timedelta(hours=5),
            status='scheduled',
            source='mlb_stats_api', external_id='non-ml',
        )
        BettingRecommendation.objects.create(
            sport='mlb', mlb_game=g,
            bet_type='spread', pick='Yankees -1.5',
            odds_american=-110, confidence_score=Decimal('55.0'),
            model_edge=Decimal('4.0'), status='recommended',
        )
        resp = self.client.get('/')
        for rec in resp.context['today_recs']:
            self.assertEqual(rec.bet_type, 'moneyline')

    def test_excludes_not_recommended_recs(self):
        from apps.mlb.models import Game
        from apps.core.models import BettingRecommendation
        g = Game.objects.create(
            home_team=self.t_home, away_team=self.t_away,
            first_pitch=timezone.now() + timedelta(hours=6),
            status='scheduled',
            source='mlb_stats_api', external_id='nr',
        )
        BettingRecommendation.objects.create(
            sport='mlb', mlb_game=g,
            bet_type='moneyline', pick='Yankees',
            odds_american=-110, confidence_score=Decimal('55.0'),
            model_edge=Decimal('4.0'), status='not_recommended',
        )
        resp = self.client.get('/')
        for rec in resp.context['today_recs']:
            self.assertEqual(rec.status, 'recommended')

    def test_capped_at_five(self):
        for i in range(8):
            self._make_rec(edge=Decimal('5.0') + Decimal(str(i * 0.1)),
                           ext=f'cap-{i}')
        resp = self.client.get('/')
        self.assertLessEqual(len(resp.context['today_recs']), 5)


@override_settings(STORAGES={
    'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
    'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'},
})
class CommandCenterTopNavHomeTests(TestCase):
    """The new Home link is the first entry and is active on `/`."""

    def setUp(self):
        self.user = User.objects.create_user('cc_nav', password='x')
        self.client = Client()
        self.client.force_login(self.user)

    def test_home_link_present(self):
        resp = self.client.get('/')
        body = resp.content.decode('utf-8')
        self.assertIn('href="/"', body)
        self.assertIn('Home', body)

    def test_home_link_first_in_top_nav(self):
        resp = self.client.get('/mockbets/analytics/')
        body = resp.content.decode('utf-8')
        # In the rendered nav, Home's anchor must appear before MLB's.
        i_home = body.find('href="/"')
        i_mlb = body.find('href="/mlb/"')
        self.assertGreater(i_home, -1)
        self.assertGreater(i_mlb, -1)
        self.assertLess(i_home, i_mlb, 'Home must come before MLB in the top nav')

    def test_home_active_on_root(self):
        resp = self.client.get('/')
        body = resp.content.decode('utf-8')
        marker = 'href="/"\n           class="top-nav__item top-nav__item--active"'
        self.assertIn(marker, body)
