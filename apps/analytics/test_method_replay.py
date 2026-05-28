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


class LaneCorrectedReplayTests(TestCase):
    """The 2026-05-22 lane-corrected replay extension.

    Critical because the prior replay overcounted recommendations
    (compute_status only, no lane filter). The corrected version
    matches production's auto-recommended population (status=
    'recommended' AND lane='core').
    """

    def test_simulation_now_emits_lane_and_corrected_flag(self):
        _, h, a = _mlb_setup('lc1')
        game = _settled_game(h, a)
        _snapshot(game)
        sim = _simulate_recommendation(game, 0.55, 'test')
        # New fields exist and are populated.
        self.assertIn(sim.lane, ('core', 'qualified', 'pass'))
        self.assertIsInstance(sim.risk_flags, dict)
        self.assertIsInstance(sim.risk_score, int)
        self.assertIsInstance(sim.is_lane_corrected_recommended, bool)
        # If status is 'recommended', the corrected flag iff lane='core'.
        if sim.status == 'recommended':
            self.assertEqual(
                sim.is_lane_corrected_recommended,
                sim.lane == 'core',
            )

    def test_short_fav_thin_cannot_demote_replay_recommended_picks(self):
        """STRUCTURAL: short_fav_thin fires when edge < 6pp, but the
        replay's compute_status already enforces edge >= MIN_EDGE=6pp.
        So this flag cannot fire on a replay-recommended pick. Confirms
        my §1 audit math: this is the most common lane demoter in
        live ops but is structurally unreachable in the corrected
        replay's recommended set."""
        _, h, a = _mlb_setup('lc2')
        # Build a short-fav scenario: home favored at -130, but
        # rating gap big enough to clear MIN_PROBABILITY + MIN_EDGE.
        # Score: 70*0.35 + 2.5 = 27. sigmoid(27/25) ≈ 0.7464.
        # Blended 0.55: 0.7464*0.45 + 0.55*0.55 = 0.638.
        # ml -130/+110: fair_home ≈ 0.5428. Edge ~9.5pp.
        h.rating = 90; h.save()
        a.rating = 20; a.save()
        game = _settled_game(h, a)
        _snapshot(game, ml_home=-130, ml_away=+110, market_home_prob=0.55)

        sim = _simulate_recommendation(game, 0.55, 'test')
        if sim.status == 'recommended':
            # If recommended, short_fav_thin flag must NOT fire because
            # edge >= MIN_EDGE = 6.0 means edge_decimal >= 0.06, which
            # is NOT strictly less than 0.06 (the threshold).
            self.assertFalse(
                (sim.risk_flags or {}).get('short_fav_thin', False),
                f'short_fav_thin fired on a replay-recommended pick. '
                f'Math should make this impossible. risk_flags={sim.risk_flags}, '
                f'edge_pp={sim.edge_pp}',
            )

    def test_movement_signal_pregame_filters_post_game_snapshots(self):
        """L1 SAFEGUARD: the pre-game movement signal helper must
        exclude post-game snapshots from the history window."""
        from apps.analytics.services.method_replay import _pregame_movement_signal

        _, h, a = _mlb_setup('lc3')
        game = _settled_game(h, a)
        # Pre-game snapshots with clear directional movement.
        for hours_before in (10, 6, 3, 1):
            _snapshot(
                game,
                hours_before=hours_before,
                ml_home=-140 + (hours_before * 5),  # moving toward home (less negative)
                ml_away=120 - (hours_before * 4),
            )
        # POST-game snapshot with extreme reversal — must be ignored.
        _post_game_snapshot(
            game, hours_after=1,
            ml_home=+9999, ml_away=-9999,
        )

        sig_with_post = _pregame_movement_signal(game, 'home')
        # The signal exists (we have multiple pre-game snapshots).
        # Now remove the post-game snapshot and confirm the signal is
        # unchanged (proving post-game wasn't influencing it).
        from apps.mlb.models import OddsSnapshot
        OddsSnapshot.objects.filter(
            game=game, captured_at__gte=game.first_pitch,
        ).delete()
        sig_without_post = _pregame_movement_signal(game, 'home')

        self.assertEqual(
            sig_with_post['movement_class'],
            sig_without_post['movement_class'],
        )
        self.assertEqual(
            sig_with_post['movement_score'],
            sig_without_post['movement_score'],
        )

    def test_lane_corrected_count_is_subset_of_uncorrected(self):
        """The lane-corrected recommended set is always a SUBSET of the
        uncorrected (compute_status-only) set, by construction. Any
        pick lane='core' implies status='recommended' (lane hard gates
        share gates with compute_status), and pick that fails lane
        demotion just gets dropped from the corrected set."""
        # Build a slate of games — some clean, some with extreme odds
        # that may push picks into qualified/pass lanes.
        for i in range(5):
            _, h, a = _mlb_setup(f'lcsub{i}')
            game = _settled_game(h, a)
            _snapshot(game, ml_home=-140, ml_away=120)

        today = timezone.localdate()
        result = run_replay(
            today - timedelta(days=2), today, [0.55],
        )
        v = result['variants'][0]
        # The corrected count <= the uncorrected count.
        self.assertLessEqual(v['lane_corrected_count'], v['recommended_count'])

    def test_demoted_by_flag_categorizes_excluded_picks(self):
        """The demoted_by_flag dict surfaces which risk flags demoted
        recommendations from core to qualified, broken out by flag.
        Useful for operator-readable explanations."""
        _, h, a = _mlb_setup('demo1')
        game = _settled_game(h, a)
        _snapshot(game)

        today = timezone.localdate()
        result = run_replay(today - timedelta(days=2), today, [0.55])
        v = result['variants'][0]
        # Shape is stable: demoted_by_flag is a dict (possibly empty).
        self.assertIsInstance(v['demoted_by_flag'], dict)
        # demoted_count = recommended_count - lane_corrected_count.
        self.assertEqual(
            v['demoted_count'],
            v['recommended_count'] - v['lane_corrected_count'],
        )

    def test_metrics_corrected_uses_lane_filtered_population(self):
        """`metrics_corrected` is computed over `is_lane_corrected_recommended`
        sims. `metrics` is over the uncorrected (status='recommended')
        sims. They should differ when there are demotions."""
        # Build a game with edge well above MIN_EDGE so it's recommended.
        _, h, a = _mlb_setup('m1')
        h.rating = 95; h.save()
        a.rating = 5; a.save()
        game = _settled_game(h, a, home_score=7, away_score=2)
        _snapshot(game, ml_home=-120, ml_away=+100, market_home_prob=0.50)

        today = timezone.localdate()
        result = run_replay(today - timedelta(days=2), today, [0.55])
        v = result['variants'][0]
        # Both metric dicts exist.
        self.assertIn('count', v['metrics'])
        self.assertIn('count', v['metrics_corrected'])
        # Sanity: counts are consistent with the recommended/corrected counts.
        self.assertEqual(v['metrics']['count'], v['recommended_count'])
        self.assertEqual(v['metrics_corrected']['count'], v['lane_corrected_count'])

    def test_diff_first_two_corrected_uses_lane_filter(self):
        """The corrected diff between two variants uses lane-corrected
        recommended sets. Output structure mirrors the uncorrected diff."""
        for i in range(3):
            _, h, a = _mlb_setup(f'dfc{i}')
            game = _settled_game(h, a)
            _snapshot(game)

        today = timezone.localdate()
        result = run_replay(today - timedelta(days=2), today, [0.40, 0.55])
        # Both diffs present.
        self.assertIn('diff_first_two', result)
        self.assertIn('diff_first_two_corrected', result)
        # Both have the same shape.
        for key in ('a_only_count', 'b_only_count', 'both_count'):
            self.assertIn(key, result['diff_first_two'])
            self.assertIn(key, result['diff_first_two_corrected'])

    def test_lane_classification_uses_pregame_movement_signal(self):
        """Verify the lane classifier sees the pre-game movement signal,
        not the live timezone.now()-anchored signal. Tests integration
        of `_pregame_movement_signal` into the lane pipeline."""
        _, h, a = _mlb_setup('lpms')
        game = _settled_game(h, a)
        # Single snapshot — too few for movement signal to fire.
        _snapshot(game)

        sim = _simulate_recommendation(game, 0.55, 'test')
        # With < 2 snapshots, movement signal is empty → movement_class=None.
        self.assertIsNone(sim.movement_class)
        self.assertFalse(sim.movement_supports_pick)


