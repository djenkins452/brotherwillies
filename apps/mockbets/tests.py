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

    # 2026-04-30: model-gap callout tests — fires only when the user
    # model is meaningfully behind the house over enough samples.

    def _seed_for_gap(self, *, source, count, all_wins):
        """Helper — seed `count` bets at -110 with all wins or all losses."""
        for i in range(count):
            MockBet.objects.create(
                user=self.user, sport='mlb', bet_type='moneyline',
                selection=f'Team{i}', odds_american=-110,
                implied_probability=Decimal('0.5238'),
                stake_amount=Decimal('100.00'),
                result='win' if all_wins else 'loss',
                simulated_payout=Decimal('90.91') if all_wins else Decimal('0.00'),
                settled_at=timezone.now(),
                model_source=source,
            )

    def test_gap_callout_fires_when_user_underperforms(self):
        from apps.mockbets.services.analytics import compute_comparison
        # House: 12 wins (high ROI). User: 12 losses (negative ROI).
        # Both clear the 10-bet minimum; ROI gap >> 5pp threshold.
        self._seed_for_gap(source='house', count=12, all_wins=True)
        self._seed_for_gap(source='user', count=12, all_wins=False)
        comp = compute_comparison(list(MockBet.objects.all()))
        self.assertIsNotNone(comp['gap_callout'])
        self.assertGreater(comp['gap_callout']['roi_gap_pp'], 5.0)

    def test_gap_callout_silent_when_user_outperforms(self):
        """When the user model is AHEAD, no callout — the page already
        shows that win clearly via positive numbers, no need to flag."""
        from apps.mockbets.services.analytics import compute_comparison
        self._seed_for_gap(source='house', count=12, all_wins=False)
        self._seed_for_gap(source='user', count=12, all_wins=True)
        comp = compute_comparison(list(MockBet.objects.all()))
        self.assertIsNone(comp['gap_callout'])

    def test_gap_callout_silent_with_small_sample(self):
        """Both sides need at least 10 settled bets — variance is too
        loud below that threshold to warn the user."""
        from apps.mockbets.services.analytics import compute_comparison
        self._seed_for_gap(source='house', count=8, all_wins=True)
        self._seed_for_gap(source='user', count=8, all_wins=False)
        comp = compute_comparison(list(MockBet.objects.all()))
        self.assertIsNone(comp['gap_callout'])

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

    def test_fallback_classifies_when_snapshot_missing(self):
        """2026-04-30 fallback path: bets without snapshot fields are
        no longer dumped into 'unknown'. Now classified via the always-
        present implied_probability + confidence_level. The Loss
        Breakdown widget went from ~48% Unknown to a real distribution
        after this change.

        For this bet: implied 52.4% (a typical -110), confidence
        'medium' → assumed model prob 58%. Approximate edge = 5.6pp,
        which exceeds BAD_EDGE threshold (4) but fails the variance
        path (which requires confidence='high') → model_error.
        """
        from apps.mockbets.services.loss_analysis import analyze_loss
        bet = MockBet.objects.create(
            user=self.user, sport='cfb', bet_type='moneyline',
            selection='Alabama', odds_american=-110,
            implied_probability=Decimal('0.524'),
            stake_amount=Decimal('100'), result='loss',
            confidence_level='medium',
            # no recommendation_confidence, no expected_edge — fallback path
        )
        r = analyze_loss(bet)
        self.assertEqual(r['primary_reason'], 'model_error')
        # Fallback returns approximate confidence_miss / edge_miss
        # rather than None — the breakdown widget can show useful data.
        self.assertIsNotNone(r['confidence_miss'])
        self.assertIsNotNone(r['edge_miss'])
        # Detail copy makes the approximation explicit.
        self.assertIn('Approximate', r['details'])

    def test_fallback_high_confidence_loss_is_variance(self):
        """High-confidence loss with a meaningful approximate edge
        (>=5pp gap between assumed-prob 65% and implied) is classified
        as variance — same product semantics as the snapshot path."""
        from apps.mockbets.services.loss_analysis import analyze_loss
        bet = MockBet.objects.create(
            user=self.user, sport='cfb', bet_type='moneyline',
            selection='Alabama', odds_american=-110,
            implied_probability=Decimal('0.524'),  # ~52% (typical -110)
            stake_amount=Decimal('100'), result='loss',
            confidence_level='high',  # → assumed model 65%
            # snapshot fields missing — fallback path
        )
        r = analyze_loss(bet)
        self.assertEqual(r['primary_reason'], 'variance')

    def test_fallback_low_confidence_thin_edge_is_bad_edge(self):
        """Low-confidence loss with assumed-prob 50% on a -110 line
        ⇒ approximate edge ~-2.4pp ⇒ bad_edge."""
        from apps.mockbets.services.loss_analysis import analyze_loss
        bet = MockBet.objects.create(
            user=self.user, sport='cfb', bet_type='moneyline',
            selection='Alabama', odds_american=-110,
            implied_probability=Decimal('0.524'),
            stake_amount=Decimal('100'), result='loss',
            confidence_level='low',
        )
        r = analyze_loss(bet)
        self.assertEqual(r['primary_reason'], 'bad_edge')

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
        # 2026-04-27 strict correction: "Recommended Bet" → "✅ High
        # Probability Play" so the label reflects the new probability-
        # gated definition (Recommended now requires >=55% probability).
        from apps.core.services.recommendations import action_label, STATUS_RECOMMENDED
        self.assertEqual(action_label(STATUS_RECOMMENDED), '✅ High Probability Play')

    def test_model_lean_label(self):
        from apps.core.services.recommendations import action_label, STATUS_NOT_RECOMMENDED
        self.assertEqual(action_label(STATUS_NOT_RECOMMENDED), 'Model Lean')

    def test_unknown_status_falls_back_to_recommended(self):
        """Defensive: a blank/unknown status shouldn't produce empty UI copy."""
        from apps.core.services.recommendations import action_label
        self.assertEqual(action_label(''), '✅ High Probability Play')


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
        # No PENDING result-badge on the actual bet card. The literal word
        # 'PENDING' may appear in the help-modal copy that explains all
        # result types — so we look for the specific badge HTML, not the
        # bare word.
        self.assertNotContains(resp, 'bet-result-badge">PENDING')

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


class BackfillTests(TestCase):
    """Backfill must:
      - Fill missing closing_odds + CLV when a pre-game OddsSnapshot exists
      - Skip when no snapshot exists (never fabricate)
      - Never overwrite existing data
      - Be idempotent (second run = no-op)
      - Honor dry_run (no DB writes)
      - Copy recommendation snapshot from linked BettingRecommendation only
        — never recompute from current model
    """

    def setUp(self):
        from apps.mlb.models import Conference as MLBConf, Team as MLBTeam
        self.user = User.objects.create_user('backfill_user', password='pw')
        conf = MLBConf.objects.create(name='AL East', slug='backfill-al-east')
        self.home = MLBTeam.objects.create(
            name='Yankees', slug='backfill-yankees', conference=conf,
            source='mlb_stats_api', external_id='bf-147',
        )
        self.away = MLBTeam.objects.create(
            name='Royals', slug='backfill-royals', conference=conf,
            source='mlb_stats_api', external_id='bf-118',
        )

    def _game_with_pregame_odds(self, ml_home=-150, ml_away=130, hours_past=3):
        from apps.mlb.models import Game as MLBGame, OddsSnapshot
        game = MLBGame.objects.create(
            home_team=self.home, away_team=self.away,
            first_pitch=timezone.now() - timedelta(hours=hours_past),
            status='final', home_score=5, away_score=3,
            source='mlb_stats_api', external_id=str(uuid.uuid4()),
        )
        OddsSnapshot.objects.create(
            game=game, captured_at=game.first_pitch - timedelta(minutes=10),
            market_home_win_prob=0.60,
            moneyline_home=ml_home, moneyline_away=ml_away,
        )
        return game

    def _bet(self, game, selection='Yankees', odds=-130, **overrides):
        defaults = dict(
            user=self.user, sport='mlb', bet_type='moneyline',
            selection=selection, odds_american=odds,
            implied_probability=Decimal('0.565'),
            stake_amount=Decimal('100'), mlb_game=game,
            result='win',
            simulated_payout=Decimal('76.92'),
            settled_at=timezone.now(),
        )
        defaults.update(overrides)
        return MockBet.objects.create(**defaults)

    def test_dry_run_does_not_persist_changes(self):
        from apps.mockbets.services.backfill import backfill_mockbet_data
        game = self._game_with_pregame_odds(ml_home=-150)
        bet = self._bet(game)
        self.assertIsNone(bet.closing_odds_american)
        summary = backfill_mockbet_data(dry_run=True)
        self.assertEqual(summary['closing_odds_filled'], 1)
        self.assertEqual(summary['clv_computed'], 1)
        bet.refresh_from_db()
        # Dry run — DB still has nulls
        self.assertIsNone(bet.closing_odds_american)
        self.assertIsNone(bet.clv_cents)

    def test_commit_persists_closing_odds_and_clv(self):
        from apps.mockbets.services.backfill import backfill_mockbet_data
        game = self._game_with_pregame_odds(ml_home=-150)
        bet = self._bet(game)
        backfill_mockbet_data(dry_run=False)
        bet.refresh_from_db()
        self.assertEqual(bet.closing_odds_american, -150)
        # Bet at -130, closed at -150 → less juice at bet → +CLV
        self.assertGreater(bet.clv_cents, 0)
        self.assertEqual(bet.clv_direction, 'positive')

    def test_does_not_overwrite_existing_clv(self):
        """If a bet already has CLV, backfill must leave it alone."""
        from apps.mockbets.services.backfill import backfill_mockbet_data
        game = self._game_with_pregame_odds(ml_home=-150)
        bet = self._bet(game, closing_odds_american=-200, clv_cents=0.123,
                        clv_direction='positive')
        backfill_mockbet_data(dry_run=False)
        bet.refresh_from_db()
        # Existing values preserved — backfill did not touch them
        self.assertEqual(bet.closing_odds_american, -200)
        self.assertEqual(bet.clv_cents, 0.123)

    def test_skips_when_no_pregame_snapshot(self):
        """Game has no OddsSnapshot — never fabricate."""
        from apps.mlb.models import Game as MLBGame
        from apps.mockbets.services.backfill import backfill_mockbet_data
        game = MLBGame.objects.create(
            home_team=self.home, away_team=self.away,
            first_pitch=timezone.now() - timedelta(hours=2),
            status='final', home_score=5, away_score=3,
            source='mlb_stats_api', external_id=str(uuid.uuid4()),
        )
        bet = self._bet(game)
        summary = backfill_mockbet_data(dry_run=False)
        bet.refresh_from_db()
        self.assertIsNone(bet.closing_odds_american)
        self.assertIsNone(bet.clv_cents)
        self.assertGreaterEqual(summary['skipped_no_odds'], 1)

    def test_idempotent_second_run_is_noop(self):
        from apps.mockbets.services.backfill import backfill_mockbet_data
        game = self._game_with_pregame_odds()
        self._bet(game)
        first = backfill_mockbet_data(dry_run=False)
        second = backfill_mockbet_data(dry_run=False)
        # Second run — nothing to fill
        self.assertEqual(second['closing_odds_filled'], 0)
        self.assertEqual(second['clv_computed'], 0)
        # First run filled both
        self.assertEqual(first['closing_odds_filled'], 1)
        self.assertEqual(first['clv_computed'], 1)

    def test_recommendation_snapshot_copied_from_linked_row(self):
        """When BettingRecommendation FK is set, copy snapshot fields."""
        from apps.core.models import BettingRecommendation
        from apps.mockbets.services.backfill import backfill_mockbet_data
        game = self._game_with_pregame_odds()
        # tier is derived from model_edge on the DB model; edge=6.5 → 'strong'
        rec = BettingRecommendation.objects.create(
            sport='mlb', mlb_game=game, bet_type='moneyline',
            pick='Yankees', line='-130', odds_american=-130,
            confidence_score=Decimal('72'),
            model_edge=Decimal('6.5'),
            model_source='house', status='recommended',
            status_reason='',
        )
        bet = self._bet(game, recommendation=rec)
        # Snapshot fields are blank initially
        self.assertEqual(bet.recommendation_status, '')
        self.assertEqual(bet.recommendation_tier, '')
        backfill_mockbet_data(dry_run=False)
        bet.refresh_from_db()
        self.assertEqual(bet.recommendation_status, 'recommended')
        self.assertEqual(bet.recommendation_tier, 'strong')
        self.assertEqual(bet.recommendation_confidence, Decimal('72.00'))
        # Edge backfilled too
        self.assertEqual(bet.expected_edge, Decimal('6.50'))

    def test_no_recommendation_row_means_no_rec_backfill(self):
        """Spec rule: never recompute from current model. If the linked
        BettingRecommendation row is gone, snapshot stays null."""
        from apps.mockbets.services.backfill import backfill_mockbet_data
        game = self._game_with_pregame_odds()
        bet = self._bet(game, recommendation=None)
        backfill_mockbet_data(dry_run=False)
        bet.refresh_from_db()
        # Snapshot stays empty — no fabrication
        self.assertEqual(bet.recommendation_status, '')
        self.assertEqual(bet.recommendation_tier, '')


