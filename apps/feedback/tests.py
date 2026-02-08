from django.test import TestCase, Client
from django.contrib.auth.models import User
from .models import FeedbackComponent, PartnerFeedback
from .access import is_partner


class PartnerAccessTest(TestCase):
    def test_partner_usernames(self):
        for name in ('djenkins', 'jsnyder', 'msnyder'):
            user = User.objects.create_user(name, password='test')
            self.assertTrue(is_partner(user))

    def test_non_partner_denied(self):
        user = User.objects.create_user('randomguy', password='test')
        self.assertFalse(is_partner(user))

    def test_anonymous_denied(self):
        from django.contrib.auth.models import AnonymousUser
        self.assertFalse(is_partner(AnonymousUser()))


class FeedbackModelTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('djenkins', password='test')
        self.component = FeedbackComponent.objects.create(name='Test Component')

    def test_default_status_is_new(self):
        fb = PartnerFeedback.objects.create(
            user=self.user, component=self.component,
            title='Test', description='Test desc',
        )
        self.assertEqual(fb.status, 'NEW')

    def test_is_ready_for_ai(self):
        fb = PartnerFeedback.objects.create(
            user=self.user, component=self.component,
            title='Test', description='Test desc', status='READY',
        )
        self.assertTrue(fb.is_ready_for_ai)

    def test_not_ready_for_ai(self):
        fb = PartnerFeedback.objects.create(
            user=self.user, component=self.component,
            title='Test', description='Test desc', status='NEW',
        )
        self.assertFalse(fb.is_ready_for_ai)


class FeedbackViewTest(TestCase):
    def setUp(self):
        self.partner = User.objects.create_user('djenkins', password='test')
        self.outsider = User.objects.create_user('outsider', password='test')
        self.component = FeedbackComponent.objects.create(name='Design')
        self.client = Client()

    def test_non_partner_gets_404_on_console(self):
        self.client.force_login(self.outsider)
        resp = self.client.get('/feedback/console/')
        self.assertEqual(resp.status_code, 404)

    def test_anonymous_gets_404_on_console(self):
        resp = self.client.get('/feedback/console/')
        self.assertEqual(resp.status_code, 404)

    def test_partner_can_view_console(self):
        self.client.force_login(self.partner)
        resp = self.client.get('/feedback/console/')
        self.assertEqual(resp.status_code, 200)

    def test_partner_can_submit_feedback(self):
        self.client.force_login(self.partner)
        resp = self.client.post('/feedback/new/', {
            'component': self.component.id,
            'title': 'Test Title',
            'description': 'Test description text',
        })
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(PartnerFeedback.objects.count(), 1)
        fb = PartnerFeedback.objects.first()
        self.assertEqual(fb.user, self.partner)
        self.assertEqual(fb.status, 'NEW')

    def test_non_partner_cannot_submit(self):
        self.client.force_login(self.outsider)
        resp = self.client.post('/feedback/new/', {
            'component': self.component.id,
            'title': 'Sneaky',
            'description': 'Should fail',
        })
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(PartnerFeedback.objects.count(), 0)

    def test_partner_can_view_detail(self):
        self.client.force_login(self.partner)
        fb = PartnerFeedback.objects.create(
            user=self.partner, component=self.component,
            title='Detail Test', description='Desc',
        )
        resp = self.client.get(f'/feedback/console/{fb.pk}/')
        self.assertEqual(resp.status_code, 200)

    def test_partner_can_update_feedback(self):
        self.client.force_login(self.partner)
        fb = PartnerFeedback.objects.create(
            user=self.partner, component=self.component,
            title='Update Test', description='Desc',
        )
        resp = self.client.post(f'/feedback/console/{fb.pk}/update/', {
            'title': 'Updated Title',
            'description': 'Updated desc',
            'status': 'ACCEPTED',
            'reviewer_notes': '',
        })
        self.assertEqual(resp.status_code, 302)
        fb.refresh_from_db()
        self.assertEqual(fb.title, 'Updated Title')
        self.assertEqual(fb.status, 'ACCEPTED')

    def test_ready_status_requires_notes(self):
        self.client.force_login(self.partner)
        fb = PartnerFeedback.objects.create(
            user=self.partner, component=self.component,
            title='Notes Test', description='Desc',
        )
        resp = self.client.post(f'/feedback/console/{fb.pk}/update/', {
            'title': 'Notes Test',
            'description': 'Desc',
            'status': 'READY',
            'reviewer_notes': '',
        })
        # Should NOT redirect — form has errors
        self.assertEqual(resp.status_code, 200)
        fb.refresh_from_db()
        self.assertEqual(fb.status, 'NEW')

    def test_ready_status_with_notes_succeeds(self):
        self.client.force_login(self.partner)
        fb = PartnerFeedback.objects.create(
            user=self.partner, component=self.component,
            title='Ready Test', description='Desc',
        )
        resp = self.client.post(f'/feedback/console/{fb.pk}/update/', {
            'title': 'Ready Test',
            'description': 'Desc',
            'status': 'READY',
            'reviewer_notes': 'Approved for AI action',
        })
        self.assertEqual(resp.status_code, 302)
        fb.refresh_from_db()
        self.assertEqual(fb.status, 'READY')

    def test_filter_by_status(self):
        self.client.force_login(self.partner)
        PartnerFeedback.objects.create(
            user=self.partner, component=self.component,
            title='New One', description='D', status='NEW',
        )
        PartnerFeedback.objects.create(
            user=self.partner, component=self.component,
            title='Accepted One', description='D', status='ACCEPTED',
        )
        resp = self.client.get('/feedback/console/?status=NEW')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.context['feedback_list']), 1)
