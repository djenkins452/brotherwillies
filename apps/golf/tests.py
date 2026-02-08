import json
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase, Client
from django.utils import timezone

from apps.golf.models import GolfEvent, Golfer, GolfOddsSnapshot
from apps.mockbets.models import MockBet


class GolfEventDetailTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.user = User.objects.create_user('testuser', password='testpass')
        today = timezone.now().date()
        self.event = GolfEvent.objects.create(
            name='The Masters', slug='the-masters',
            start_date=today + timedelta(days=3),
            end_date=today + timedelta(days=6),
        )
        self.golfer = Golfer.objects.create(name='Scottie Scheffler')
        GolfOddsSnapshot.objects.create(
            event=self.event, golfer=self.golfer,
            captured_at=timezone.now(),
            sportsbook='consensus',
            outright_odds=600, implied_prob=14.29,
        )

    def test_event_detail_anonymous(self):
        resp = self.client.get(f'/golf/{self.event.slug}/')
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'The Masters')
        self.assertContains(resp, 'Scottie Scheffler')
        self.assertContains(resp, '+600')
        # Anonymous user should not see Mock Bet buttons (but help modal may mention "Mock Bet")
        self.assertNotContains(resp, 'openMockBetModal')
        self.assertContains(resp, 'Log in to place mock bets')

    def test_event_detail_authenticated(self):
        self.client.force_login(self.user)
        resp = self.client.get(f'/golf/{self.event.slug}/')
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'openMockBetModal')
        self.assertNotContains(resp, 'Log in to place mock bets')

    def test_event_detail_completed_event(self):
        """Completed events should not show Mock Bet buttons."""
        self.client.force_login(self.user)
        past_event = GolfEvent.objects.create(
            name='Past Open', slug='past-open',
            start_date=timezone.now().date() - timedelta(days=10),
            end_date=timezone.now().date() - timedelta(days=7),
        )
        resp = self.client.get(f'/golf/{past_event.slug}/')
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Completed')
        self.assertNotContains(resp, 'openMockBetModal')

    def test_event_detail_404(self):
        resp = self.client.get('/golf/nonexistent-event/')
        self.assertEqual(resp.status_code, 404)


class GolferSearchTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.user = User.objects.create_user('testuser', password='testpass')
        Golfer.objects.create(name='Scottie Scheffler')
        Golfer.objects.create(name='Rory McIlroy')
        Golfer.objects.create(name='Jon Rahm')

    def test_search_requires_login(self):
        resp = self.client.get('/golf/api/golfer-search/?q=scott')
        self.assertEqual(resp.status_code, 302)

    def test_search_too_short(self):
        self.client.force_login(self.user)
        resp = self.client.get('/golf/api/golfer-search/?q=s')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), [])

    def test_search_by_name(self):
        self.client.force_login(self.user)
        resp = self.client.get('/golf/api/golfer-search/?q=scheff')
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]['name'], 'Scottie Scheffler')

    def test_search_by_first_name(self):
        self.client.force_login(self.user)
        resp = self.client.get('/golf/api/golfer-search/?q=rory')
        data = resp.json()
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]['name'], 'Rory McIlroy')

    def test_search_no_results(self):
        self.client.force_login(self.user)
        resp = self.client.get('/golf/api/golfer-search/?q=tiger')
        self.assertEqual(resp.json(), [])


class GolfMockBetPlacementTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.user = User.objects.create_user('testuser', password='testpass')
        self.client.force_login(self.user)
        today = timezone.now().date()
        self.event = GolfEvent.objects.create(
            name='PGA Championship', slug='pga-championship',
            start_date=today + timedelta(days=5),
            end_date=today + timedelta(days=8),
        )
        self.golfer = Golfer.objects.create(name='Rory McIlroy')

    def test_place_golf_mock_bet(self):
        data = json.dumps({
            'sport': 'golf',
            'bet_type': 'outright',
            'selection': 'Rory McIlroy',
            'odds_american': 800,
            'stake_amount': '100',
            'confidence_level': 'high',
            'model_source': 'house',
            'event_id': str(self.event.id),
            'golfer_id': str(self.golfer.id),
        })
        resp = self.client.post('/mockbets/place/', content_type='application/json', data=data)
        self.assertEqual(resp.status_code, 200)
        result = resp.json()
        self.assertTrue(result['success'])
        bet = MockBet.objects.first()
        self.assertEqual(bet.sport, 'golf')
        self.assertEqual(bet.bet_type, 'outright')
        self.assertEqual(bet.golf_event, self.event)
        self.assertEqual(bet.golf_golfer, self.golfer)
        self.assertEqual(bet.odds_american, 800)

    def test_place_golf_bet_invalid_event(self):
        data = json.dumps({
            'sport': 'golf',
            'bet_type': 'top_10',
            'selection': 'Rory McIlroy',
            'odds_american': 400,
            'event_id': '99999',
            'golfer_id': str(self.golfer.id),
        })
        resp = self.client.post('/mockbets/place/', content_type='application/json', data=data)
        self.assertEqual(resp.status_code, 404)

    def test_place_golf_bet_invalid_golfer(self):
        data = json.dumps({
            'sport': 'golf',
            'bet_type': 'make_cut',
            'selection': 'Unknown Golfer',
            'odds_american': 200,
            'event_id': str(self.event.id),
            'golfer_id': '99999',
        })
        resp = self.client.post('/mockbets/place/', content_type='application/json', data=data)
        self.assertEqual(resp.status_code, 404)

    def test_place_golf_bet_no_event_or_golfer(self):
        """Golf bet without event_id/golfer_id should still succeed."""
        data = json.dumps({
            'sport': 'golf',
            'bet_type': 'outright',
            'selection': 'Rory McIlroy',
            'odds_american': 800,
        })
        resp = self.client.post('/mockbets/place/', content_type='application/json', data=data)
        self.assertEqual(resp.status_code, 200)
        bet = MockBet.objects.first()
        self.assertEqual(bet.sport, 'golf')
        self.assertIsNone(bet.golf_event)
        self.assertIsNone(bet.golf_golfer)
