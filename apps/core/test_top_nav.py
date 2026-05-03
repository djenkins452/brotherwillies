"""Top-nav rendering tests.

The persistent top-nav surfaces Betting + Analytics for everyone, plus
Evaluation + System Tuning for staff. Active-state class is set based
on request.path. These tests guard the discoverability contract — if a
page rename or template refactor breaks the nav, this catches it.
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

    def test_staff_sees_all_four_links(self):
        c = Client()
        c.force_login(self.staff)
        resp = c.get('/mockbets/analytics/')
        body = resp.content.decode('utf-8')
        # Bar present
        self.assertIn('class="top-nav"', body)
        # All four links
        self.assertIn('href="/mlb/"', body)
        self.assertIn('href="/mockbets/analytics/"', body)
        self.assertIn('href="/mockbets/moneyline-evaluation/"', body)
        self.assertIn('href="/mockbets/system-tuning/"', body)

    def test_non_staff_sees_only_public_links(self):
        c = Client()
        c.force_login(self.normal)
        resp = c.get('/mockbets/analytics/')
        body = resp.content.decode('utf-8')
        self.assertIn('class="top-nav"', body)
        self.assertIn('href="/mlb/"', body)
        self.assertIn('href="/mockbets/analytics/"', body)
        # Staff-only links must NOT render — those pages 404 for non-staff,
        # so a visible link would just lead to a dead end.
        self.assertNotIn('href="/mockbets/moneyline-evaluation/"', body)
        self.assertNotIn('href="/mockbets/system-tuning/"', body)

    def test_anon_user_sees_public_links(self):
        # Anonymous hitting the public lobby still gets the bar.
        c = Client()
        resp = c.get('/lobby/')
        body = resp.content.decode('utf-8')
        self.assertIn('class="top-nav"', body)
        self.assertIn('href="/mlb/"', body)
        self.assertNotIn('href="/mockbets/system-tuning/"', body)

    # --- Active state -------------------------------------------------------

    def test_analytics_link_active_on_analytics_page(self):
        c = Client()
        c.force_login(self.staff)
        resp = c.get('/mockbets/analytics/')
        body = resp.content.decode('utf-8')
        # The Analytics anchor carries the --active modifier; the others
        # don't. We assert by looking for the link's class string.
        self.assertIn('href="/mockbets/analytics/"\n           class="top-nav__item top-nav__item--active"', body)

    def test_betting_link_active_on_mlb_hub(self):
        c = Client()
        c.force_login(self.staff)
        resp = c.get('/mlb/')
        body = resp.content.decode('utf-8')
        self.assertIn('href="/mlb/"\n           class="top-nav__item top-nav__item--active"', body)

    def test_evaluation_link_active_on_evaluation_page(self):
        c = Client()
        c.force_login(self.staff)
        resp = c.get('/mockbets/moneyline-evaluation/')
        body = resp.content.decode('utf-8')
        self.assertIn(
            'href="/mockbets/moneyline-evaluation/"\n           class="top-nav__item top-nav__item--active"',
            body,
        )

    def test_system_tuning_link_active_on_tuning_page(self):
        c = Client()
        c.force_login(self.staff)
        resp = c.get('/mockbets/system-tuning/')
        body = resp.content.decode('utf-8')
        self.assertIn(
            'href="/mockbets/system-tuning/"\n           class="top-nav__item top-nav__item--active"',
            body,
        )
