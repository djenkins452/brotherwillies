import uuid
from datetime import timedelta
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


class LossAnalysisRuleTests(TestCase):
    """Priority-ordered rules applied by analyze_loss. We build MockBet rows
    directly (no settlement) so the rules are tested in isolation."""

    def setUp(self):
        self.user = User.objects.create_user('loss_user', password='pw')

    def _loss_bet(self, confidence, edge, odds=-110):
        return MockBet.objects.create(
            user=self.user, sport='cfb', bet_type='moneyline',
            selection='Alabama', odds_american=odds,
            implied_probability=Decimal('0.524'),
            stake_amount=Decimal('100'), result='loss',
            recommendation_confidence=Decimal(str(confidence)),
            expected_edge=Decimal(str(edge)),
        )

    def test_bad_edge_wins_over_everything(self):
        """edge < 4pp → bad_edge even if confidence is high."""
        from apps.mockbets.services.loss_analysis import analyze_loss
        bet = self._loss_bet(confidence=90, edge=2.0)
        r = analyze_loss(bet)
        self.assertEqual(r['primary_reason'], 'bad_edge')

    def test_variance_beats_model_error_when_edge_is_strong(self):
        """edge >= 5 AND conf >= 60 → variance (the bet was good, just lost)."""
        from apps.mockbets.services.loss_analysis import analyze_loss
        bet = self._loss_bet(confidence=70, edge=7.0, odds=+150)
        r = analyze_loss(bet)
        self.assertEqual(r['primary_reason'], 'variance')

    def test_market_movement_when_implied_exceeds_confidence(self):
        """Market implied > our confidence → we bet against a market that was right."""
        from apps.mockbets.services.loss_analysis import analyze_loss
        # Odds -300 implies 75% win; confidence 55 is lower. Edge 4.5 (clears bad_edge but below variance min of 5).
        bet = self._loss_bet(confidence=55, edge=4.5, odds=-300)
        r = analyze_loss(bet)
        self.assertEqual(r['primary_reason'], 'market_movement')

    def test_model_error_for_high_confidence_no_edge_cushion(self):
        """Confidence >= 65 but edge < 5 (not variance territory) → model_error."""
        from apps.mockbets.services.loss_analysis import analyze_loss
        # Odds +120 implies ~45%; confidence 72 > implied. Edge 4.5 avoids bad_edge AND variance.
        bet = self._loss_bet(confidence=72, edge=4.5, odds=+120)
        r = analyze_loss(bet)
        self.assertEqual(r['primary_reason'], 'model_error')

    def test_analysis_includes_confidence_miss_and_edge_miss(self):
        from apps.mockbets.services.loss_analysis import analyze_loss
        bet = self._loss_bet(confidence=70, edge=6.0, odds=+150)
        r = analyze_loss(bet)
        # +150 implied = 40%. confidence_miss = 70 - 40 = 30
        self.assertEqual(r['confidence_miss'], Decimal('30'))
        self.assertEqual(r['edge_miss'], Decimal('6'))

    def test_unknown_when_snapshot_missing(self):
        """Bets without snapshot fields (pre-migration) get 'unknown'."""
        from apps.mockbets.services.loss_analysis import analyze_loss
        bet = MockBet.objects.create(
            user=self.user, sport='cfb', bet_type='moneyline',
            selection='Alabama', odds_american=-110,
            implied_probability=Decimal('0.524'),
            stake_amount=Decimal('100'), result='loss',
            # no recommendation_confidence, no expected_edge
        )
        r = analyze_loss(bet)
        self.assertEqual(r['primary_reason'], 'unknown')
        self.assertIsNone(r['confidence_miss'])
        self.assertIsNone(r['edge_miss'])

    def test_non_loss_returns_unknown_without_raising(self):
        from apps.mockbets.services.loss_analysis import analyze_loss
        bet = self._loss_bet(confidence=70, edge=6.0)
        bet.result = 'win'
        r = analyze_loss(bet)
        self.assertEqual(r['primary_reason'], 'unknown')


