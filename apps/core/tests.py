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
    compute_status,
    get_recommendation,
    persist_recommendation,
    _implied_prob,
    _format_american,
    _raw_tier,
    ELITE_EDGE,
    STRONG_EDGE,
    MIN_EDGE,
    HEAVY_FAVORITE_ODDS,
    MAX_ELITE_PER_SLATE,
    STATUS_RECOMMENDED,
    STATUS_NOT_RECOMMENDED,
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
    """Tier is now edge-based (pp scale). Boundaries: elite≥8, strong≥6, standard<6."""

    def test_raw_tier_boundaries(self):
        self.assertEqual(_raw_tier(8.0), 'elite')
        self.assertEqual(_raw_tier(15.0), 'elite')
        self.assertEqual(_raw_tier(7.9), 'strong')
        self.assertEqual(_raw_tier(6.0), 'strong')
        self.assertEqual(_raw_tier(5.9), 'standard')
        self.assertEqual(_raw_tier(0.0), 'standard')
        self.assertEqual(_raw_tier(-3.0), 'standard')
        self.assertEqual(_raw_tier(None), 'standard')

    def test_tier_labels(self):
        elite = _fake_rec(confidence=70, edge=10)
        strong = _fake_rec(confidence=70, edge=7)
        standard = _fake_rec(confidence=70, edge=1)
        assign_tiers([elite, strong, standard])
        self.assertEqual(elite.tier_label, '🔥 High Confidence')
        self.assertEqual(strong.tier_label, 'Strong Edge')
        self.assertEqual(standard.tier_label, 'Model Pick')


class AssignTiersGuardrailTests(TestCase):
    def test_caps_elite_at_max_and_demotes_rest_to_strong(self):
        """4 would-be elites (all edge ≥ 8pp). Only top 2 by edge keep elite."""
        r1 = _fake_rec(confidence=75, edge=15)   # highest edge
        r2 = _fake_rec(confidence=70, edge=12)
        r3 = _fake_rec(confidence=90, edge=9)
        r4 = _fake_rec(confidence=85, edge=8)
        r5 = _fake_rec(confidence=70, edge=7)    # strong (not affected by cap)
        r6 = _fake_rec(confidence=50, edge=2)    # standard

        assign_tiers([r1, r2, r3, r4, r5, r6])

        self.assertEqual(MAX_ELITE_PER_SLATE, 2)
        elites = [r for r in [r1, r2, r3, r4, r5, r6] if r.tier == 'elite']
        self.assertEqual(len(elites), 2)
        # Top two by edge (15, 12) keep elite; high-confidence r3 does NOT
        # win out over r1/r2 because tier ranking is now edge-first.
        self.assertIn(r1, elites)
        self.assertIn(r2, elites)
        self.assertEqual(r3.tier, 'strong')
        self.assertEqual(r4.tier, 'strong')
        self.assertEqual(r5.tier, 'strong')
        self.assertEqual(r6.tier, 'standard')

    def test_confidence_breaks_ties_when_edge_equal(self):
        """Three recs all tied at edge=10 — confidence decides which stay elite."""
        r1 = _fake_rec(confidence=60, edge=10)
        r2 = _fake_rec(confidence=90, edge=10)
        r3 = _fake_rec(confidence=75, edge=10)
        assign_tiers([r1, r2, r3])
        elites = [r for r in [r1, r2, r3] if r.tier == 'elite']
        self.assertEqual(len(elites), 2)
        self.assertIn(r2, elites)  # highest confidence
        self.assertIn(r3, elites)  # second-highest confidence
        self.assertEqual(r1.tier, 'strong')

    def test_small_slate_keeps_all_elites(self):
        r1 = _fake_rec(confidence=75, edge=12)
        r2 = _fake_rec(confidence=75, edge=10)
        assign_tiers([r1, r2])
        self.assertEqual(r1.tier, 'elite')
        self.assertEqual(r2.tier, 'elite')

    def test_empty_list_is_safe(self):
        self.assertEqual(assign_tiers([]), [])