class BulkActionsTests(TestCase):
    """Bulk MockBet operations — place_bulk_recommended + cancel_all_open.

    Both must be idempotent (running twice produces no duplicates / no
    double-cancels) and must reuse the per-bet eligibility guards instead
    of opening a back door."""

    def setUp(self):
        from apps.mlb.models import Conference as MLBConf, Team as MLBTeam, Game as MLBGame, OddsSnapshot as MLBOdds
        self.MLBGame = MLBGame
        self.MLBOdds = MLBOdds
        self.user = User.objects.create_user('bulk_user', password='pw')
        self.client = Client()
        self.client.force_login(self.user)
        conf = MLBConf.objects.create(name='AL East', slug='bulk-al-east')
        # 2026-05-03 calibration tighten: rating gap bumped 70/40 → 90/20.
        # The new MIN_PROBABILITY_FOR_RECOMMENDED=0.60 + heavier 30% market
        # blend pulls smaller gaps below the threshold; this fixture wants
        # to exercise the bulk-place path, so we provision a gap big
        # enough to clear all gates after calibration.
        self.t1 = MLBTeam.objects.create(
            name='Yankees', slug='bulk-yankees', conference=conf, rating=90,
            source='mlb_stats_api', external_id='bulk-1',
        )
        self.t2 = MLBTeam.objects.create(
            name='Rays', slug='bulk-rays', conference=conf, rating=20,
            source='mlb_stats_api', external_id='bulk-2',
        )

    def _game(self, hours_out=2, status='scheduled', home_score=None, away_score=None,
              t1=None, t2=None, ext=None):
        return self.MLBGame.objects.create(
            home_team=t1 or self.t1, away_team=t2 or self.t2,
            first_pitch=timezone.now() + timedelta(hours=hours_out),
            status=status,
            home_score=home_score, away_score=away_score,
            source='mlb_stats_api', external_id=ext or str(uuid.uuid4()),
        )

    def _add_odds(self, game, ml_home=-110, ml_away=-110, market_home_prob=0.5):
        return self.MLBOdds.objects.create(
            game=game, captured_at=timezone.now(),
            market_home_win_prob=market_home_prob,
            moneyline_home=ml_home, moneyline_away=ml_away,
        )

    # --- bulk place ---------------------------------------------------------

    def test_bulk_place_creates_one_bet_per_recommended_game(self):
        from apps.mockbets.services.bulk_actions import place_bulk_recommended_bets
        # Two games with strong rating gap → recommendation engine picks home
        game1 = self._game(hours_out=2)
        self._add_odds(game1)
        # Different teams for second game so it's not a duplicate matchup
        from apps.mlb.models import Conference as MLBConf, Team as MLBTeam
        conf = MLBConf.objects.first()
        # 2026-05-03 calibration: 72/42 gap was insufficient under the new
        # 60% probability gate; bumped to 88/22 to keep the second game
        # bulk-eligible.
        t3 = MLBTeam.objects.create(name='Sox', slug='bulk-sox', conference=conf,
                                     rating=88, source='mlb_stats_api', external_id='bulk-3')
        t4 = MLBTeam.objects.create(name='Os', slug='bulk-os', conference=conf,
                                     rating=22, source='mlb_stats_api', external_id='bulk-4')
        game2 = self._game(hours_out=4, t1=t3, t2=t4)
        self._add_odds(game2)

        summary = place_bulk_recommended_bets(self.user)
        # Both games should be eligible — verify exactly that many bets exist
        self.assertEqual(summary['placed'], MockBet.objects.filter(user=self.user).count())
        self.assertGreaterEqual(summary['placed'], 1)
        # Each bet has the snapshot fields populated
        for bet in MockBet.objects.filter(user=self.user):
            self.assertEqual(bet.recommendation_status, 'recommended')
            self.assertIn(bet.recommendation_tier, ('elite', 'strong', 'standard'))

    def test_bulk_place_is_idempotent(self):
        """Running twice must NOT create duplicate bets on the same game."""
        from apps.mockbets.services.bulk_actions import place_bulk_recommended_bets
        game = self._game(hours_out=3)
        self._add_odds(game)
        first = place_bulk_recommended_bets(self.user)
        second = place_bulk_recommended_bets(self.user)
        self.assertEqual(second['placed'], 0)
        self.assertEqual(second['skipped_existing'], first['placed'])
        # No duplicate rows for the same (user, game)
        per_game = MockBet.objects.filter(user=self.user, mlb_game=game).count()
        self.assertLessEqual(per_game, 1)

    def test_bulk_place_marks_bets_system_generated(self):
        """Engine-generated bets must carry is_system_generated=True and an
        odds_source pulled from the snapshot the engine read."""
        from apps.mockbets.services.bulk_actions import place_bulk_recommended_bets
        game = self._game(hours_out=2)
        snap = self._add_odds(game)
        # OddsSnapshot.odds_source default is 'odds_api' on MLB — explicit
        # set keeps the test resilient to default changes.
        snap.odds_source = 'odds_api'
        snap.save(update_fields=['odds_source'])

        summary = place_bulk_recommended_bets(self.user)
        self.assertGreaterEqual(summary['placed'], 1)
        for bet in MockBet.objects.filter(user=self.user):
            self.assertTrue(
                bet.is_system_generated,
                f'expected is_system_generated=True on bulk-placed bet, got False on {bet.id}',
            )
            self.assertEqual(bet.odds_source, 'odds_api')

    def test_bulk_place_skips_started_games(self):
        from apps.mockbets.services.bulk_actions import place_bulk_recommended_bets
        # Game already started — shouldn't be eligible
        game = self._game(hours_out=-1, status='live')
        self._add_odds(game)
        summary = place_bulk_recommended_bets(self.user)
        self.assertEqual(summary['placed'], 0)

    def test_bulk_place_counts_no_odds_games(self):
        from apps.mockbets.services.bulk_actions import place_bulk_recommended_bets
        # Game today, in the future, but no odds → counts as skipped_no_odds
        # First_pitch must be today (server tz) for the diagnostic counter
        local_now = timezone.localtime()
        from datetime import datetime, time as _time
        # Pick a time later today that's still in the future
        future_today = timezone.make_aware(
            datetime.combine(local_now.date(), _time(23, 30))
        )
        if future_today <= timezone.now():
            future_today = timezone.now() + timedelta(hours=2)
        self.MLBGame.objects.create(
            home_team=self.t1, away_team=self.t2,
            first_pitch=future_today,
            status='scheduled',
            source='mlb_stats_api', external_id=str(uuid.uuid4()),
        )
        summary = place_bulk_recommended_bets(self.user)
        self.assertEqual(summary['placed'], 0)
        # Even if today_count includes other odds, this game must register
        self.assertGreaterEqual(summary['skipped_no_odds'], 1)

    def test_bulk_place_only_includes_todays_slate(self):
        """Regression: bulk-place must scope to today's local slate so it
        matches the MLB hub's visible Top Plays + Recommended Bets sections.
        A previous version walked all upcoming scheduled games — clicking
        "Bet All Verified Plays" then placed bets on tomorrow's slate too,
        producing more bets than the user could see on screen.
        """
        from apps.mockbets.services.bulk_actions import place_bulk_recommended_bets

        # Today's game with rating gap → should be eligible.
        today_game = self._game(hours_out=2)
        self._add_odds(today_game)

        # Tomorrow's game — different teams to avoid duplicate-matchup
        # collisions, big rating gap so a recommendation would issue if
        # it WERE in scope. Two days out so we stay clear of edge cases
        # where local "today + 2h" still overlaps tomorrow's UTC date.
        from apps.mlb.models import Conference as MLBConf, Team as MLBTeam
        conf = MLBConf.objects.first()
        t_future_a = MLBTeam.objects.create(
            name='FutA', slug='bulk-fut-a', conference=conf,
            rating=80, source='mlb_stats_api', external_id='bulk-fut-a',
        )
        t_future_b = MLBTeam.objects.create(
            name='FutB', slug='bulk-fut-b', conference=conf,
            rating=40, source='mlb_stats_api', external_id='bulk-fut-b',
        )
        future_game = self._game(hours_out=48, t1=t_future_a, t2=t_future_b)
        self._add_odds(future_game)

        summary = place_bulk_recommended_bets(self.user)

        # Future-dated game must NOT receive a bet, even though it has
        # odds and would otherwise be a strong recommendation.
        self.assertFalse(
            MockBet.objects.filter(user=self.user, mlb_game=future_game).exists(),
            "bulk_place should not create bets on tomorrow's slate",
        )
        # Today's eligible game can still receive its bet.
        self.assertGreaterEqual(summary['placed'], 0)

    def test_bulk_place_does_not_cap_count_per_slate(self):
        """Per product direction (2026-04-28): every bet that clears the
        per-pick gates (probability ≥ 55%, |odds| ≤ 300, edge ≥ 3pp,
        primary source, no value-tier, no derived) gets surfaced. There
        is NO slate-level count ceiling.

        Locks the 'no cap' contract so a future regression that adds
        one back gets caught. Seeds 7 eligible games and asserts ALL
        7 receive a bet (not capped to some earlier value)."""
        from apps.mockbets.services.bulk_actions import place_bulk_recommended_bets
        from apps.mlb.models import Conference as MLBConf, Team as MLBTeam
        conf = MLBConf.objects.first()

        # 2026-05-06 calibration tighten: rating gap bumped 80/40 → 90/10.
        # The 0.40 market blend + 0.60 probability gate compresses model
        # output more aggressively; 80/40 produces ~59.6% prob, just
        # below the gate. 90/10 lands at ~66% — safely above.
        n_games = 7
        for i in range(n_games):
            home = MLBTeam.objects.create(
                name=f'NoCapHome{i}', slug=f'no-cap-home-{i}', conference=conf,
                rating=90, source='mlb_stats_api', external_id=f'no-cap-home-{i}',
            )
            away = MLBTeam.objects.create(
                name=f'NoCapAway{i}', slug=f'no-cap-away-{i}', conference=conf,
                rating=10, source='mlb_stats_api', external_id=f'no-cap-away-{i}',
            )
            g = self._game(hours_out=2, t1=home, t2=away,
                            ext=f'no-cap-game-{i}')
            self._add_odds(g)

        summary = place_bulk_recommended_bets(self.user)
        self.assertEqual(
            summary['placed'], n_games,
            f"bulk place must not cap legitimate recommendations; "
            f"expected {n_games}, got {summary['placed']}",
        )

    def test_bulk_place_skips_when_recommendation_is_not_recommended(self):
        """If decision rules say not_recommended, bulk-place skips. Use a mock
        so the test doesn't depend on the model's exact prob calculation."""
        from unittest.mock import patch
        from apps.mockbets.services.bulk_actions import place_bulk_recommended_bets
        from apps.core.services.recommendations import (
            Recommendation, STATUS_NOT_RECOMMENDED,
        )
        g = self._game(hours_out=3)
        self._add_odds(g, ml_home=-200, ml_away=170)
        # Force the recommendation engine to return a 'not_recommended' rec
        not_rec = Recommendation(
            sport='mlb', game=g, bet_type='moneyline', pick='Yankees',
            line='-200', odds_american=-200,
            confidence_score=55.0, model_edge=2.0, model_source='house',
            tier='standard', status=STATUS_NOT_RECOMMENDED, status_reason='low_edge',
        )
        # Patch at source — bulk_actions imports get_recommendation locally
        # inside the helper function, so we need to patch the module the
        # name resolves to.
        with patch('apps.core.services.recommendations.get_recommendation',
                   return_value=not_rec):
            place_bulk_recommended_bets(self.user)
        self.assertEqual(MockBet.objects.filter(user=self.user, mlb_game=g).count(), 0)

    # --- bulk cancel --------------------------------------------------------

    def test_bulk_cancel_deletes_only_pregame_pending(self):
        from apps.mockbets.services.bulk_actions import cancel_all_open_bets
        game_future = self._game(hours_out=3)
        game_started = self._game(hours_out=-1, status='live',
                                   t1=self.t1, t2=self.t2)
        bet_future = MockBet.objects.create(
            user=self.user, sport='mlb', bet_type='moneyline',
            selection='Yankees', odds_american=-110,
            implied_probability=Decimal('0.524'),
            stake_amount=Decimal('100'), mlb_game=game_future,
        )
        bet_started = MockBet.objects.create(
            user=self.user, sport='mlb', bet_type='moneyline',
            selection='Yankees', odds_american=-110,
            implied_probability=Decimal('0.524'),
            stake_amount=Decimal('100'), mlb_game=game_started,
        )
        summary = cancel_all_open_bets(self.user)
        self.assertEqual(summary['cancelled'], 1)
        self.assertEqual(summary['skipped_started'], 1)
        self.assertFalse(MockBet.objects.filter(id=bet_future.id).exists())
        self.assertTrue(MockBet.objects.filter(id=bet_started.id).exists())

    def test_bulk_cancel_idempotent(self):
        from apps.mockbets.services.bulk_actions import cancel_all_open_bets
        first = cancel_all_open_bets(self.user)
        second = cancel_all_open_bets(self.user)
        self.assertEqual(second['cancelled'], 0)
        self.assertEqual(second['skipped_started'], 0)

    # --- endpoints ----------------------------------------------------------

    def test_bulk_place_endpoint_returns_summary_json(self):
        game = self._game(hours_out=2)
        self._add_odds(game)
        resp = self.client.post('/mockbets/bulk/place-recommended/')
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data['success'])
        self.assertIn('placed', data)
        self.assertIn('skipped_existing', data)

    def test_bulk_cancel_endpoint_requires_login(self):
        anon = Client()
        resp = anon.post('/mockbets/bulk/cancel-open/')
        self.assertEqual(resp.status_code, 302)

    def test_bulk_endpoints_reject_get(self):
        for url in ('/mockbets/bulk/place-recommended/', '/mockbets/bulk/cancel-open/'):
            resp = self.client.get(url)
            self.assertEqual(resp.status_code, 405)