class SettlementLossHookTests(TestCase):
    """When the settlement engine flips a bet to loss, loss analysis runs
    and persists the reason + miss metrics. This is the user-visible loop."""

    def setUp(self):
        self.user = User.objects.create_user('hook_user', password='pw')
        conf = Conference.objects.create(name='SEC', slug='sec-hook')
        self.home = Team.objects.create(name='Alabama', slug='alabama-hook', conference=conf)
        self.away = Team.objects.create(name='Auburn', slug='auburn-hook', conference=conf)

    def test_settlement_populates_loss_reason(self):
        game = Game.objects.create(
            home_team=self.home, away_team=self.away,
            kickoff=timezone.now(), status='final',
            home_score=10, away_score=28,  # Alabama loses
        )
        bet = MockBet.objects.create(
            user=self.user, sport='cfb', bet_type='moneyline',
            selection='Alabama', odds_american=+120,
            implied_probability=Decimal('0.454'),
            stake_amount=Decimal('100'), cfb_game=game,
            # Snapshot a strong edge + confidence so this is variance, not bad_edge
            recommendation_confidence=Decimal('65'),
            expected_edge=Decimal('6.0'),
        )
        settle_pending_bets(sport='cfb')
        bet.refresh_from_db()
        self.assertEqual(bet.result, 'loss')
        self.assertEqual(bet.loss_reason, 'variance')
        self.assertIsNotNone(bet.edge_miss)

    def test_settlement_on_win_leaves_loss_fields_empty(self):
        game = Game.objects.create(
            home_team=self.home, away_team=self.away,
            kickoff=timezone.now(), status='final',
            home_score=28, away_score=10,  # Alabama wins
        )
        bet = MockBet.objects.create(
            user=self.user, sport='cfb', bet_type='moneyline',
            selection='Alabama', odds_american=+120,
            implied_probability=Decimal('0.454'),
            stake_amount=Decimal('100'), cfb_game=game,
            recommendation_confidence=Decimal('65'),
            expected_edge=Decimal('6.0'),
        )
        settle_pending_bets(sport='cfb')
        bet.refresh_from_db()
        self.assertEqual(bet.result, 'win')
        self.assertEqual(bet.loss_reason, '')
        self.assertIsNone(bet.edge_miss)


class LossBreakdownAggregateTests(TestCase):
    """compute_loss_breakdown groups losses across the user's bet history."""

    def setUp(self):
        self.user = User.objects.create_user('agg_user', password='pw')

    def _bet(self, result, reason=''):
        return MockBet.objects.create(
            user=self.user, sport='cfb', bet_type='moneyline',
            selection='Alabama', odds_american=-110,
            implied_probability=Decimal('0.524'),
            stake_amount=Decimal('100'), result=result,
            loss_reason=reason,
        )

    def test_percentages_sum_to_100_across_losses(self):
        from apps.mockbets.services.recommendation_performance import compute_loss_breakdown
        self._bet('loss', 'variance')
        self._bet('loss', 'variance')
        self._bet('loss', 'model_error')
        self._bet('loss', 'bad_edge')
        self._bet('win')  # wins ignored

        result = compute_loss_breakdown(MockBet.objects.filter(user=self.user))
        self.assertEqual(result['total_losses'], 4)
        by_reason = {r['reason']: r for r in result['rows']}
        self.assertEqual(by_reason['variance']['count'], 2)
        self.assertEqual(by_reason['variance']['pct'], 50.0)
        self.assertEqual(by_reason['model_error']['count'], 1)
        self.assertEqual(by_reason['bad_edge']['count'], 1)
        # Stable display order
        self.assertEqual(
            [r['reason'] for r in result['rows']],
            ['variance', 'model_error', 'market_movement', 'bad_edge', 'unknown'],
        )

    def test_empty_losses_returns_zero_counts(self):
        from apps.mockbets.services.recommendation_performance import compute_loss_breakdown
        self._bet('win')
        result = compute_loss_breakdown(MockBet.objects.filter(user=self.user))
        self.assertEqual(result['total_losses'], 0)
        for row in result['rows']:
            self.assertEqual(row['count'], 0)
            self.assertEqual(row['pct'], 0.0)


class ActionLabelTests(TestCase):
    """Phase 1 actionable language — 'Recommended Bet' vs 'Model Lean'."""

    def test_recommended_bet_label(self):
        from apps.core.services.recommendations import action_label, STATUS_RECOMMENDED
        self.assertEqual(action_label(STATUS_RECOMMENDED), 'Recommended Bet')

    def test_model_lean_label(self):
        from apps.core.services.recommendations import action_label, STATUS_NOT_RECOMMENDED
        self.assertEqual(action_label(STATUS_NOT_RECOMMENDED), 'Model Lean')

    def test_unknown_status_falls_back_to_recommended(self):
        """Defensive: a blank/unknown status shouldn't produce empty UI copy."""
        from apps.core.services.recommendations import action_label
        self.assertEqual(action_label(''), 'Recommended Bet')


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