class ProductionEquivalenceAssertionTests(TestCase):
    """The corrected replay is supposed to be production-equivalent.
    This locks the structural contract that the lane-corrected
    recommended population matches what `_moneyline_candidate` +
    `is_bulk_moneyline_eligible` would produce."""

    def test_lane_corrected_passes_is_bulk_moneyline_eligible_predicate(self):
        """Every lane-corrected recommendation in the replay should also
        pass `is_bulk_moneyline_eligible` (the canonical production
        predicate). The two-lane system makes them equivalent by design."""
        from unittest.mock import MagicMock
        from apps.mockbets.services.bulk_actions import is_bulk_moneyline_eligible

        _, h, a = _mlb_setup('peq1')
        h.rating = 92; h.save()
        a.rating = 8; a.save()
        game = _settled_game(h, a)
        _snapshot(game, ml_home=-130, ml_away=+110, market_home_prob=0.55)

        sim = _simulate_recommendation(game, 0.55, 'test')
        if sim.is_lane_corrected_recommended:
            # Build a mock recommendation-like object that mirrors
            # what `is_bulk_moneyline_eligible` consumes.
            fake = MagicMock()
            fake.status = sim.status
            fake.lane = sim.lane
            fake.tier = sim.tier
            fake.status_reason = sim.status_reason
            fake.confidence_score = sim.pick_prob * 100.0
            fake.odds_american = sim.pick_odds
            fake.is_secondary = False
            self.assertTrue(
                is_bulk_moneyline_eligible(fake, source_filter='verified'),
                f'lane-corrected sim should pass is_bulk_moneyline_eligible, '
                f'but didn\'t. sim={sim}',
            )


