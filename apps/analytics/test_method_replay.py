"""Tests for the Method Replay service.

The leakage tests are the most important ones in this file. Treat any
failure in `NoLeakageTests` as a production blocker.
"""
from datetime import date, datetime, timedelta, timezone as dt_timezone
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from apps.analytics.services.method_replay import (
    BLEND_WEIGHT_HISTORY,
    _compute_metrics,
    _pregame_snapshots,
    _simulate_recommendation,
    diff_recommendations,
    historical_blend_weight,
    run_replay,
)


# ---------------------------------------------------------------------------
# Helpers


def _mlb_setup(suffix='0'):
    """Build a fresh MLB conference + team pair for fixtures."""
    from apps.mlb.models import Conference, Team
    league = Conference.objects.create(
        name=f'AL-{suffix}', slug=f'al-{suffix}-{timezone.now().timestamp()}',
    )
    home = Team.objects.create(
        name=f'Home-{suffix}',
        slug=f'h-{suffix}-{timezone.now().timestamp()}',
        conference=league,
        rating=80.0,
    )
    away = Team.objects.create(
        name=f'Away-{suffix}',
        slug=f'a-{suffix}-{timezone.now().timestamp()}',
        conference=league,
        rating=30.0,
    )
    return league, home, away


def _mlb_pitcher(team, name='P', rating=70.0, suffix='0'):
    from apps.mlb.models import StartingPitcher
    return StartingPitcher.objects.create(
        team=team, name=name, rating=rating,
        external_id=f'p-{suffix}-{timezone.now().timestamp()}',
    )


def _settled_game(home, away, *, first_pitch=None, hours_ago=24,
                  home_score=5, away_score=3, home_pitcher=None,
                  away_pitcher=None):
    """Build a final game with scores. first_pitch defaults to N hours ago."""
    from apps.mlb.models import Game
    if first_pitch is None:
        first_pitch = timezone.now() - timedelta(hours=hours_ago)
    return Game.objects.create(
        home_team=home, away_team=away,
        first_pitch=first_pitch,
        status='final',
        home_score=home_score, away_score=away_score,
        home_pitcher=home_pitcher, away_pitcher=away_pitcher,
    )


def _snapshot(game, *, hours_before=2, market_home_prob=0.55,
              ml_home=-140, ml_away=120, odds_source='odds_api'):
    """Build a pre-game OddsSnapshot."""
    from apps.mlb.models import OddsSnapshot
    return OddsSnapshot.objects.create(
        game=game,
        captured_at=game.first_pitch - timedelta(hours=hours_before),
        market_home_win_prob=market_home_prob,
        moneyline_home=ml_home, moneyline_away=ml_away,
        odds_source=odds_source,
        source_quality='primary' if odds_source == 'odds_api' else 'fallback',
    )


def _post_game_snapshot(game, *, hours_after=1, market_home_prob=0.99,
                       ml_home=-9999, ml_away=9999):
    """Build a POST-game OddsSnapshot — this should NEVER affect a
    replay recommendation. Used by leakage tests."""
    from apps.mlb.models import OddsSnapshot
    return OddsSnapshot.objects.create(
        game=game,
        captured_at=game.first_pitch + timedelta(hours=hours_after),
        market_home_win_prob=market_home_prob,
        moneyline_home=ml_home, moneyline_away=ml_away,
        odds_source='odds_api',
        source_quality='primary',
    )


# ---------------------------------------------------------------------------
# L1, L2, L5: Pre-game snapshot strictness + closing-odds-only-for-CLV