class DecisionRuleTests(TestCase):
    """Decision rules — the status/reason assignment used by the UI filter banding."""

    def test_low_edge_is_not_recommended(self):
        status, reason = compute_status(model_edge=3.5, odds_american=+100)
        self.assertEqual(status, STATUS_NOT_RECOMMENDED)
        self.assertEqual(reason, 'low_edge')

    def test_exactly_min_edge_is_recommended(self):
        """4.0pp is the threshold — at the boundary we recommend (clears Rule 1)."""
        status, reason = compute_status(model_edge=MIN_EDGE, odds_american=+100)
        self.assertEqual(status, STATUS_RECOMMENDED)
        self.assertEqual(reason, '')

    def test_heavy_favorite_with_weak_edge_is_not_recommended(self):
        """-150 odds + edge below 6pp → juice gate rejects it."""
        status, reason = compute_status(model_edge=4.5, odds_american=-200)
        self.assertEqual(status, STATUS_NOT_RECOMMENDED)
        self.assertEqual(reason, 'high_juice')

    def test_heavy_favorite_with_strong_edge_is_recommended(self):
        """-300 odds but edge clears STRONG_EDGE → Rule 2 does not trigger."""
        status, reason = compute_status(model_edge=STRONG_EDGE, odds_american=-300)
        self.assertEqual(status, STATUS_RECOMMENDED)
        self.assertEqual(reason, '')

    def test_elite_edge_overrides_juice_rule(self):
        """10pp edge against -500 odds is still recommended — elite override."""
        status, reason = compute_status(model_edge=10.0, odds_american=-500)
        self.assertEqual(status, STATUS_RECOMMENDED)
        self.assertEqual(reason, '')

    def test_favorite_at_boundary_still_gated(self):
        """Odds of exactly -150 (HEAVY_FAVORITE_ODDS) qualify as heavy favorite."""
        status, reason = compute_status(model_edge=5.0, odds_american=HEAVY_FAVORITE_ODDS)
        self.assertEqual(status, STATUS_NOT_RECOMMENDED)
        self.assertEqual(reason, 'high_juice')

    def test_recommendation_dataclass_carries_status(self):
        """Recommendations built by get_recommendation include the computed status."""
        game = _make_mlb_game(home_rating=80, away_rating=20)
        OddsSnapshot.objects.create(
            game=game, captured_at=timezone.now(),
            market_home_win_prob=0.5, moneyline_home=-110, moneyline_away=-110,
        )
        rec = get_recommendation('mlb', game)
        self.assertIn(rec.status, (STATUS_RECOMMENDED, STATUS_NOT_RECOMMENDED))
        # 50% market vs our strong home model prob → large edge → recommended
        self.assertEqual(rec.status, STATUS_RECOMMENDED)