class CLVCaptureTests(TestCase):
    """Closing-line-value capture on bet settlement.

    Each test drives a minimal full path: make a game, snapshot pre-game odds,
    place a mock bet, flip game final, verify CLV populates correctly.
    """

    def setUp(self):
        from apps.mlb.models import Conference as MLBConf, Team as MLBTeam

        self.user = User.objects.create_user('clv_user', password='pw')
        conf = MLBConf.objects.create(name='AL East', slug='clv-al-east')
        self.home = MLBTeam.objects.create(
            name='Yankees', slug='yankees-clv', conference=conf,
            source='mlb_stats_api', external_id='clv-147',
        )
        self.away = MLBTeam.objects.create(
            name='Royals', slug='royals-clv', conference=conf,
            source='mlb_stats_api', external_id='clv-118',
        )

    def _game_with_closing_odds(self, home_close=-150, away_close=130, status='final',
                                home_score=5, away_score=3):
        from apps.mlb.models import Game as MLBGame, OddsSnapshot
        game = MLBGame.objects.create(
            home_team=self.home, away_team=self.away,
            first_pitch=timezone.now() - timedelta(hours=3),
            status=status,
            home_score=home_score, away_score=away_score,
            source='mlb_stats_api', external_id=str(uuid.uuid4()),
        )
        OddsSnapshot.objects.create(
            game=game,
            captured_at=game.first_pitch - timedelta(minutes=10),  # pre-game
            market_home_win_prob=0.60,
            moneyline_home=home_close,
            moneyline_away=away_close,
        )
        return game

    def _place_bet(self, game, selection='Yankees', odds=-130):
        return MockBet.objects.create(
            user=self.user, sport='mlb', bet_type='moneyline',
            selection=selection, odds_american=odds,
            implied_probability=Decimal('0.565'),
            stake_amount=Decimal('100'), mlb_game=game,
        )

    def test_positive_clv_for_bet_that_beat_the_line(self):
        """Bet at -130, closed at -150 — bet got less juice, positive CLV."""
        from apps.mockbets.services.clv import capture_bet_clv
        game = self._game_with_closing_odds(home_close=-150)
        bet = self._place_bet(game, odds=-130)
        self.assertTrue(capture_bet_clv(bet))
        bet.refresh_from_db()
        self.assertEqual(bet.closing_odds_american, -150)
        self.assertGreater(bet.clv_cents, 0)
        self.assertEqual(bet.clv_direction, 'positive')

    def test_negative_clv_when_line_moved_against_pick(self):
        """Bet at -130, closed at -110 — close offered better price, negative CLV."""
        from apps.mockbets.services.clv import capture_bet_clv
        game = self._game_with_closing_odds(home_close=-110)
        bet = self._place_bet(game, odds=-130)
        self.assertTrue(capture_bet_clv(bet))
        bet.refresh_from_db()
        self.assertLess(bet.clv_cents, 0)
        self.assertEqual(bet.clv_direction, 'negative')

    def test_capture_is_idempotent(self):
        """Running capture twice must not double-write or clobber the value."""
        from apps.mockbets.services.clv import capture_bet_clv
        game = self._game_with_closing_odds(home_close=-150)
        bet = self._place_bet(game, odds=-130)
        self.assertTrue(capture_bet_clv(bet))
        bet.refresh_from_db()
        first_clv = bet.clv_cents
        # Second call: bet already has closing_odds, should no-op.
        self.assertFalse(capture_bet_clv(bet))
        bet.refresh_from_db()
        self.assertEqual(bet.clv_cents, first_clv)

    def test_capture_picks_away_side_closing_odds(self):
        """When the bet's selection matches the away team, pull away moneyline."""
        from apps.mockbets.services.clv import capture_bet_clv
        game = self._game_with_closing_odds(home_close=-150, away_close=130)
        bet = self._place_bet(game, selection='Royals', odds=120)
        capture_bet_clv(bet)
        bet.refresh_from_db()
        self.assertEqual(bet.closing_odds_american, 130)

    def test_no_pregame_snapshot_leaves_clv_null(self):
        """If no OddsSnapshot exists before first_pitch, CLV stays null."""
        from apps.mlb.models import Game as MLBGame
        from apps.mockbets.services.clv import capture_bet_clv
        # Game with no odds_snapshots at all
        game = MLBGame.objects.create(
            home_team=self.home, away_team=self.away,
            first_pitch=timezone.now() - timedelta(hours=1),
            status='final', home_score=5, away_score=3,
            source='mlb_stats_api', external_id=str(uuid.uuid4()),
        )
        bet = self._place_bet(game, odds=-130)
        result = capture_bet_clv(bet)
        self.assertFalse(result)
        bet.refresh_from_db()
        self.assertIsNone(bet.clv_cents)

    def test_non_moneyline_bet_skipped(self):
        """Spread / total CLV not defined in v1 — skip cleanly."""
        from apps.mockbets.services.clv import capture_bet_clv
        game = self._game_with_closing_odds()
        bet = MockBet.objects.create(
            user=self.user, sport='mlb', bet_type='spread',
            selection='Yankees -1.5', odds_american=-110,
            implied_probability=Decimal('0.524'),
            stake_amount=Decimal('100'), mlb_game=game,
        )
        self.assertFalse(capture_bet_clv(bet))

    def test_settlement_populates_clv_automatically(self):
        """End-to-end: run settle_pending_bets → CLV lands on the bet."""
        from apps.mockbets.services.settlement import settle_pending_bets
        game = self._game_with_closing_odds(home_close=-150)
        bet = self._place_bet(game, odds=-130)
        self.assertEqual(bet.result, 'pending')
        settle_pending_bets(sport='mlb')
        bet.refresh_from_db()
        self.assertEqual(bet.result, 'win')  # Yankees won 5-3
        self.assertEqual(bet.closing_odds_american, -150)
        self.assertEqual(bet.clv_direction, 'positive')