class BlendExperimentTests(TestCase):
    """0.40 vs 0.55 on the EXACT SAME slate. Read-only counterfactual.

    The contract that matters: same game population for both weights, only
    the blend weight differs, and the delta is computed b − a.
    """

    def _build_slate(self):
        """A handful of settled games with pre-game snapshots, all within
        the last few days so they fall inside every test window."""
        for i in range(4):
            _, h, a = _mlb_setup(f'be{i}')
            h.rating = 88; h.save()
            a.rating = 14; a.save()
            g = _settled_game(
                h, a, hours_ago=24 + i * 6,
                home_score=(6 if i % 2 == 0 else 2),
                away_score=(2 if i % 2 == 0 else 6),
            )
            _snapshot(g, market_home_prob=0.55, ml_home=-135, ml_away=115)

    def test_perf_helper_wl_roi_clv_mix(self):
        from apps.analytics.services.method_replay import (
            _perf, SimulatedRecommendation,
        )

        def _sim(won, odds, clv):
            return SimulatedRecommendation(
                sport='mlb', game_id='x', game_label='A @ B',
                first_pitch_iso='2026-05-20T18:00:00+00:00',
                method_label='t', blend_weight=0.55,
                home_rating_pregame=70.0, away_rating_pregame=40.0,
                home_pitcher_rating=70.0, away_pitcher_rating=40.0,
                raw_score=27.0, raw_prob_pre_blend=0.74,
                market_prob_pregame=0.55, blended_prob=0.64,
                final_prob=0.64,
                opening_moneyline_home=odds, opening_moneyline_away=120,
                fair_home_prob=0.56, fair_away_prob=0.44,
                pick_side='home', pick_odds=odds, pick_prob=0.64,
                edge_pp=7.0, status='recommended', status_reason='', tier='strong',
                home_score=5, away_score=3, won=won,
                closing_moneyline_home=-150, closing_moneyline_away=130,
                clv_decimal=clv,
            )

        sims = [
            _sim(True, -140, 0.05),    # win, beat
            _sim(False, -140, 0.0),    # loss, matched
            _sim(True, +120, -0.03),   # win, lost
        ]
        p = _perf(sims)
        self.assertEqual(p['count'], 3)
        self.assertEqual(p['wins'], 2)
        self.assertEqual(p['losses'], 1)
        self.assertEqual(p['clv_beat'], 1)
        self.assertEqual(p['clv_matched'], 1)
        self.assertEqual(p['clv_lost'], 1)

    def test_experiment_structure_and_delta(self):
        from apps.analytics.services.method_replay import run_blend_experiment

        self._build_slate()
        exp = run_blend_experiment(
            blend_a=0.40, blend_b=0.55, windows=(7,),
            min_games_for_window=2,
        )
        self.assertEqual(exp['blend_a'], 0.40)
        self.assertEqual(exp['blend_b'], 0.55)
        self.assertEqual(len(exp['windows']), 1)
        wr = exp['windows'][0]
        self.assertEqual(wr['days'], 7)
        self.assertIn('metrics', wr['a'])
        self.assertIn('buckets', wr['b'])
        self.assertIn('by_odds_type', wr['a']['buckets'])
        self.assertIn('by_confidence_bucket', wr['b']['buckets'])
        self.assertIn('favorites', wr['a']['buckets'])
        # Delta is b − a on a numeric field.
        am = wr['a']['metrics']
        bm = wr['b']['metrics']
        if am['roi'] is not None and bm['roi'] is not None:
            self.assertAlmostEqual(
                wr['delta']['roi'], round(bm['roi'] - am['roi'], 4), places=2,
            )

    def test_same_population_both_weights(self):
        """Both weights must evaluate the identical game set — only the
        blend differs. Game-evaluable count is shared; recommended counts
        may differ but neither variant can exceed the slate size."""
        from apps.analytics.services.method_replay import run_blend_experiment
        self._build_slate()
        exp = run_blend_experiment(
            blend_a=0.40, blend_b=0.55, windows=(7,), min_games_for_window=2,
        )
        wr = exp['windows'][0]
        self.assertGreaterEqual(wr['games_evaluable'], 1)
        self.assertLessEqual(wr['a']['metrics']['count'], wr['games_evaluable'])
        self.assertLessEqual(wr['b']['metrics']['count'], wr['games_evaluable'])

    def test_render_blend_experiment_text(self):
        from apps.analytics.services.method_replay import (
            run_blend_experiment, render_blend_experiment,
        )
        self._build_slate()
        exp = run_blend_experiment(
            blend_a=0.40, blend_b=0.55, windows=(7, 14), min_games_for_window=2,
        )
        txt = render_blend_experiment(exp)
        self.assertIn('BLEND EXPERIMENT', txt)
        self.assertIn('WINDOW: 7 days', txt)
        self.assertIn('Δ (b−a)', txt)
        self.assertIn('BY ODDS BUCKET', txt)
        self.assertIn('BY CONFIDENCE BUCKET', txt)
        self.assertIn('FAVORITE vs DOG', txt)

    def test_thin_data_flag(self):
        from apps.analytics.services.method_replay import run_blend_experiment
        # No games built → every window is thin.
        exp = run_blend_experiment(
            blend_a=0.40, blend_b=0.55, windows=(7,), min_games_for_window=20,
        )
        self.assertFalse(exp['windows'][0]['data_ok'])


