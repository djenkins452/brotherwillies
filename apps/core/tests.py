"""Tests for the decision layer — BettingRecommendation model and service.

Covers the two failure modes we actually care about:
  1. Correct side-selection math when house prob disagrees with market
  2. Graceful no-op when inputs are missing (no odds, unknown sport)
Placement → recommendation-snapshot wiring is tested implicitly via the
mockbets view integration test.
"""
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase, Client
from django.utils import timezone

from apps.core.models import BettingRecommendation
from apps.core.services.recommendations import (
    get_recommendation,
    persist_recommendation,
    _implied_prob,
    _format_american,
)
from apps.mlb.models import Conference, Team, Game, OddsSnapshot
from apps.mockbets.models import MockBet


def _make_mlb_game(home_rating=60, away_rating=40, neutral=False):
    league = Conference.objects.create(name='AL East', slug=f'al-east-{timezone.now().timestamp()}')
    home = Team.objects.create(name='Home', slug=f'home-{id(home_rating)}', conference=league, rating=home_rating)
    away = Team.objects.create(name='Away', slug=f'away-{id(away_rating)}', conference=league, rating=away_rating)
    return Game.objects.create(
        home_team=home,
        away_team=away,
        first_pitch=timezone.now() + timedelta(hours=2),
        neutral_site=neutral,
    )


class ImpliedProbHelpersTests(TestCase):
    def test_positive_odds_implied_prob(self):
        # +100 → 50%, +200 → 33.3%
        self.assertAlmostEqual(_implied_prob(100), 0.5, places=4)
        self.assertAlmostEqual(_implied_prob(200), 1 / 3, places=4)

    def test_negative_odds_implied_prob(self):
        # -100 → 50%, -200 → 66.7%
        self.assertAlmostEqual(_implied_prob(-100), 0.5, places=4)
        self.assertAlmostEqual(_implied_prob(-200), 2 / 3, places=4)

    def test_format_american(self):
        self.assertEqual(_format_american(120), '+120')
        self.assertEqual(_format_american(-150), '-150')


class GetRecommendationTests(TestCase):
    def test_returns_none_for_unknown_sport(self):
        game = _make_mlb_game()
        self.assertIsNone(get_recommendation('nfl', game))

    def test_returns_none_when_no_odds(self):
        game = _make_mlb_game()
        self.assertIsNone(get_recommendation('mlb', game))

    def test_picks_home_when_house_prob_beats_market(self):
        # Home team heavily favored by rating AND underpriced by market → bet home.
        game = _make_mlb_game(home_rating=80, away_rating=20)
        OddsSnapshot.objects.create(
            game=game,
            captured_at=timezone.now(),
            market_home_win_prob=0.5,  # market says 50/50
            moneyline_home=-110,       # ~52% implied
            moneyline_away=-110,
        )
        rec = get_recommendation('mlb', game)
        self.assertIsNotNone(rec)
        self.assertEqual(rec.bet_type, 'moneyline')
        self.assertEqual(rec.pick, 'Home')
        self.assertEqual(rec.odds_american, -110)
        self.assertEqual(rec.line, '-110')
        self.assertGreater(rec.model_edge, 0)

    def test_picks_away_when_away_has_edge(self):
        # Away is weaker team per rating, but home is getting -500 so away has implied-prob edge
        game = _make_mlb_game(home_rating=55, away_rating=45)
        OddsSnapshot.objects.create(
            game=game,
            captured_at=timezone.now(),
            market_home_win_prob=0.83,  # market says home wins 83%
            moneyline_home=-500,        # 83.3% implied — no edge for home
            moneyline_away=+400,        # 20% implied — our model says ~45% for away
        )
        rec = get_recommendation('mlb', game)
        self.assertIsNotNone(rec)
        self.assertEqual(rec.pick, 'Away')
        self.assertEqual(rec.odds_american, 400)
        self.assertEqual(rec.line, '+400')

    def test_confidence_score_matches_model_prob_for_picked_side(self):
        game = _make_mlb_game(home_rating=70, away_rating=30)
        OddsSnapshot.objects.create(
            game=game,
            captured_at=timezone.now(),
            market_home_win_prob=0.5,
            moneyline_home=-110,
            moneyline_away=-110,
        )
        rec = get_recommendation('mlb', game)
        # confidence = model prob for the picked side; on a ratings gap of 40
        # the sigmoid gives ~80%+ for home
        self.assertGreater(rec.confidence_score, 60.0)
        self.assertLess(rec.confidence_score, 99.01)


class PersistRecommendationTests(TestCase):
    def test_persist_creates_row_with_correct_fk(self):
        game = _make_mlb_game(home_rating=70, away_rating=30)
        OddsSnapshot.objects.create(
            game=game,
            captured_at=timezone.now(),
            market_home_win_prob=0.5,
            moneyline_home=-110,
            moneyline_away=-110,
        )
        rec = persist_recommendation('mlb', game)
        self.assertIsNotNone(rec)
        self.assertEqual(rec.sport, 'mlb')
        self.assertEqual(rec.mlb_game_id, game.id)
        self.assertIsNone(rec.cfb_game)
        self.assertIsInstance(rec.confidence_score, Decimal)

    def test_persist_returns_none_without_odds(self):
        game = _make_mlb_game()
        self.assertIsNone(persist_recommendation('mlb', game))
        self.assertEqual(BettingRecommendation.objects.count(), 0)


class PlaceBetSnapshotsRecommendationTests(TestCase):
    """Integration: POSTing to /mockbets/place/ should attach a recommendation FK."""

    def setUp(self):
        self.user = User.objects.create_user(username='better', password='pw')
        self.client = Client()
        self.client.force_login(self.user)
        self.game = _make_mlb_game(home_rating=70, away_rating=30)
        OddsSnapshot.objects.create(
            game=self.game,
            captured_at=timezone.now(),
            market_home_win_prob=0.5,
            moneyline_home=-110,
            moneyline_away=-110,
        )

    def test_place_bet_attaches_recommendation(self):
        resp = self.client.post(
            '/mockbets/place/',
            data={
                'sport': 'mlb',
                'game_id': str(self.game.id),
                'bet_type': 'moneyline',
                'selection': 'Home',
                'odds_american': -110,
                'stake_amount': '100',
                'confidence_level': 'medium',
                'model_source': 'house',
            },
            content_type='application/json',
        )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()['success'])
        bet = MockBet.objects.get(user=self.user)
        self.assertIsNotNone(bet.recommendation)
        self.assertEqual(bet.recommendation.sport, 'mlb')
        self.assertEqual(bet.recommendation.bet_type, 'moneyline')
