"""Smoke test for the calibration audit view + service."""
from django.test import TestCase, Client
from django.contrib.auth.models import User


class CalibrationViewTests(TestCase):
    def test_staff_gets_plaintext_calibration(self):
        u = User.objects.create_user('cal_staff', password='x', is_staff=True)
        c = Client()
        c.force_login(u)
        resp = c.get('/analytics/method-replay/?experiment=calibration'
                     '&since=2026-04-01&until=2026-06-21')
        self.assertEqual(resp.status_code, 200)
        self.assertIn('text/plain', resp['Content-Type'])
        body = resp.content.decode('utf-8')
        self.assertIn('MODEL CALIBRATION', body)
        self.assertIn('LANE-CORRECTED', body)
        self.assertIn('predicted', body)
        self.assertIn('actual', body)

    def test_non_staff_forbidden(self):
        u = User.objects.create_user('cal_reg', password='x')
        c = Client()
        c.force_login(u)
        resp = c.get('/analytics/method-replay/?experiment=calibration')
        self.assertEqual(resp.status_code, 403)


class CalibrationBucketTests(TestCase):
    def test_bucket_classification(self):
        from apps.analytics.services.calibration import _bucket_for
        self.assertEqual(_bucket_for(0.55)[0], '0.55–0.60')
        self.assertEqual(_bucket_for(0.599)[0], '0.55–0.60')
        self.assertEqual(_bucket_for(0.60)[0], '0.60–0.65')
        self.assertEqual(_bucket_for(0.74)[0], '0.70–0.75')
        self.assertEqual(_bucket_for(0.80)[0], '0.75+')
        self.assertEqual(_bucket_for(0.50)[0], None)  # below MIN_PROBABILITY