class NoLeakageTests(TestCase):
    """Leakage safeguards. Any failure here = production blocker."""

    def test_pregame_snapshots_filter_strictly_before_first_pitch(self):
        """L1: post-game snapshots are excluded by `captured_at < first_pitch`."""
        _, home, away = _mlb_setup('lk1')
        game = _settled_game(home, away)
        pre = _snapshot(game, hours_before=2)
        post = _post_game_snapshot(game, hours_after=1)

        snaps = _pregame_snapshots(game)
        snap_ids = {s.id for s in snaps}
        self.assertIn(pre.id, snap_ids)
        self.assertNotIn(post.id, snap_ids)

    def test_post_game_snapshot_does_not_affect_recommendation(self):
        """L1 end-to-end: simulating with and without a post-game snapshot
        must produce IDENTICAL recommendations."""
        _, home, away = _mlb_setup('lk2')
        game = _settled_game(home, away)
        _snapshot(game, hours_before=2, market_home_prob=0.55)
        # Run simulation WITHOUT post-game snapshot
        sim_before = _simulate_recommendation(game, 0.55, 'test')

        # Now add a post-game snapshot with extreme values that WOULD
        # contaminate any leakage path.
        _post_game_snapshot(game, hours_after=1, market_home_prob=0.99)
        sim_after = _simulate_recommendation(game, 0.55, 'test')

        self.assertEqual(sim_before.final_prob, sim_after.final_prob)
        self.assertEqual(sim_before.edge_pp, sim_after.edge_pp)
        self.assertEqual(sim_before.status, sim_after.status)
        self.assertEqual(sim_before.pick_side, sim_after.pick_side)
        self.assertEqual(sim_before.market_prob_pregame, sim_after.market_prob_pregame)

    def test_closing_odds_only_used_for_clv_not_recommendation(self):
        """L2: changing the CLOSING (latest pre-game) snapshot's market
        probability must NOT change the recommendation (we use OPENING
        for placement). It MAY change CLV. We pin both."""
        _, home, away = _mlb_setup('lk3')
        game = _settled_game(home, away)
        # OPENING snapshot — this drives the recommendation.
        opening = _snapshot(
            game, hours_before=4, market_home_prob=0.55,
            ml_home=-140, ml_away=120,
        )
        # CLOSING snapshot at a DIFFERENT market — this should ONLY
        # affect CLV, NEVER the recommendation.
        closing = _snapshot(
            game, hours_before=1, market_home_prob=0.70,
            ml_home=-200, ml_away=170,
        )

        sim = _simulate_recommendation(game, 0.55, 'test')
        # The market_prob the model blended with is the OPENING value.
        self.assertAlmostEqual(sim.market_prob_pregame, 0.55, places=4)
        # The placement-time moneylines are from the OPENING.
        self.assertEqual(sim.opening_moneyline_home, -140)
        self.assertEqual(sim.opening_moneyline_away, 120)
        # The closing moneylines are visible but only for CLV.
        self.assertEqual(sim.closing_moneyline_home, -200)
        self.assertEqual(sim.closing_moneyline_away, 170)
        # CLV is non-None because opening != closing.
        self.assertIsNotNone(sim.clv_decimal)

    def test_outcome_does_not_affect_recommendation(self):
        """L5: changing home_score/away_score after simulation must
        not change the simulated probability or edge."""
        _, home, away = _mlb_setup('lk4')
        game = _settled_game(home, away, home_score=5, away_score=3)
        _snapshot(game, hours_before=2)
        sim_5_3 = _simulate_recommendation(game, 0.55, 'test')

        # Flip the outcome — was home win, now away win.
        game.home_score = 1
        game.away_score = 10
        game.save()
        sim_1_10 = _simulate_recommendation(game, 0.55, 'test')

        self.assertEqual(sim_5_3.final_prob, sim_1_10.final_prob)
        self.assertEqual(sim_5_3.edge_pp, sim_1_10.edge_pp)
        self.assertEqual(sim_5_3.status, sim_1_10.status)
        # The 'won' field DOES change — that's the analytics output.
        self.assertNotEqual(sim_5_3.won, sim_1_10.won)


# ---------------------------------------------------------------------------
# L3: Pre-game Elo from history


class PregameEloFromHistoryTests(TestCase):
    """Pre-game Elo ratings come from TeamEloHistory.pre_rating, not
    from the current team.elo_rating. Critical for not leaking later-
    game outcomes into earlier-game simulations."""

    def test_pre_rating_from_history_used_when_elo_active(self):
        from apps.analytics.models import TeamEloHistory
        from apps.analytics.services.method_replay import _pregame_team_rating
        from apps.core.services.elo_service import elo_to_legacy_scale
        from django.test import override_settings

        _, home, away = _mlb_setup('elo1')
        game = _settled_game(home, away)
        # CURRENT elo_rating is high — but the pre-game history says 1500.
        home.elo_rating = 1700.0
        home.save()
        TeamEloHistory.objects.create(
            sport='mlb', mlb_team=home, mlb_game=game,
            pre_rating=1500.0, post_rating=1505.0,
            k_factor=4.0, is_home=True, won=True,
            margin=None, margin_multiplier=1.0,
        )

        with override_settings(USE_DYNAMIC_RATINGS=True):
            rating = _pregame_team_rating(home, game)

        # Must project the PRE-GAME 1500, NOT the current 1700.
        self.assertAlmostEqual(rating, elo_to_legacy_scale(1500.0), places=3)
        # Confirm that's different from the post-game current value.
        self.assertNotAlmostEqual(rating, elo_to_legacy_scale(1700.0), places=3)