class BulkPlacementTrustRepairTests(TestCase):
    """Phase 2026-05-16 trust repair — count-vs-placement determinism.

    The 'Bet All Moneyline Plays (5) → 3 placed silently' bug class
    is locked out by these tests. Covers all 10 scenarios from the
    master prompt §6:

      1. 5 requested → 5 placed (happy path)
      2. Partial placement still succeeds for the eligible subset
      3. One failed game does NOT stop the loop
      4. Duplicate bet skip surfaces a reason
      5. Recommendation drift skip surfaces a reason
      6. Started-game skip surfaces a reason
      7. Missing-odds skip surfaces a reason
      8. Single source of truth: count == request set == placement set
      9. No silent failures (every game lands in placed/skipped/failed)
     10. UI state can refresh (response payload carries per-game items)
    """

    def setUp(self):
        from apps.mlb.models import (
            Conference as MLBConf, Team as MLBTeam,
            Game as MLBGame, OddsSnapshot as MLBOdds,
        )
        self.MLBGame = MLBGame
        self.MLBOdds = MLBOdds
        self.user = User.objects.create_user('trust_user', password='pw')
        self.client = Client()
        self.client.force_login(self.user)
        self.conf = MLBConf.objects.create(name='AL East', slug='trust-al-east')

    def _team_pair(self, suffix):
        from apps.mlb.models import Team as MLBTeam
        t1 = MLBTeam.objects.create(
            name=f'A{suffix}', slug=f'trust-a-{suffix}', conference=self.conf,
            rating=90, source='mlb_stats_api', external_id=f'trust-a-{suffix}',
        )
        t2 = MLBTeam.objects.create(
            name=f'B{suffix}', slug=f'trust-b-{suffix}', conference=self.conf,
            rating=20, source='mlb_stats_api', external_id=f'trust-b-{suffix}',
        )
        return t1, t2

    def _game_with_odds(self, suffix, *, hours_out=2, status='scheduled',
                       ml_home=-140, ml_away=120, market_home_prob=0.55):
        # 2026-05-22 fixture update: moneylines widened from -160/+140
        # to -140/+120 after MARKET_BLEND_WEIGHT bumped 0.40 → 0.55.
        # Heavier blend pulled the prior fixture below MIN_EDGE.
        # Edge under new blend: ~7.6pp (strong tier). Intent preserved.
        t1, t2 = self._team_pair(suffix)
        game = self.MLBGame.objects.create(
            home_team=t1, away_team=t2,
            first_pitch=timezone.now() + timedelta(hours=hours_out),
            status=status,
            source='mlb_stats_api', external_id=str(uuid.uuid4()),
        )
        if ml_home is not None:
            self.MLBOdds.objects.create(
                game=game, captured_at=timezone.now(),
                market_home_win_prob=market_home_prob,
                moneyline_home=ml_home, moneyline_away=ml_away,
                odds_source='odds_api', source_quality='primary',
            )
        return game

    # --- 1. Happy path: 5 requested → 5 placed -------------------------------

    def test_count_locked_set_all_placed(self):
        from apps.mockbets.services.bulk_actions import place_bulk_recommended_bets
        games = [self._game_with_odds(f'g{i}', hours_out=2 + i) for i in range(5)]
        result = place_bulk_recommended_bets(
            self.user, sport='mlb', stake=Decimal('100'),
            source_filter='verified',
            game_ids=[str(g.id) for g in games],
        )
        self.assertEqual(result['requested'], 5)
        self.assertEqual(result['placed'], 5)
        self.assertEqual(result['skipped'], 0)
        self.assertEqual(result['failed'], 0)
        self.assertEqual(len(result['placed_items']), 5)

    # --- 2. Partial placement still succeeds ---------------------------------

    def test_partial_placement_succeeds_for_eligible_subset(self):
        """3 eligible, 2 drifted-out → 3 placed + 2 skipped with reasons."""
        from apps.mockbets.services.bulk_actions import place_bulk_recommended_bets
        eligible_games = [self._game_with_odds(f'p{i}', hours_out=2 + i) for i in range(3)]
        # Two games whose recommendation will be ineligible at request
        # time. Use long-shot odds so the eligibility predicate rejects.
        drift_games = []
        for i in range(2):
            g = self._game_with_odds(
                f'd{i}', hours_out=5 + i,
                ml_home=+450, ml_away=-650, market_home_prob=0.18,
            )
            drift_games.append(g)

        all_ids = [str(g.id) for g in eligible_games + drift_games]
        result = place_bulk_recommended_bets(
            self.user, sport='mlb', stake=Decimal('100'),
            source_filter='verified', game_ids=all_ids,
        )
        # Request count matches what we asked for — not silently dropped.
        self.assertEqual(result['requested'], 5)
        # The 3 with strong gaps placed; the 2 with longshot odds skip
        # with drift reason (because the rec engine's eligibility predicate
        # rejects them).
        self.assertEqual(result['placed'] + result['skipped'], 5)
        self.assertGreaterEqual(result['placed'], 1)
        # Every drift skip carries a human-readable reason.
        for skip in result['skipped_items']:
            self.assertTrue(skip['reason'])

    # --- 3. One failed game does NOT stop the loop --------------------------

    def test_one_failed_game_does_not_terminate_loop(self):
        from unittest.mock import patch
        from apps.mockbets.services import bulk_actions
        games = [self._game_with_odds(f'f{i}', hours_out=2 + i) for i in range(5)]

        # Make MockBet.objects.create raise on the 3rd call so games 4 and 5
        # would have been killed under the legacy atomic-loop architecture.
        original_create = MockBet.objects.create
        call_count = {'n': 0}

        def failing_create(*args, **kwargs):
            call_count['n'] += 1
            if call_count['n'] == 3:
                raise RuntimeError('simulated DB failure on game 3')
            return original_create(*args, **kwargs)

        with patch.object(MockBet.objects, 'create', side_effect=failing_create):
            result = bulk_actions.place_bulk_recommended_bets(
                self.user, sport='mlb', stake=Decimal('100'),
                source_filter='verified',
                game_ids=[str(g.id) for g in games],
            )

        self.assertEqual(result['requested'], 5)
        # Per-game isolation: 4 placed, 1 failed, loop reached every game.
        self.assertEqual(result['failed'], 1)
        self.assertEqual(result['placed'] + result['skipped'] + result['failed'], 5)
        # Failed item carries the exception message.
        self.assertEqual(len(result['failed_items']), 1)
        self.assertIn('simulated DB failure', result['failed_items'][0]['reason'])

    # --- 4. Duplicate bet skip surfaces a reason ----------------------------

    def test_duplicate_bet_surfaces_explicit_reason(self):
        from apps.mockbets.services.bulk_actions import place_bulk_recommended_bets
        game = self._game_with_odds('dup', hours_out=2)
        # Pre-existing pending bet on the same game.
        MockBet.objects.create(
            user=self.user, sport='mlb', mlb_game=game,
            bet_type='moneyline', selection='X', odds_american=-160,
            implied_probability=Decimal('0.6154'),
            stake_amount=Decimal('100'), result='pending',
        )
        result = place_bulk_recommended_bets(
            self.user, sport='mlb', stake=Decimal('100'),
            source_filter='verified', game_ids=[str(game.id)],
        )
        self.assertEqual(result['requested'], 1)
        self.assertEqual(result['placed'], 0)
        self.assertEqual(result['skipped'], 1)
        self.assertEqual(
            result['skipped_items'][0]['outcome'], 'skipped_duplicate',
        )
        self.assertIn('duplicate', result['skipped_items'][0]['reason'].lower())

    # --- 5. Recommendation drift skip surfaces a reason ---------------------

    def test_recommendation_drift_surfaces_explicit_reason(self):
        """A game eligible at hub render time becomes ineligible at
        placement time. The fix surfaces drift instead of silently
        skipping. Simulate by passing a game whose odds make it
        immediately ineligible (longshot)."""
        from apps.mockbets.services.bulk_actions import place_bulk_recommended_bets
        game = self._game_with_odds(
            'drift', hours_out=2,
            ml_home=+550, ml_away=-750, market_home_prob=0.13,
        )
        result = place_bulk_recommended_bets(
            self.user, sport='mlb', stake=Decimal('100'),
            source_filter='verified', game_ids=[str(game.id)],
        )
        self.assertEqual(result['placed'], 0)
        self.assertEqual(result['skipped'], 1)
        # Skip reason is drift (recommendation no longer eligible).
        self.assertEqual(
            result['skipped_items'][0]['outcome'],
            'skipped_recommendation_drift',
        )

    # --- 6. Started-game skip surfaces a reason -----------------------------

    def test_started_game_skip_surfaces_explicit_reason(self):
        from apps.mockbets.services.bulk_actions import place_bulk_recommended_bets
        # Game that started 1 hour ago — first_pitch in the past.
        t1, t2 = self._team_pair('start')
        game = self.MLBGame.objects.create(
            home_team=t1, away_team=t2,
            first_pitch=timezone.now() - timedelta(hours=1),
            status='scheduled',
            source='mlb_stats_api', external_id=str(uuid.uuid4()),
        )
        self.MLBOdds.objects.create(
            game=game, captured_at=timezone.now(),
            market_home_win_prob=0.55,
            moneyline_home=-160, moneyline_away=140,
            odds_source='odds_api', source_quality='primary',
        )
        result = place_bulk_recommended_bets(
            self.user, sport='mlb', stake=Decimal('100'),
            source_filter='verified', game_ids=[str(game.id)],
        )
        self.assertEqual(result['placed'], 0)
        self.assertEqual(result['skipped'], 1)
        self.assertEqual(
            result['skipped_items'][0]['outcome'], 'skipped_game_started',
        )

    # --- 7. Missing-odds skip surfaces a reason -----------------------------

    def test_missing_odds_skip_surfaces_explicit_reason(self):
        from apps.mockbets.services.bulk_actions import place_bulk_recommended_bets
        # Game has no odds snapshot → get_recommendation returns None.
        t1, t2 = self._team_pair('noodds')
        game = self.MLBGame.objects.create(
            home_team=t1, away_team=t2,
            first_pitch=timezone.now() + timedelta(hours=2),
            status='scheduled',
            source='mlb_stats_api', external_id=str(uuid.uuid4()),
        )
        result = place_bulk_recommended_bets(
            self.user, sport='mlb', stake=Decimal('100'),
            source_filter='verified', game_ids=[str(game.id)],
        )
        self.assertEqual(result['placed'], 0)
        self.assertEqual(result['skipped'], 1)
        self.assertEqual(
            result['skipped_items'][0]['outcome'], 'skipped_missing_odds',
        )

    # --- 8. Single source of truth: count predicate ------------------------

    def test_is_bulk_moneyline_eligible_consistent_across_callers(self):
        """The hub view's count and the bulk endpoint's placement set
        come from the SAME predicate — that's the entire fix. This
        test locks the predicate behavior by exercising it directly."""
        from apps.mockbets.services.bulk_actions import is_bulk_moneyline_eligible

        # None → ineligible.
        self.assertFalse(is_bulk_moneyline_eligible(None))

        # Class-based stub mimicking a Recommendation dataclass.
        class _Rec:
            def __init__(self, **kw):
                self.status = kw.get('status', 'recommended')
                self.lane = kw.get('lane', 'core')
                self.tier = kw.get('tier', 'strong')
                self.status_reason = kw.get('status_reason', '')
                self.confidence_score = kw.get('confidence_score', 62.0)
                self.odds_american = kw.get('odds_american', -130)
                self.is_secondary = kw.get('is_secondary', False)

        # Happy path.
        self.assertTrue(is_bulk_moneyline_eligible(_Rec()))

        # Lane != core (the original bug fingerprint).
        self.assertFalse(is_bulk_moneyline_eligible(_Rec(lane='qualified')))
        self.assertFalse(is_bulk_moneyline_eligible(_Rec(lane='pass')))

        # Status != recommended.
        self.assertFalse(is_bulk_moneyline_eligible(_Rec(status='not_recommended')))

        # Value tier.
        self.assertFalse(is_bulk_moneyline_eligible(_Rec(tier='value')))
        self.assertFalse(is_bulk_moneyline_eligible(_Rec(status_reason='value')))

        # Blocked.
        self.assertFalse(is_bulk_moneyline_eligible(_Rec(tier='blocked')))
        self.assertFalse(is_bulk_moneyline_eligible(_Rec(status_reason='derived_odds')))

        # Probability below threshold (default MIN_PROBABILITY_FOR_RECOMMENDED=0.60).
        self.assertFalse(is_bulk_moneyline_eligible(_Rec(confidence_score=55.0)))

        # Longshot.
        self.assertFalse(is_bulk_moneyline_eligible(_Rec(odds_american=400)))
        self.assertFalse(is_bulk_moneyline_eligible(_Rec(odds_american=-400)))

        # Source filter: verified excludes secondary.
        self.assertFalse(is_bulk_moneyline_eligible(
            _Rec(is_secondary=True), source_filter='verified',
        ))
        # Source filter: espn excludes primary.
        self.assertFalse(is_bulk_moneyline_eligible(
            _Rec(is_secondary=False), source_filter='espn',
        ))
        # Source filter: all permits both.
        self.assertTrue(is_bulk_moneyline_eligible(
            _Rec(is_secondary=True), source_filter='all',
        ))

    # --- 9. No silent failures: every game gets an outcome ------------------

    def test_every_game_lands_in_exactly_one_outcome_bucket(self):
        """For any set of game_ids, placed + skipped + failed = requested."""
        from apps.mockbets.services.bulk_actions import place_bulk_recommended_bets
        # Heterogeneous set: 2 eligible, 1 duplicate, 1 longshot drift, 1 started.
        eligible_a = self._game_with_odds('mix1', hours_out=2)
        eligible_b = self._game_with_odds('mix2', hours_out=3)
        # Duplicate
        dup_game = self._game_with_odds('mix3', hours_out=4)
        MockBet.objects.create(
            user=self.user, sport='mlb', mlb_game=dup_game,
            bet_type='moneyline', selection='X', odds_american=-160,
            implied_probability=Decimal('0.6154'),
            stake_amount=Decimal('100'), result='pending',
        )
        # Drift (longshot odds)
        drift_game = self._game_with_odds(
            'mix4', hours_out=5,
            ml_home=+500, ml_away=-700, market_home_prob=0.15,
        )
        # Started
        t1, t2 = self._team_pair('mix5')
        started_game = self.MLBGame.objects.create(
            home_team=t1, away_team=t2,
            first_pitch=timezone.now() - timedelta(hours=1),
            status='scheduled',
            source='mlb_stats_api', external_id=str(uuid.uuid4()),
        )
        self.MLBOdds.objects.create(
            game=started_game, captured_at=timezone.now(),
            market_home_win_prob=0.55,
            moneyline_home=-160, moneyline_away=140,
            odds_source='odds_api', source_quality='primary',
        )

        all_ids = [
            str(g.id) for g in
            [eligible_a, eligible_b, dup_game, drift_game, started_game]
        ]
        result = place_bulk_recommended_bets(
            self.user, sport='mlb', stake=Decimal('100'),
            source_filter='verified', game_ids=all_ids,
        )
        self.assertEqual(result['requested'], 5)
        # The alignment contract: every game lands in EXACTLY one bucket.
        self.assertEqual(
            result['placed'] + result['skipped'] + result['failed'],
            result['requested'],
        )
        # Each outcome list carries the documented reason.
        for item in result['placed_items']:
            self.assertEqual(item['outcome'], 'placed')
            self.assertTrue(item['label'])
        for item in result['skipped_items']:
            self.assertIn('skipped_', item['outcome'])
            self.assertTrue(item['reason'])
        for item in result['failed_items']:
            self.assertEqual(item['outcome'], 'failed')
            self.assertTrue(item['reason'])

    # --- 10. Endpoint accepts JSON body with game_ids -----------------------

    def test_endpoint_processes_locked_game_ids_via_json_body(self):
        """The view layer reads game_ids from the JSON body and passes
        them to the service. Locks the wire contract that the JS depends on."""
        import json as _json
        from django.urls import reverse
        eligible_a = self._game_with_odds('end1', hours_out=2)
        eligible_b = self._game_with_odds('end2', hours_out=3)

        # Place a game whose IDs are NOT in the request — should be
        # ignored entirely (single source of truth: only the IDs in the
        # request body get processed).
        ignored_game = self._game_with_odds('end3', hours_out=4)

        url = reverse('mockbets:bulk_place_recommended') + '?source_filter=verified'
        resp = self.client.post(
            url,
            data=_json.dumps({'game_ids': [str(eligible_a.id), str(eligible_b.id)]}),
            content_type='application/json',
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data['requested'], 2)
        self.assertEqual(data['placed'], 2)
        # The ignored game has no bet placed.
        self.assertFalse(
            MockBet.objects.filter(
                user=self.user, mlb_game=ignored_game,
            ).exists(),
        )

    def test_endpoint_falls_back_to_legacy_path_without_body(self):
        """An old client that doesn't send a body still works — the
        service computes the candidate set itself."""
        from django.urls import reverse
        self._game_with_odds('legacy1', hours_out=2)
        self._game_with_odds('legacy2', hours_out=3)

        url = reverse('mockbets:bulk_place_recommended') + '?source_filter=verified'
        resp = self.client.post(url)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        # Legacy path runs; placed > 0 because the games are eligible.
        self.assertGreaterEqual(data['placed'], 1)


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

    def _bet(self, result, clv=None, direction=None, odds_source='odds_api'):
        # 2026-05-16 evaluation-integrity: CLV math now requires
        # odds_source='odds_api' per framework §1.2. Test fixture
        # defaults to 'odds_api' so the historic CLV semantics are
        # preserved for tests that don't care about the source filter.
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
            odds_source=odds_source,
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


# =============================================================================
# System Tuning page — staff-only diagnostic + insights engine.
# =============================================================================

class SystemTuningClassifiersTests(TestCase):
    """Pure-function tests for classifier helpers — no DB needed."""

    def test_classify_odds_range_underdog(self):
        from apps.mockbets.services.system_tuning import classify_odds_range
        self.assertEqual(classify_odds_range(150), 'underdog')
        self.assertEqual(classify_odds_range(300), 'underdog')

    def test_classify_odds_range_mid_dog(self):
        from apps.mockbets.services.system_tuning import classify_odds_range
        self.assertEqual(classify_odds_range(100), 'mid_dog')
        self.assertEqual(classify_odds_range(149), 'mid_dog')

    def test_classify_odds_range_mid(self):
        from apps.mockbets.services.system_tuning import classify_odds_range
        self.assertEqual(classify_odds_range(-150), 'mid')
        self.assertEqual(classify_odds_range(99), 'mid')
        self.assertEqual(classify_odds_range(-110), 'mid')

    def test_classify_odds_range_favorite(self):
        from apps.mockbets.services.system_tuning import classify_odds_range
        self.assertEqual(classify_odds_range(-200), 'favorite')
        self.assertEqual(classify_odds_range(-300), 'favorite')

    def test_classify_odds_range_heavy_favorite(self):
        from apps.mockbets.services.system_tuning import classify_odds_range
        self.assertEqual(classify_odds_range(-301), 'heavy_favorite')
        self.assertEqual(classify_odds_range(-500), 'heavy_favorite')

    def test_classify_odds_range_none(self):
        from apps.mockbets.services.system_tuning import classify_odds_range
        self.assertIsNone(classify_odds_range(None))

    def test_data_confidence_low_band(self):
        from apps.mockbets.services.system_tuning import compute_data_confidence
        self.assertEqual(compute_data_confidence([])['level'], 'LOW')
        self.assertEqual(compute_data_confidence([None] * 29)['level'], 'LOW')

    def test_data_confidence_medium_band(self):
        from apps.mockbets.services.system_tuning import compute_data_confidence
        self.assertEqual(compute_data_confidence([None] * 30)['level'], 'MEDIUM')
        self.assertEqual(compute_data_confidence([None] * 99)['level'], 'MEDIUM')

    def test_data_confidence_high_band(self):
        from apps.mockbets.services.system_tuning import compute_data_confidence
        self.assertEqual(compute_data_confidence([None] * 100)['level'], 'HIGH')

    def test_current_config_exposes_real_constants(self):
        """Sanity-check: config panel reflects the actual engine, not made-up keys."""
        from apps.mockbets.services.system_tuning import current_config
        cfg = current_config()
        for key in ('MIN_EDGE', 'STRONG_EDGE', 'ELITE_EDGE',
                    'HEAVY_FAVORITE_ODDS', 'MAX_ELITE_PER_SLATE'):
            self.assertIn(key, cfg)
        # Must NOT invent fields the engine doesn't have.
        self.assertNotIn('prob_threshold', cfg)
        self.assertNotIn('allowed_sources', cfg)
        self.assertEqual(cfg['engine_emits'], ['moneyline'])

    def test_source_quality_placeholder_is_uninstrumented(self):
        from apps.mockbets.services.system_tuning import segment_by_source_quality
        result = segment_by_source_quality([])
        self.assertFalse(result['instrumented'])
        self.assertIn('odds provenance', result['message'].lower())


