"""Smoke + structural tests for the replay-vs-actual overlap diagnostic."""
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.utils import timezone


class ReplayOverlapStructureTests(TestCase):
    def test_bucket_metrics_empty(self):
        from apps.analytics.services.replay_overlap import _bucket_metrics
        m = _bucket_metrics([])
        self.assertEqual(m['count'], 0)
        self.assertIsNone(m['win_pct'])
        self.assertIsNone(m['roi_pct'])

    def test_bucket_metrics_canonical_pl_convention(self):
        from apps.analytics.services.replay_overlap import _bucket_metrics
        rows = [
            {'won': True,  'pl': Decimal('150.00'), 'result': 'win',
             'stake': Decimal('100.00'), 'clv': 0.05},
            {'won': False, 'pl': Decimal('-100.00'), 'result': 'loss',
             'stake': Decimal('100.00'), 'clv': -0.03},
            {'won': True,  'pl': Decimal('50.00'),  'result': 'win',
             'stake': Decimal('100.00'), 'clv': 0.0},
        ]
        m = _bucket_metrics(rows)
        self.assertEqual(m['wins'], 2)
        self.assertEqual(m['losses'], 1)
        self.assertEqual(m['win_pct'], round(200/3, 1))
        # net = 150 - 100 + 50 = 100 on 300 stake → +33.3%
        self.assertEqual(m['net_pl'], Decimal('100.00'))
        self.assertEqual(m['roi_pct'], 33.3)
        # CLV mix
        self.assertEqual(m['clv_beat'], 1)
        self.assertEqual(m['clv_matched'], 1)
        self.assertEqual(m['clv_lost'], 1)

    def test_flat_pl_from_actual_matches_canonical(self):
        from apps.analytics.services.replay_overlap import _flat_pl_from_actual
        bet = SimpleNamespace(result='win', simulated_payout=Decimal('150.00'),
                              stake_amount=Decimal('100.00'))
        self.assertEqual(_flat_pl_from_actual(bet), Decimal('150.00'))
        bet = SimpleNamespace(result='loss', simulated_payout=Decimal('0.00'),
                              stake_amount=Decimal('100.00'))
        self.assertEqual(_flat_pl_from_actual(bet), Decimal('-100.00'))
        bet = SimpleNamespace(result='push', simulated_payout=Decimal('100.00'),
                              stake_amount=Decimal('100.00'))
        self.assertEqual(_flat_pl_from_actual(bet), Decimal('0.00'))


class ReplayOverlapViewTests(TestCase):
    def test_overlap_view_staff_returns_plaintext(self):
        staff = User.objects.create_user('sov', password='x', is_staff=True)
        c = Client()
        c.force_login(staff)
        resp = c.get('/analytics/method-replay/?experiment=overlap'
                     '&since=2026-06-01&until=2026-06-21')
        self.assertEqual(resp.status_code, 200)
        self.assertIn('text/plain', resp['Content-Type'])
        body = resp.content.decode('utf-8')
        self.assertIn('REPLAY vs ACTUAL OVERLAP', body)
        self.assertIn('OVERLAP', body)
        self.assertIn('PRODUCTION-ONLY', body)
        self.assertIn('REPLAY-ONLY', body)

    def test_overlap_view_non_staff_forbidden(self):
        reg = User.objects.create_user('rov', password='x')
        c = Client()
        c.force_login(reg)
        resp = c.get('/analytics/method-replay/?experiment=overlap'
                     '&since=2026-06-01&until=2026-06-21')
        self.assertEqual(resp.status_code, 403)