class DecisionQualityTests(TestCase):
    """MockBet.decision_quality combines outcome with CLV direction."""

    def setUp(self):
        self.user = User.objects.create_user('dq_user', password='pw')

    def _bet(self, result, clv=None, direction=''):
        return MockBet.objects.create(
            user=self.user, sport='mlb', bet_type='moneyline',
            selection='X', odds_american=-110,
            implied_probability=Decimal('0.50'),
            stake_amount=Decimal('100'),
            simulated_payout=Decimal('100') if result == 'win' else None,
            result=result,
            clv_cents=clv,
            clv_direction=direction or '',
        )

    def test_perfect_when_win_plus_positive_clv(self):
        bet = self._bet('win', clv=0.05, direction='positive')
        self.assertEqual(bet.decision_quality, 'perfect')
        self.assertEqual(bet.decision_quality_label, 'Perfect')
        self.assertEqual(bet.decision_quality_class, 'dq-perfect')

    def test_lucky_when_win_plus_negative_clv(self):
        bet = self._bet('win', clv=-0.03, direction='negative')
        self.assertEqual(bet.decision_quality, 'lucky')

    def test_unlucky_when_loss_plus_positive_clv(self):
        bet = self._bet('loss', clv=0.04, direction='positive')
        self.assertEqual(bet.decision_quality, 'unlucky')

    def test_bad_when_loss_plus_negative_clv(self):
        bet = self._bet('loss', clv=-0.02, direction='negative')
        self.assertEqual(bet.decision_quality, 'bad')

    def test_neutral_for_push(self):
        bet = self._bet('push', clv=0.01, direction='positive')
        self.assertEqual(bet.decision_quality, 'neutral')

    def test_empty_when_pending(self):
        bet = self._bet('pending')
        self.assertEqual(bet.decision_quality, '')

    def test_empty_when_clv_missing(self):
        """Honest classification requires both legs — no CLV, no DQ."""
        bet = self._bet('win', clv=None, direction='')
        self.assertEqual(bet.decision_quality, '')


class SystemVerdictTests(TestCase):
    """compute_system_verdict deterministic logic."""

    def setUp(self):
        self.user = User.objects.create_user('verdict_user', password='pw')

    def _bet(self, result, clv=None, direction='', stake='100', payout=None):
        return MockBet.objects.create(
            user=self.user, sport='mlb', bet_type='moneyline',
            selection='X', odds_american=-110,
            implied_probability=Decimal('0.50'),
            stake_amount=Decimal(stake),
            simulated_payout=Decimal(payout) if payout is not None else None,
            result=result,
            clv_cents=clv,
            clv_direction=direction or '',
            settled_at=timezone.now() if result != 'pending' else None,
        )

    def _cc(self):
        from apps.mockbets.services.command_center import build_command_center
        return build_command_center(MockBet.objects.filter(user=self.user))

    def test_strong_verdict_with_strong_signals(self):
        # 25 settled bets, 17 wins (68% win rate, +ROI), CLV 70%+
        for _ in range(17):
            self._bet('win', clv=0.05, direction='positive', payout='100')
        for _ in range(8):
            self._bet('loss', clv=0.02, direction='positive')
        cc = self._cc()
        self.assertEqual(cc['system_verdict']['verdict'], 'STRONG')
        # 25 settled bets — under MEDIUM threshold (30) so confidence is LOW
        self.assertEqual(cc['system_verdict']['confidence_level'], 'LOW')
        joined = ' '.join(cc['system_verdict']['reasons'])
        self.assertIn('CLV', joined)

    def test_weak_verdict_when_negative_roi(self):
        for _ in range(20):
            self._bet('loss', clv=-0.02, direction='negative')
        cc = self._cc()
        self.assertEqual(cc['system_verdict']['verdict'], 'WEAK')

    def test_weak_verdict_when_clv_below_50(self):
        # Profitable streak but CLV %+ < 50% — verdict should still flag WEAK
        for _ in range(10):
            self._bet('win', clv=-0.01, direction='negative', payout='100')
        for _ in range(2):
            self._bet('loss', clv=-0.01, direction='negative')
        cc = self._cc()
        self.assertEqual(cc['system_verdict']['verdict'], 'WEAK')

    def test_neutral_verdict_when_signals_mixed(self):
        """No CLV captured, slightly positive ROI, sub-25 sample → NEUTRAL.
        Not WEAK (ROI is positive), not STRONG (no CLV signal yet)."""
        for _ in range(8):
            self._bet('win', payout='100')
        for _ in range(7):
            self._bet('loss')
        cc = self._cc()
        self.assertEqual(cc['system_verdict']['verdict'], 'NEUTRAL')

    def test_confidence_low_under_30_bets(self):
        for _ in range(10):
            self._bet('win', clv=0.04, direction='positive', payout='100')
        cc = self._cc()
        self.assertEqual(cc['system_verdict']['confidence_level'], 'LOW')
        # Warning surfaces explicitly
        self.assertTrue(any('Small sample' in w for w in cc['system_verdict']['warnings']))

    def test_no_settled_bets_yields_zero_state(self):
        from apps.mockbets.services.command_center import compute_system_verdict
        cc = self._cc()
        verdict = cc['system_verdict']
        # 0 bets — falls into NEUTRAL by default since no signals match WEAK/STRONG
        self.assertEqual(verdict['confidence_level'], 'LOW')


class EdgeBucketsTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('eb_user', password='pw')

    def _bet(self, result, edge, payout=None):
        return MockBet.objects.create(
            user=self.user, sport='mlb', bet_type='moneyline',
            selection='X', odds_american=-110,
            implied_probability=Decimal('0.50'),
            stake_amount=Decimal('100'),
            expected_edge=Decimal(str(edge)),
            simulated_payout=Decimal(payout) if payout is not None else None,
            result=result,
            settled_at=timezone.now() if result != 'pending' else None,
        )

    def test_buckets_group_by_edge_range(self):
        from apps.mockbets.services.command_center import compute_edge_buckets
        # 0-2pp: 1 win
        self._bet('win', 1.5, payout='100')
        # 2-4pp: 1 loss
        self._bet('loss', 3.0)
        # 4-6pp: 1 win
        self._bet('win', 5.0, payout='100')
        # 6pp+: 1 win
        self._bet('win', 8.0, payout='100')

        rows = compute_edge_buckets(MockBet.objects.filter(user=self.user))
        self.assertEqual(len(rows), 4)
        ranges = [r['range'] for r in rows]
        self.assertEqual(ranges, ['0–2pp', '2–4pp', '4–6pp', '6pp+'])
        counts = [r['count'] for r in rows]
        self.assertEqual(counts, [1, 1, 1, 1])

    def test_bucket_boundaries_are_left_inclusive_right_exclusive(self):
        """An edge of exactly 4.0 lands in 4-6pp, not in 2-4pp."""
        from apps.mockbets.services.command_center import compute_edge_buckets
        self._bet('win', 4.0, payout='100')
        rows = compute_edge_buckets(MockBet.objects.filter(user=self.user))
        by_range = {r['range']: r for r in rows}
        self.assertEqual(by_range['2–4pp']['count'], 0)
        self.assertEqual(by_range['4–6pp']['count'], 1)

    def test_pending_bets_excluded(self):
        from apps.mockbets.services.command_center import compute_edge_buckets
        self._bet('pending', 5.0)
        rows = compute_edge_buckets(MockBet.objects.filter(user=self.user))
        self.assertEqual(sum(r['count'] for r in rows), 0)

    def test_bets_without_expected_edge_excluded(self):
        from apps.mockbets.services.command_center import compute_edge_buckets
        MockBet.objects.create(
            user=self.user, sport='mlb', bet_type='moneyline',
            selection='X', odds_american=-110,
            implied_probability=Decimal('0.50'),
            stake_amount=Decimal('100'),
            expected_edge=None,  # no snapshot
            result='win',
            simulated_payout=Decimal('100'),
            settled_at=timezone.now(),
        )
        rows = compute_edge_buckets(MockBet.objects.filter(user=self.user))
        self.assertEqual(sum(r['count'] for r in rows), 0)


class TopPlayReasonsTests(TestCase):
    """top_play_reasons explanation logic for the elite banner."""

    def test_includes_edge_pp(self):
        from apps.core.services.recommendations import top_play_reasons
        reasons = top_play_reasons(model_edge=8.5, confidence_score=72.0,
                                    tier='elite', status='recommended')
        self.assertTrue(any('+8.5pp model edge' in r for r in reasons))

    def test_elite_edge_threshold_triggers_mispricing_call(self):
        from apps.core.services.recommendations import top_play_reasons
        reasons = top_play_reasons(model_edge=10.0, confidence_score=72.0,
                                    tier='elite', status='recommended')
        self.assertTrue(any('Market mispricing detected' in r for r in reasons))

    def test_below_elite_threshold_no_mispricing_call(self):
        from apps.core.services.recommendations import top_play_reasons
        reasons = top_play_reasons(model_edge=6.5, confidence_score=72.0,
                                    tier='strong', status='recommended')
        self.assertFalse(any('mispricing' in r.lower() for r in reasons))

    def test_robust_to_none_inputs(self):
        from apps.core.services.recommendations import top_play_reasons
        reasons = top_play_reasons(model_edge=None, confidence_score=None,
                                    tier='elite', status='recommended')
        # Doesn't crash and still emits the cleared-rules bullet
        self.assertIsInstance(reasons, list)