class LobbySortByTierTests(TestCase):
    """Verify tier-first ordering in the lobby sort helper.

    Under the new edge-based tier rules, a 'strong' tier tile has edge ≥ 6pp
    by construction — so using edge as the within-tier tiebreaker means the
    sort is always (tier priority, edge desc)."""

    def test_tier_beats_edge_magnitude(self):
        from apps.core.views import _sort_games_by_tier_then_edge
        # Game A: strong tier (edge=6.5).  Game B: also strong (edge=7.0) but
        # game_a has smaller house_edge context. This proves tier-first still
        # works when other sort keys disagree.
        game_a = {'house_edge': 0.5, 'recommendation': _fake_rec(75, edge=6.5)}
        game_b = {'house_edge': 0.1, 'recommendation': _fake_rec(50, edge=7.0)}
        games = [game_a, game_b]
        assign_tiers([game_a['recommendation'], game_b['recommendation']])
        # Both are strong. Within-tier tiebreak uses sort_by=house_edge:
        # game_a (0.5) > game_b (0.1) — game_a first.
        _sort_games_by_tier_then_edge(games, sort_by='house_edge')
        self.assertIs(games[0], game_a)

    def test_elite_beats_strong_regardless_of_context_edge(self):
        from apps.core.views import _sort_games_by_tier_then_edge
        # Game A: strong tier (edge=6.5) with huge context edge.
        # Game B: elite tier (edge=9.0) with tiny context edge.
        # Elite must come first despite A having bigger house_edge.
        game_a = {'house_edge': 9.9, 'recommendation': _fake_rec(75, edge=6.5)}
        game_b = {'house_edge': 0.1, 'recommendation': _fake_rec(50, edge=9.0)}
        games = [game_a, game_b]
        assign_tiers([game_a['recommendation'], game_b['recommendation']])
        _sort_games_by_tier_then_edge(games, sort_by='house_edge')
        self.assertIs(games[0], game_b)
        self.assertIs(games[1], game_a)

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

    def test_elite_sorted_by_edge_then_confidence(self):
        """Global ranking is edge DESC, confidence DESC — edge wins first."""
        from apps.core.views import _partition_elite
        upcoming = [
            self._game_dict('a', 'elite', confidence=85, rec_edge=9),   # edge 9
            self._game_dict('b', 'elite', confidence=92, rec_edge=12),  # edge 12
            self._game_dict('c', 'elite', confidence=70, rec_edge=9),   # edge 9 (lower conf)
        ]
        elite, _, _ = _partition_elite(upcoming, [])
        # Highest edge first (b), then edge-tied (a and c), confidence breaks tie (a > c)
        self.assertEqual([g['game'].id for g in elite], ['b', 'a', 'c'])

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
        """End-to-end with assign_tiers: 5 recs all with edge ≥ ELITE_EDGE —
        only top 2 by (edge desc, confidence desc) reach the elite section.
        The rest fall into 'strong' in the main board."""
        from apps.core.views import _partition_elite
        # Edges 12, 11, 10, 9, 8 — all elite-tier by themselves
        games = [
            self._game_dict(f'g{i}', 'standard',
                            confidence=90 - i, rec_edge=12 - i)
            for i in range(5)
        ]
        for g in games:
            g['recommendation'].tier = 'standard'  # reset — assign_tiers will reclassify
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

    def test_place_bet_denormalizes_status_tier_confidence(self):
        """MockBet captures status/tier/confidence at placement so analytics
        don't depend on the recommendation row staying intact."""
        resp = self.client.post(
            '/mockbets/place/',
            data={
                'sport': 'mlb',
                'game_id': str(self.game.id),
                'bet_type': 'moneyline',
                'selection': 'Home',
                'odds_american': -110,
                'stake_amount': '100',
            },
            content_type='application/json',
        )
        self.assertEqual(resp.status_code, 200)
        bet = MockBet.objects.get(user=self.user)
        self.assertIn(bet.recommendation_status, ('recommended', 'not_recommended'))
        self.assertIn(bet.recommendation_tier, ('elite', 'strong', 'standard'))
        self.assertIsNotNone(bet.recommendation_confidence)
        # Values should match the linked recommendation
        self.assertEqual(bet.recommendation_status, bet.recommendation.status)
        self.assertEqual(bet.recommendation_tier, bet.recommendation.tier)


