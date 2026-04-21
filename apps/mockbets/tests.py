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

    def test_ai_commentary_requires_login(self):
        self.client.logout()
        resp = self.client.post('/mockbets/ai-commentary/', content_type='application/json', data='{}')
        self.assertEqual(resp.status_code, 302)

    def test_ai_commentary_too_few_bets(self):
        """AI commentary needs at least 5 settled bets."""
        MockBet.objects.create(
            user=self.user, sport='cfb', bet_type='moneyline',
            selection='Alabama', odds_american=-150,
            implied_probability=Decimal('0.6000'),
            stake_amount=Decimal('100.00'), result='win',
            simulated_payout=Decimal('66.67'),
            settled_at=timezone.now(),
        )
        resp = self.client.post('/mockbets/ai-commentary/', content_type='application/json', data='{}')
        self.assertEqual(resp.status_code, 400)
        self.assertIn('5 settled bets', resp.json()['error'])


class MLBSettlementTests(TestCase):
    """Verify the generalized _settle_team_sport helper works for MLB."""

    def setUp(self):
        from apps.mlb.models import Conference as MLBConference
        from apps.mlb.models import Team as MLBTeam
        self.user = User.objects.create_user('baseballbettor', password='pw')
        conf, _ = MLBConference.objects.get_or_create(slug='al-east', defaults={'name': 'AL East'})
        self.home = MLBTeam.objects.create(
            name='Yankees', slug='yankees', conference=conf,
            source='mlb_stats_api', external_id='147',
        )
        self.away = MLBTeam.objects.create(
            name='Royals', slug='royals', conference=conf,
            source='mlb_stats_api', external_id='118',
        )

    def _final_game(self, home_score, away_score):
        from apps.mlb.models import Game as MLBGame
        return MLBGame.objects.create(
            home_team=self.home, away_team=self.away,
            first_pitch=timezone.now(), status='final',
            home_score=home_score, away_score=away_score,
            source='mlb_stats_api', external_id=str(uuid.uuid4()),
        )

    def test_moneyline_win_on_home(self):
        game = self._final_game(6, 2)
        bet = MockBet.objects.create(
            user=self.user, sport='mlb', bet_type='moneyline',
            selection='Yankees', odds_american=-150,
            implied_probability=Decimal('0.60'),
            stake_amount=Decimal('100'), mlb_game=game,
        )
        counts = settle_pending_bets(sport='mlb')
        self.assertEqual(counts['mlb'], 1)
        bet.refresh_from_db()
        self.assertEqual(bet.result, 'win')
        self.assertEqual(bet.game, game)  # .game property dispatches correctly

    def test_moneyline_loss_on_home_when_away_wins(self):
        game = self._final_game(2, 6)
        bet = MockBet.objects.create(
            user=self.user, sport='mlb', bet_type='moneyline',
            selection='Yankees', odds_american=-150,
            implied_probability=Decimal('0.60'),
            stake_amount=Decimal('100'), mlb_game=game,
        )
        settle_pending_bets(sport='mlb')
        bet.refresh_from_db()
        self.assertEqual(bet.result, 'loss')

    def test_total_over_win(self):
        game = self._final_game(5, 4)  # total 9
        bet = MockBet.objects.create(
            user=self.user, sport='mlb', bet_type='total',
            selection='Over 8.5', odds_american=-110,
            implied_probability=Decimal('0.52'),
            stake_amount=Decimal('100'), mlb_game=game,
        )
        settle_pending_bets(sport='mlb')
        bet.refresh_from_db()
        self.assertEqual(bet.result, 'win')

    def test_all_sport_settles_mlb_too(self):
        game = self._final_game(5, 4)
        MockBet.objects.create(
            user=self.user, sport='mlb', bet_type='moneyline',
            selection='Yankees', odds_american=-110,
            implied_probability=Decimal('0.52'),
            stake_amount=Decimal('100'), mlb_game=game,
        )
        counts = settle_pending_bets(sport='all')
        self.assertEqual(counts['mlb'], 1)