class CommandCenterTests(TestCase):
    """The build_command_center facade is the single source of analytics
    truth for the dashboard + AI summary. These tests cover the new
    aggregations (drivers, CLV extremes, capability flags) and ensure
    delegated KPIs survive the wrapper untouched."""

    def setUp(self):
        self.user = User.objects.create_user('cc_user', password='pw')

    def _bet(self, result, stake='100', payout=None, status='recommended',
             tier='strong', clv=None, direction='', odds=-110, selection='X'):
        return MockBet.objects.create(
            user=self.user, sport='mlb', bet_type='moneyline',
            selection=selection, odds_american=odds,
            implied_probability=Decimal('0.50'),
            stake_amount=Decimal(stake),
            simulated_payout=Decimal(payout) if payout is not None else None,
            result=result,
            recommendation_status=status,
            recommendation_tier=tier,
            clv_cents=clv,
            clv_direction=direction or '',
            settled_at=timezone.now() if result != 'pending' else None,
        )

    def test_capabilities_with_no_bets(self):
        from apps.mockbets.services.command_center import build_command_center
        cc = build_command_center([])
        self.assertFalse(cc['capabilities']['has_any_bets'])
        self.assertFalse(cc['capabilities']['has_settled_bets'])
        self.assertFalse(cc['capabilities']['has_clv_data'])

    def test_capabilities_settled_no_clv(self):
        from apps.mockbets.services.command_center import build_command_center
        self._bet('win', payout='100')  # no clv
        cc = build_command_center(MockBet.objects.filter(user=self.user))
        self.assertTrue(cc['capabilities']['has_settled_bets'])
        self.assertFalse(cc['capabilities']['has_clv_data'])

    def test_hero_record_string(self):
        from apps.mockbets.services.command_center import build_command_center
        self._bet('win', payout='100')
        self._bet('win', payout='100')
        self._bet('loss')
        self._bet('push')
        cc = build_command_center(MockBet.objects.filter(user=self.user))
        self.assertEqual(cc['hero']['record'], '2-1-1')

    def test_hero_record_omits_pushes_when_zero(self):
        from apps.mockbets.services.command_center import build_command_center
        self._bet('win', payout='100')
        self._bet('loss')
        cc = build_command_center(MockBet.objects.filter(user=self.user))
        self.assertEqual(cc['hero']['record'], '1-1')

    def test_clv_block_with_data(self):
        from apps.mockbets.services.command_center import build_command_center
        self._bet('win', payout='100', clv=0.05, direction='positive')
        self._bet('win', payout='100', clv=0.10, direction='positive')
        self._bet('loss', clv=-0.03, direction='negative')
        cc = build_command_center(MockBet.objects.filter(user=self.user))
        self.assertEqual(cc['clv']['sample_size'], 3)
        self.assertEqual(cc['clv']['positive_count'], 2)
        self.assertAlmostEqual(cc['clv']['positive_rate'], 66.7, places=1)
        self.assertAlmostEqual(cc['clv']['avg_clv'], 0.04, places=2)
        self.assertEqual(cc['clv']['best'].clv_cents, 0.10)
        self.assertEqual(cc['clv']['worst'].clv_cents, -0.03)

    def test_clv_block_empty_when_no_data(self):
        from apps.mockbets.services.command_center import build_command_center
        self._bet('win', payout='100')  # no clv captured
        cc = build_command_center(MockBet.objects.filter(user=self.user))
        self.assertEqual(cc['clv']['sample_size'], 0)
        self.assertIsNone(cc['clv']['best'])
        self.assertIsNone(cc['clv']['worst'])

    def test_drivers_best_wins_sorted_by_payout(self):
        from apps.mockbets.services.command_center import build_command_center
        self._bet('win', payout='50', selection='Small Win')
        self._bet('win', payout='200', selection='Big Win')
        self._bet('win', payout='100', selection='Mid Win')
        cc = build_command_center(MockBet.objects.filter(user=self.user))
        names = [b.selection for b in cc['drivers']['best_wins']]
        self.assertEqual(names, ['Big Win', 'Mid Win', 'Small Win'])

    def test_drivers_worst_losses_sorted_by_stake(self):
        from apps.mockbets.services.command_center import build_command_center
        self._bet('loss', stake='50', selection='Small')
        self._bet('loss', stake='200', selection='Big')
        self._bet('loss', stake='100', selection='Mid')
        cc = build_command_center(MockBet.objects.filter(user=self.user))
        names = [b.selection for b in cc['drivers']['worst_losses']]
        self.assertEqual(names, ['Big', 'Mid', 'Small'])

    def test_drivers_best_validations_require_rec_and_positive_clv(self):
        from apps.mockbets.services.command_center import build_command_center
        # Win with rec + positive CLV → validation
        self._bet('win', payout='100', status='recommended', tier='strong',
                  clv=0.05, direction='positive', selection='Validated')
        # Win without CLV → not a validation
        self._bet('win', payout='100', status='recommended', tier='strong',
                  selection='Win No CLV')
        # Win with negative CLV → not a validation
        self._bet('win', payout='100', status='recommended', tier='strong',
                  clv=-0.02, direction='negative', selection='Win Bad CLV')
        # Win with positive CLV but not_recommended → not a validation
        self._bet('win', payout='100', status='not_recommended', tier='standard',
                  clv=0.05, direction='positive', selection='Lucky Not Rec')
        cc = build_command_center(MockBet.objects.filter(user=self.user))
        names = [b.selection for b in cc['drivers']['best_validations']]
        self.assertEqual(names, ['Validated'])

    def test_drivers_biggest_misses_only_recommended_or_elite_losses(self):
        from apps.mockbets.services.command_center import build_command_center
        self._bet('loss', stake='100', status='recommended', tier='strong',
                  selection='Recommended Miss')
        self._bet('loss', stake='100', status='', tier='elite',
                  selection='Elite No Status')
        self._bet('loss', stake='100', status='not_recommended', tier='standard',
                  selection='Not Rec Loss')  # excluded
        cc = build_command_center(MockBet.objects.filter(user=self.user))
        names = sorted(b.selection for b in cc['drivers']['biggest_misses'])
        self.assertEqual(names, ['Elite No Status', 'Recommended Miss'])

    def test_ledger_pending_bets_first(self):
        """Pending bets surface above settled in the ledger so the user
        sees what's in play before what's done."""
        from apps.mockbets.services.command_center import build_command_center
        self._bet('win', payout='100', selection='Settled Win')
        self._bet('pending', selection='Pending Bet')
        cc = build_command_center(MockBet.objects.filter(user=self.user))
        self.assertEqual(cc['ledger'][0].selection, 'Pending Bet')

    def test_top_n_cap_on_drivers(self):
        """No matter how many wins, drivers cap at 5 per category."""
        from apps.mockbets.services.command_center import build_command_center
        for i in range(8):
            self._bet('win', payout=str(100 + i), selection=f'Win {i}')
        cc = build_command_center(MockBet.objects.filter(user=self.user))
        self.assertEqual(len(cc['drivers']['best_wins']), 5)