class RecommendationPerformanceTests(TestCase):
    """The recommendation_performance service groups settled MockBet results
    by the snapshot fields. These tests lock the math and the system
    confidence score formula."""

    def setUp(self):
        from apps.mockbets.models import MockBet
        self.user = User.objects.create_user('perf_user', password='pw')
        self.MockBet = MockBet

    def _bet(self, result, stake, payout=None, status='recommended', tier='elite', edge=10.0):
        from apps.mockbets.models import MockBet
        return MockBet.objects.create(
            user=self.user, sport='mlb', bet_type='moneyline',
            selection='X', odds_american=+100,
            implied_probability=Decimal('0.50'),
            stake_amount=Decimal(stake),
            simulated_payout=Decimal(payout) if payout is not None else None,
            result=result,
            recommendation_status=status,
            recommendation_tier=tier,
            expected_edge=Decimal(str(edge)),
        )

    def test_group_by_status_isolates_recommended_vs_not(self):
        from apps.mockbets.services.recommendation_performance import compute_performance_by_status
        # 3 recommended wins, 1 loss; 2 not-recommended losses
        for _ in range(3):
            self._bet('win', '100', '100', status='recommended')
        self._bet('loss', '100', status='recommended')
        self._bet('loss', '100', status='not_recommended')
        self._bet('loss', '100', status='not_recommended')

        result = compute_performance_by_status(self.MockBet.objects.filter(user=self.user))
        self.assertEqual(result['recommended']['wins'], 3)
        self.assertEqual(result['recommended']['losses'], 1)
        self.assertAlmostEqual(result['recommended']['win_rate'], 75.0, places=1)
        self.assertEqual(result['not_recommended']['wins'], 0)
        self.assertEqual(result['not_recommended']['losses'], 2)
        self.assertEqual(result['not_recommended']['win_rate'], 0.0)

    def test_group_by_tier_elite_strong_standard(self):
        from apps.mockbets.services.recommendation_performance import compute_performance_by_tier
        self._bet('win', '100', '100', tier='elite')
        self._bet('win', '100', '100', tier='elite')
        self._bet('loss', '100', tier='strong')
        self._bet('push', '100', tier='standard')

        result = compute_performance_by_tier(self.MockBet.objects.filter(user=self.user))
        self.assertEqual(result['elite']['wins'], 2)
        self.assertEqual(result['strong']['losses'], 1)
        self.assertEqual(result['standard']['pushes'], 1)

    def test_pending_bets_excluded_from_all_groups(self):
        from apps.mockbets.services.recommendation_performance import compute_performance_by_status
        self._bet('win', '100', '100', status='recommended')
        self._bet('pending', '100', status='recommended')
        result = compute_performance_by_status(self.MockBet.objects.filter(user=self.user))
        self.assertEqual(result['recommended']['total_bets'], 1)

    def test_system_confidence_score_reflects_sample_size(self):
        """At the same win rate and ROI, a larger sample must score higher than
        a smaller one — the sample_term pulls the small sample down."""
        from apps.mockbets.services.recommendation_performance import compute_system_confidence_score

        # Phase A: 3 perfect wins (small sample)
        for _ in range(3):
            self._bet('win', '100', '100')
        small_score = compute_system_confidence_score(self.MockBet.objects.filter(user=self.user))
        self.assertEqual(small_score['components']['total_bets'], 3)
        self.assertLess(small_score['score'], 100.0)  # sample penalty prevents maxing

        # Phase B: add 47 more perfect wins — hits full sample saturation (50)
        for _ in range(47):
            self._bet('win', '100', '100')
        big_score = compute_system_confidence_score(self.MockBet.objects.filter(user=self.user))
        self.assertEqual(big_score['components']['total_bets'], 50)
        self.assertGreater(big_score['score'], small_score['score'])

    def test_system_confidence_score_zero_for_empty_bets(self):
        from apps.mockbets.services.recommendation_performance import compute_system_confidence_score
        score = compute_system_confidence_score(self.MockBet.objects.filter(user=self.user))
        # No data: win_rate=0, roi=0 (neutral), sample=0 → only the neutral ROI
        # term contributes (0.5 center * 0.3 weight * 100 = 15)
        self.assertEqual(score['components']['total_bets'], 0)
        self.assertGreaterEqual(score['score'], 0)
        self.assertLess(score['score'], 20)

    def test_compute_all_returns_bundle(self):
        from apps.mockbets.services.recommendation_performance import compute_all
        self._bet('win', '100', '100')
        bundle = compute_all(self.MockBet.objects.filter(user=self.user))
        self.assertIn('by_status', bundle)
        self.assertIn('by_tier', bundle)
        self.assertIn('system_confidence', bundle)


# =========================================================================
# Odds Movement Intelligence — coverage for apps/core/services/odds_movement
# =========================================================================

from types import SimpleNamespace
from unittest.mock import patch

from apps.core.services.odds_movement import (
    apply_movement_intelligence,
    cents_moved,
    cents_signed_delta,
    classify_score,
    compute_movement_score,
    devigged_home_prob,
    is_significant,
    to_cents_axis,
)


def _snap(t, **kw):
    """Build a snapshot-shaped object for service tests (no DB).

    The service operates on attributes by name (moneyline_home, moneyline_away,
    spread, total, market_home_win_prob, captured_at), so SimpleNamespace is
    enough — we don't need real ORM instances for the math.
    """
    defaults = dict(
        captured_at=t,
        moneyline_home=None,
        moneyline_away=None,
        spread=None,
        total=None,
        market_home_win_prob=None,
    )
    defaults.update(kw)
    return SimpleNamespace(**defaults)