class StalePendingRegressionTests(TestCase):
    """Direct regression coverage for the shipped defect: game finalized but
    the corresponding MockBet stayed 'pending' because nothing ever ran
    settle_mockbets in production. These tests exercise the two fix layers
    independently and then the full placement -> final -> display pipeline."""

    def setUp(self):
        from apps.mlb.models import Conference as MLBConf, Team as MLBTeam
        self.user = User.objects.create_user('stale_user', password='pw')
        self.client = Client()
        self.client.force_login(self.user)

        conf = MLBConf.objects.create(name='AL East', slug='al-east')
        self.home = MLBTeam.objects.create(
            name='Yankees', slug='yankees', conference=conf,
            source='mlb_stats_api', external_id='147',
        )
        self.away = MLBTeam.objects.create(
            name='Royals', slug='royals', conference=conf,
            source='mlb_stats_api', external_id='118',
        )

    def _final_game(self, home_score, away_score):
        from apps.mlb.models import Game as MLBGame
        return MLBGame.objects.create(
            home_team=self.home, away_team=self.away,
            first_pitch=timezone.now(), status='final',
            home_score=home_score, away_score=away_score,
            source='mlb_stats_api', external_id=str(uuid.uuid4()),
        )

    # --- Layer 1: the management command (the primary cron fix) -----------

    def test_settle_mockbets_command_clears_stale_pending(self):
        """The scenario that broke in production: a game is 'final' with
        scores, a MockBet is still 'pending'. The management command — now
        wired into refresh_data — must settle it."""
        from django.core.management import call_command
        from io import StringIO

        game = self._final_game(6, 2)
        bet = MockBet.objects.create(
            user=self.user, sport='mlb', bet_type='moneyline',
            selection='Yankees', odds_american=-150,
            implied_probability=Decimal('0.60'),
            stake_amount=Decimal('100'), mlb_game=game,
        )
        self.assertEqual(bet.result, 'pending')
        call_command('settle_mockbets', stdout=StringIO())
        bet.refresh_from_db()
        self.assertEqual(bet.result, 'win')
        self.assertEqual(bet.simulated_payout, Decimal('66.67').quantize(Decimal('0.01')))
        self.assertIsNotNone(bet.settled_at)

    # --- Layer 2: the settle-on-read view hook (defense-in-depth) ---------

    def test_my_bets_view_settles_stale_pending_on_read(self):
        """If the cron fell behind, visiting /mockbets/ must not show stale
        'pending'. settle_user_pending_bets is called before render."""
        game = self._final_game(6, 2)
        MockBet.objects.create(
            user=self.user, sport='mlb', bet_type='moneyline',
            selection='Yankees', odds_american=-150,
            implied_probability=Decimal('0.60'),
            stake_amount=Decimal('100'), mlb_game=game,
        )
        resp = self.client.get('/mockbets/')
        self.assertEqual(resp.status_code, 200)
        bet = MockBet.objects.get(user=self.user)
        self.assertEqual(bet.result, 'win')
        self.assertContains(resp, 'WIN')  # badge renders on the card
        self.assertContains(resp, 'Payout')
        self.assertNotContains(resp, 'PENDING')

    def test_settle_user_pending_does_not_touch_other_users(self):
        """Per-user scoping — one user visiting /mockbets/ must never settle
        another user's bets, so each page stays deterministic per viewer."""
        from apps.mockbets.services.settlement import settle_user_pending_bets
        other = User.objects.create_user('other', password='pw')
        game = self._final_game(6, 2)
        my_bet = MockBet.objects.create(
            user=self.user, sport='mlb', bet_type='moneyline',
            selection='Yankees', odds_american=-150,
            implied_probability=Decimal('0.60'),
            stake_amount=Decimal('100'), mlb_game=game,
        )
        other_bet = MockBet.objects.create(
            user=other, sport='mlb', bet_type='moneyline',
            selection='Yankees', odds_american=-150,
            implied_probability=Decimal('0.60'),
            stake_amount=Decimal('100'), mlb_game=game,
        )
        settle_user_pending_bets(self.user)
        my_bet.refresh_from_db()
        other_bet.refresh_from_db()
        self.assertEqual(my_bet.result, 'win')
        self.assertEqual(other_bet.result, 'pending')

    # --- Full pipeline: placement -> final -> display update --------------

    def test_full_pipeline_place_to_final_to_display(self):
        """End-to-end proof: POST a mock bet -> flip the game to final ->
        GET /mockbets/ -> page renders the bet as WIN with correct payout.
        This is the exact flow a user experiences in production."""
        import json
        from apps.mlb.models import Game as MLBGame

        game = MLBGame.objects.create(
            home_team=self.home, away_team=self.away,
            first_pitch=timezone.now(), status='scheduled',
            source='mlb_stats_api', external_id=str(uuid.uuid4()),
        )
        # Phase 1: user places a mock bet against a scheduled game
        resp = self.client.post(
            '/mockbets/place/',
            content_type='application/json',
            data=json.dumps({
                'sport': 'mlb',
                'game_id': str(game.id),
                'bet_type': 'moneyline',
                'selection': 'Yankees',
                'odds_american': +120,
                'stake_amount': '100',
            }),
        )
        self.assertEqual(resp.status_code, 200)
        bet = MockBet.objects.get(user=self.user)
        self.assertEqual(bet.result, 'pending')

        # Phase 2: cron ingests the final score (status + scores flip)
        game.status = 'final'
        game.home_score = 5
        game.away_score = 3
        game.save()

        # Phase 3: user visits /mockbets/ — the settle-on-read hook resolves
        resp = self.client.get('/mockbets/')
        self.assertEqual(resp.status_code, 200)
        bet.refresh_from_db()
        self.assertEqual(bet.result, 'win')
        self.assertEqual(bet.simulated_payout, Decimal('120.00'))
        # Bankroll summary reflects the win
        self.assertContains(resp, 'Total Won')
        self.assertContains(resp, '$120.00')
        # Card badge + per-bet money row render correctly
        self.assertContains(resp, 'WIN')
        self.assertContains(resp, 'Stake')


