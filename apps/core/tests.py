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
    Recommendation,
    assign_tiers,
    get_recommendation,
    persist_recommendation,
    _implied_prob,
    _format_american,
    _raw_tier,
    ELITE_THRESHOLD,
    STRONG_THRESHOLD,
    MAX_ELITE_PER_SLATE,
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


def _fake_rec(confidence, edge=1.0, pick='X'):
    """Build a plain Recommendation for tier-logic tests — no game/odds needed."""
    return Recommendation(
        sport='mlb', game=None, bet_type='moneyline', pick=pick,
        line='+100', odds_american=100,
        confidence_score=confidence, model_edge=edge, model_source='house',
    )


class TierThresholdTests(TestCase):
    def test_raw_tier_boundaries(self):
        # elite: >= 80, strong: 65..<80, standard: <65
        self.assertEqual(_raw_tier(80.0), 'elite')
        self.assertEqual(_raw_tier(95.0), 'elite')
        self.assertEqual(_raw_tier(79.9), 'strong')
        self.assertEqual(_raw_tier(65.0), 'strong')
        self.assertEqual(_raw_tier(64.9), 'standard')
        self.assertEqual(_raw_tier(0.0), 'standard')

    def test_tier_labels(self):
        elite = _fake_rec(90)
        strong = _fake_rec(70)
        standard = _fake_rec(50)
        assign_tiers([elite, strong, standard])
        self.assertEqual(elite.tier_label, '🔥 High Confidence')
        self.assertEqual(strong.tier_label, 'Strong Edge')
        self.assertEqual(standard.tier_label, 'Model Pick')


class AssignTiersGuardrailTests(TestCase):
    def test_caps_elite_at_max_and_demotes_rest_to_strong(self):
        # 4 would-be elites. Only top 2 by confidence should stay elite.
        r1 = _fake_rec(95, edge=5)
        r2 = _fake_rec(90, edge=3)
        r3 = _fake_rec(88, edge=8)
        r4 = _fake_rec(85, edge=1)
        r5 = _fake_rec(70, edge=9)  # strong — untouched by cap
        r6 = _fake_rec(50, edge=2)  # standard

        assign_tiers([r1, r2, r3, r4, r5, r6])

        self.assertEqual(MAX_ELITE_PER_SLATE, 2)
        elites = [r for r in [r1, r2, r3, r4, r5, r6] if r.tier == 'elite']
        self.assertEqual(len(elites), 2)
        # Top by confidence (95, 90) kept; rest dropped to strong
        self.assertIn(r1, elites)
        self.assertIn(r2, elites)
        self.assertEqual(r3.tier, 'strong')
        self.assertEqual(r4.tier, 'strong')
        # Already-strong and standard untouched
        self.assertEqual(r5.tier, 'strong')
        self.assertEqual(r6.tier, 'standard')

    def test_edge_breaks_ties_when_confidence_equal(self):
        # Two recs tied at 85 confidence — edge decides which stays elite.
        r1 = _fake_rec(85, edge=2)
        r2 = _fake_rec(85, edge=7)
        r3 = _fake_rec(85, edge=4)
        assign_tiers([r1, r2, r3])
        elites = [r for r in [r1, r2, r3] if r.tier == 'elite']
        self.assertEqual(len(elites), 2)
        self.assertIn(r2, elites)  # highest edge
        self.assertIn(r3, elites)  # second-highest edge
        self.assertEqual(r1.tier, 'strong')

    def test_small_slate_keeps_all_elites(self):
        r1 = _fake_rec(95)
        r2 = _fake_rec(85)
        assign_tiers([r1, r2])
        self.assertEqual(r1.tier, 'elite')
        self.assertEqual(r2.tier, 'elite')

    def test_empty_list_is_safe(self):
        self.assertEqual(assign_tiers([]), [])


class LobbySortByTierTests(TestCase):
    """Verify tier-first ordering in the lobby sort helper."""

    def test_tier_beats_edge_magnitude(self):
        from apps.core.views import _sort_games_by_tier_then_edge
        # Game A: strong tier, tiny edge.  Game B: standard tier, huge edge.
        # Tier comes first — A must win even though B's edge is larger.
        game_a = {'house_edge': 0.5, 'recommendation': _fake_rec(75, edge=0.5)}
        game_b = {'house_edge': 9.9, 'recommendation': _fake_rec(50, edge=9.9)}
        games = [game_b, game_a]
        assign_tiers([game_a['recommendation'], game_b['recommendation']])
        _sort_games_by_tier_then_edge(games, sort_by='house_edge')
        self.assertIs(games[0], game_a)
        self.assertIs(games[1], game_b)

    def test_edge_is_tiebreaker_within_tier(self):
        from apps.core.views import _sort_games_by_tier_then_edge
        a = {'house_edge': 2.0, 'recommendation': _fake_rec(75, edge=2.0)}
        b = {'house_edge': 8.0, 'recommendation': _fake_rec(70, edge=8.0)}
        assign_tiers([a['recommendation'], b['recommendation']])
        _sort_games_by_tier_then_edge([a, b], sort_by='house_edge')
        games = [a, b]
        _sort_games_by_tier_then_edge(games, sort_by='house_edge')
        # Both are 'strong' after tier assignment — bigger absolute edge wins
        self.assertIs(games[0], b)
        self.assertIs(games[1], a)

    def test_none_recommendation_sorts_last(self):
        from apps.core.views import _sort_games_by_tier_then_edge
        no_rec = {'house_edge': 9.9, 'recommendation': None}
        standard = {'house_edge': 1.0, 'recommendation': _fake_rec(50, edge=1.0)}
        assign_tiers([standard['recommendation']])
        games = [no_rec, standard]
        _sort_games_by_tier_then_edge(games, sort_by='house_edge')
        self.assertIs(games[0], standard)
        self.assertIs(games[1], no_rec)