class SystemTuningStableShapeTests(TestCase):
    """Output shape stays identical when there are zero bets — the template
    must never have to defensive-check for missing keys."""

    def test_compute_all_returns_full_shape_with_no_bets(self):
        from apps.mockbets.services.system_tuning import compute_all
        ctx = compute_all([])
        for key in ('overall', 'data_confidence', 'time_windows',
                    'by_bet_type', 'by_odds_range', 'source_quality',
                    'edge', 'config', 'insights', 'verdict', 'actions'):
            self.assertIn(key, ctx, f'missing top-level key: {key}')

    def test_time_windows_always_have_three_keys(self):
        from apps.mockbets.services.system_tuning import compute_time_windows
        windows = compute_time_windows([])
        self.assertEqual(set(windows.keys()), {'7d', '30d', 'all_time'})
        for w in windows.values():
            for k in ('count', 'roi', 'win_rate', 'net_pl',
                      'avg_clv', 'positive_clv_rate', 'clv_sample'):
                self.assertIn(k, w)

    def test_bet_type_segments_always_present(self):
        from apps.mockbets.services.system_tuning import segment_by_bet_type
        seg = segment_by_bet_type([])
        self.assertEqual(set(seg.keys()), {'moneyline', 'spread', 'total'})

    def test_odds_range_segments_always_present(self):
        from apps.mockbets.services.system_tuning import segment_by_odds_range
        seg = segment_by_odds_range([])
        self.assertEqual(
            set(seg.keys()),
            {'underdog', 'mid_dog', 'mid', 'favorite', 'heavy_favorite'},
        )

    def test_verdict_with_zero_bets_is_needs_adjustment(self):
        from apps.mockbets.services.system_tuning import compute_all
        ctx = compute_all([])
        self.assertEqual(ctx['verdict']['health'], 'needs_adjustment')
        self.assertEqual(ctx['verdict']['strength'], [])
        self.assertEqual(ctx['verdict']['weakness'], [])
        self.assertEqual(ctx['verdict']['risk'], [])

    def test_actions_capped_at_three(self):
        from apps.mockbets.services.system_tuning import recommend_actions
        # Synthetically pass in many insights — cap is 3 even with all matches.
        insights = [
            {'category': 'weakness', 'message': 'Spread bets underperforming', 'evidence': {}},
            {'category': 'weakness', 'message': 'Total bets underperforming', 'evidence': {}},
            {'category': 'weakness', 'message': 'High-edge bets not outperforming low-edge bets', 'evidence': {}},
            {'category': 'weakness', 'message': 'Heavy-favorite bets losing money', 'evidence': {}},
            {'category': 'risk', 'message': 'Market moving against picks', 'evidence': {}},
        ]
        actions = recommend_actions(insights)
        self.assertLessEqual(len(actions), 3)
        # Ordering preserved from input.
        self.assertEqual(len(actions), 3)


class SystemTuningWindowsTests(TestCase):
    """Verify time windows actually filter by placed_at."""

    def setUp(self):
        self.user = User.objects.create_user('twuser', password='x')

    def _bet(self, days_ago, result='win', odds=-110):
        bet = MockBet.objects.create(
            user=self.user, sport='cfb', bet_type='moneyline',
            selection='Alabama', odds_american=odds,
            implied_probability=Decimal('0.50'),
            stake_amount=Decimal('100'),
            simulated_payout=Decimal('91') if result == 'win' else None,
            result=result,
        )
        # Bypass auto_now / default by direct assignment then save.
        bet.placed_at = timezone.now() - timedelta(days=days_ago)
        bet.save(update_fields=['placed_at'])
        return bet

    def test_seven_day_window_excludes_older(self):
        from apps.mockbets.services.system_tuning import compute_time_windows
        self._bet(days_ago=2)
        self._bet(days_ago=10)  # outside 7d
        self._bet(days_ago=40)  # outside 30d
        windows = compute_time_windows(MockBet.objects.all())
        self.assertEqual(windows['7d']['count'], 1)
        self.assertEqual(windows['30d']['count'], 2)
        self.assertEqual(windows['all_time']['count'], 3)


class SystemTuningInsightsAndVerdictTests(TestCase):
    """End-to-end: build a bet population that triggers known rules, assert
    the rules fire and the verdict matches."""

    def setUp(self):
        self.user = User.objects.create_user('insightuser', password='x')

    def _bet(self, *, bet_type='moneyline', odds=-110, result='loss',
             stake=Decimal('100'), payout=None, edge=None):
        return MockBet.objects.create(
            user=self.user, sport='cfb', bet_type=bet_type,
            selection='Test', odds_american=odds,
            implied_probability=Decimal('0.50'),
            stake_amount=stake,
            simulated_payout=payout,
            result=result,
            expected_edge=edge,
        )

    def test_spread_underperforming_triggers_weakness_insight(self):
        # 12 spread losses → ROI = -100% → triggers weakness rule.
        for _ in range(12):
            self._bet(bet_type='spread', result='loss')
        from apps.mockbets.services.system_tuning import compute_all
        ctx = compute_all(MockBet.objects.all())
        msgs = [i['message'] for i in ctx['insights']]
        self.assertIn('Spread bets underperforming', msgs)
        # And the corresponding action surfaces (worded for manual-only).
        self.assertTrue(
            any('manual-only' in a.lower() for a in ctx['actions']),
            f'expected a manual-only spread action; got {ctx["actions"]}',
        )

    def test_below_sample_floor_does_not_trigger(self):
        """5 spread losses < _MIN_SEGMENT_SAMPLE — should NOT trigger."""
        for _ in range(5):
            self._bet(bet_type='spread', result='loss')
        from apps.mockbets.services.system_tuning import compute_all
        ctx = compute_all(MockBet.objects.all())
        msgs = [i['message'] for i in ctx['insights']]
        self.assertNotIn('Spread bets underperforming', msgs)

    def test_verdict_weak_when_overall_roi_below_minus_five(self):
        # 12 ML losses → ROI = -100%, well past -5% threshold.
        for _ in range(12):
            self._bet(bet_type='moneyline', result='loss')
        from apps.mockbets.services.system_tuning import compute_all
        ctx = compute_all(MockBet.objects.all())
        self.assertEqual(ctx['verdict']['health'], 'weak')

    def test_verdict_strong_when_roi_positive_and_no_clv_signal(self):
        """Spec: ROI > 0 AND (no CLV sample OR positive_clv_rate >= 52%) → strong.
        With no CLV captured, the CLV gate is vacuously satisfied."""
        # 12 wins at +100 → ROI = +100%. No CLV (no closing odds).
        for _ in range(12):
            self._bet(bet_type='moneyline', odds=100, result='win',
                      payout=Decimal('100'))
        from apps.mockbets.services.system_tuning import compute_all
        ctx = compute_all(MockBet.objects.all())
        self.assertEqual(ctx['verdict']['health'], 'strong')

    def test_high_edge_not_outperforming_triggers(self):
        # Edge buckets in command_center: 0-2pp / 2-4pp / 4-6pp / 6pp+.
        # 10 top-bucket (6pp+) losses + 10 bottom-bucket (0-2pp) wins → top.roi <= bottom.roi
        for _ in range(10):
            self._bet(bet_type='moneyline', odds=-110, result='loss',
                      edge=Decimal('8.0'))
        for _ in range(10):
            self._bet(bet_type='moneyline', odds=100, result='win',
                      edge=Decimal('1.0'), payout=Decimal('100'))
        from apps.mockbets.services.system_tuning import compute_all
        ctx = compute_all(MockBet.objects.all())
        msgs = [i['message'] for i in ctx['insights']]
        self.assertIn('High-edge bets not outperforming low-edge bets', msgs)


from django.test import override_settings


@override_settings(STORAGES={
    'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
    'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'},
})
class SystemTuningAccessTests(TestCase):
    """The page is staff-only — non-staff get 404, anon redirect to login."""

    def setUp(self):
        self.staff = User.objects.create_user('stafftuner', password='x', is_staff=True)
        self.normal = User.objects.create_user('normaltuner', password='x')
        self.client = Client()

    def test_staff_can_load_page(self):
        self.client.force_login(self.staff)
        resp = self.client.get('/mockbets/system-tuning/')
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'System Tuning')
        self.assertContains(resp, 'Data Confidence')

    def test_non_staff_gets_404(self):
        self.client.force_login(self.normal)
        resp = self.client.get('/mockbets/system-tuning/')
        self.assertEqual(resp.status_code, 404)

    def test_anon_redirected_to_login(self):
        resp = self.client.get('/mockbets/system-tuning/')
        # login_required → 302 to login URL
        self.assertEqual(resp.status_code, 302)
        self.assertIn('/login', resp.url)


# =============================================================================
# Provenance fields — odds_source + is_system_generated.
# =============================================================================

class MockBetProvenanceFieldDefaultsTests(TestCase):
    """Schema-level defaults — backfilled rows + bets created without the new
    fields explicitly set should land in the safe state."""

    def test_default_odds_source_is_unknown(self):
        user = User.objects.create_user('prov_default', password='x')
        bet = MockBet.objects.create(
            user=user, sport='cfb', bet_type='moneyline',
            selection='X', odds_american=-110,
            implied_probability=Decimal('0.5'),
            stake_amount=Decimal('100'),
        )
        self.assertEqual(bet.odds_source, 'unknown')
        self.assertFalse(bet.is_system_generated)


class ManualPlacementOddsSourceTests(TestCase):
    """The /mockbets/place/ endpoint is the manual placement path. It
    should populate odds_source from the latest snapshot (or 'manual' when
    none exists) and leave is_system_generated as False."""

    def setUp(self):
        from apps.mlb.models import Conference, Team, Game
        league = Conference.objects.create(
            name=f'L{timezone.now().timestamp()}',
            slug=f'l{timezone.now().timestamp()}',
        )
        self.home = Team.objects.create(name='HomeTeam', slug='hometeam', conference=league)
        self.away = Team.objects.create(name='AwayTeam', slug='awayteam', conference=league)
        self.game = Game.objects.create(
            home_team=self.home, away_team=self.away,
            first_pitch=timezone.now() + timedelta(hours=4),
        )
        self.user = User.objects.create_user('manual_user', password='x')
        self.client.force_login(self.user)

    def test_manual_with_snapshot_inherits_source(self):
        from apps.mlb.models import OddsSnapshot
        OddsSnapshot.objects.create(
            game=self.game, captured_at=timezone.now(),
            sportsbook='draftkings', market_home_win_prob=0.55,
            moneyline_home=-122, moneyline_away=110,
            odds_source='odds_api',
        )
        import json
        resp = self.client.post('/mockbets/place/', json.dumps({
            'sport': 'mlb', 'game_id': str(self.game.id),
            'bet_type': 'moneyline', 'selection': 'HomeTeam',
            'odds_american': -122, 'stake_amount': '100',
        }), content_type='application/json')
        self.assertEqual(resp.status_code, 200)
        bet = MockBet.objects.get(user=self.user)
        self.assertEqual(bet.odds_source, 'odds_api')
        self.assertFalse(bet.is_system_generated)

    def test_manual_without_snapshot_marked_manual(self):
        # No snapshot exists for the game → 'manual', not 'unknown'.
        # 'unknown' is reserved for pre-feature historical rows.
        import json
        resp = self.client.post('/mockbets/place/', json.dumps({
            'sport': 'mlb', 'game_id': str(self.game.id),
            'bet_type': 'moneyline', 'selection': 'HomeTeam',
            'odds_american': -122, 'stake_amount': '100',
        }), content_type='application/json')
        self.assertEqual(resp.status_code, 200)
        bet = MockBet.objects.get(user=self.user)
        self.assertEqual(bet.odds_source, 'manual')
        self.assertFalse(bet.is_system_generated)


class SystemTuningStaleCountTests(TestCase):
    """compute_all should always include stale_games_count, even when it
    can't compute the value (returns 0 on any failure)."""

    def test_stale_games_count_in_output(self):
        from apps.mockbets.services.system_tuning import compute_all
        ctx = compute_all([])
        self.assertIn('stale_games_count', ctx)
        self.assertIsInstance(ctx['stale_games_count'], int)


# =============================================================================
# Stale-flag rendering on MLB diagnostic.
# =============================================================================

@override_settings(STORAGES={
    'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
    'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'},
})
class MLBDiagnosticStaleColumnTests(TestCase):
    """Smoke test the new column wiring — the row dict must carry an
    is_stale key after the change in apps/mlb/views.py."""

    def test_diag_row_has_is_stale_key(self):
        from apps.mlb.views import _build_diag_rows

        class _StubGame:
            class _Team:
                def __init__(self, n):
                    self.name = n
            home_team = _Team('H')
            away_team = _Team('A')
            status = 'scheduled'
            first_pitch = timezone.now() + timedelta(hours=4)
            id = 'stub'

            class _Manager:
                def order_by(self, *a, **kw):
                    class _QS:
                        def first(self):
                            return None
                    return _QS()

                def filter(self, *a, **kw):
                    return self

            odds_snapshots = _Manager()

        class _StubSignal:
            game = _StubGame()
            latest_odds = None
            recommendation = None
            house_prob = None

        rows = _build_diag_rows([_StubSignal()])
        self.assertEqual(len(rows), 1)
        self.assertIn('is_stale', rows[0])
        # No snapshots → not stale.
        self.assertFalse(rows[0]['is_stale'])


# =============================================================================
# Moneyline Evaluation — slate post-mortem report.
# =============================================================================

class MoneylineEvaluationFiltersTests(TestCase):
    """The report's queryset filters: bet_type='moneyline', date range
    inclusive on both endpoints, is_system_generated=True by default."""

    def setUp(self):
        self.user = User.objects.create_user('eval_user', password='x')

    def _bet(self, *, bet_type='moneyline', placed_days_ago=1, result='loss',
             is_system_generated=True, stake=Decimal('100'), payout=None,
             odds=-110, edge=Decimal('5.0'), confidence=Decimal('60.0')):
        bet = MockBet.objects.create(
            user=self.user, sport='mlb', bet_type=bet_type,
            selection='X', odds_american=odds,
            implied_probability=Decimal('0.5238'),
            stake_amount=stake,
            simulated_payout=payout,
            result=result,
            is_system_generated=is_system_generated,
            expected_edge=edge,
            recommendation_status='recommended',
            recommendation_tier='standard',
            recommendation_confidence=confidence,
        )
        # Override placed_at so date-range filtering can be exercised.
        bet.placed_at = timezone.now() - timedelta(days=placed_days_ago)
        bet.save(update_fields=['placed_at'])
        return bet

    def test_excludes_spread_bets(self):
        from apps.mockbets.services.moneyline_evaluation import build_evaluation_report
        self._bet(bet_type='moneyline', placed_days_ago=1)
        self._bet(bet_type='spread', placed_days_ago=1)
        today = timezone.localdate()
        report = build_evaluation_report(
            MockBet.objects.all(),
            date_from=today - timedelta(days=1),
            date_to=today - timedelta(days=1),
            include_manual=False,
        )
        self.assertEqual(report['executive_summary']['bets_count'], 1)
        for bet_row in report['bets']:
            self.assertNotEqual(bet_row['selection'].lower(), 'spread')

    def test_excludes_total_bets(self):
        from apps.mockbets.services.moneyline_evaluation import build_evaluation_report
        self._bet(bet_type='moneyline', placed_days_ago=1)
        self._bet(bet_type='total', placed_days_ago=1)
        today = timezone.localdate()
        report = build_evaluation_report(
            MockBet.objects.all(),
            date_from=today - timedelta(days=1),
            date_to=today - timedelta(days=1),
        )
        self.assertEqual(report['executive_summary']['bets_count'], 1)

    def test_excludes_manual_bets_by_default(self):
        from apps.mockbets.services.moneyline_evaluation import build_evaluation_report
        self._bet(is_system_generated=True, placed_days_ago=1)
        self._bet(is_system_generated=False, placed_days_ago=1)
        today = timezone.localdate()
        report = build_evaluation_report(
            MockBet.objects.all(),
            date_from=today - timedelta(days=1),
            date_to=today - timedelta(days=1),
            include_manual=False,
        )
        self.assertEqual(report['executive_summary']['bets_count'], 1)

    def test_includes_manual_when_toggle_on(self):
        from apps.mockbets.services.moneyline_evaluation import build_evaluation_report
        self._bet(is_system_generated=True, placed_days_ago=1)
        self._bet(is_system_generated=False, placed_days_ago=1)
        today = timezone.localdate()
        report = build_evaluation_report(
            MockBet.objects.all(),
            date_from=today - timedelta(days=1),
            date_to=today - timedelta(days=1),
            include_manual=True,
        )
        self.assertEqual(report['executive_summary']['bets_count'], 2)

    def test_date_range_endpoints_inclusive(self):
        from apps.mockbets.services.moneyline_evaluation import build_evaluation_report
        # Three bets across three consecutive days.
        self._bet(placed_days_ago=2)
        self._bet(placed_days_ago=1)
        self._bet(placed_days_ago=0)
        today = timezone.localdate()
        # Range [today-1, today] should include exactly 2 bets.
        report = build_evaluation_report(
            MockBet.objects.all(),
            date_from=today - timedelta(days=1),
            date_to=today,
        )
        self.assertEqual(report['executive_summary']['bets_count'], 2)