class BlendExperimentViewTests(TestCase):
    def test_experiment_mode_staff_plaintext(self):
        u = User.objects.create_user('staff_be', password='x', is_staff=True)
        c = Client()
        c.force_login(u)
        resp = c.get(reverse('analytics:method_replay') + '?experiment=blend&windows=7,14')
        self.assertEqual(resp.status_code, 200)
        self.assertIn('text/plain', resp['Content-Type'])
        body = resp.content.decode('utf-8')
        self.assertIn('BLEND EXPERIMENT', body)

    def test_experiment_mode_non_staff_forbidden(self):
        u = User.objects.create_user('reg_be', password='x')
        c = Client()
        c.force_login(u)
        resp = c.get(reverse('analytics:method_replay') + '?experiment=blend')
        self.assertEqual(resp.status_code, 403)


class BlendExperimentProductionShapeTests(TestCase):
    """Regression tests for the production 500 on ?experiment=blend.

    The local unit tests originally used a small, uniform slate that never
    exercised: (a) multi-snapshot movement signals, (b) Elo history rows,
    (c) the full 30/60-day windows, (d) graceful degradation when a single
    game's simulation raises. These tests reproduce production-shaped data
    and lock the hardening (per-game isolation + simulate-once-and-slice +
    staff diagnostic capture)."""

    def _team_pair(self, s, hr=85, ar=20):
        from apps.mlb.models import Conference, Team
        c = Conference.objects.create(
            name=f'C{s}', slug=f'c{s}-{timezone.now().timestamp()}')
        h = Team.objects.create(
            name=f'H{s}', slug=f'h{s}-{timezone.now().timestamp()}',
            conference=c, rating=hr, elo_rating=1560)
        a = Team.objects.create(
            name=f'A{s}', slug=f'a{s}-{timezone.now().timestamp()}',
            conference=c, rating=ar, elo_rating=1440)
        return h, a

    def _game(self, h, a, days_ago, hs, as_, neutral=False):
        from apps.mlb.models import Game
        fp = timezone.now() - timedelta(days=days_ago)
        return Game.objects.create(
            home_team=h, away_team=a, first_pitch=fp, status='final',
            home_score=hs, away_score=as_, neutral_site=neutral)

    def _snap(self, g, hours_before, mlh, mla, mhp=0.55):
        from apps.mlb.models import OddsSnapshot
        return OddsSnapshot.objects.create(
            game=g, captured_at=g.first_pitch - timedelta(hours=hours_before),
            market_home_win_prob=mhp, moneyline_home=mlh, moneyline_away=mla,
            odds_source='odds_api', source_quality='primary')

    def _elo(self, team, game, pre, post):
        from apps.analytics.models import TeamEloHistory
        return TeamEloHistory.objects.create(
            sport='mlb', mlb_team=team, mlb_game=game, pre_rating=pre,
            post_rating=post, k_factor=4.0,
            is_home=(team == game.home_team), won=False)

    def _build_production_shape(self, n=18):
        """Heterogeneous slate spanning 60 days: multi-snapshot movement,
        Elo history rows, neutral sites, missing pitchers, partial snapshots,
        long dogs, pushes."""
        for i in range(n):
            h, a = self._team_pair(f'ps{i}', hr=80 + (i % 10), ar=10 + (i % 15))
            hs, as_ = (6, 2) if i % 3 else (2, 6)
            if i % 9 == 0:
                hs = as_ = 4  # push/tie
            g = self._game(h, a, days_ago=2 + i * 3, hs=hs, as_=as_,
                           neutral=(i % 7 == 0))
            # Multi-snapshot → exercises _pregame_movement_signal/_per_market_signal
            self._snap(g, 18, -120 - i, 100 + i)
            self._snap(g, 6, -130 - i, 110 + i)
            if i % 5 == 0:
                self._snap(g, 4, None, None)        # partial snapshot
            self._snap(g, 1, -140 - i, 120 + i)
            self._elo(h, g, 1560 + i, 1564 + i)
            self._elo(a, g, 1440 - i, 1436 - i)

    def test_full_experiment_production_shape_does_not_crash(self):
        """The exact production call: windows 7/14/30/60 over heterogeneous
        data with Elo history + multi-snapshots. Must render, not 500."""
        from apps.analytics.services.method_replay import (
            run_blend_experiment, render_blend_experiment,
        )
        self._build_production_shape()
        exp = run_blend_experiment(
            blend_a=0.40, blend_b=0.55, windows=(7, 14, 30, 60),
            min_games_for_window=2,
        )
        txt = render_blend_experiment(exp)
        self.assertIn('BLEND EXPERIMENT', txt)
        self.assertEqual(len(exp['windows']), 4)
        self.assertEqual([w['days'] for w in exp['windows']], [7, 14, 30, 60])

    def test_view_production_shape_returns_200(self):
        from django.urls import reverse
        self._build_production_shape()
        u = User.objects.create_user('staff_ps', password='x', is_staff=True)
        c = Client()
        c.force_login(u)
        resp = c.get(reverse('analytics:method_replay') + '?experiment=blend')
        self.assertEqual(resp.status_code, 200)
        self.assertIn('text/plain', resp['Content-Type'])
        self.assertIn('BLEND EXPERIMENT', resp.content.decode('utf-8'))

    def test_nested_windows_are_subsets(self):
        """7d ⊆ 14d ⊆ 30d ⊆ 60d — sub-window games_evaluable is monotonic
        non-decreasing as the window widens (slicing correctness)."""
        from apps.analytics.services.method_replay import run_blend_experiment
        self._build_production_shape()
        exp = run_blend_experiment(
            blend_a=0.40, blend_b=0.55, windows=(7, 14, 30, 60),
            min_games_for_window=2,
        )
        counts = [w['games_evaluable'] for w in exp['windows']]
        self.assertEqual(counts, sorted(counts))  # monotonic non-decreasing
        # Recommended counts also monotonic (subset property).
        a_counts = [w['a']['metrics']['count'] for w in exp['windows']]
        self.assertEqual(a_counts, sorted(a_counts))

    def test_single_bad_game_is_skipped_not_fatal(self):
        """If _simulate_recommendation raises on one game, the experiment
        skips it (counts the error) and still renders the rest."""
        from unittest.mock import patch
        from apps.analytics.services import method_replay as mr

        self._build_production_shape(n=6)
        real = mr._simulate_recommendation
        calls = {'n': 0}

        def flaky(game, weight, label):
            calls['n'] += 1
            if calls['n'] == 2:  # blow up on the 2nd sim call
                raise ValueError('simulated bad game data')
            return real(game, weight, label)

        with patch.object(mr, '_simulate_recommendation', side_effect=flaky):
            exp = mr.run_blend_experiment(
                blend_a=0.40, blend_b=0.55, windows=(60,),
                min_games_for_window=2,
            )
        # Did not raise; error was counted.
        self.assertGreaterEqual(exp['sim_errors']['a'] + exp['sim_errors']['b'], 1)
        txt = mr.render_blend_experiment(exp)
        self.assertIn('BLEND EXPERIMENT', txt)

    def test_run_replay_skips_bad_game(self):
        """run_replay (regular path) also isolates a per-game failure."""
        from unittest.mock import patch
        from apps.analytics.services import method_replay as mr

        self._build_production_shape(n=4)
        real = mr._simulate_recommendation
        calls = {'n': 0}

        def flaky(game, weight, label):
            calls['n'] += 1
            if calls['n'] == 1:   # blow up on the first game
                raise RuntimeError('boom')
            return real(game, weight, label)

        with patch.object(mr, '_simulate_recommendation', side_effect=flaky):
            result = mr.run_replay(
                timezone.localdate() - timedelta(days=60),
                timezone.localdate() - timedelta(days=1),
                [0.55],
            )
        # Did not raise; the variant records sim_errors.
        self.assertGreaterEqual(result['variants'][0]['sim_errors'], 1)

    def test_view_captures_exception_as_diagnostic(self):
        """If run_blend_experiment raises (non-timeout), the staff view
        returns the traceback as plaintext rather than an opaque 500."""
        from unittest.mock import patch
        from django.urls import reverse

        u = User.objects.create_user('staff_diag', password='x', is_staff=True)
        c = Client()
        c.force_login(u)
        with patch(
            'apps.analytics.services.method_replay.run_blend_experiment',
            side_effect=RuntimeError('kaboom in experiment'),
        ):
            resp = c.get(reverse('analytics:method_replay') + '?experiment=blend')
        # View did not propagate an uncaught 500; it returned the diagnostic.
        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode('utf-8')
        self.assertIn('STAFF DIAGNOSTIC', body)
        self.assertIn('kaboom in experiment', body)

    def test_empty_windows_degrade_gracefully(self):
        """No games at all → every window renders with zeros, no crash."""
        from apps.analytics.services.method_replay import (
            run_blend_experiment, render_blend_experiment,
        )
        exp = run_blend_experiment(
            blend_a=0.40, blend_b=0.55, windows=(7, 14, 30, 60),
            min_games_for_window=20,
        )
        txt = render_blend_experiment(exp)
        self.assertIn('THIN DATA', txt)
        for w in exp['windows']:
            self.assertEqual(w['a']['metrics']['count'], 0)