class ExplanationRowsTests(TestCase):
    """The elite card's explanation block is driven by Recommendation.explanation_rows.
    It must be deterministic, never fabricated, and skip metrics that can't be computed."""

    def test_full_rows_when_all_fields_present(self):
        rec = _fake_rec(confidence=72.0, edge=14.0)
        rec.odds_american = -120
        rows = rec.explanation_rows
        labels = [r['label'] for r in rows]
        self.assertEqual(labels, ['Win Probability', 'Market Implied', 'Edge'])
        # Deterministic formatting
        values = {r['label']: r['value'] for r in rows}
        self.assertEqual(values['Win Probability'], '72%')
        # -120 → 120/220 ≈ 54.5% → rounds to 55%
        self.assertEqual(values['Market Implied'], '55%')
        self.assertEqual(values['Edge'], '+14.0%')

    def test_negative_edge_renders_without_plus_sign(self):
        rec = _fake_rec(confidence=60.0, edge=-3.2)
        rec.odds_american = 100
        values = {r['label']: r['value'] for r in rec.explanation_rows}
        self.assertEqual(values['Edge'], '-3.2%')

    def test_zero_odds_omits_market_implied_row(self):
        rec = _fake_rec(confidence=70.0, edge=5.0)
        rec.odds_american = 0
        labels = [r['label'] for r in rec.explanation_rows]
        self.assertNotIn('Market Implied', labels)
        self.assertIn('Win Probability', labels)
        self.assertIn('Edge', labels)


class ElitePartitionTests(TestCase):
    """Ensure the lobby view partitions elite games into elite_games and keeps
    them out of the main board."""

    def _game_dict(self, game_id, tier, house_edge=1.0, confidence=70.0, rec_edge=1.0):
        """Minimal shape compatible with _partition_elite."""
        class _Stub:
            pass
        g = _Stub()
        g.id = game_id
        rec = _fake_rec(confidence=confidence, edge=rec_edge)
        rec.tier = tier  # bypass assign_tiers — test is about partitioning
        return {'game': g, 'house_edge': house_edge, 'recommendation': rec}

    def test_partitions_elite_from_upcoming(self):
        from apps.core.views import _partition_elite
        upcoming = [
            self._game_dict('u1', 'elite', confidence=90, rec_edge=5),
            self._game_dict('u2', 'strong', confidence=70, rec_edge=3),
            self._game_dict('u3', 'standard', confidence=55, rec_edge=1),
        ]
        elite, remaining_up, remaining_live = _partition_elite(upcoming, [])
        self.assertEqual(len(elite), 1)
        self.assertEqual(elite[0]['game'].id, 'u1')
        self.assertEqual([g['game'].id for g in remaining_up], ['u2', 'u3'])
        self.assertEqual(remaining_live, [])

    def test_partitions_elite_from_live_too(self):
        from apps.core.views import _partition_elite
        upcoming = [self._game_dict('u1', 'standard')]
        live = [self._game_dict('l1', 'elite', confidence=95, rec_edge=10)]
        elite, remaining_up, remaining_live = _partition_elite(upcoming, live)
        self.assertEqual([g['game'].id for g in elite], ['l1'])
        self.assertEqual([g['game'].id for g in remaining_up], ['u1'])
        self.assertEqual(remaining_live, [])

    def test_elite_sorted_by_confidence_then_edge(self):
        from apps.core.views import _partition_elite
        upcoming = [
            self._game_dict('a', 'elite', confidence=85, rec_edge=3),
            self._game_dict('b', 'elite', confidence=92, rec_edge=1),
            self._game_dict('c', 'elite', confidence=85, rec_edge=9),
        ]
        elite, _, _ = _partition_elite(upcoming, [])
        # 92 first; then 85s tied on confidence → higher edge (c) wins
        self.assertEqual([g['game'].id for g in elite], ['b', 'c', 'a'])

    def test_no_duplication_across_lists(self):
        """Defensive: if the same game somehow appears in both live and upcoming
        (shouldn't happen in practice, but code must not duplicate it)."""
        from apps.core.views import _partition_elite
        shared = self._game_dict('dup', 'elite', confidence=90, rec_edge=5)
        elite, remaining_up, remaining_live = _partition_elite([shared], [shared])
        self.assertEqual(len(elite), 1)
        self.assertEqual(remaining_up, [])
        self.assertEqual(remaining_live, [])

    def test_no_recommendation_games_stay_in_main(self):
        from apps.core.views import _partition_elite
        no_rec = {'game': type('G', (), {'id': 'x'})(), 'house_edge': 0, 'recommendation': None}
        elite, remaining_up, _ = _partition_elite([no_rec], [])
        self.assertEqual(elite, [])
        self.assertEqual(len(remaining_up), 1)

    def test_guardrail_cap_enforced_before_partition(self):
        """End-to-end with assign_tiers: even with 5 would-be elites, only 2
        reach the elite section. The rest remain as 'strong' in the main board."""
        from apps.core.views import _partition_elite
        games = [self._game_dict(f'g{i}', 'standard', confidence=90 - i, rec_edge=5 - i) for i in range(5)]
        # Build with raw tiers first, then run assign_tiers so it mimics the view path
        for g in games:
            g['recommendation'].tier = 'standard'
        assign_tiers([g['recommendation'] for g in games])
        elite, remaining_up, _ = _partition_elite(games, [])
        self.assertEqual(len(elite), MAX_ELITE_PER_SLATE)
        self.assertEqual(len(remaining_up), 5 - MAX_ELITE_PER_SLATE)
        for g in remaining_up:
            self.assertEqual(g['recommendation'].tier, 'strong')


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