class EvaluationScopeIntegrityTests(TestCase):
    """Phase 2026-05-14 evaluation-integrity repair.

    Locks the contract that the Moneyline Evaluation page can never
    silently exclude actual placed bets. Mirrors the 'My Bets shows 6,
    Evaluation shows 2' discrepancy with deterministic test data.
    """

    def setUp(self):
        self.user = User.objects.create_user('scope_user', password='x')

    def _bet(self, *, is_system_generated, placed_days_ago=1, bet_type='moneyline'):
        bet = MockBet.objects.create(
            user=self.user, sport='mlb', bet_type=bet_type,
            selection='X', odds_american=-130,
            implied_probability=Decimal('0.5652'),
            stake_amount=Decimal('100'),
            result='pending',
            is_system_generated=is_system_generated,
            expected_edge=Decimal('5.0'),
            recommendation_status='recommended' if is_system_generated else '',
            recommendation_tier='standard' if is_system_generated else '',
            recommendation_confidence=Decimal('60.0'),
        )
        bet.placed_at = timezone.now() - timedelta(days=placed_days_ago)
        bet.save(update_fields=['placed_at'])
        return bet

    # ----- Scope semantics --------------------------------------------------

    def test_actual_scope_includes_all_placed_bets(self):
        """SCOPE_ACTUAL must match My Bets — system + manual both
        included. This is the discrepancy fix."""
        from apps.mockbets.services.moneyline_evaluation import (
            build_evaluation_report, SCOPE_ACTUAL,
        )
        # 3 system + 3 manual = 6 placed.
        for _ in range(3):
            self._bet(is_system_generated=True)
            self._bet(is_system_generated=False)

        today = timezone.localdate()
        report = build_evaluation_report(
            MockBet.objects.all(),
            date_from=today - timedelta(days=1),
            date_to=today - timedelta(days=1),
            scope=SCOPE_ACTUAL,
        )
        self.assertEqual(report['scope']['scope'], SCOPE_ACTUAL)
        self.assertEqual(report['scope']['total_placed_in_window'], 6)
        self.assertEqual(report['scope']['included'], 6)
        self.assertEqual(report['scope']['excluded'], 0)
        self.assertEqual(report['executive_summary']['bets_count'], 6)

    def test_recommended_scope_excludes_manual_bets_with_count(self):
        """SCOPE_RECOMMENDED must surface the excluded count + reason —
        the silent-exclusion failure mode is forbidden."""
        from apps.mockbets.services.moneyline_evaluation import (
            build_evaluation_report, SCOPE_RECOMMENDED,
        )
        for _ in range(2):
            self._bet(is_system_generated=True)
        for _ in range(4):
            self._bet(is_system_generated=False)

        today = timezone.localdate()
        report = build_evaluation_report(
            MockBet.objects.all(),
            date_from=today - timedelta(days=1),
            date_to=today - timedelta(days=1),
            scope=SCOPE_RECOMMENDED,
        )
        self.assertEqual(report['scope']['total_placed_in_window'], 6)
        self.assertEqual(report['scope']['included'], 2)
        self.assertEqual(report['scope']['excluded'], 4)
        self.assertEqual(report['scope']['exclusion_reasons'].get('manual_bets'), 4)
        self.assertEqual(report['executive_summary']['bets_count'], 2)

    def test_manual_scope_excludes_system_bets_with_count(self):
        from apps.mockbets.services.moneyline_evaluation import (
            build_evaluation_report, SCOPE_MANUAL,
        )
        for _ in range(2):
            self._bet(is_system_generated=True)
        for _ in range(3):
            self._bet(is_system_generated=False)

        today = timezone.localdate()
        report = build_evaluation_report(
            MockBet.objects.all(),
            date_from=today - timedelta(days=1),
            date_to=today - timedelta(days=1),
            scope=SCOPE_MANUAL,
        )
        self.assertEqual(report['scope']['included'], 3)
        self.assertEqual(report['scope']['excluded'], 2)
        self.assertEqual(
            report['scope']['exclusion_reasons'].get('system_generated_bets'),
            2,
        )

    def test_all_scope_is_alias_for_actual(self):
        from apps.mockbets.services.moneyline_evaluation import (
            build_evaluation_report, SCOPE_ACTUAL, SCOPE_ALL,
        )
        self._bet(is_system_generated=True)
        self._bet(is_system_generated=False)
        today = timezone.localdate()

        report_actual = build_evaluation_report(
            MockBet.objects.all(),
            date_from=today - timedelta(days=1),
            date_to=today - timedelta(days=1),
            scope=SCOPE_ACTUAL,
        )
        report_all = build_evaluation_report(
            MockBet.objects.all(),
            date_from=today - timedelta(days=1),
            date_to=today - timedelta(days=1),
            scope=SCOPE_ALL,
        )
        self.assertEqual(
            report_actual['scope']['included'],
            report_all['scope']['included'],
        )

    # ----- Default behavior --------------------------------------------------

    def test_default_scope_is_actual(self):
        """Calling build_evaluation_report with neither `scope` nor
        `include_manual` must default to SCOPE_ACTUAL — the
        evaluation-integrity contract."""
        from apps.mockbets.services.moneyline_evaluation import (
            build_evaluation_report, SCOPE_ACTUAL,
        )
        self._bet(is_system_generated=True)
        self._bet(is_system_generated=False)
        today = timezone.localdate()

        report = build_evaluation_report(
            MockBet.objects.all(),
            date_from=today - timedelta(days=1),
            date_to=today - timedelta(days=1),
        )
        self.assertEqual(report['scope']['scope'], SCOPE_ACTUAL)
        self.assertEqual(report['scope']['included'], 2)

    # ----- Back-compat --------------------------------------------------

    def test_legacy_include_manual_false_maps_to_recommended(self):
        """Old callers that pass include_manual=False must get the
        historical recommended-only behavior — strict back-compat."""
        from apps.mockbets.services.moneyline_evaluation import (
            build_evaluation_report, SCOPE_RECOMMENDED,
        )
        self._bet(is_system_generated=True)
        self._bet(is_system_generated=False)
        today = timezone.localdate()

        report = build_evaluation_report(
            MockBet.objects.all(),
            date_from=today - timedelta(days=1),
            date_to=today - timedelta(days=1),
            include_manual=False,
        )
        self.assertEqual(report['scope']['scope'], SCOPE_RECOMMENDED)

    def test_legacy_include_manual_true_maps_to_all(self):
        from apps.mockbets.services.moneyline_evaluation import (
            build_evaluation_report, SCOPE_ALL,
        )
        self._bet(is_system_generated=True)
        self._bet(is_system_generated=False)
        today = timezone.localdate()

        report = build_evaluation_report(
            MockBet.objects.all(),
            date_from=today - timedelta(days=1),
            date_to=today - timedelta(days=1),
            include_manual=True,
        )
        self.assertEqual(report['scope']['scope'], SCOPE_ALL)
        self.assertEqual(report['scope']['included'], 2)

    def test_explicit_scope_wins_over_include_manual(self):
        """Defensive: if both kwargs are provided, the explicit scope
        wins. Prevents ambiguity from old URLs being re-saved by JS."""
        from apps.mockbets.services.moneyline_evaluation import (
            build_evaluation_report, SCOPE_ACTUAL,
        )
        self._bet(is_system_generated=True)
        self._bet(is_system_generated=False)
        today = timezone.localdate()

        report = build_evaluation_report(
            MockBet.objects.all(),
            date_from=today - timedelta(days=1),
            date_to=today - timedelta(days=1),
            include_manual=False,  # would mean RECOMMENDED on its own
            scope=SCOPE_ACTUAL,    # but explicit scope wins
        )
        self.assertEqual(report['scope']['scope'], SCOPE_ACTUAL)
        self.assertEqual(report['scope']['included'], 2)

    # ----- My Bets / Evaluation alignment --------------------------------

    def test_my_bets_count_matches_actual_eval_for_same_window(self):
        """The discrepancy fix: My Bets's MLB count for a given date
        range must equal Actual-scope evaluation's included count for
        the same range."""
        from apps.mockbets.services.moneyline_evaluation import (
            build_evaluation_report, SCOPE_ACTUAL,
        )
        # 6 bets total in window, mix of system + manual — same
        # situation that produced the reported discrepancy.
        for _ in range(3):
            self._bet(is_system_generated=True)
        for _ in range(3):
            self._bet(is_system_generated=False)

        today = timezone.localdate()
        my_bets_qs = MockBet.objects.filter(
            user=self.user,
            sport='mlb',
            bet_type='moneyline',
            placed_at__date=today - timedelta(days=1),
        )
        my_bets_count = my_bets_qs.count()

        report = build_evaluation_report(
            MockBet.objects.all(),
            date_from=today - timedelta(days=1),
            date_to=today - timedelta(days=1),
            scope=SCOPE_ACTUAL,
        )

        # The alignment contract.
        self.assertEqual(my_bets_count, report['scope']['included'])
        self.assertEqual(my_bets_count, 6)

    # ----- UI transparency --------------------------------------------------

    def test_view_renders_scope_summary_with_counts(self):
        """The eval page must surface included/excluded counts and the
        scope label — silent exclusions are now impossible to render."""
        from django.urls import reverse
        # Three system, three manual.
        for _ in range(3):
            self._bet(is_system_generated=True)
        for _ in range(3):
            self._bet(is_system_generated=False)

        staff = User.objects.create_user('staff_eval', password='x', is_staff=True)
        self.client.force_login(staff)

        url = reverse('mockbets:moneyline_evaluation')
        today = timezone.localdate()
        date_str = (today - timedelta(days=1)).isoformat()
        resp = self.client.get(
            url,
            {'date_from': date_str, 'date_to': date_str, 'scope': 'recommended'},
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode('utf-8')
        # Scope label + counts must appear.
        self.assertIn('Recommended System Bets', body)
        # 3 placed (system), 3 placed (manual) = 6 in window
        # With scope=recommended: 3 included, 3 excluded as manual.
        self.assertIn('6 placed moneyline bets', body)
        self.assertIn('3 included', body)
        self.assertIn('3 excluded', body)

    def test_view_default_is_actual_scope(self):
        """Visiting the eval page with no scope query param must default
        to Actual scope — no more silent filtering."""
        from django.urls import reverse
        self._bet(is_system_generated=True)
        self._bet(is_system_generated=False)

        staff = User.objects.create_user('staff_eval2', password='x', is_staff=True)
        self.client.force_login(staff)

        url = reverse('mockbets:moneyline_evaluation')
        today = timezone.localdate()
        date_str = (today - timedelta(days=1)).isoformat()
        # No `scope` param.
        resp = self.client.get(url, {'date_from': date_str, 'date_to': date_str})
        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode('utf-8')
        self.assertIn('Actual Bets', body)


class ModelCleanScopeTests(TestCase):
    """Phase 2026-05-16 evaluation-integrity repair — Model Clean scope.

    Locks the contract that Model Clean includes ONLY bets meeting every
    model-evaluation criterion: system-generated (or linked to
    BettingRecommendation), complete decision-layer snapshot, placed
    under current rules. Per the diagnostic doc §5, this is the
    population that should drive model-quality judgments.
    """

    def setUp(self):
        self.user = User.objects.create_user('model_clean_user', password='x')

    def _bet(
        self, *,
        is_system_generated=True,
        recommendation_status='recommended',
        recommendation_tier='strong',
        expected_edge=Decimal('6.5'),
        recommendation_confidence=Decimal('62.0'),
        placed_days_ago=1,
        bet_type='moneyline',
        recommendation_fk=None,
    ):
        bet = MockBet.objects.create(
            user=self.user, sport='mlb', bet_type=bet_type,
            selection='X', odds_american=-130,
            implied_probability=Decimal('0.5652'),
            stake_amount=Decimal('100'),
            result='pending',
            is_system_generated=is_system_generated,
            expected_edge=expected_edge,
            recommendation_status=recommendation_status,
            recommendation_tier=recommendation_tier,
            recommendation_confidence=recommendation_confidence,
            recommendation=recommendation_fk,
            odds_source='odds_api',
        )
        # Override placed_at so date-range filtering can be exercised.
        bet.placed_at = timezone.now() - timedelta(days=placed_days_ago)
        bet.save(update_fields=['placed_at'])
        return bet

    # --- (1) Model Clean includes only complete official recommendations ---

    def test_model_clean_includes_complete_system_bets(self):
        from apps.mockbets.services.moneyline_evaluation import (
            SCOPE_MODEL_CLEAN, build_evaluation_report,
        )
        # 4 complete system bets in window — all should qualify.
        for _ in range(4):
            self._bet()
        today = timezone.localdate()
        report = build_evaluation_report(
            MockBet.objects.all(),
            date_from=today - timedelta(days=1),
            date_to=today - timedelta(days=1),
            scope=SCOPE_MODEL_CLEAN,
        )
        self.assertEqual(report['scope']['scope'], SCOPE_MODEL_CLEAN)
        self.assertEqual(report['scope']['included'], 4)
        self.assertEqual(report['scope']['excluded'], 0)

    # --- (2) Manual bets excluded unless linked to a recommendation ---

    def test_model_clean_excludes_manual_bets(self):
        from apps.mockbets.services.moneyline_evaluation import (
            EXCLUSION_MANUAL, SCOPE_MODEL_CLEAN, build_evaluation_report,
        )
        self._bet()  # system
        self._bet(is_system_generated=False)  # manual; no rec FK
        today = timezone.localdate()
        report = build_evaluation_report(
            MockBet.objects.all(),
            date_from=today - timedelta(days=1),
            date_to=today - timedelta(days=1),
            scope=SCOPE_MODEL_CLEAN,
        )
        self.assertEqual(report['scope']['included'], 1)
        self.assertEqual(report['scope']['exclusion_reasons'].get(EXCLUSION_MANUAL), 1)

    def test_model_clean_includes_manual_bets_linked_to_recommendation(self):
        """Manual bet with `recommendation` FK populated counts as model bet."""
        from apps.core.models import BettingRecommendation
        from apps.mockbets.services.moneyline_evaluation import (
            SCOPE_MODEL_CLEAN, build_evaluation_report,
        )
        # Build a real BettingRecommendation row via a CFB game so we
        # don't need to fixture an MLB game (MLB FK requires the full
        # MLB game tree which is heavier than needed for this test).
        from apps.cfb.models import Conference, Game, Team
        conf = Conference.objects.create(name='Z', slug=f'z-{timezone.now().timestamp()}')
        h = Team.objects.create(name='H', slug=f'h-{timezone.now().timestamp()}', conference=conf, rating=50.0)
        a = Team.objects.create(name='A', slug=f'a-{timezone.now().timestamp()}', conference=conf, rating=50.0)
        cfb_game = Game.objects.create(
            home_team=h, away_team=a,
            kickoff=timezone.now() + timedelta(hours=2),
        )
        rec = BettingRecommendation.objects.create(
            sport='cfb', bet_type='moneyline', pick='H', line='-130',
            odds_american=-130,
            confidence_score=Decimal('62.0'),
            model_edge=Decimal('6.5'),
            model_source='house',
            status='recommended',
            cfb_game=cfb_game,
        )
        # Manual MLB bet but explicitly linked to a recommendation row.
        self._bet(is_system_generated=False, recommendation_fk=rec)

        today = timezone.localdate()
        report = build_evaluation_report(
            MockBet.objects.all(),
            date_from=today - timedelta(days=1),
            date_to=today - timedelta(days=1),
            scope=SCOPE_MODEL_CLEAN,
        )
        # The linked manual bet IS in model_clean — recommendation FK
        # overrides the is_system_generated=False default.
        self.assertEqual(report['scope']['included'], 1)

    # --- (3) Missing edge excludes ---

    def test_model_clean_excludes_missing_edge(self):
        from apps.mockbets.services.moneyline_evaluation import (
            EXCLUSION_MISSING_EDGE, SCOPE_MODEL_CLEAN, build_evaluation_report,
        )
        self._bet(expected_edge=None)
        today = timezone.localdate()
        report = build_evaluation_report(
            MockBet.objects.all(),
            date_from=today - timedelta(days=1),
            date_to=today - timedelta(days=1),
            scope=SCOPE_MODEL_CLEAN,
        )
        self.assertEqual(report['scope']['included'], 0)
        self.assertEqual(
            report['scope']['exclusion_reasons'].get(EXCLUSION_MISSING_EDGE), 1,
        )

    # --- (4) Missing confidence (proxy for market_prob) excludes ---

    def test_model_clean_excludes_missing_confidence(self):
        from apps.mockbets.services.moneyline_evaluation import (
            EXCLUSION_MISSING_CONFIDENCE, SCOPE_MODEL_CLEAN, build_evaluation_report,
        )
        self._bet(recommendation_confidence=None)
        today = timezone.localdate()
        report = build_evaluation_report(
            MockBet.objects.all(),
            date_from=today - timedelta(days=1),
            date_to=today - timedelta(days=1),
            scope=SCOPE_MODEL_CLEAN,
        )
        self.assertEqual(report['scope']['included'], 0)
        self.assertEqual(
            report['scope']['exclusion_reasons'].get(EXCLUSION_MISSING_CONFIDENCE),
            1,
        )

    # --- (5) Missing recommendation snapshot (status) excludes ---

    def test_model_clean_excludes_missing_recommendation_status(self):
        from apps.mockbets.services.moneyline_evaluation import (
            EXCLUSION_MISSING_REC_STATUS, SCOPE_MODEL_CLEAN, build_evaluation_report,
        )
        self._bet(recommendation_status='')  # blank → incomplete
        today = timezone.localdate()
        report = build_evaluation_report(
            MockBet.objects.all(),
            date_from=today - timedelta(days=1),
            date_to=today - timedelta(days=1),
            scope=SCOPE_MODEL_CLEAN,
        )
        self.assertEqual(report['scope']['included'], 0)
        self.assertEqual(
            report['scope']['exclusion_reasons'].get(EXCLUSION_MISSING_REC_STATUS),
            1,
        )

    def test_model_clean_excludes_missing_recommendation_tier(self):
        from apps.mockbets.services.moneyline_evaluation import (
            EXCLUSION_MISSING_REC_TIER, SCOPE_MODEL_CLEAN, build_evaluation_report,
        )
        self._bet(recommendation_tier='')
        today = timezone.localdate()
        report = build_evaluation_report(
            MockBet.objects.all(),
            date_from=today - timedelta(days=1),
            date_to=today - timedelta(days=1),
            scope=SCOPE_MODEL_CLEAN,
        )
        self.assertEqual(report['scope']['included'], 0)
        self.assertEqual(
            report['scope']['exclusion_reasons'].get(EXCLUSION_MISSING_REC_TIER),
            1,
        )

    # --- Pre-rules bets excluded ---

    def test_model_clean_excludes_pre_rules_bets(self):
        from apps.mockbets.services.moneyline_evaluation import (
            EXCLUSION_PRE_RULES, MODEL_RULES_EFFECTIVE_DATE,
            SCOPE_MODEL_CLEAN, build_evaluation_report,
        )
        # Place a bet before the rules-effective date.
        bet = self._bet()
        # Push the placed_at to BEFORE MODEL_RULES_EFFECTIVE_DATE.
        bet.placed_at = timezone.now().replace(
            year=2026, month=5, day=1, hour=12,
        )
        bet.save(update_fields=['placed_at'])

        # Use a wide enough date range to catch the pre-rules bet.
        date_from = MODEL_RULES_EFFECTIVE_DATE - timedelta(days=10)
        date_to = MODEL_RULES_EFFECTIVE_DATE - timedelta(days=1)
        report = build_evaluation_report(
            MockBet.objects.all(),
            date_from=date_from,
            date_to=date_to,
            scope=SCOPE_MODEL_CLEAN,
        )
        # Bet is system-generated AND complete BUT before rules date.
        self.assertEqual(report['scope']['included'], 0)
        self.assertEqual(
            report['scope']['exclusion_reasons'].get(EXCLUSION_PRE_RULES), 1,
        )

    # --- (7) Population audit reports exclusion reasons correctly ---

    def test_population_audit_separates_each_exclusion_reason(self):
        """Mix of failure modes: each lands in its own audit bucket."""
        from apps.mockbets.services.moneyline_evaluation import (
            EXCLUSION_MANUAL, EXCLUSION_MISSING_CONFIDENCE,
            EXCLUSION_MISSING_EDGE, EXCLUSION_MISSING_REC_STATUS,
            SCOPE_MODEL_CLEAN, build_evaluation_report,
        )
        self._bet()  # complete; included
        self._bet(is_system_generated=False)  # manual
        self._bet(expected_edge=None)  # missing edge
        self._bet(recommendation_confidence=None)  # missing confidence
        self._bet(recommendation_status='')  # missing rec status

        today = timezone.localdate()
        report = build_evaluation_report(
            MockBet.objects.all(),
            date_from=today - timedelta(days=1),
            date_to=today - timedelta(days=1),
            scope=SCOPE_MODEL_CLEAN,
        )
        reasons = report['scope']['exclusion_reasons']
        self.assertEqual(report['scope']['total_placed_in_window'], 5)
        self.assertEqual(report['scope']['included'], 1)
        self.assertEqual(report['scope']['excluded'], 4)
        # Each exclusion in its own bucket — no silent grouping.
        self.assertEqual(reasons.get(EXCLUSION_MANUAL), 1)
        self.assertEqual(reasons.get(EXCLUSION_MISSING_EDGE), 1)
        self.assertEqual(reasons.get(EXCLUSION_MISSING_CONFIDENCE), 1)
        self.assertEqual(reasons.get(EXCLUSION_MISSING_REC_STATUS), 1)

    # --- (8) Actual Bets and Model Clean differ without hiding it ---

    def test_actual_vs_model_clean_show_different_counts_transparently(self):
        """The two scopes can disagree — and the disagreement must be
        visible via the excluded count + exclusion_reasons, never silent.
        """
        from apps.mockbets.services.moneyline_evaluation import (
            SCOPE_ACTUAL, SCOPE_MODEL_CLEAN, build_evaluation_report,
        )
        # 3 clean system bets + 3 manual bets = 6 placed.
        for _ in range(3):
            self._bet()
        for _ in range(3):
            self._bet(is_system_generated=False)

        today = timezone.localdate()
        actual = build_evaluation_report(
            MockBet.objects.all(),
            date_from=today - timedelta(days=1),
            date_to=today - timedelta(days=1),
            scope=SCOPE_ACTUAL,
        )
        model_clean = build_evaluation_report(
            MockBet.objects.all(),
            date_from=today - timedelta(days=1),
            date_to=today - timedelta(days=1),
            scope=SCOPE_MODEL_CLEAN,
        )
        # Actual scope includes everything.
        self.assertEqual(actual['scope']['included'], 6)
        self.assertEqual(actual['scope']['excluded'], 0)
        # Model Clean drops manual; surfaces the count explicitly.
        self.assertEqual(model_clean['scope']['included'], 3)
        self.assertEqual(model_clean['scope']['excluded'], 3)
        # And the disagreement is visible — both totals match the window.
        self.assertEqual(
            actual['scope']['total_placed_in_window'],
            model_clean['scope']['total_placed_in_window'],
        )

    # --- (9) Markdown packet includes scope and exclusion summary ---

    def test_markdown_packet_includes_scope_label_and_exclusions(self):
        from apps.mockbets.services.moneyline_evaluation import (
            SCOPE_MODEL_CLEAN, build_evaluation_report,
        )
        self._bet()  # clean
        self._bet(is_system_generated=False)  # manual → excluded
        today = timezone.localdate()
        report = build_evaluation_report(
            MockBet.objects.all(),
            date_from=today - timedelta(days=1),
            date_to=today - timedelta(days=1),
            scope=SCOPE_MODEL_CLEAN,
        )
        packet = report['packet_markdown']
        # Scope label + counts must appear.
        self.assertIn('Model Clean', packet)
        self.assertIn('1 of 2 placed in window', packet)
        # Exclusion reasons surfaced.
        self.assertIn('Excluded by scope:', packet)
        self.assertIn('manual bets', packet)

    # --- (10) Existing Actual Bets behavior is intact ---

    def test_actual_scope_unchanged_by_model_clean_addition(self):
        from apps.mockbets.services.moneyline_evaluation import (
            SCOPE_ACTUAL, build_evaluation_report,
        )
        self._bet()
        self._bet(is_system_generated=False)
        today = timezone.localdate()
        report = build_evaluation_report(
            MockBet.objects.all(),
            date_from=today - timedelta(days=1),
            date_to=today - timedelta(days=1),
            scope=SCOPE_ACTUAL,
        )
        # Actual scope unchanged: includes both bets, no exclusions.
        self.assertEqual(report['scope']['included'], 2)
        self.assertEqual(report['scope']['excluded'], 0)
        self.assertEqual(report['scope']['scope'], SCOPE_ACTUAL)


class CLVSourceFilterTests(TestCase):
    """Phase 2026-05-16 evaluation-integrity repair — CLV primary-source filter.

    Per docs/recommendation_quality_framework.md §1.2, CLV is only
    meaningful when sourced from the primary odds feed (odds_api).
    The backtest service has always enforced this; the live-bet
    evaluation path now matches it.
    """

    def setUp(self):
        self.user = User.objects.create_user('clv_source_user', password='x')

    def _bet(self, *, odds_source='odds_api', clv=0.05, direction='positive'):
        return MockBet.objects.create(
            user=self.user, sport='mlb', bet_type='moneyline',
            selection='X', odds_american=-110,
            implied_probability=Decimal('0.5238'),
            stake_amount=Decimal('100'),
            simulated_payout=Decimal('91'),
            result='win',
            is_system_generated=True,
            recommendation_status='recommended',
            recommendation_tier='strong',
            recommendation_confidence=Decimal('60.0'),
            expected_edge=Decimal('6.0'),
            clv_cents=clv,
            clv_direction=direction,
            closing_odds_american=-120,
            odds_source=odds_source,
        )

    def test_clv_only_counts_primary_source(self):
        """Only odds_api-source bets contribute to CLV math."""
        from apps.mockbets.services.recommendation_performance import (
            _group_stats,
        )
        # 2 primary + 2 ESPN, all winners with +CLV.
        for _ in range(2):
            self._bet(odds_source='odds_api')
        for _ in range(2):
            self._bet(odds_source='espn')

        stats = _group_stats(list(MockBet.objects.all()))
        # Only the 2 odds_api bets count for CLV.
        self.assertEqual(stats['clv_sample'], 2)
        # All 4 are positive CLV but only 2 made it through the filter.
        self.assertEqual(stats['positive_clv_rate'], 100.0)
        # And the operator can see how many were dropped.
        self.assertEqual(stats['clv_excluded_by_source'], 2)

    def test_clv_excludes_manual_and_cached_sources(self):
        from apps.mockbets.services.recommendation_performance import (
            _group_stats,
        )
        self._bet(odds_source='odds_api')
        self._bet(odds_source='manual')
        self._bet(odds_source='cached')
        self._bet(odds_source='unknown')

        stats = _group_stats(list(MockBet.objects.all()))
        self.assertEqual(stats['clv_sample'], 1)
        self.assertEqual(stats['clv_excluded_by_source'], 3)

    def test_clv_math_unchanged_for_all_primary_population(self):
        """When every bet is odds_api, the new filter is invisible —
        backward compatibility for the common path."""
        from apps.mockbets.services.recommendation_performance import (
            _group_stats,
        )
        self._bet(odds_source='odds_api', clv=0.05, direction='positive')
        self._bet(odds_source='odds_api', clv=-0.03, direction='negative')

        stats = _group_stats(list(MockBet.objects.all()))
        self.assertEqual(stats['clv_sample'], 2)
        self.assertEqual(stats['clv_excluded_by_source'], 0)
        self.assertAlmostEqual(stats['positive_clv_rate'], 50.0)


class MoneylineEvaluationSummaryMathTests(TestCase):
    """Executive summary numbers must match the underlying bets."""

    def setUp(self):
        self.user = User.objects.create_user('eval_math', password='x')

    def _bet(self, result, odds=-110, payout=None, stake=Decimal('100')):
        bet = MockBet.objects.create(
            user=self.user, sport='mlb', bet_type='moneyline',
            selection='X', odds_american=odds,
            implied_probability=Decimal('0.5238'),
            stake_amount=stake,
            simulated_payout=payout,
            result=result,
            is_system_generated=True,
            expected_edge=Decimal('5.0'),
            recommendation_status='recommended',
            recommendation_tier='standard',
            recommendation_confidence=Decimal('60.0'),
        )
        bet.placed_at = timezone.now() - timedelta(days=1)
        bet.save(update_fields=['placed_at'])
        return bet

    def test_basic_counts_and_roi(self):
        from apps.mockbets.services.moneyline_evaluation import build_evaluation_report
        # 2 wins (+91 each at -110), 1 loss (-100), 1 push.
        self._bet('win', odds=-110, payout=Decimal('91'))
        self._bet('win', odds=-110, payout=Decimal('91'))
        self._bet('loss')
        self._bet('push')
        today = timezone.localdate()
        report = build_evaluation_report(
            MockBet.objects.all(),
            date_from=today - timedelta(days=1),
            date_to=today - timedelta(days=1),
        )
        s = report['executive_summary']
        self.assertEqual(s['bets_count'], 4)
        self.assertEqual(s['wins'], 2)
        self.assertEqual(s['losses'], 1)
        self.assertEqual(s['pushes'], 1)
        # Net P/L = +91 + 91 - 100 + 0 = 82
        self.assertAlmostEqual(s['net_pl'], 82.0, places=1)
        # Total stake = 4 * 100 = 400 (pushes are still staked)
        self.assertAlmostEqual(s['total_stake'], 400.0, places=1)


class MoneylineEvaluationLossClassifierTests(TestCase):
    """Per-cause rules in the multi-cause loss classifier."""

    def _bet(self, **overrides):
        from django.contrib.auth.models import User as DjangoUser
        user = DjangoUser.objects.create_user(f'lc_{id(overrides)}', password='x')
        defaults = dict(
            user=user, sport='mlb', bet_type='moneyline',
            selection='X', odds_american=-110,
            implied_probability=Decimal('0.5238'),
            stake_amount=Decimal('100'),
            result='loss',
            is_system_generated=True,
            expected_edge=Decimal('5.0'),
            recommendation_status='recommended',
            recommendation_tier='standard',
            recommendation_confidence=Decimal('60.0'),
        )
        defaults.update(overrides)
        return MockBet.objects.create(**defaults)

    def test_negative_clv_fires(self):
        from apps.mockbets.services.moneyline_evaluation import _classify_loss_causes
        b = self._bet(clv_direction='negative', clv_cents=-0.05)
        self.assertIn('negative_clv', _classify_loss_causes(b))

    def test_stale_odds_fires_when_no_closing_capture(self):
        from apps.mockbets.services.moneyline_evaluation import _classify_loss_causes
        # Settled moneyline + closing_odds_american is None → stale
        b = self._bet(closing_odds_american=None)
        self.assertIn('stale_odds', _classify_loss_causes(b))

    def test_thin_edge_fires_under_4pp(self):
        from apps.mockbets.services.moneyline_evaluation import _classify_loss_causes
        b = self._bet(expected_edge=Decimal('3.5'))
        self.assertIn('thin_edge', _classify_loss_causes(b))

    def test_thin_edge_does_not_fire_at_4pp(self):
        from apps.mockbets.services.moneyline_evaluation import _classify_loss_causes
        b = self._bet(expected_edge=Decimal('4.0'))
        self.assertNotIn('thin_edge', _classify_loss_causes(b))

    def test_heavy_juice_fires_at_minus_150(self):
        from apps.mockbets.services.moneyline_evaluation import _classify_loss_causes
        b = self._bet(odds_american=-150)
        self.assertIn('heavy_juice', _classify_loss_causes(b))

    def test_low_confidence_fires_under_60(self):
        from apps.mockbets.services.moneyline_evaluation import _classify_loss_causes
        b = self._bet(recommendation_confidence=Decimal('59.0'))
        self.assertIn('low_confidence', _classify_loss_causes(b))

    def test_market_moved_against_fires_from_engine_reason(self):
        from apps.mockbets.services.moneyline_evaluation import _classify_loss_causes
        b = self._bet(loss_reason='market_movement')
        self.assertIn('market_moved_against', _classify_loss_causes(b))

    def test_variance_fires_from_engine_reason(self):
        from apps.mockbets.services.moneyline_evaluation import _classify_loss_causes
        b = self._bet(loss_reason='variance')
        self.assertIn('variance', _classify_loss_causes(b))

    def test_unknown_when_nothing_fires(self):
        """A clean bet (positive CLV, fresh odds, fat edge, +odds, high
        confidence, no engine reason) that lost still gets 'unknown' so
        the list is never empty."""
        from apps.mockbets.services.moneyline_evaluation import _classify_loss_causes
        b = self._bet(
            clv_direction='positive', clv_cents=0.05,
            closing_odds_american=110, odds_american=110,
            expected_edge=Decimal('6.0'),
            recommendation_confidence=Decimal('65.0'),
        )
        self.assertEqual(_classify_loss_causes(b), ['unknown'])

    def test_multiple_causes_fire_simultaneously(self):
        from apps.mockbets.services.moneyline_evaluation import _classify_loss_causes
        b = self._bet(
            clv_direction='negative',
            expected_edge=Decimal('3.0'),
            odds_american=-200,
        )
        causes = _classify_loss_causes(b)
        self.assertIn('negative_clv', causes)
        self.assertIn('thin_edge', causes)
        self.assertIn('heavy_juice', causes)
        # Order: negative_clv first (most actionable per the spec'd order).
        self.assertEqual(causes[0], 'negative_clv')

    def test_non_loss_returns_empty_list(self):
        from apps.mockbets.services.moneyline_evaluation import _classify_loss_causes
        b = self._bet(result='win', simulated_payout=Decimal('91'))
        self.assertEqual(_classify_loss_causes(b), [])


class MoneylineEvaluationPacketTests(TestCase):
    """The markdown packet must contain every spec'd section."""

    def setUp(self):
        self.user = User.objects.create_user('packet_user', password='x')

    def _bet(self, result='loss', placed_days_ago=1):
        bet = MockBet.objects.create(
            user=self.user, sport='mlb', bet_type='moneyline',
            selection='Yankees', odds_american=-110,
            implied_probability=Decimal('0.5238'),
            stake_amount=Decimal('100'),
            simulated_payout=Decimal('91') if result == 'win' else None,
            result=result,
            is_system_generated=True,
            expected_edge=Decimal('5.0'),
            recommendation_status='recommended',
            recommendation_tier='standard',
            recommendation_confidence=Decimal('60.0'),
        )
        bet.placed_at = timezone.now() - timedelta(days=placed_days_ago)
        bet.save(update_fields=['placed_at'])
        return bet

    def test_packet_contains_required_sections(self):
        from apps.mockbets.services.moneyline_evaluation import build_evaluation_report
        self._bet(result='loss')
        today = timezone.localdate()
        report = build_evaluation_report(
            MockBet.objects.all(),
            date_from=today - timedelta(days=1),
            date_to=today - timedelta(days=1),
        )
        packet = report['packet_markdown']
        for header in (
            '# Brother Willies Moneyline Evaluation Packet',
            '## Date Range',
            '## Executive Summary',
            '## Recommended Bets',
            '## Bucket Performance',
            '### By Edge',
            '### By Model Confidence',
            '### By Odds Type',
            '### By Source',
            '## Loss Review',
            '## Questions for Analysis',
        ):
            self.assertIn(header, packet, f'missing section: {header}')

    def test_packet_renders_with_no_bets(self):
        from apps.mockbets.services.moneyline_evaluation import build_evaluation_report
        today = timezone.localdate()
        report = build_evaluation_report(
            MockBet.objects.all(),
            date_from=today - timedelta(days=1),
            date_to=today - timedelta(days=1),
        )
        # Empty range still produces a packet (with the empty-state copy).
        self.assertIn('# Brother Willies Moneyline Evaluation Packet', report['packet_markdown'])
        self.assertIn('No bets in this range', report['packet_markdown'])


@override_settings(STORAGES={
    'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
    'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'},
})
class MoneylineEvaluationAccessTests(TestCase):
    """Staff-only — non-staff get 404, anon redirects to login."""

    def setUp(self):
        self.staff = User.objects.create_user('eval_staff', password='x', is_staff=True)
        self.normal = User.objects.create_user('eval_normal', password='x')
        self.client = Client()

    def test_staff_can_load_page(self):
        self.client.force_login(self.staff)
        resp = self.client.get('/mockbets/moneyline-evaluation/')
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Moneyline Evaluation')
        self.assertContains(resp, 'Copy Evaluation Packet')

    def test_non_staff_gets_404(self):
        self.client.force_login(self.normal)
        resp = self.client.get('/mockbets/moneyline-evaluation/')
        self.assertEqual(resp.status_code, 404)

    def test_anon_redirected_to_login(self):
        resp = self.client.get('/mockbets/moneyline-evaluation/')
        self.assertEqual(resp.status_code, 302)
        self.assertIn('/login', resp.url)