# ---------------------------------------------------------------------------
# Date-window filter


class WindowFilterTests(TestCase):
    def test_date_range_includes_endpoints(self):
        from apps.mlb.models import Game

        _, h, a = _mlb_setup('w1')
        # Game today at noon local
        today = timezone.localdate()
        today_noon = timezone.make_aware(
            datetime.combine(today, datetime.min.time()) + timedelta(hours=12)
        )
        g_today = Game.objects.create(
            home_team=h, away_team=a, first_pitch=today_noon,
            status='final', home_score=5, away_score=3,
        )
        _snapshot(g_today, hours_before=2)

        # Game yesterday
        yesterday_noon = today_noon - timedelta(days=1)
        g_yesterday = Game.objects.create(
            home_team=h, away_team=a, first_pitch=yesterday_noon,
            status='final', home_score=5, away_score=3,
        )
        _snapshot(g_yesterday, hours_before=2)

        result = run_replay(today - timedelta(days=1), today, [0.55])
        self.assertEqual(result['total_games_evaluable'], 2)

    def test_games_outside_window_excluded(self):
        _, h, a = _mlb_setup('w2')
        today = timezone.localdate()
        g = _settled_game(h, a, hours_ago=24 * 90)  # 90 days ago
        _snapshot(g, hours_before=2)
        result = run_replay(today - timedelta(days=7), today, [0.55])
        self.assertEqual(result['total_games_evaluable'], 0)


# ---------------------------------------------------------------------------
# Method comparison + metrics


class MethodComparisonTests(TestCase):
    def test_higher_blend_pulls_more_picks_toward_market(self):
        """With identical game state, the 0.55 blend should produce
        probabilities closer to the market than the 0.40 blend."""
        _, h, a = _mlb_setup('mc1')
        game = _settled_game(h, a)
        _snapshot(game, market_home_prob=0.55, ml_home=-140, ml_away=120)

        sim_low = _simulate_recommendation(game, 0.40, 'low')
        sim_high = _simulate_recommendation(game, 0.55, 'high')

        # Heavier blend = final prob closer to market_home_prob.
        delta_low = abs(sim_low.final_prob - 0.55)
        delta_high = abs(sim_high.final_prob - 0.55)
        self.assertLess(delta_high, delta_low)

    def test_diff_recommendations_partitions_correctly(self):
        # Two slightly different blend weights produce two simulations
        # per game. Some may recommend in one and not the other.
        _, h, a = _mlb_setup('mc2')
        # Game 1: borderline edge — likely qualifies under 0.40 (more
        # model-aggressive) but not 0.55.
        g1 = _settled_game(h, a, home_score=5, away_score=3)
        _snapshot(g1, market_home_prob=0.55, ml_home=-140, ml_away=120)
        # Game 2: strong edge — qualifies under both.
        _, h2, a2 = _mlb_setup('mc2b')
        h2.rating = 90; h2.save()
        a2.rating = 20; a2.save()
        g2 = _settled_game(h2, a2)
        _snapshot(g2, market_home_prob=0.50, ml_home=-110, ml_away=-110)

        result = run_replay(
            timezone.localdate() - timedelta(days=2),
            timezone.localdate(),
            [0.40, 0.55],
        )
        # Diff structure populated.
        self.assertIn('diff_first_two', result)
        diff = result['diff_first_two']
        self.assertGreaterEqual(diff['a_only_count'] + diff['both_count'], 1)


# ---------------------------------------------------------------------------
# Metrics shape


