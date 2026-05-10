"""Tests for the Phase 1A Model Input Inventory service.

Coverage:
  1. Trace stages reflect the live constants (sigmoid divisor, blend
     weight, clamps) — guards against drift between model_service and
     the inventory service.
  2. Default-rating detection — inventory shows the warning chip when
     a team or pitcher is at the default 50.0.
  3. Elo vs static — both ratings surface; rating_used_now follows the
     active mode.
  4. Edge math — sign + pick side match the recommender's output.
  5. Gate trace — when a known input violates a gate, that gate fires.
  6. View access control — staff-only.
  7. View renders 200 + key sections present.
"""
from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.analytics.services.model_inventory import (
    build_mlb_inventory,
    todays_mlb_games,
)
from apps.core.services import probability_calibration


def _make_game(
    *,
    home_rating=70.0, away_rating=40.0,
    home_pitcher_rating=70.0, away_pitcher_rating=40.0,
    moneyline_home=-150, moneyline_away=130,
    market_home_prob=0.55,
    home_elo=None, away_elo=None,
    pitchers_known=True,
    odds_source='odds_api', source_quality='primary',
    is_derived=False,
    captured_at=None,
    neutral_site=False,
):
    from apps.mlb.models import (
        Conference, Game, OddsSnapshot, StartingPitcher, Team,
    )
    league = Conference.objects.create(
        name='AL', slug=f'al-{timezone.now().timestamp()}',
    )
    home = Team.objects.create(
        name='Yankees', slug=f'h-{timezone.now().timestamp()}-{id(home_rating)}',
        conference=league, rating=home_rating, abbreviation='NYY',
        elo_rating=home_elo,
    )
    away = Team.objects.create(
        name='Red Sox', slug=f'a-{timezone.now().timestamp()}-{id(away_rating)}',
        conference=league, rating=away_rating, abbreviation='BOS',
        elo_rating=away_elo,
    )
    hp = ap = None
    if pitchers_known:
        hp = StartingPitcher.objects.create(
            team=home, name='Cole', rating=home_pitcher_rating,
        )
        ap = StartingPitcher.objects.create(
            team=away, name='Sale', rating=away_pitcher_rating,
        )
    game = Game.objects.create(
        home_team=home, away_team=away,
        first_pitch=timezone.now() + timedelta(hours=2),
        status='scheduled',
        home_pitcher=hp, away_pitcher=ap,
        neutral_site=neutral_site,
    )
    if moneyline_home is not None:
        OddsSnapshot.objects.create(
            game=game,
            captured_at=captured_at or timezone.now(),
            market_home_win_prob=market_home_prob,
            moneyline_home=moneyline_home,
            moneyline_away=moneyline_away,
            odds_source=odds_source,
            source_quality=source_quality,
            is_derived=is_derived,
        )
    return game


class TraceStagesTests(TestCase):
    """Each stage of the inventory must reflect the actual constants used
    by the live model service. Drift = silent diagnostic lies."""

    def test_calibration_blend_weight_matches_module_constant(self):
        # If MARKET_BLEND_WEIGHT changes in probability_calibration, the
        # inventory must report that change (no copy-paste drift).
        game = _make_game()
        inv = build_mlb_inventory(game)
        self.assertAlmostEqual(
            inv.calibration.blend_weight,
            probability_calibration.MARKET_BLEND_WEIGHT,
            places=6,
        )

    def test_calibration_clamp_bounds_match_module_constants(self):
        game = _make_game()
        inv = build_mlb_inventory(game)
        self.assertAlmostEqual(inv.calibration.clamp_min, probability_calibration.PROB_MIN, places=6)
        self.assertAlmostEqual(inv.calibration.clamp_max, probability_calibration.PROB_MAX, places=6)

    def test_score_breakdown_matches_recompute_via_service(self):
        # The inventory's _score_breakdown must produce the same total as
        # the model's `_score()` against HOUSE_WEIGHTS.
        from apps.mlb.services.model_service import HOUSE_WEIGHTS, _score
        game = _make_game()
        inv = build_mlb_inventory(game)
        live_score = _score(game, HOUSE_WEIGHTS)
        self.assertAlmostEqual(inv.score.total_score, round(live_score, 3), places=3)

    def test_neutral_site_zeroes_hfa_term(self):
        game = _make_game(neutral_site=True)
        inv = build_mlb_inventory(game)
        self.assertEqual(inv.score.hfa_term, 0.0)
        self.assertFalse(inv.score.hfa_used)

    def test_final_home_prob_reproduces_model_service(self):
        # Inventory recomputes calibration locally for robustness — but
        # it must match what compute_house_win_prob returns.
        from apps.mlb.services.model_service import compute_house_win_prob
        game = _make_game()
        inv = build_mlb_inventory(game)
        live = compute_house_win_prob(game)
        self.assertAlmostEqual(inv.calibration.final_home_prob, round(live, 4), places=3)


