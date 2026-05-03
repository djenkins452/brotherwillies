"""Top-nav rendering tests.

The persistent top-nav follows the workflow ACT → TRACK → REVIEW →
DIAGNOSE → IMPROVE:

    MLB | My Bets | Performance | Evaluation (staff) | Tuning (staff)

Active-state class is set based on request.path. These tests guard
the discoverability contract — if a page rename or template refactor
breaks the nav, this catches it.
"""
from django.contrib.auth.models import User
from django.test import TestCase, Client, override_settings


@override_settings(STORAGES={
    'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
    'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'},
})
class TopNavRenderTests(TestCase):
    """The bar renders with the right link set per user role and
    highlights the active page."""

    def setUp(self):
        self.staff = User.objects.create_user('topnav_staff', password='x', is_staff=True)
        self.normal = User.objects.create_user('topnav_normal', password='x')

    # --- Link visibility per role ------------------------------------------

    def test_staff_sees_all_five_links(self):
        c = Client()
        c.force_login(self.staff)
        resp = c.get('/mockbets/analytics/')
        body = resp.content.decode('utf-8')
        self.assertIn('class="top-nav"', body)
        # All five links present, in any order — order is asserted separately.
        self.assertIn('href="/mlb/"', body)
        self.assertIn('href="/mockbets/"', body)
        self.assertIn('href="/mockbets/analytics/"', body)
        self.assertIn('href="/mockbets/moneyline-evaluation/"', body)
        self.assertIn('href="/mockbets/system-tuning/"', body)
        # New labels, not the old ones.
        self.assertIn('Performance', body)
        self.assertIn('My Bets', body)
        self.assertNotIn('>Betting<', body)  # old label gone
        self.assertNotIn('>Analytics<', body)  # old label gone (the heading)

    def test_non_staff_sees_only_public_links(self):
        c = Client()
        c.force_login(self.normal)
        resp = c.get('/mockbets/analytics/')
        body = resp.content.decode('utf-8')
        self.assertIn('class="top-nav"', body)
        self.assertIn('href="/mlb/"', body)
        self.assertIn('href="/mockbets/"', body)
        self.assertIn('href="/mockbets/analytics/"', body)
        # Staff-only links must NOT render — those pages 404 for non-staff,
        # so a visible link would just lead to a dead end.
        self.assertNotIn('href="/mockbets/moneyline-evaluation/"', body)
        self.assertNotIn('href="/mockbets/system-tuning/"', body)

    def test_anon_user_sees_public_links(self):
        c = Client()
        resp = c.get('/lobby/')
        body = resp.content.decode('utf-8')
        self.assertIn('class="top-nav"', body)
        self.assertIn('href="/mlb/"', body)
        self.assertIn('href="/mockbets/"', body)
        self.assertNotIn('href="/mockbets/system-tuning/"', body)

    # --- Order — spec mandates: MLB | My Bets | Performance | Eval | Tuning -

    def test_link_order_is_workflow(self):
        c = Client()
        c.force_login(self.staff)
        body = c.get('/mockbets/analytics/').content.decode('utf-8')
        # Ordered indices in the body — each must appear later than the
        # previous one. assertLess gives a clear failure message if the
        # nav drifts.
        i_mlb       = body.find('href="/mlb/"')
        i_mybets    = body.find('href="/mockbets/"')
        i_perf      = body.find('href="/mockbets/analytics/"')
        i_eval      = body.find('href="/mockbets/moneyline-evaluation/"')
        i_tuning    = body.find('href="/mockbets/system-tuning/"')
        self.assertLess(i_mlb, i_mybets, 'MLB must come before My Bets')
        self.assertLess(i_mybets, i_perf, 'My Bets must come before Performance')
        self.assertLess(i_perf, i_eval, 'Performance must come before Evaluation')
        self.assertLess(i_eval, i_tuning, 'Evaluation must come before Tuning')

    # --- Active state -------------------------------------------------------

    def _expect_active(self, body, href):
        marker = (
            f'href="{href}"\n'
            f'           class="top-nav__item top-nav__item--active"'
        )
        self.assertIn(marker, body, f'expected --active class on link {href}')

    def test_mlb_active_on_mlb_hub(self):
        c = Client()
        c.force_login(self.staff)
        body = c.get('/mlb/').content.decode('utf-8')
        self._expect_active(body, '/mlb/')

    def test_mlb_active_on_other_sport_hubs(self):
        """Spec: MLB link is active on every sport hub page so the
        user sees they're 'in the betting flow' regardless of which
        sport they picked at the bottom."""
        c = Client()
        c.force_login(self.staff)
        for path in ('/cfb/', '/cbb/', '/golf/'):
            body = c.get(path).content.decode('utf-8')
            self._expect_active(body, '/mlb/')

    def test_my_bets_active_on_mockbets_root(self):
        c = Client()
        c.force_login(self.staff)
        body = c.get('/mockbets/').content.decode('utf-8')
        self._expect_active(body, '/mockbets/')

    def test_my_bets_active_on_bet_detail_page(self):
        """Bet detail pages live at /mockbets/<uuid>/ — they're a deep
        slice of the user's bets, so they should highlight My Bets too."""
        from decimal import Decimal
        from apps.mockbets.models import MockBet
        bet = MockBet.objects.create(
            user=self.staff, sport='cfb', bet_type='moneyline',
            selection='X', odds_american=-110,
            implied_probability=Decimal('0.5238'),
            stake_amount=Decimal('100.00'),
        )
        c = Client()
        c.force_login(self.staff)
        body = c.get(f'/mockbets/{bet.id}/').content.decode('utf-8')
        self._expect_active(body, '/mockbets/')

    def test_my_bets_NOT_active_on_analytics_page(self):
        """Sibling top-nav destinations under /mockbets/ must NOT
        highlight My Bets — each is excluded explicitly in the rule."""
        c = Client()
        c.force_login(self.staff)
        body = c.get('/mockbets/analytics/').content.decode('utf-8')
        self.assertIn('href="/mockbets/"', body)
        marker = 'href="/mockbets/"\n           class="top-nav__item top-nav__item--active"'
        self.assertNotIn(marker, body)

    def test_my_bets_NOT_active_on_evaluation_page(self):
        c = Client()
        c.force_login(self.staff)
        body = c.get('/mockbets/moneyline-evaluation/').content.decode('utf-8')
        marker = 'href="/mockbets/"\n           class="top-nav__item top-nav__item--active"'
        self.assertNotIn(marker, body)

    def test_my_bets_NOT_active_on_tuning_page(self):
        c = Client()
        c.force_login(self.staff)
        body = c.get('/mockbets/system-tuning/').content.decode('utf-8')
        marker = 'href="/mockbets/"\n           class="top-nav__item top-nav__item--active"'
        self.assertNotIn(marker, body)

    def test_performance_active_on_analytics_page(self):
        c = Client()
        c.force_login(self.staff)
        body = c.get('/mockbets/analytics/').content.decode('utf-8')
        self._expect_active(body, '/mockbets/analytics/')

    def test_evaluation_active_on_evaluation_page(self):
        c = Client()
        c.force_login(self.staff)
        body = c.get('/mockbets/moneyline-evaluation/').content.decode('utf-8')
        self._expect_active(body, '/mockbets/moneyline-evaluation/')

    def test_tuning_active_on_tuning_page(self):
        c = Client()
        c.force_login(self.staff)
        body = c.get('/mockbets/system-tuning/').content.decode('utf-8')
        self._expect_active(body, '/mockbets/system-tuning/')

    # --- Cleanup ------------------------------------------------------------

    def test_my_mock_bets_dropdown_link_removed(self):
        """The duplicate 'My Mock Bets' entry was removed from the
        profile dropdown when My Bets landed in the top nav."""
        c = Client()
        c.force_login(self.staff)
        body = c.get('/mockbets/').content.decode('utf-8')
        self.assertNotIn(
            'class="profile-dropdown-item">My Mock Bets',
            body,
        )