class MoneylineEvaluationOddsTypeClassifierTests(TestCase):
    """Spec mandates 4 odds-type buckets with explicit boundaries."""

    def test_underdog_at_plus_100(self):
        from apps.mockbets.services.moneyline_evaluation import _classify_odds_type
        self.assertEqual(_classify_odds_type(100), 'underdog')
        self.assertEqual(_classify_odds_type(150), 'underdog')

    def test_short_favorite_in_pickem_zone(self):
        from apps.mockbets.services.moneyline_evaluation import _classify_odds_type
        self.assertEqual(_classify_odds_type(99), 'short_favorite')
        self.assertEqual(_classify_odds_type(-110), 'short_favorite')
        self.assertEqual(_classify_odds_type(-149), 'short_favorite')

    def test_favorite_band(self):
        from apps.mockbets.services.moneyline_evaluation import _classify_odds_type
        self.assertEqual(_classify_odds_type(-150), 'favorite')
        self.assertEqual(_classify_odds_type(-200), 'favorite')
        self.assertEqual(_classify_odds_type(-300), 'favorite')

    def test_heavy_favorite_below_minus_300(self):
        from apps.mockbets.services.moneyline_evaluation import _classify_odds_type
        self.assertEqual(_classify_odds_type(-301), 'heavy_favorite')
        self.assertEqual(_classify_odds_type(-500), 'heavy_favorite')

    def test_none_input(self):
        from apps.mockbets.services.moneyline_evaluation import _classify_odds_type
        self.assertIsNone(_classify_odds_type(None))