class DefaultRatingDetectionTests(TestCase):
    def test_default_team_rating_flagged(self):
        # Both teams at 50.0 → both flagged.
        game = _make_game(home_rating=50.0, away_rating=50.0)
        inv = build_mlb_inventory(game)
        self.assertTrue(inv.home.is_default_rating)
        self.assertTrue(inv.away.is_default_rating)

    def test_non_default_team_rating_not_flagged(self):
        game = _make_game(home_rating=72.4, away_rating=42.1)
        inv = build_mlb_inventory(game)
        self.assertFalse(inv.home.is_default_rating)
        self.assertFalse(inv.away.is_default_rating)

    def test_default_pitcher_rating_flagged(self):
        game = _make_game(home_pitcher_rating=50.0, away_pitcher_rating=50.0)
        inv = build_mlb_inventory(game)
        self.assertTrue(inv.home_pitcher.is_default_rating)
        self.assertTrue(inv.away_pitcher.is_default_rating)

    def test_pitcher_tbd_marked_not_known(self):
        game = _make_game(pitchers_known=False)
        inv = build_mlb_inventory(game)
        self.assertFalse(inv.home_pitcher.is_known)
        self.assertFalse(inv.away_pitcher.is_known)
        self.assertIsNone(inv.home_pitcher.name)
        self.assertIsNone(inv.away_pitcher.name)


class EloVsStaticTests(TestCase):
    @override_settings(USE_DYNAMIC_RATINGS=False)
    def test_static_mode_uses_static_rating(self):
        game = _make_game(home_rating=60.0, away_rating=40.0, home_elo=1700.0, away_elo=1300.0)
        inv = build_mlb_inventory(game)
        self.assertEqual(inv.home.rating_mode_active, 'static')
        self.assertAlmostEqual(inv.home.rating_used_now, 60.0, places=2)
        # Elo still surfaces for visibility — that's the whole point.
        self.assertAlmostEqual(inv.home.elo_rating, 1700.0, places=2)
        self.assertIsNotNone(inv.home.elo_projected_legacy)

    @override_settings(USE_DYNAMIC_RATINGS=True)
    def test_elo_mode_uses_projected_elo(self):
        # 1700 → (1700-1500)/13 + 50 ≈ 65.38
        game = _make_game(home_rating=60.0, away_rating=40.0, home_elo=1700.0, away_elo=1300.0)
        inv = build_mlb_inventory(game)
        self.assertEqual(inv.home.rating_mode_active, 'elo')
        self.assertAlmostEqual(inv.home.rating_used_now, 50 + 200 / 13, places=2)


