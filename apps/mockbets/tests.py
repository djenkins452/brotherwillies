import uuid
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase, Client
from django.utils import timezone

from apps.cfb.models import Conference, Team, Game, OddsSnapshot
from apps.mockbets.models import MockBet, MockBetSettlementLog
from apps.mockbets.services.settlement import settle_pending_bets


class MockBetModelTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('testuser', password='testpass')

    def test_calculate_payout_win_positive_odds(self):
        bet = MockBet(
            user=self.user, sport='cfb', bet_type='moneyline',
            selection='Alabama', odds_american=150,
            implied_probability=Decimal('0.4000'),
            stake_amount=Decimal('100.00'), result='win',
        )
        payout = bet.calculate_payout()
        self.assertEqual(payout, Decimal('150.00'))

    def test_calculate_payout_win_negative_odds(self):
        bet = MockBet(
            user=self.user, sport='cfb', bet_type='moneyline',
            selection='Alabama', odds_american=-200,
            implied_probability=Decimal('0.6667'),
            stake_amount=Decimal('100.00'), result='win',
        )
        payout = bet.calculate_payout()
        self.assertEqual(payout, Decimal('50.00'))

    def test_calculate_payout_loss(self):
        bet = MockBet(
            user=self.user, sport='cfb', bet_type='moneyline',
            selection='Alabama', odds_american=-110,
            implied_probability=Decimal('0.5238'),
            stake_amount=Decimal('100.00'), result='loss',
        )
        self.assertEqual(bet.calculate_payout(), Decimal('0.00'))

    def test_calculate_payout_push(self):
        bet = MockBet(
            user=self.user, sport='cfb', bet_type='spread',
            selection='Alabama -7', odds_american=-110,
            implied_probability=Decimal('0.5238'),
            stake_amount=Decimal('100.00'), result='push',
        )
        self.assertEqual(bet.calculate_payout(), Decimal('100.00'))

    def test_calculate_payout_pending(self):
        bet = MockBet(
            user=self.user, sport='cfb', bet_type='moneyline',
            selection='Alabama', odds_american=-110,
            implied_probability=Decimal('0.5238'),
            stake_amount=Decimal('100.00'), result='pending',
        )
        self.assertIsNone(bet.calculate_payout())

    def test_net_result_win(self):
        bet = MockBet(
            user=self.user, sport='cfb', bet_type='moneyline',
            selection='Alabama', odds_american=150,
            implied_probability=Decimal('0.4000'),
            stake_amount=Decimal('100.00'), result='win',
            simulated_payout=Decimal('150.00'),
        )
        self.assertEqual(bet.net_result, Decimal('150.00'))

    def test_net_result_loss(self):
        bet = MockBet(
            user=self.user, sport='cfb', bet_type='moneyline',
            selection='Alabama', odds_american=-110,
            implied_probability=Decimal('0.5238'),
            stake_amount=Decimal('100.00'), result='loss',
        )
        self.assertEqual(bet.net_result, Decimal('-100.00'))

    def test_net_result_pending(self):
        bet = MockBet(
            user=self.user, sport='cfb', bet_type='moneyline',
            selection='Alabama', odds_american=-110,
            implied_probability=Decimal('0.5238'),
            stake_amount=Decimal('100.00'), result='pending',
        )
        self.assertIsNone(bet.net_result)

    def test_is_settled(self):
        bet = MockBet(
            user=self.user, sport='cfb', bet_type='moneyline',
            selection='Alabama', odds_american=-110,
            implied_probability=Decimal('0.5238'),
            stake_amount=Decimal('100.00'),
        )
        self.assertFalse(bet.is_settled)
        bet.result = 'win'
        self.assertTrue(bet.is_settled)


class MockBetViewTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.user = User.objects.create_user('testuser', password='testpass')

    def test_my_bets_requires_login(self):
        resp = self.client.get('/mockbets/')
        self.assertEqual(resp.status_code, 302)

    def test_my_bets_authenticated(self):
        self.client.force_login(self.user)
        resp = self.client.get('/mockbets/')
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'My Mock Bets')

    def test_place_bet_requires_login(self):
        resp = self.client.post('/mockbets/place/', content_type='application/json', data='{}')
        self.assertEqual(resp.status_code, 302)

    def test_place_bet_invalid_json(self):
        self.client.force_login(self.user)
        resp = self.client.post('/mockbets/place/', content_type='application/json', data='not json')
        self.assertEqual(resp.status_code, 400)

    def test_place_bet_missing_sport(self):
        self.client.force_login(self.user)
        resp = self.client.post('/mockbets/place/', content_type='application/json',
                                data='{"bet_type":"moneyline","selection":"Alabama","odds_american":-110}')
        self.assertEqual(resp.status_code, 400)

    def test_place_bet_success(self):
        self.client.force_login(self.user)
        import json
        data = json.dumps({
            'sport': 'cfb',
            'bet_type': 'moneyline',
            'selection': 'Alabama',
            'odds_american': -150,
            'stake_amount': '100',
            'confidence_level': 'high',
            'model_source': 'house',
        })
        resp = self.client.post('/mockbets/place/', content_type='application/json', data=data)
        self.assertEqual(resp.status_code, 200)
        result = resp.json()
        self.assertTrue(result['success'])
        self.assertEqual(MockBet.objects.count(), 1)
        bet = MockBet.objects.first()
        self.assertEqual(bet.sport, 'cfb')
        self.assertEqual(bet.odds_american, -150)
        self.assertEqual(bet.confidence_level, 'high')

    def test_place_bet_with_game(self):
        self.client.force_login(self.user)
        conf = Conference.objects.create(name='SEC', slug='sec')
        home = Team.objects.create(name='Alabama', slug='alabama', conference=conf)
        away = Team.objects.create(name='Auburn', slug='auburn', conference=conf)
        game = Game.objects.create(home_team=home, away_team=away, kickoff=timezone.now())

        import json
        data = json.dumps({
            'sport': 'cfb',
            'bet_type': 'moneyline',
            'selection': 'Alabama',
            'odds_american': -150,
            'game_id': str(game.id),
        })
        resp = self.client.post('/mockbets/place/', content_type='application/json', data=data)
        self.assertEqual(resp.status_code, 200)
        bet = MockBet.objects.first()
        self.assertEqual(bet.cfb_game, game)


class MockBetSettlementTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('testuser', password='testpass')
        conf = Conference.objects.create(name='SEC', slug='sec')
        self.home = Team.objects.create(name='Alabama', slug='alabama', conference=conf)
        self.away = Team.objects.create(name='Auburn', slug='auburn', conference=conf)

    def test_settle_moneyline_win(self):
        game = Game.objects.create(
            home_team=self.home, away_team=self.away,
            kickoff=timezone.now(), status='final',
            home_score=28, away_score=14,
        )
        bet = MockBet.objects.create(
            user=self.user, sport='cfb', bet_type='moneyline',
            selection='Alabama', odds_american=-150,
            implied_probability=Decimal('0.6000'),
            stake_amount=Decimal('100.00'), cfb_game=game,
        )
        counts = settle_pending_bets(sport='cfb')
        self.assertEqual(counts['cfb'], 1)
        bet.refresh_from_db()
        self.assertEqual(bet.result, 'win')
        self.assertIsNotNone(bet.simulated_payout)
        self.assertIsNotNone(bet.settled_at)
        self.assertEqual(MockBetSettlementLog.objects.count(), 1)

    def test_settle_moneyline_loss(self):
        game = Game.objects.create(
            home_team=self.home, away_team=self.away,
            kickoff=timezone.now(), status='final',
            home_score=14, away_score=28,
        )
        bet = MockBet.objects.create(
            user=self.user, sport='cfb', bet_type='moneyline',
            selection='Alabama', odds_american=-150,
            implied_probability=Decimal('0.6000'),
            stake_amount=Decimal('100.00'), cfb_game=game,
        )
        settle_pending_bets(sport='cfb')
        bet.refresh_from_db()
        self.assertEqual(bet.result, 'loss')
        self.assertEqual(bet.simulated_payout, Decimal('0.00'))

    def test_settle_total_over_win(self):
        game = Game.objects.create(
            home_team=self.home, away_team=self.away,
            kickoff=timezone.now(), status='final',
            home_score=35, away_score=28,
        )
        bet = MockBet.objects.create(
            user=self.user, sport='cfb', bet_type='total',
            selection='Over 55.5', odds_american=-110,
            implied_probability=Decimal('0.5238'),
            stake_amount=Decimal('100.00'), cfb_game=game,
        )
        settle_pending_bets(sport='cfb')
        bet.refresh_from_db()
        self.assertEqual(bet.result, 'win')

    def test_settle_total_under_win(self):
        game = Game.objects.create(
            home_team=self.home, away_team=self.away,
            kickoff=timezone.now(), status='final',
            home_score=10, away_score=7,
        )
        bet = MockBet.objects.create(
            user=self.user, sport='cfb', bet_type='total',
            selection='Under 45.5', odds_american=-110,
            implied_probability=Decimal('0.5238'),
            stake_amount=Decimal('100.00'), cfb_game=game,
        )
        settle_pending_bets(sport='cfb')
        bet.refresh_from_db()
        self.assertEqual(bet.result, 'win')

    def test_no_settle_scheduled_game(self):
        game = Game.objects.create(
            home_team=self.home, away_team=self.away,
            kickoff=timezone.now(), status='scheduled',
        )
        MockBet.objects.create(
            user=self.user, sport='cfb', bet_type='moneyline',
            selection='Alabama', odds_american=-150,
            implied_probability=Decimal('0.6000'),
            stake_amount=Decimal('100.00'), cfb_game=game,
        )
        counts = settle_pending_bets(sport='cfb')
        self.assertEqual(counts['cfb'], 0)


class MockBetReviewTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('testuser', password='testpass')
        self.client.force_login(self.user)
        conf = Conference.objects.create(name='SEC', slug='sec')
        home = Team.objects.create(name='Alabama', slug='alabama', conference=conf)
        away = Team.objects.create(name='Auburn', slug='auburn', conference=conf)
        self.game = Game.objects.create(
            home_team=home, away_team=away,
            kickoff=timezone.now(), status='final',
            home_score=28, away_score=14,
        )

    def test_review_bet(self):
        bet = MockBet.objects.create(
            user=self.user, sport='cfb', bet_type='moneyline',
            selection='Alabama', odds_american=-150,
            implied_probability=Decimal('0.6000'),
            stake_amount=Decimal('100.00'), cfb_game=self.game,
            result='win', simulated_payout=Decimal('66.67'),
            settled_at=timezone.now(),
        )
        import json
        resp = self.client.post(
            f'/mockbets/{bet.id}/review/',
            content_type='application/json',
            data=json.dumps({'review_flag': 'repeat', 'review_notes': 'Good read on this game'}),
        )
        self.assertEqual(resp.status_code, 200)
        bet.refresh_from_db()
        self.assertEqual(bet.review_flag, 'repeat')
        self.assertEqual(bet.review_notes, 'Good read on this game')

    def test_review_pending_bet_rejected(self):
        bet = MockBet.objects.create(
            user=self.user, sport='cfb', bet_type='moneyline',
            selection='Alabama', odds_american=-150,
            implied_probability=Decimal('0.6000'),
            stake_amount=Decimal('100.00'), cfb_game=self.game,
            result='pending',
        )
        import json
        resp = self.client.post(
            f'/mockbets/{bet.id}/review/',
            content_type='application/json',
            data=json.dumps({'review_flag': 'repeat'}),
        )
        self.assertEqual(resp.status_code, 400)


class MockBetAnalyticsTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('testuser', password='testpass')
        self.client.force_login(self.user)

    def test_analytics_requires_login(self):
        self.client.logout()
        resp = self.client.get('/mockbets/analytics/')
        self.assertEqual(resp.status_code, 302)

    def test_analytics_empty(self):
        resp = self.client.get('/mockbets/analytics/')
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Mock Bet Analytics')

    def test_analytics_with_bets(self):
        MockBet.objects.create(
            user=self.user, sport='cfb', bet_type='moneyline',
            selection='Alabama', odds_american=-150,
            implied_probability=Decimal('0.6000'),
            stake_amount=Decimal('100.00'), result='win',
            simulated_payout=Decimal('66.67'),
            settled_at=timezone.now(),
        )
        MockBet.objects.create(
            user=self.user, sport='cfb', bet_type='moneyline',
            selection='Auburn', odds_american=130,
            implied_probability=Decimal('0.4348'),
            stake_amount=Decimal('50.00'), result='loss',
            simulated_payout=Decimal('0.00'),
            settled_at=timezone.now(),
        )
        resp = self.client.get('/mockbets/analytics/')
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Cumulative P/L')

    def test_analytics_filters(self):
        resp = self.client.get('/mockbets/analytics/?sport=cfb&confidence=high&model_source=house')
        self.assertEqual(resp.status_code, 200)

    def test_flat_bet_sim_requires_login(self):
        self.client.logout()
        resp = self.client.post('/mockbets/flat-bet-sim/', content_type='application/json', data='{}')
        self.assertEqual(resp.status_code, 302)

    def test_flat_bet_sim_no_bets(self):
        import json
        resp = self.client.post(
            '/mockbets/flat-bet-sim/',
            content_type='application/json',
            data=json.dumps({'flat_stake': '100'}),
        )
        self.assertEqual(resp.status_code, 400)

    def test_flat_bet_sim_with_bets(self):
        MockBet.objects.create(
            user=self.user, sport='cfb', bet_type='moneyline',
            selection='Alabama', odds_american=-150,
            implied_probability=Decimal('0.6000'),
            stake_amount=Decimal('100.00'), result='win',
            simulated_payout=Decimal('66.67'),
            settled_at=timezone.now(),
        )
        MockBet.objects.create(
            user=self.user, sport='cfb', bet_type='moneyline',
            selection='Auburn', odds_american=130,
            implied_probability=Decimal('0.4348'),
            stake_amount=Decimal('50.00'), result='loss',
            simulated_payout=Decimal('0.00'),
            settled_at=timezone.now(),
        )
        import json
        resp = self.client.post(
            '/mockbets/flat-bet-sim/',
            content_type='application/json',
            data=json.dumps({'flat_stake': '50'}),
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data['total_bets'], 2)
        self.assertIn('roi', data)
        self.assertIn('cumulative_pl', data)