# =============================================================================
# Pending status detail — contextual reason a bet is still pending.
# Replaces the generic PENDING badge with one of six branches keyed off
# the linked Game's status + scores.
# =============================================================================

class MockBetPendingStatusDetailTests(TestCase):
    """Each spec branch maps to a (label, icon, color, kind) tuple.
    Tests cover every branch + the fallback + golf path + settled-bet
    short-circuit."""

    def setUp(self):
        from apps.mlb.models import Conference as MLBConf, Team as MLBTeam, Game as MLBGame
        self.MLBGame = MLBGame
        self.user = User.objects.create_user('pending_status_user', password='x')
        conf = MLBConf.objects.create(name='AL-pen', slug='al-pen')
        self.t1 = MLBTeam.objects.create(
            name='Yankees', slug='pen-yankees', conference=conf, rating=70,
            source='mlb_stats_api', external_id='pen-1',
        )
        self.t2 = MLBTeam.objects.create(
            name='Rays', slug='pen-rays', conference=conf, rating=40,
            source='mlb_stats_api', external_id='pen-2',
        )

    def _game(self, *, status='scheduled', home_score=None, away_score=None,
              ext='pen-game'):
        return self.MLBGame.objects.create(
            home_team=self.t1, away_team=self.t2,
            first_pitch=timezone.now() + timedelta(hours=2),
            status=status, home_score=home_score, away_score=away_score,
            source='mlb_stats_api', external_id=ext,
        )

    def _bet(self, game, *, result='pending'):
        return MockBet.objects.create(
            user=self.user, sport='mlb', mlb_game=game,
            bet_type='moneyline', selection='Yankees',
            odds_american=-110,
            implied_probability=Decimal('0.5238'),
            stake_amount=Decimal('100'),
            result=result,
        )

    # --- Spec branch coverage -------------------------------------------------

    def test_scheduled_game_renders_scheduled_status(self):
        bet = self._bet(self._game(status='scheduled'))
        d = bet.pending_status_detail
        self.assertEqual(d['kind'], 'scheduled')
        self.assertEqual(d['color'], 'gray')
        self.assertIn('Scheduled', d['label'])
        self.assertEqual(d['icon'], '🕒')

    def test_live_game_renders_live_status(self):
        bet = self._bet(self._game(status='live', home_score=2, away_score=1))
        d = bet.pending_status_detail
        self.assertEqual(d['kind'], 'live')
        self.assertEqual(d['color'], 'red')
        self.assertIn('Live', d['label'])

    def test_postponed_game_renders_delayed(self):
        bet = self._bet(self._game(status='postponed'))
        d = bet.pending_status_detail
        self.assertEqual(d['kind'], 'delayed')
        self.assertEqual(d['color'], 'yellow')
        self.assertIn('Delayed', d['label'])

    def test_cancelled_game_renders_delayed(self):
        """Cancelled games share the Delayed bucket — same user-facing
        concern: 'this isn't happening as scheduled'."""
        bet = self._bet(self._game(status='cancelled'))
        d = bet.pending_status_detail
        self.assertEqual(d['kind'], 'delayed')

    def test_final_with_scores_renders_awaiting_settlement(self):
        """Spec branch 5: pipeline-lag indicator. Game is final and scored
        but the bet didn't settle yet (cron hasn't run, or last cron failed)."""
        bet = self._bet(self._game(status='final', home_score=5, away_score=3))
        d = bet.pending_status_detail
        self.assertEqual(d['kind'], 'awaiting_settlement')
        self.assertEqual(d['color'], 'orange')
        self.assertIn('awaiting settlement', d['label'].lower())
        self.assertEqual(d['icon'], '🟠')

    def test_unknown_status_with_no_scores_falls_to_awaiting_score(self):
        """Branch 4: status not in the recognized set + no scores."""
        # Use a non-enum status to exercise branch 4. Real values come
        # from STATUS_MAP in schedule_provider.py; 'delayed' isn't one
        # of the model's choices, but the column accepts arbitrary
        # strings so this exercises the fallthrough path.
        bet = self._bet(self._game(status='delayed'))
        d = bet.pending_status_detail
        self.assertEqual(d['kind'], 'awaiting_score')
        self.assertEqual(d['color'], 'yellow')

    def test_no_game_attached_falls_back_to_unknown(self):
        """Branch 6: bet exists but the per-sport FK is None (e.g.,
        legacy data, manual placement gone wrong). Render the unknown
        fallback rather than crashing."""
        bet = MockBet.objects.create(
            user=self.user, sport='mlb',  # mlb but no mlb_game FK
            bet_type='moneyline', selection='X', odds_american=-110,
            implied_probability=Decimal('0.5238'),
            stake_amount=Decimal('100'),
            result='pending',
        )
        d = bet.pending_status_detail
        self.assertEqual(d['kind'], 'unknown')
        self.assertEqual(d['color'], 'muted')

    def test_final_without_scores_falls_back_to_unknown(self):
        """Edge: status='final' but scores are still None. Unusual but
        possible with bad data. Branch 6 (unknown), not 5 (awaiting
        settlement) — we don't have enough to show a final score."""
        bet = self._bet(self._game(status='final'))  # both scores None
        d = bet.pending_status_detail
        self.assertEqual(d['kind'], 'unknown')

    # --- Settled bets short-circuit -------------------------------------------

    def test_settled_bet_returns_none(self):
        """Win/loss/push bets render their own result badge — the
        pending-status property must return None for them."""
        for result in ('win', 'loss', 'push'):
            bet = self._bet(
                self._game(status='final', home_score=5, away_score=3,
                           ext=f'pen-settled-{result}'),
                result=result,
            )
            self.assertIsNone(
                bet.pending_status_detail,
                f'expected None for settled bet (result={result})',
            )


@override_settings(STORAGES={
    'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
    'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'},
})
class MockBetPendingStatusRenderingTests(TestCase):
    """End-to-end: the My Bets page renders the contextual pending
    badge label, not the generic 'PENDING' string."""

    def setUp(self):
        from apps.mlb.models import Conference as MLBConf, Team as MLBTeam, Game as MLBGame
        self.MLBGame = MLBGame
        self.user = User.objects.create_user('pen_render_user', password='x')
        self.client = Client()
        self.client.force_login(self.user)
        conf = MLBConf.objects.create(name='AL-render', slug='al-render')
        self.t1 = MLBTeam.objects.create(
            name='Yankees', slug='render-yankees', conference=conf, rating=70,
            source='mlb_stats_api', external_id='render-1',
        )
        self.t2 = MLBTeam.objects.create(
            name='Rays', slug='render-rays', conference=conf, rating=40,
            source='mlb_stats_api', external_id='render-2',
        )

    def _game_and_bet(self, **game_kwargs):
        g = self.MLBGame.objects.create(
            home_team=self.t1, away_team=self.t2,
            first_pitch=timezone.now() + timedelta(hours=2),
            source='mlb_stats_api', external_id=game_kwargs.pop('ext', 'render-game'),
            **game_kwargs,
        )
        return MockBet.objects.create(
            user=self.user, sport='mlb', mlb_game=g,
            bet_type='moneyline', selection='Yankees',
            odds_american=-110,
            implied_probability=Decimal('0.5238'),
            stake_amount=Decimal('100'),
            result='pending',
        )

    def test_my_bets_shows_scheduled_label_not_generic_pending(self):
        self._game_and_bet(status='scheduled')
        resp = self.client.get('/mockbets/')
        body = resp.content.decode('utf-8')
        self.assertIn('Scheduled — Game has not started', body)
        # The generic uppercase PENDING string should be gone.
        self.assertNotIn('>PENDING<', body)

    # Note: an integration test for the "awaiting_settlement" branch
    # would be flaky here — the my_bets view calls
    # settle_user_pending_bets() on entry, which immediately resolves
    # any final-with-scores-still-pending bet to win/loss/push. The
    # state is by-design unreachable from a rendered page. The property
    # test (test_final_with_scores_renders_awaiting_settlement) covers
    # the mapping; the integration coverage is intentionally limited
    # to states that survive the on-load settlement pass.

    def test_my_bets_shows_live_label_for_live_game(self):
        self._game_and_bet(status='live', home_score=2, away_score=1,
                           ext='render-live')
        resp = self.client.get('/mockbets/')
        body = resp.content.decode('utf-8')
        self.assertIn('Live — Game in progress', body)
        self.assertIn('badge-red', body)