class AISummaryTests(TestCase):
    """ai_summary always returns content — falls back to deterministic
    narrative when OpenAI is unavailable."""

    def setUp(self):
        self.user = User.objects.create_user('ai_user', password='pw')

    def _bet(self, result, payout=None, clv=None, direction=''):
        return MockBet.objects.create(
            user=self.user, sport='mlb', bet_type='moneyline',
            selection='X', odds_american=-110,
            implied_probability=Decimal('0.50'),
            stake_amount=Decimal('100'),
            simulated_payout=Decimal(payout) if payout is not None else None,
            result=result,
            clv_cents=clv,
            clv_direction=direction or '',
            settled_at=timezone.now() if result != 'pending' else None,
        )

    def test_no_settled_bets_returns_friendly_message(self):
        from apps.mockbets.services.command_center import build_command_center
        from apps.mockbets.services.ai_summary import generate_mockbet_analytics_summary
        cc = build_command_center([])
        result = generate_mockbet_analytics_summary(cc)
        self.assertEqual(result['source'], 'deterministic')
        self.assertIsNone(result['error'])
        self.assertIn('No settled bets', result['content'])

    def test_no_api_key_falls_back_to_deterministic(self):
        from apps.mockbets.services.command_center import build_command_center
        from apps.mockbets.services.ai_summary import generate_mockbet_analytics_summary
        self._bet('win', payout='100')
        self._bet('loss')
        cc = build_command_center(MockBet.objects.filter(user=self.user))
        with self.settings(OPENAI_API_KEY=''):
            result = generate_mockbet_analytics_summary(cc)
        self.assertEqual(result['source'], 'deterministic')
        self.assertIsNone(result['error'])
        # Deterministic narrative references settled bets count
        self.assertIn('settled bets', result['content'])
        # Must include the legal-style closer
        self.assertIn('Past simulated performance', result['content'])

    def test_deterministic_fallback_flags_small_sample(self):
        from apps.mockbets.services.command_center import build_command_center
        from apps.mockbets.services.ai_summary import generate_mockbet_analytics_summary
        for _ in range(3):
            self._bet('win', payout='100')
        cc = build_command_center(MockBet.objects.filter(user=self.user))
        with self.settings(OPENAI_API_KEY=''):
            result = generate_mockbet_analytics_summary(cc)
        self.assertIn('Sample size is small', result['content'])