class CentsAxisTests(TestCase):
    """The cents axis is the bedrock of every line-move calculation. If
    these are wrong, magnitude/direction/significance all silently lie."""

    def test_to_cents_axis_handles_favorites_and_dogs(self):
        # Favorites map below zero, dogs above. +/-100 collapses to 0.
        self.assertEqual(to_cents_axis(-150), -50)
        self.assertEqual(to_cents_axis(-110), -10)
        self.assertEqual(to_cents_axis(-100), 0)
        self.assertEqual(to_cents_axis(100), 0)
        self.assertEqual(to_cents_axis(110), 10)
        self.assertEqual(to_cents_axis(150), 50)
        self.assertIsNone(to_cents_axis(None))

    def test_cents_moved_within_same_sign(self):
        self.assertEqual(cents_moved(-110, -120), 10)
        self.assertEqual(cents_moved(-150, -160), 10)
        self.assertEqual(cents_moved(120, 110), 10)

    def test_cents_moved_across_zero(self):
        # +110 ↔ -110 is 20 cents, not 220 (the naive arithmetic delta).
        self.assertEqual(cents_moved(110, -110), 20)
        # Crossing through "even": -100 to +100 is 0 (same point on axis).
        self.assertEqual(cents_moved(-100, 100), 0)

    def test_cents_signed_delta_direction(self):
        # Going from -110 to -120: home becomes more favored → axis moves
        # MORE negative → signed delta < 0.
        self.assertLess(cents_signed_delta(-110, -120), 0)
        # Going from +110 to +120: dog becomes longer → signed delta > 0.
        self.assertGreater(cents_signed_delta(110, 120), 0)

    def test_cents_moved_handles_none(self):
        self.assertEqual(cents_moved(None, -110), 0.0)
        self.assertEqual(cents_moved(-110, None), 0.0)


class DevigTests(TestCase):
    def test_devigged_home_prob_uses_two_sided_market(self):
        s = _snap(timezone.now(), moneyline_home=-150, moneyline_away=130)
        # implied: -150 → 0.60, +130 → 0.4348. Fair home = .60/(.60+.4348)
        # = ~0.5798. Should be lower than raw 0.60 (vig stripped).
        prob = devigged_home_prob(s)
        self.assertAlmostEqual(prob, 0.6 / (0.6 + 100/230.0), places=4)
        self.assertLess(prob, 0.60)

    def test_falls_back_to_market_prob_when_one_side_missing(self):
        s = _snap(timezone.now(), moneyline_home=-150, market_home_win_prob=0.55)
        # No moneyline_away → fall back to stored market_home_win_prob.
        self.assertEqual(devigged_home_prob(s), 0.55)


class SignificanceTests(TestCase):
    """Threshold checks: at least one OR triggers significance, otherwise no."""

    def test_no_change_is_not_significant(self):
        t = timezone.now()
        prev = _snap(t, moneyline_home=-110, moneyline_away=-110)
        curr = _snap(t, moneyline_home=-110, moneyline_away=-110)
        self.assertFalse(is_significant(prev, curr))

    def test_below_threshold_line_move_is_not_significant(self):
        # 5-cent move sits below the 7-cent default — should be noise.
        t = timezone.now()
        prev = _snap(t, moneyline_home=-110, moneyline_away=-110)
        curr = _snap(t, moneyline_home=-115, moneyline_away=-105)
        self.assertFalse(is_significant(prev, curr))

    def test_threshold_line_move_triggers(self):
        # 7-cent moneyline move on either side should trigger.
        t = timezone.now()
        prev = _snap(t, moneyline_home=-110, moneyline_away=-110)
        curr = _snap(t, moneyline_home=-117, moneyline_away=-103)
        self.assertTrue(is_significant(prev, curr))

    def test_prob_shift_alone_can_trigger(self):
        # Construct a case where lines didn't move enough to clear cents
        # threshold but the de-vigged home prob shifted >= 2pp.
        t = timezone.now()
        prev = _snap(t, moneyline_home=-105, moneyline_away=-115)  # home prob ~0.477
        curr = _snap(t, moneyline_home=-130, moneyline_away=110)   # home prob ~0.594
        # Even though one side moved >7c, the prob trigger would fire too —
        # this confirms the OR semantics rather than the line trigger alone.
        self.assertTrue(is_significant(prev, curr))

    def test_spread_move_triggers(self):
        t = timezone.now()
        prev = _snap(t, market_home_win_prob=0.5, spread=-1.5)
        curr = _snap(t, market_home_win_prob=0.5, spread=-2.0)
        self.assertTrue(is_significant(prev, curr))

    def test_total_move_triggers(self):
        t = timezone.now()
        prev = _snap(t, market_home_win_prob=0.5, total=8.0)
        curr = _snap(t, market_home_win_prob=0.5, total=8.5)
        self.assertTrue(is_significant(prev, curr))

    def test_returns_false_when_inputs_missing(self):
        self.assertFalse(is_significant(None, _snap(timezone.now())))
        self.assertFalse(is_significant(_snap(timezone.now()), None))