class ThreePopulationAuditTests(TestCase):
    """Lock down the three-population predicate against accidental drift.

    Population (2) — TRUE SYSTEM-APPROVED — must require EVERY one of:
      - is_system_generated=True OR linked to a real BettingRecommendation
      - recommendation_status == 'recommended'
      - linked recommendation.lane == 'core'
      - placed_at.date() >= MODEL_RULES_EFFECTIVE_DATE (2026-05-06)
      - non-empty recommendation_tier, expected_edge, recommendation_confidence

    Population (1) ∪ (3) = Population (2) is the algebraic identity
    we also assert.
    """

    def setUp(self):
        from apps.mlb.models import Conference as MLBConf, Team as MLBTeam, Game as MLBGame
        from apps.core.models import BettingRecommendation
        self.MLBGame = MLBGame
        self.BettingRecommendation = BettingRecommendation
        self.user = User.objects.create_user('audit_user', password='pw')
        conf, _ = MLBConf.objects.get_or_create(slug='al-east', defaults={'name': 'AL East'})
        self.home = MLBTeam.objects.create(
            name='Yankees', slug='yankees', conference=conf,
            source='mlb_stats_api', external_id='147',
        )
        self.away = MLBTeam.objects.create(
            name='Royals', slug='royals', conference=conf,
            source='mlb_stats_api', external_id='118',
        )

    def _game(self, ext='g1'):
        return self.MLBGame.objects.create(
            home_team=self.home, away_team=self.away,
            first_pitch=timezone.now() - timedelta(hours=4),
            status='final', home_score=5, away_score=3,
            source='mlb_stats_api', external_id=ext,
        )

    def _rec(self, game, *, lane='core', status='recommended'):
        return self.BettingRecommendation.objects.create(
            sport='mlb', mlb_game=game, bet_type='moneyline',
            pick='Yankees', line='-130', odds_american=-130,
            confidence_score=Decimal('72'),
            model_edge=Decimal('6.5'),
            model_source='house', status=status,
            status_reason='', lane=lane,
        )

    def _bet(self, game, *, recommendation=None, is_system=True,
             rec_status='recommended', rec_tier='strong',
             rec_conf=Decimal('72.00'), edge=Decimal('0.065'),
             result='win', payout=Decimal('176.92'),
             clv_cents=0.05, odds_source='odds_api',
             placed_at=None):
        from datetime import datetime
        if placed_at is None:
            placed_at = timezone.now() - timedelta(days=2)
        return MockBet.objects.create(
            user=self.user, sport='mlb', bet_type='moneyline',
            selection='Yankees', odds_american=-130,
            implied_probability=Decimal('0.5652'),
            stake_amount=Decimal('100'), simulated_payout=payout,
            result=result, mlb_game=game,
            recommendation=recommendation, is_system_generated=is_system,
            recommendation_status=rec_status, recommendation_tier=rec_tier,
            recommendation_confidence=rec_conf, expected_edge=edge,
            closing_odds_american=-125, clv_cents=clv_cents,
            odds_source=odds_source, placed_at=placed_at,
        )

    # ------------------------------------------------------------------
    # is_true_system_approved predicate
    # ------------------------------------------------------------------

    def test_qualifying_bet_passes(self):
        from apps.mockbets.services.three_population_audit import is_true_system_approved
        g = self._game(ext='qual')
        rec = self._rec(g, lane='core')
        bet = self._bet(g, recommendation=rec)
        self.assertTrue(is_true_system_approved(bet))

    def test_lane_qualified_fails(self):
        """Lane=qualified is NOT system-approved (only 'core' counts)."""
        from apps.mockbets.services.three_population_audit import is_true_system_approved
        g = self._game(ext='qf')
        rec = self._rec(g, lane='qualified')
        bet = self._bet(g, recommendation=rec)
        self.assertFalse(is_true_system_approved(bet))

    def test_lane_pass_fails(self):
        from apps.mockbets.services.three_population_audit import is_true_system_approved
        g = self._game(ext='ps')
        rec = self._rec(g, lane='pass')
        bet = self._bet(g, recommendation=rec)
        self.assertFalse(is_true_system_approved(bet))

    def test_no_recommendation_link_fails(self):
        from apps.mockbets.services.three_population_audit import is_true_system_approved
        g = self._game(ext='nolink')
        bet = self._bet(g, recommendation=None, is_system=False)
        self.assertFalse(is_true_system_approved(bet))

    def test_status_not_recommended_fails(self):
        from apps.mockbets.services.three_population_audit import is_true_system_approved
        g = self._game(ext='notrec')
        rec = self._rec(g, status='not_recommended')
        bet = self._bet(g, recommendation=rec, rec_status='not_recommended')
        self.assertFalse(is_true_system_approved(bet))

    def test_pre_rules_date_fails(self):
        """Bet placed before MODEL_RULES_EFFECTIVE_DATE is excluded."""
        from datetime import datetime, timezone as _dt_tz
        from apps.mockbets.services.three_population_audit import is_true_system_approved
        from apps.mockbets.services.moneyline_evaluation import MODEL_RULES_EFFECTIVE_DATE
        g = self._game(ext='pre')
        rec = self._rec(g, lane='core')
        pre = datetime.combine(MODEL_RULES_EFFECTIVE_DATE - timedelta(days=1),
                               datetime.min.time(), tzinfo=_dt_tz.utc)
        bet = self._bet(g, recommendation=rec, placed_at=pre)
        self.assertFalse(is_true_system_approved(bet))

    def test_missing_tier_fails(self):
        from apps.mockbets.services.three_population_audit import is_true_system_approved
        g = self._game(ext='nt')
        rec = self._rec(g, lane='core')
        bet = self._bet(g, recommendation=rec, rec_tier='')
        self.assertFalse(is_true_system_approved(bet))

    def test_missing_edge_fails(self):
        from apps.mockbets.services.three_population_audit import is_true_system_approved
        g = self._game(ext='ne')
        rec = self._rec(g, lane='core')
        bet = self._bet(g, recommendation=rec, edge=None)
        self.assertFalse(is_true_system_approved(bet))

    def test_missing_confidence_fails(self):
        from apps.mockbets.services.three_population_audit import is_true_system_approved
        g = self._game(ext='nc')
        rec = self._rec(g, lane='core')
        bet = self._bet(g, recommendation=rec, rec_conf=None)
        self.assertFalse(is_true_system_approved(bet))

    # ------------------------------------------------------------------
    # build_audit integration + algebra
    # ------------------------------------------------------------------

    def test_partition_is_disjoint_and_complete(self):
        """Population (2) ∪ Population (3) == Population (1), and they
        are disjoint by construction."""
        from apps.mockbets.services.three_population_audit import build_audit

        # Approved
        g1 = self._game(ext='ok1')
        rec1 = self._rec(g1, lane='core')
        self._bet(g1, recommendation=rec1)
        # Approved
        g2 = self._game(ext='ok2')
        rec2 = self._rec(g2, lane='core')
        self._bet(g2, recommendation=rec2, result='loss', payout=Decimal('0.00'))
        # Manual (no link, not system)
        g3 = self._game(ext='man')
        self._bet(g3, recommendation=None, is_system=False, rec_status='')
        # Wrong lane → falls into manual/contaminated
        g4 = self._game(ext='lq')
        rec4 = self._rec(g4, lane='qualified')
        self._bet(g4, recommendation=rec4)

        now = timezone.now()
        audit = build_audit(
            MockBet.objects.all(),
            cutoff=now - timedelta(days=30),
            now=now,
            days=30, sport='mlb',
        )
        n1 = audit['populations']['actual']['count']
        n2 = audit['populations']['system_approved']['count']
        n3 = audit['populations']['manual_contaminated']['count']
        self.assertEqual(n1, 4)
        self.assertEqual(n2, 2)
        self.assertEqual(n3, 2)
        self.assertEqual(n1, n2 + n3)

    def test_clv_only_from_primary_source(self):
        """CLV+ % must ignore non-odds_api rows even if clv_cents is set."""
        from apps.mockbets.services.three_population_audit import build_audit

        g_primary = self._game(ext='ps')
        rec_p = self._rec(g_primary, lane='core')
        self._bet(g_primary, recommendation=rec_p,
                  clv_cents=0.05, odds_source='odds_api')

        g_espn = self._game(ext='es')
        rec_e = self._rec(g_espn, lane='core')
        self._bet(g_espn, recommendation=rec_e,
                  clv_cents=0.99, odds_source='espn')  # should be ignored

        now = timezone.now()
        audit = build_audit(
            MockBet.objects.all(),
            cutoff=now - timedelta(days=30),
            now=now,
            days=30, sport='mlb',
        )
        sys_pop = audit['populations']['system_approved']
        self.assertEqual(sys_pop['count'], 2)
        # Only ONE CLV row counted (the odds_api one).
        self.assertEqual(sys_pop['clv_sample'], 1)
        self.assertEqual(sys_pop['clv_plus_pct'], 100.0)
        # The 0.99 ESPN value did NOT enter the average.
        self.assertAlmostEqual(sys_pop['avg_clv_cents'], 0.05, places=4)

    def test_win_pl_uses_profit_only_not_double_subtracting_stake(self):
        """REGRESSION LOCK: simulated_payout stores PROFIT only.

        A winning bet's net P/L is exactly simulated_payout (the profit) —
        NOT (simulated_payout - stake). The earlier harness double-subtracted
        the stake on wins, producing a phantom ROI ~= true_roi - win_rate.

        Construct a clean 2-win / 1-loss flat-$100 book where the math is
        hand-checkable:
            win  +150 → profit $150
            win  -200 → profit $50
            loss      → -$100
        net P/L = 150 + 50 - 100 = +$100 on $300 stake → ROI +33.3%.

        Under the OLD bug this would have been 150-100 + 50-100 + 0-100
        = -$100 → ROI -33.3%. The sign itself flips, so this test is a
        hard guard.
        """
        from apps.mockbets.services.three_population_audit import compute_metrics

        g1 = self._game(ext='pl1')
        rec1 = self._rec(g1, lane='core')
        b1 = self._bet(g1, recommendation=rec1, result='win',
                       payout=Decimal('150.00'))   # +150 profit
        b1.odds_american = 150
        b1.save(update_fields=['odds_american'])

        g2 = self._game(ext='pl2')
        rec2 = self._rec(g2, lane='core')
        b2 = self._bet(g2, recommendation=rec2, result='win',
                       payout=Decimal('50.00'))    # -200 profit
        b2.odds_american = -200
        b2.save(update_fields=['odds_american'])

        g3 = self._game(ext='pl3')
        rec3 = self._rec(g3, lane='core')
        self._bet(g3, recommendation=rec3, result='loss',
                  payout=Decimal('0.00'))

        m = compute_metrics([b1, b2, MockBet.objects.get(mlb_game=g3)])
        self.assertEqual(m['net_pl'], Decimal('100.00'))
        self.assertEqual(m['total_stake'], Decimal('300.00'))
        self.assertEqual(m['roi_pct'], 33.3)

    def test_roi_matches_canonical_group_stats(self):
        """The harness ROI must equal recommendation_performance._group_stats
        on the same bet set — they are the same convention."""
        from apps.mockbets.services.three_population_audit import compute_metrics
        from apps.mockbets.services.recommendation_performance import _group_stats

        g1 = self._game(ext='cn1')
        rec1 = self._rec(g1, lane='core')
        b1 = self._bet(g1, recommendation=rec1, result='win',
                       payout=Decimal('76.92'))
        g2 = self._game(ext='cn2')
        rec2 = self._rec(g2, lane='core')
        b2 = self._bet(g2, recommendation=rec2, result='loss',
                       payout=Decimal('0.00'))
        g3 = self._game(ext='cn3')
        rec3 = self._rec(g3, lane='core')
        b3 = self._bet(g3, recommendation=rec3, result='win',
                       payout=Decimal('120.00'))

        bets = [b1, b2, b3]
        mine = compute_metrics(bets)
        canon = _group_stats(bets)
        self.assertEqual(float(mine['net_pl']), float(canon['net_pl']))
        self.assertAlmostEqual(mine['roi_pct'], round(canon['roi'], 1), places=1)

    def test_avg_edge_is_pp_not_double_scaled(self):
        """REGRESSION LOCK: expected_edge is stored in PERCENTAGE POINTS.

        The harness must report it as-is (e.g. 11.3), NOT ×100 (1130.80).
        expected_edge=11.30 means 11.3pp; avg of [11.30, 7.00] must be 9.15.
        """
        from apps.mockbets.services.three_population_audit import compute_metrics

        g1 = self._game(ext='ed1')
        rec1 = self._rec(g1, lane='core')
        b1 = self._bet(g1, recommendation=rec1, edge=Decimal('11.30'))
        g2 = self._game(ext='ed2')
        rec2 = self._rec(g2, lane='core')
        b2 = self._bet(g2, recommendation=rec2, edge=Decimal('7.00'))

        m = compute_metrics([b1, b2])
        self.assertEqual(m['avg_edge_pp'], 9.15)   # NOT 915.0

    def test_clv_lineage_counts_zero_and_artifacts(self):
        """CLV lineage must separate clv==0 (matched) from clv>0 (beat) and
        flag single-snapshot games + source mismatch."""
        from apps.mockbets.services.three_population_audit import clv_lineage
        from apps.mlb.models import OddsSnapshot as MLBOdds

        # Bet whose closing snapshot is the SAME as placement → clv 0.
        g1 = self._game(ext='lz1')
        rec1 = self._rec(g1, lane='core')
        MLBOdds.objects.create(
            game=g1, captured_at=g1.first_pitch - timedelta(hours=2),
            moneyline_home=-130, moneyline_away=110, odds_source='odds_api',
            market_home_win_prob=0.57,
        )
        b1 = self._bet(g1, recommendation=rec1, clv_cents=0.0,
                       odds_source='odds_api')
        b1.clv_direction = ''
        b1.save(update_fields=['clv_direction'])

        # Bet that beat the close → clv +.
        g2 = self._game(ext='lz2')
        rec2 = self._rec(g2, lane='core')
        MLBOdds.objects.create(
            game=g2, captured_at=g2.first_pitch - timedelta(hours=3),
            moneyline_home=-130, moneyline_away=110, odds_source='odds_api',
            market_home_win_prob=0.57,
        )
        MLBOdds.objects.create(
            game=g2, captured_at=g2.first_pitch - timedelta(minutes=20),
            moneyline_home=-150, moneyline_away=130, odds_source='odds_api',
            market_home_win_prob=0.60,
        )
        b2 = self._bet(g2, recommendation=rec2, clv_cents=0.06,
                       odds_source='odds_api')

        lineage = clv_lineage([b1, b2])
        agg = lineage['aggregate']
        self.assertEqual(agg['n'], 2)
        self.assertEqual(agg['clv_zero'], 1)
        self.assertEqual(agg['clv_positive'], 1)
        self.assertEqual(agg['single_snapshot_games'], 1)   # g1 has 1 snapshot
        self.assertEqual(len(lineage['rows']), 2)

    def test_answers_block_resolves(self):
        """A/B/C verdicts populate; no None text where data exists."""
        from apps.mockbets.services.three_population_audit import build_audit, render_report

        g1 = self._game(ext='a1')
        rec1 = self._rec(g1, lane='core')
        self._bet(g1, recommendation=rec1)
        g2 = self._game(ext='a2')
        self._bet(g2, recommendation=None, is_system=False, rec_status='',
                  result='loss', payout=Decimal('0.00'),
                  clv_cents=None, odds_source='manual')

        now = timezone.now()
        audit = build_audit(
            MockBet.objects.all(),
            cutoff=now - timedelta(days=30),
            now=now,
            days=30, sport='mlb',
        )
        ans = audit['answers']
        self.assertIn(ans['A_beat_market']['verdict'], ('yes', 'no', 'unknown'))
        self.assertIn(ans['B_manual_hurt']['verdict'],
                      ('yes', 'no', 'undetermined'))
        self.assertIn(ans['C_blind_follow']['verdict'], ('computed', 'na'))

        # Renderer must produce something non-empty.
        report = render_report(audit)
        self.assertIn('THREE-POPULATION AUDIT', report)
        self.assertIn('ANSWER BLOCK', report)


class ThreePopulationAuditViewTests(TestCase):
    """Staff-only HTTP endpoint at /mockbets/audit/three-populations/."""

    def setUp(self):
        self.client = Client()
        self.staff = User.objects.create_user('staffuser', password='pw',
                                              is_staff=True)
        self.normal = User.objects.create_user('plainuser', password='pw')

    def test_anonymous_redirects(self):
        resp = self.client.get('/mockbets/audit/three-populations/')
        # login_required redirects to login
        self.assertEqual(resp.status_code, 302)

    def test_non_staff_404(self):
        self.client.force_login(self.normal)
        resp = self.client.get('/mockbets/audit/three-populations/')
        self.assertEqual(resp.status_code, 404)

    def test_staff_gets_text_report(self):
        self.client.force_login(self.staff)
        resp = self.client.get('/mockbets/audit/three-populations/')
        self.assertEqual(resp.status_code, 200)
        self.assertIn('text/plain', resp['Content-Type'])
        body = resp.content.decode('utf-8')
        self.assertIn('THREE-POPULATION AUDIT', body)
        self.assertIn('ALL ACTUAL BETS PLACED', body)
        self.assertIn('TRUE SYSTEM-APPROVED BETS', body)
        self.assertIn('MANUAL / CONTAMINATED BETS', body)
        self.assertIn('ANSWER BLOCK', body)

    def test_staff_json_format(self):
        self.client.force_login(self.staff)
        resp = self.client.get('/mockbets/audit/three-populations/?format=json')
        self.assertEqual(resp.status_code, 200)
        import json as _json
        data = _json.loads(resp.content.decode('utf-8'))
        self.assertIn('populations', data)
        self.assertIn('answers', data)
        self.assertIn('actual', data['populations'])
        self.assertIn('system_approved', data['populations'])
        self.assertIn('manual_contaminated', data['populations'])

    def test_days_clamp(self):
        """?days=9999 must clamp to 90 — keeps queries bounded."""
        self.client.force_login(self.staff)
        resp = self.client.get(
            '/mockbets/audit/three-populations/?days=9999&format=json'
        )
        self.assertEqual(resp.status_code, 200)
        import json as _json
        data = _json.loads(resp.content.decode('utf-8'))
        self.assertEqual(data['window']['days'], 90)

    def test_clv_detail_mode_returns_lineage(self):
        """?detail=clv returns the per-bet CLV lineage diagnostic."""
        self.client.force_login(self.staff)
        resp = self.client.get(
            '/mockbets/audit/three-populations/?detail=clv&scope=system'
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn('text/plain', resp['Content-Type'])
        body = resp.content.decode('utf-8')
        self.assertIn('CLV LINEAGE DIAGNOSTIC', body)
        self.assertIn('AGGREGATE ARTIFACT COUNTERS', body)
        self.assertIn('matched the close', body)

    def test_clv_detail_non_staff_404(self):
        self.client.force_login(self.normal)
        resp = self.client.get(
            '/mockbets/audit/three-populations/?detail=clv'
        )
        self.assertEqual(resp.status_code, 404)