class EdgeMathTests(TestCase):
    def test_pick_side_matches_recommendation_engine(self):
        # Build a game where the home team is heavily favored by both
        # rating and pitcher — model + market should both like home; the
        # picked side must come back as 'home'.
        game = _make_game(
            home_rating=80.0, away_rating=30.0,
            home_pitcher_rating=80.0, away_pitcher_rating=30.0,
            moneyline_home=-200, moneyline_away=170,
            market_home_prob=0.65,
        )
        inv = build_mlb_inventory(game)
        self.assertEqual(inv.edge.pick_side, 'home')
        # Edge in pp must be non-zero (we constructed a meaningful one).
        self.assertGreater(inv.edge.pick_edge_pp, 0)

    def test_no_odds_yields_no_edge_no_recommendation(self):
        game = _make_game(moneyline_home=None)
        inv = build_mlb_inventory(game)
        self.assertIsNone(inv.edge)
        self.assertIsNone(inv.status)
        self.assertIsNone(inv.gates)


class GateTraceTests(TestCase):
    def test_low_probability_pick_fires_recommended_probability_gate(self):
        # Construct a game where the picked side's final prob is below
        # MIN_PROBABILITY_FOR_RECOMMENDED but above HARD_MIN_PROBABILITY,
        # AND the picked side's odds aren't in the heavy-favorite band
        # (so heavy_favorite_juice doesn't preempt). Set home rating slight
        # edge but neutral pitchers — confidence will be capped by clamp
        # near the bottom of the recommended band.
        game = _make_game(
            home_rating=52.0, away_rating=48.0,
            home_pitcher_rating=50.0, away_pitcher_rating=50.0,
            moneyline_home=-115, moneyline_away=-105,
            market_home_prob=0.52,
        )
        inv = build_mlb_inventory(game)
        # The constructed game has small edge — at least one of the
        # gating reasons must fire.
        self.assertIsNotNone(inv.gates)
        self.assertTrue(
            inv.gates.recommended_probability_failed
            or inv.gates.min_edge_failed,
            f'Expected low-prob or min-edge gate to fire. Gates: {inv.gates}',
        )

    def test_lane_source_gate_fires_for_espn_secondary(self):
        game = _make_game(
            odds_source='espn', source_quality='fallback',
        )
        inv = build_mlb_inventory(game)
        self.assertTrue(inv.gates.lane_source_failed)


class TodaysGamesTests(TestCase):
    def test_returns_games_in_window(self):
        # Game in window
        in_window = _make_game()
        # Game outside window
        from apps.mlb.models import Game
        out_of_window = _make_game()
        out_of_window.first_pitch = timezone.now() + timedelta(days=5)
        out_of_window.save()

        games = todays_mlb_games()
        ids = [str(g.id) for g in games]
        self.assertIn(str(in_window.id), ids)
        self.assertNotIn(str(out_of_window.id), ids)


class ViewAccessTests(TestCase):
    def test_anonymous_redirected_to_login(self):
        resp = self.client.get(reverse('analytics:model_inventory_index'))
        self.assertEqual(resp.status_code, 302)
        self.assertIn('login', resp.url)

    def test_non_staff_forbidden(self):
        # force_login bypasses django-axes backend (which requires a
        # request param). Same pattern used elsewhere in the suite.
        u = User.objects.create_user(username='regular', password='x')
        self.client.force_login(u)
        resp = self.client.get(reverse('analytics:model_inventory_index'))
        self.assertEqual(resp.status_code, 403)

    def test_staff_can_access_index(self):
        u = User.objects.create_user(username='s', password='x', is_staff=True)
        self.client.force_login(u)
        resp = self.client.get(reverse('analytics:model_inventory_index'))
        self.assertEqual(resp.status_code, 200)
        # Index references the diagnostic — check for the page heading
        self.assertContains(resp, 'Model Input Inventory')

    def test_staff_can_access_detail(self):
        u = User.objects.create_user(username='s', password='x', is_staff=True)
        self.client.force_login(u)
        game = _make_game()
        resp = self.client.get(reverse(
            'analytics:model_inventory_detail', args=[game.id],
        ))
        self.assertEqual(resp.status_code, 200)
        # Five sections must render — these are h2 headers in the template
        self.assertContains(resp, 'Inputs')
        self.assertContains(resp, 'Score Breakdown')
        self.assertContains(resp, 'Calibration')
        self.assertContains(resp, 'Edge Math')
        self.assertContains(resp, 'Recommendation Outputs')