class MetricsTests(TestCase):
    def test_empty_input_yields_full_zero_shape(self):
        m = _compute_metrics([])
        self.assertEqual(m['count'], 0)
        self.assertIsNone(m['win_rate'])
        self.assertIsNone(m['roi'])
        self.assertEqual(m['net_pl'], 0.0)
        # All buckets present, all zero.
        for k in ('by_tier', 'by_edge_bucket', 'by_confidence_bucket', 'by_odds_type'):
            self.assertIn(k, m)
            self.assertEqual(sum(m[k].values()), 0)

    def test_metrics_compute_for_single_win(self):
        from apps.analytics.services.method_replay import SimulatedRecommendation
        sim = SimulatedRecommendation(
            sport='mlb', game_id='1', game_label='A @ B',
            first_pitch_iso='2026-05-20T18:00:00+00:00',
            method_label='test', blend_weight=0.55,
            home_rating_pregame=70.0, away_rating_pregame=40.0,
            home_pitcher_rating=70.0, away_pitcher_rating=40.0,
            raw_score=27.0, raw_prob_pre_blend=0.7464,
            market_prob_pregame=0.55, blended_prob=0.6384,
            final_prob=0.6384,
            opening_moneyline_home=-140, opening_moneyline_away=120,
            fair_home_prob=0.5621, fair_away_prob=0.4379,
            pick_side='home', pick_odds=-140, pick_prob=0.6384,
            edge_pp=7.63,
            status='recommended', status_reason='', tier='strong',
            home_score=5, away_score=3, won=True,
            closing_moneyline_home=-150, closing_moneyline_away=130,
            clv_decimal=0.04,
        )
        m = _compute_metrics([sim])
        self.assertEqual(m['count'], 1)
        self.assertEqual(m['wins'], 1)
        self.assertEqual(m['losses'], 0)
        self.assertEqual(m['win_rate'], 100.0)
        # ROI = (payout - stake) / stake. Payout at -140 = 100 * (1 + 100/140) ≈ 171.43.
        # Net = 71.43. ROI ≈ 71.43%.
        self.assertGreater(m['roi'], 70)
        self.assertEqual(m['positive_clv_rate'], 100.0)
        self.assertEqual(m['by_tier']['strong'], 1)
        # edge 7.63 falls in '6-8' bucket
        self.assertEqual(m['by_edge_bucket']['6-8'], 1)


# ---------------------------------------------------------------------------
# Blend weight history


class HistoricalBlendWeightTests(TestCase):
    def test_recent_date_returns_current_weight(self):
        self.assertEqual(historical_blend_weight(date(2026, 5, 22)), 0.55)
        self.assertEqual(historical_blend_weight(date(2026, 6, 1)), 0.55)

    def test_intermediate_dates_return_correct_weight(self):
        self.assertEqual(historical_blend_weight(date(2026, 5, 10)), 0.40)
        self.assertEqual(historical_blend_weight(date(2026, 5, 4)), 0.30)

    def test_pre_history_returns_floor(self):
        self.assertEqual(historical_blend_weight(date(2024, 1, 1)), 0.15)


# ---------------------------------------------------------------------------
# View access control + rendering


class ViewAccessTests(TestCase):
    def test_anonymous_redirects(self):
        resp = self.client.get(reverse('analytics:method_replay'))
        self.assertEqual(resp.status_code, 302)

    def test_non_staff_forbidden(self):
        u = User.objects.create_user('regular_mr', password='x')
        c = Client()
        c.force_login(u)
        resp = c.get(reverse('analytics:method_replay'))
        self.assertEqual(resp.status_code, 403)

    def test_staff_can_access_empty_state(self):
        u = User.objects.create_user('staff_mr', password='x', is_staff=True)
        c = Client()
        c.force_login(u)
        resp = c.get(reverse('analytics:method_replay'))
        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode('utf-8')
        self.assertIn('Method Replay', body)

    def test_staff_can_access_with_data(self):
        u = User.objects.create_user('staff_mr2', password='x', is_staff=True)
        c = Client()
        c.force_login(u)
        _, h, a = _mlb_setup('v1')
        g = _settled_game(h, a)
        _snapshot(g)
        resp = c.get(reverse('analytics:method_replay') + '?range=7d')
        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode('utf-8')
        self.assertIn('Comparison', body)