class CancelBetTests(TestCase):
    """Cancellation workflow: allowed pre-game, rejected when game has
    started or the bet has already settled, and ownership-gated."""

    def setUp(self):
        from apps.mlb.models import Conference as MLBConf, Team as MLBTeam
        self.user = User.objects.create_user('canceller', password='pw')
        self.other = User.objects.create_user('other', password='pw')
        self.client = Client()
        self.client.force_login(self.user)

        conf = MLBConf.objects.create(name='AL East', slug='cancel-al-east')
        self.home = MLBTeam.objects.create(
            name='Yankees', slug='yankees-cancel', conference=conf,
            source='mlb_stats_api', external_id='cxl-147',
        )
        self.away = MLBTeam.objects.create(
            name='Royals', slug='royals-cancel', conference=conf,
            source='mlb_stats_api', external_id='cxl-118',
        )

    def _game(self, status='scheduled', first_pitch_offset_hours=2,
              home_score=None, away_score=None):
        from apps.mlb.models import Game as MLBGame
        return MLBGame.objects.create(
            home_team=self.home, away_team=self.away,
            first_pitch=timezone.now() + timedelta(hours=first_pitch_offset_hours),
            status=status,
            home_score=home_score, away_score=away_score,
            source='mlb_stats_api', external_id=str(uuid.uuid4()),
        )

    def _bet(self, game, user=None, result='pending', selection='Yankees'):
        return MockBet.objects.create(
            user=user or self.user, sport='mlb', bet_type='moneyline',
            selection=selection, odds_american=-130,
            implied_probability=Decimal('0.565'),
            stake_amount=Decimal('100'),
            result=result, mlb_game=game,
        )

    # --- can_cancel property ------------------------------------------------

    def test_can_cancel_scheduled_future_game(self):
        bet = self._bet(self._game(first_pitch_offset_hours=2))
        self.assertTrue(bet.can_cancel)

    def test_cannot_cancel_when_game_is_live(self):
        bet = self._bet(self._game(status='live', first_pitch_offset_hours=-1))
        self.assertFalse(bet.can_cancel)

    def test_cannot_cancel_when_game_is_final(self):
        bet = self._bet(
            self._game(status='final', first_pitch_offset_hours=-3,
                       home_score=5, away_score=3),
        )
        self.assertFalse(bet.can_cancel)

    def test_cannot_cancel_after_first_pitch_even_if_status_stale(self):
        """Defensive: status might still be 'scheduled' if the cron is late,
        but if first_pitch has passed the bet is already locked."""
        bet = self._bet(self._game(status='scheduled', first_pitch_offset_hours=-1))
        self.assertFalse(bet.can_cancel)

    def test_cannot_cancel_settled_bet_even_with_future_game(self):
        """Edge case: bet somehow settled while game is still scheduled
        (shouldn't happen, but the guard holds)."""
        bet = self._bet(self._game(first_pitch_offset_hours=2), result='win')
        self.assertFalse(bet.can_cancel)

    # --- cancel_bet endpoint ------------------------------------------------

    def test_cancel_endpoint_deletes_pending_pregame_bet(self):
        bet = self._bet(self._game(first_pitch_offset_hours=2))
        bet_id = bet.id
        resp = self.client.post(
            f'/mockbets/{bet_id}/cancel/',
            content_type='application/json',
        )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()['success'])
        self.assertFalse(MockBet.objects.filter(id=bet_id).exists())

    def test_cancel_endpoint_rejects_live_game(self):
        bet = self._bet(self._game(status='live', first_pitch_offset_hours=-1))
        resp = self.client.post(
            f'/mockbets/{bet.id}/cancel/',
            content_type='application/json',
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn('error', resp.json())
        self.assertTrue(MockBet.objects.filter(id=bet.id).exists())

    def test_cancel_endpoint_rejects_other_users_bet(self):
        """Ownership gate: user A cannot cancel user B's bet (404 via qs filter)."""
        bet = self._bet(self._game(first_pitch_offset_hours=2), user=self.other)
        resp = self.client.post(
            f'/mockbets/{bet.id}/cancel/',
            content_type='application/json',
        )
        self.assertEqual(resp.status_code, 404)
        self.assertTrue(MockBet.objects.filter(id=bet.id).exists())

    def test_cancel_endpoint_requires_login(self):
        bet = self._bet(self._game(first_pitch_offset_hours=2))
        anon = Client()
        resp = anon.post(
            f'/mockbets/{bet.id}/cancel/',
            content_type='application/json',
        )
        self.assertEqual(resp.status_code, 302)  # redirect to login

    def test_cancel_endpoint_rejects_get(self):
        bet = self._bet(self._game(first_pitch_offset_hours=2))
        resp = self.client.get(f'/mockbets/{bet.id}/cancel/')
        # @require_POST returns 405 Method Not Allowed
        self.assertEqual(resp.status_code, 405)
        self.assertTrue(MockBet.objects.filter(id=bet.id).exists())


class CLVAnalyticsTests(TestCase):
    """recommendation_performance should surface avg_clv + positive_clv_rate."""

    def setUp(self):
        self.user = User.objects.create_user('clv_analytics', password='pw')

    def _bet(self, result, clv=None, direction=None):
        return MockBet.objects.create(
            user=self.user, sport='mlb', bet_type='moneyline',
            selection='X', odds_american=+100,
            implied_probability=Decimal('0.50'),
            stake_amount=Decimal('100'),
            simulated_payout=Decimal('100') if result == 'win' else None,
            result=result,
            recommendation_status='recommended',
            recommendation_tier='strong',
            clv_cents=clv,
            clv_direction=direction or '',
        )

    def test_avg_clv_computed_only_from_bets_with_data(self):
        """Bets without CLV don't dilute the average."""
        from apps.mockbets.services.recommendation_performance import (
            compute_performance_by_status,
        )
        self._bet('win', clv=0.10, direction='positive')
        self._bet('loss', clv=-0.05, direction='negative')
        self._bet('loss')  # no CLV captured
        result = compute_performance_by_status(MockBet.objects.filter(user=self.user))
        stats = result['recommended']
        self.assertEqual(stats['clv_sample'], 2)
        self.assertAlmostEqual(stats['avg_clv'], 0.025, places=3)
        self.assertAlmostEqual(stats['positive_clv_rate'], 50.0, places=1)

    def test_clv_fields_zero_when_no_data(self):
        from apps.mockbets.services.recommendation_performance import compute_all
        bundle = compute_all(MockBet.objects.filter(user=self.user))
        self.assertEqual(bundle['by_status']['recommended']['clv_sample'], 0)
        self.assertEqual(bundle['by_status']['recommended']['avg_clv'], 0.0)
        self.assertEqual(bundle['by_status']['recommended']['positive_clv_rate'], 0.0)