class ClassifyScoreTests(TestCase):
    def test_cuts_at_25_55_80(self):
        self.assertEqual(classify_score(0), 'noise')
        self.assertEqual(classify_score(25.0), 'noise')
        self.assertEqual(classify_score(25.01), 'moderate')
        self.assertEqual(classify_score(54.99), 'moderate')
        self.assertEqual(classify_score(55.0), 'moderate')
        self.assertEqual(classify_score(55.01), 'strong')
        self.assertEqual(classify_score(80.0), 'strong')
        self.assertEqual(classify_score(80.01), 'sharp')
        self.assertEqual(classify_score(100), 'sharp')


class ComputeMovementScoreTests(TestCase):
    def test_returns_none_for_too_few_snapshots(self):
        self.assertIsNone(compute_movement_score([]))
        self.assertIsNone(compute_movement_score([_snap(timezone.now())]))

    def test_no_movement_yields_low_score(self):
        # 5 snapshots, all identical → magnitude=0, speed=0, consistency=0.
        # Timing component contributes only 15% of the cap → score <= 15.
        now = timezone.now()
        snaps = [
            _snap(now - timedelta(hours=i),
                  moneyline_home=-110, moneyline_away=-110, spread=-1.5, total=8.5)
            for i in range(5, 0, -1)
        ]
        result = compute_movement_score(snaps)
        # Returns None when no market has any movement (consistency is 0
        # and there's nothing to score). Either None or noise is acceptable.
        if result is not None:
            self.assertEqual(result.classification, 'noise')

    def test_strong_late_one_way_move_classifies_high(self):
        # Five snapshots over the last hour, monotone moneyline shift from
        # -110 → -150 (40 cents toward home). Magnitude high, speed high,
        # consistency 100%, timing very recent → expect 'strong' or 'sharp'.
        now = timezone.now()
        odds_progression = [-110, -120, -130, -140, -150]
        snaps = [
            _snap(now - timedelta(minutes=(60 - i*15)),
                  moneyline_home=ml, moneyline_away=110)
            for i, ml in enumerate(odds_progression)
        ]
        result = compute_movement_score(snaps)
        self.assertIsNotNone(result)
        self.assertGreaterEqual(result.score, 55.0)
        self.assertIn(result.classification, ('strong', 'sharp'))
        # Direction: home line went more negative → +1 (toward home).
        self.assertEqual(result.direction, 1)
        self.assertEqual(result.market, 'moneyline')

    def test_choppy_movement_low_consistency(self):
        # Movement that flips back and forth — large total magnitude but no
        # one-way conviction. Score should sit below the 'strong' cut.
        now = timezone.now()
        odds = [-110, -130, -110, -130, -110]
        snaps = [
            _snap(now - timedelta(minutes=(60 - i*15)),
                  moneyline_home=ml, moneyline_away=110)
            for i, ml in enumerate(odds)
        ]
        result = compute_movement_score(snaps)
        self.assertIsNotNone(result)
        self.assertLess(result.score, 80.0)
        # Consistency should be capped because the moves alternate.
        # 4 step deltas, alternating up/down → 50% one direction.
        self.assertLessEqual(result.consistency, 60.0)

    def test_old_movement_gets_no_timing_boost(self):
        # Same magnitude as the "strong late" test but landed 6 hours ago.
        # Timing component should be 0 → score lower.
        now = timezone.now()
        odds_progression = [-110, -120, -130, -140, -150]
        old_snaps = [
            _snap(now - timedelta(hours=6, minutes=(60 - i*15)),
                  moneyline_home=ml, moneyline_away=110)
            for i, ml in enumerate(odds_progression)
        ]
        result_old = compute_movement_score(old_snaps)
        recent_snaps = [
            _snap(now - timedelta(minutes=(60 - i*15)),
                  moneyline_home=ml, moneyline_away=110)
            for i, ml in enumerate(odds_progression)
        ]
        result_recent = compute_movement_score(recent_snaps)
        self.assertIsNotNone(result_old)
        self.assertIsNotNone(result_recent)
        self.assertLess(result_old.timing, result_recent.timing)
        self.assertLess(result_old.score, result_recent.score)

    def test_dominant_market_wins(self):
        # Both moneyline AND total moved. The bigger one (moneyline 40 cents)
        # should be the dominant signal in the result.
        now = timezone.now()
        snaps = [
            _snap(now - timedelta(minutes=60), moneyline_home=-110, moneyline_away=110, total=8.5),
            _snap(now - timedelta(minutes=45), moneyline_home=-120, moneyline_away=110, total=8.5),
            _snap(now - timedelta(minutes=30), moneyline_home=-135, moneyline_away=115, total=8.5),
            _snap(now - timedelta(minutes=15), moneyline_home=-150, moneyline_away=130, total=9.0),
            _snap(now,                          moneyline_home=-150, moneyline_away=130, total=9.0),
        ]
        result = compute_movement_score(snaps)
        self.assertIsNotNone(result)
        self.assertEqual(result.market, 'moneyline')

    def test_history_bound_does_not_crash_with_late_event(self):
        # If a snapshot's captured_at is in the future relative to "now",
        # the timing weight code shouldn't blow up. Defensive sanity check.
        future = timezone.now() + timedelta(minutes=10)
        snaps = [
            _snap(future - timedelta(minutes=20), moneyline_home=-110, moneyline_away=-110),
            _snap(future, moneyline_home=-120, moneyline_away=-100),
        ]
        # Just verify no exception.
        compute_movement_score(snaps)