def _make_sim(*, pick_odds, won=True, clv=0.02):
    """Minimal SimulatedRecommendation factory for filter/bucket unit tests."""
    from apps.analytics.services.method_replay import SimulatedRecommendation
    return SimulatedRecommendation(
        sport='mlb', game_id='x', game_label='A @ B',
        first_pitch_iso='2026-05-20T18:00:00+00:00',
        method_label='0.55', blend_weight=0.55,
        home_rating_pregame=70.0, away_rating_pregame=40.0,
        home_pitcher_rating=70.0, away_pitcher_rating=40.0,
        raw_score=27.0, raw_prob_pre_blend=0.74,
        market_prob_pregame=0.55, blended_prob=0.64, final_prob=0.64,
        opening_moneyline_home=pick_odds, opening_moneyline_away=120,
        fair_home_prob=0.56, fair_away_prob=0.44,
        pick_side='home', pick_odds=pick_odds, pick_prob=0.64,
        edge_pp=7.0, status='recommended', status_reason='', tier='strong',
        lane='core', is_lane_corrected_recommended=True,
        home_score=5, away_score=3, won=won,
        closing_moneyline_home=-150, closing_moneyline_away=130,
        clv_decimal=clv,
    )


class FavoritesExperimentTests(TestCase):
    """0.55 standard (A) vs 0.55 + favorites-only (B). Read-only. Only the
    underdog-allowed flag differs; B is strictly a subset of A."""

    def test_is_favorite_only_filter(self):
        from apps.analytics.services.method_replay import _is_favorite_only
        self.assertTrue(_is_favorite_only(_make_sim(pick_odds=-150)))
        self.assertTrue(_is_favorite_only(_make_sim(pick_odds=99)))   # +99 = fav
        self.assertFalse(_is_favorite_only(_make_sim(pick_odds=100)))  # +100 = dog
        self.assertFalse(_is_favorite_only(_make_sim(pick_odds=130)))

    def test_favorite_subrange_classification(self):
        from apps.analytics.services.method_replay import _favorite_subrange_performance
        sims = [
            _make_sim(pick_odds=-220),   # heavy
            _make_sim(pick_odds=-170),   # mid
            _make_sim(pick_odds=-120),   # short
            _make_sim(pick_odds=-100),   # short
            _make_sim(pick_odds=140),    # underdog
        ]
        r = _favorite_subrange_performance(sims)
        self.assertEqual(r['heavy_fav (≤ -200)']['count'], 1)
        self.assertEqual(r['mid_fav (-150..-199)']['count'], 1)
        self.assertEqual(r['short_fav (-149..+99)']['count'], 2)
        self.assertEqual(r['underdog (≥ +100)']['count'], 1)

    def _team_pair(self, s, hr, ar):
        from apps.mlb.models import Conference, Team
        c = Conference.objects.create(
            name=f'C{s}', slug=f'c{s}-{timezone.now().timestamp()}')
        h = Team.objects.create(
            name=f'H{s}', slug=f'h{s}-{timezone.now().timestamp()}',
            conference=c, rating=hr, elo_rating=1500)
        a = Team.objects.create(
            name=f'A{s}', slug=f'a{s}-{timezone.now().timestamp()}',
            conference=c, rating=ar, elo_rating=1500)
        return h, a

    def _game(self, h, a, days_ago, hs, as_):
        from apps.mlb.models import Game
        return Game.objects.create(
            home_team=h, away_team=a,
            first_pitch=timezone.now() - timedelta(days=days_ago),
            status='final', home_score=hs, away_score=as_)

    def _snap(self, g, hours, mlh, mla, mhp):
        from apps.mlb.models import OddsSnapshot
        return OddsSnapshot.objects.create(
            game=g, captured_at=g.first_pitch - timedelta(hours=hours),
            market_home_win_prob=mhp, moneyline_home=mlh, moneyline_away=mla,
            odds_source='odds_api', source_quality='primary')

    def test_b_removes_recommended_underdog_pick(self):
        """A game where the model strongly favors a team the MARKET prices as
        a dog (+odds) → recommended DOG pick. B must drop it; A must keep it."""
        from apps.analytics.services.method_replay import run_favorites_experiment
        # Model loves home (rating 88 vs 14) but market prices home +130 (dog).
        h, a = self._team_pair('dog', hr=88, ar=14)
        g = self._game(h, a, days_ago=10, hs=6, as_=2)
        self._snap(g, 6, mlh=130, mla=-150, mhp=0.43)  # home is a price dog

        exp = run_favorites_experiment(
            blend=0.55, windows=(30,), min_games_for_window=1,
        )
        w = exp['windows'][0]
        # A recommended the +130 home pick; B removed it.
        if w['a']['metrics']['count'] >= 1:
            self.assertGreaterEqual(w['underdogs_removed'], 1)
            self.assertLess(w['b']['metrics']['count'], w['a']['metrics']['count'])
            self.assertTrue(
                w['a']['metrics']['count'] == 0
                or w['delta']['count'] is not None
            )

    def test_b_is_subset_no_op_when_no_underdogs(self):
        """When A holds only favorites, B == A (no-op) and delta count is 0."""
        from apps.analytics.services.method_replay import (
            run_favorites_experiment, render_favorites_experiment,
        )
        # Strong home favorite, priced as favorite (-160). Recommended FAV pick.
        h, a = self._team_pair('fav', hr=88, ar=14)
        g = self._game(h, a, days_ago=12, hs=6, as_=2)
        self._snap(g, 6, mlh=-160, mla=140, mhp=0.60)

        exp = run_favorites_experiment(
            blend=0.55, windows=(30, 60, 90), min_games_for_window=1,
        )
        w = exp['windows'][0]
        self.assertEqual(w['underdogs_removed'], 0)
        self.assertEqual(w['b']['metrics']['count'], w['a']['metrics']['count'])
        self.assertEqual(w['delta']['count'], 0)
        txt = render_favorites_experiment(exp)
        self.assertIn('FAVORITES-ONLY EXPERIMENT', txt)
        self.assertIn('NO-OP', txt)

    def test_view_favorites_mode_returns_200(self):
        from django.urls import reverse
        u = User.objects.create_user('staff_fav', password='x', is_staff=True)
        c = Client()
        c.force_login(u)
        resp = c.get(reverse('analytics:method_replay') + '?experiment=favorites')
        self.assertEqual(resp.status_code, 200)
        self.assertIn('text/plain', resp['Content-Type'])
        self.assertIn('FAVORITES-ONLY EXPERIMENT', resp.content.decode('utf-8'))

    def test_view_favorites_non_staff_forbidden(self):
        from django.urls import reverse
        u = User.objects.create_user('reg_fav', password='x')
        c = Client()
        c.force_login(u)
        resp = c.get(reverse('analytics:method_replay') + '?experiment=favorites')
        self.assertEqual(resp.status_code, 403)

    def test_view_favorites_captures_exception(self):
        from unittest.mock import patch
        from django.urls import reverse
        u = User.objects.create_user('staff_favd', password='x', is_staff=True)
        c = Client()
        c.force_login(u)
        with patch(
            'apps.analytics.services.method_replay.run_favorites_experiment',
            side_effect=RuntimeError('fav kaboom'),
        ):
            resp = c.get(reverse('analytics:method_replay') + '?experiment=favorites')
        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode('utf-8')
        self.assertIn('STAFF DIAGNOSTIC', body)
        self.assertIn('fav kaboom', body)