class BankrollKPIsTests(TestCase):
    """compute_kpis must feed the summary tiles on /mockbets/. We changed
    it to track total_won / total_lost — lock the math with a direct test."""

    def setUp(self):
        self.user = User.objects.create_user('kpi_user', password='pw')

    def _bet(self, result, stake, payout=None):
        return MockBet.objects.create(
            user=self.user, sport='cfb', bet_type='moneyline',
            selection='Alabama', odds_american=+100,
            implied_probability=Decimal('0.50'),
            stake_amount=Decimal(stake),
            simulated_payout=Decimal(payout) if payout is not None else None,
            result=result,
        )

    def test_summary_math_with_mixed_results(self):
        from apps.mockbets.services.analytics import compute_kpis
        self._bet('win', '100', '120')
        self._bet('win', '50', '40')
        self._bet('loss', '75')
        self._bet('push', '40')
        self._bet('pending', '200')  # pending excluded from settled math

        kpis = compute_kpis(MockBet.objects.filter(user=self.user))
        self.assertEqual(kpis['total_bets'], 5)
        self.assertEqual(kpis['settled_count'], 4)
        self.assertEqual(kpis['pending_count'], 1)
        self.assertEqual(kpis['wins'], 2)
        self.assertEqual(kpis['losses'], 1)
        self.assertEqual(kpis['pushes'], 1)
        self.assertEqual(kpis['total_stake'], Decimal('265'))  # 100+50+75+40
        self.assertEqual(kpis['total_won'], Decimal('160'))    # 120+40
        self.assertEqual(kpis['total_lost'], Decimal('75'))    # loss stake
        # net = (stake returned on wins + wins payout + push stake) - total stake
        #     = (100+120 + 50+40 + 40) - 265 = 350 - 265 = 85
        self.assertEqual(kpis['net_pl'], Decimal('85'))