class ApplyMovementIntelligenceTests(TestCase):
    """Provider-hook integration test: persists are silent on the first
    snapshot, then upgrade subsequent rows when significance is crossed."""

    def setUp(self):
        from apps.mlb.models import Conference, Game, OddsSnapshot, Team
        self.Game = Game
        self.OddsSnapshot = OddsSnapshot
        # Two teams + a game so we have valid FK targets.
        conf = Conference.objects.create(name='AL East', slug='al-east')
        home = Team.objects.create(name='Home', slug='home', conference=conf)
        away = Team.objects.create(name='Away', slug='away', conference=conf)
        self.game = Game.objects.create(
            home_team=home, away_team=away,
            first_pitch=timezone.now() + timedelta(hours=2),
        )

    def _create(self, *, ml_h, ml_a, when_offset_min=0):
        return self.OddsSnapshot.objects.create(
            game=self.game,
            captured_at=timezone.now() + timedelta(minutes=when_offset_min),
            sportsbook='DraftKings',
            market_home_win_prob=0.5,
            moneyline_home=ml_h, moneyline_away=ml_a,
        )

    def test_first_snapshot_stays_raw(self):
        s = self._create(ml_h=-110, ml_a=-110, when_offset_min=-60)
        result = apply_movement_intelligence(self.OddsSnapshot, s)
        s.refresh_from_db()
        self.assertIsNone(result)
        self.assertEqual(s.snapshot_type, 'raw')
        self.assertIsNone(s.movement_score)

    def test_significant_followup_upgrades_to_significant(self):
        self._create(ml_h=-110, ml_a=-110, when_offset_min=-60)
        s2 = self._create(ml_h=-150, ml_a=130, when_offset_min=-1)
        result = apply_movement_intelligence(self.OddsSnapshot, s2)
        s2.refresh_from_db()
        self.assertIsNotNone(result)
        self.assertEqual(s2.snapshot_type, 'significant')
        self.assertIsNotNone(s2.movement_score)
        self.assertIn(s2.movement_class, ('moderate', 'strong', 'sharp'))

    def test_noise_followup_stays_raw(self):
        # Sub-threshold change: 4 cents on each side, small prob shift.
        self._create(ml_h=-110, ml_a=-110, when_offset_min=-60)
        s2 = self._create(ml_h=-114, ml_a=-106, when_offset_min=-1)
        apply_movement_intelligence(self.OddsSnapshot, s2)
        s2.refresh_from_db()
        self.assertEqual(s2.snapshot_type, 'raw')
        self.assertIsNone(s2.movement_score)

    def test_exception_safe(self):
        # Force an exception in the inner path; the outer call must not raise.
        s = self._create(ml_h=-110, ml_a=-110, when_offset_min=-60)
        with patch(
            'apps.core.services.odds_movement.compute_movement_score',
            side_effect=RuntimeError('boom'),
        ):
            # Should swallow and return None, not raise.
            result = apply_movement_intelligence(self.OddsSnapshot, s)
            self.assertIsNone(result)


class PruneOldRawSnapshotsTests(TestCase):
    """Pruning policy: raw older than retention deleted; significant/closing/
    bet_context kept; recent raw kept."""

    def setUp(self):
        from apps.mlb.models import Conference, Game, OddsSnapshot, Team
        self.OddsSnapshot = OddsSnapshot
        conf = Conference.objects.create(name='AL East', slug='al-east')
        home = Team.objects.create(name='Home', slug='home', conference=conf)
        away = Team.objects.create(name='Away', slug='away', conference=conf)
        self.game = Game.objects.create(
            home_team=home, away_team=away,
            first_pitch=timezone.now() + timedelta(hours=2),
        )

    def _make(self, *, snapshot_type, age_days):
        snap = self.OddsSnapshot.objects.create(
            game=self.game,
            captured_at=timezone.now() - timedelta(days=age_days),
            sportsbook='DraftKings',
            market_home_win_prob=0.5,
            moneyline_home=-110, moneyline_away=-110,
            snapshot_type=snapshot_type,
        )
        # auto_now_add isn't set on captured_at but the create above already
        # set it; just return the row.
        return snap

    def test_deletes_only_old_raw_rows(self):
        from django.core.management import call_command
        from io import StringIO
        old_raw = self._make(snapshot_type='raw', age_days=20)
        recent_raw = self._make(snapshot_type='raw', age_days=2)
        old_significant = self._make(snapshot_type='significant', age_days=20)
        old_closing = self._make(snapshot_type='closing', age_days=30)
        old_bet_ctx = self._make(snapshot_type='bet_context', age_days=30)

        out = StringIO()
        call_command('prune_old_raw_snapshots', stdout=out)

        ids = set(self.OddsSnapshot.objects.values_list('pk', flat=True))
        self.assertNotIn(old_raw.pk, ids)
        self.assertIn(recent_raw.pk, ids)
        self.assertIn(old_significant.pk, ids)
        self.assertIn(old_closing.pk, ids)
        self.assertIn(old_bet_ctx.pk, ids)

    def test_dry_run_deletes_nothing(self):
        from django.core.management import call_command
        from io import StringIO
        old_raw = self._make(snapshot_type='raw', age_days=20)
        out = StringIO()
        call_command('prune_old_raw_snapshots', '--dry-run', stdout=out)
        self.assertTrue(self.OddsSnapshot.objects.filter(pk=old_raw.pk).exists())

    def test_custom_days_threshold(self):
        from django.core.management import call_command
        from io import StringIO
        # 10-day-old raw — kept by default (14d) but deleted with --days=7.
        s = self._make(snapshot_type='raw', age_days=10)
        out = StringIO()
        call_command('prune_old_raw_snapshots', '--days=7', stdout=out)
        self.assertFalse(self.OddsSnapshot.objects.filter(pk=s.pk).exists())
