"""Tests for the Elo rating service.

Coverage targets:
  1. Pure Elo math — symmetry, conservation, K-factor scaling.
  2. Margin multiplier — sport-aware (1.0 for MLB), cap, diminishing returns.
  3. Feature flag fallback — USE_DYNAMIC_RATINGS=False keeps legacy behavior.
  4. Feature flag use — flag on + elo_rating set → projected scale flows
     into the model_service.
  5. process_game — applies update + writes history rows + idempotent.
  6. reset_sport — wipes only the targeted sport's state.
  7. Rebuild idempotence — same input data → same final ratings.
  8. Update idempotence — running twice produces zero new rows.
"""
from datetime import timedelta
import math

from django.test import TestCase, override_settings
from django.utils import timezone

from apps.analytics.models import TeamEloHistory
from apps.core.services.elo_service import (
    ELO_BASELINE,
    HFA_ELO,
    INITIAL_RATING,
    K_FACTORS,
    MAX_MARGIN,
    elo_to_legacy_scale,
    expected_win_prob,
    margin_multiplier,
    process_game,
    reset_sport,
    team_rating_for_model,
    update_ratings,
)


class ExpectedWinProbTests(TestCase):
    """Standard Elo expected-score behavior."""

    def test_equal_ratings_no_hfa_is_50pct(self):
        self.assertAlmostEqual(expected_win_prob(1500, 1500, 0), 0.5, places=4)

    def test_400_point_advantage_is_about_91pct(self):
        # Classical Elo property: 400-point gap ≈ 10:1 odds.
        self.assertAlmostEqual(expected_win_prob(1900, 1500, 0), 10.0 / 11.0, places=4)
        self.assertAlmostEqual(expected_win_prob(1500, 1900, 0), 1.0 / 11.0, places=4)

    def test_hfa_boosts_home_team(self):
        # HFA of 65 (CFB) bumps home team's expected score above 50%.
        no_hfa = expected_win_prob(1500, 1500, 0)
        with_hfa = expected_win_prob(1500, 1500, 65)
        self.assertGreater(with_hfa, no_hfa)
        self.assertLess(with_hfa, 0.65)  # but not by a huge margin


class MarginMultiplierTests(TestCase):
    """Sport-aware margin handling."""

    def test_mlb_returns_one_regardless_of_margin(self):
        # MLB intentionally ignores margin — long-season variance.
        self.assertEqual(margin_multiplier(1, 0, 'mlb'), 1.0)
        self.assertEqual(margin_multiplier(20, 200, 'mlb'), 1.0)
        self.assertEqual(margin_multiplier(0, 0, 'mlb'), 1.0)

    def test_college_baseball_returns_one(self):
        self.assertEqual(margin_multiplier(15, 100, 'college_baseball'), 1.0)

    def test_cfb_diminishing_returns(self):
        # ln(margin+1) shape: 7→14 should be smaller jump than 1→7.
        m_1 = margin_multiplier(1, 0, 'cfb')
        m_7 = margin_multiplier(7, 0, 'cfb')
        m_14 = margin_multiplier(14, 0, 'cfb')
        self.assertGreater(m_7 - m_1, m_14 - m_7)

    def test_cfb_caps_at_max_margin(self):
        # Margin > MAX_MARGIN clips to MAX_MARGIN, so larger margins
        # produce identical multipliers.
        cap = MAX_MARGIN['cfb']
        at_cap = margin_multiplier(cap, 0, 'cfb')
        well_over = margin_multiplier(cap * 5, 0, 'cfb')
        self.assertAlmostEqual(at_cap, well_over, places=6)

    def test_underdog_win_amplifies_multiplier(self):
        # Same margin, but the multiplier is larger when the underdog
        # wins (rating_diff_for_winner < 0).
        margin = 7
        favorite_wins = margin_multiplier(margin, 200, 'cfb')   # +200 = winner was favorite
        underdog_wins = margin_multiplier(margin, -200, 'cfb')  # -200 = winner was underdog
        self.assertGreater(underdog_wins, favorite_wins)

    def test_zero_margin_returns_one(self):
        # Tie defensively maps to 1.0 (no multiplier effect).
        self.assertEqual(margin_multiplier(0, 100, 'cfb'), 1.0)


class UpdateRatingsTests(TestCase):
    """Conservation + sport-aware behavior."""

    def test_zero_sum_conservation(self):
        # Elo updates are symmetric — home delta == -away delta.
        new_h, new_a, delta, _ = update_ratings(
            1500, 1500, home_won=True, margin=7, sport='cfb', neutral_site=True,
        )
        self.assertAlmostEqual((new_h - 1500), -(new_a - 1500), places=6)
        self.assertAlmostEqual(new_h - 1500, delta, places=6)

    def test_equal_ratings_neutral_home_win_increases_home(self):
        new_h, new_a, _, _ = update_ratings(
            1500, 1500, home_won=True, margin=7, sport='cfb', neutral_site=True,
        )
        self.assertGreater(new_h, 1500)
        self.assertLess(new_a, 1500)

    def test_hfa_dampens_home_gain_on_win(self):
        # When HFA is active, the home team is "expected" to win more
        # often, so a home win produces a SMALLER rating gain than the
        # same win at a neutral site.
        gain_neutral, _, _, _ = update_ratings(
            1500, 1500, home_won=True, margin=3, sport='cfb', neutral_site=True,
        )
        gain_home, _, _, _ = update_ratings(
            1500, 1500, home_won=True, margin=3, sport='cfb', neutral_site=False,
        )
        self.assertGreater(gain_neutral, gain_home)

    def test_mlb_uses_only_winloss_not_margin(self):
        # MLB: same K, same teams, same outcome — margin should not
        # affect the rating delta at all.
        _, _, delta_blowout, _ = update_ratings(
            1500, 1500, home_won=True, margin=15, sport='mlb', neutral_site=True,
        )
        _, _, delta_squeaker, _ = update_ratings(
            1500, 1500, home_won=True, margin=1, sport='mlb', neutral_site=True,
        )
        self.assertAlmostEqual(delta_blowout, delta_squeaker, places=6)

    def test_cfb_uses_margin(self):
        # CFB: bigger margin = bigger delta (within the cap).
        _, _, delta_close, _ = update_ratings(
            1500, 1500, home_won=True, margin=3, sport='cfb', neutral_site=True,
        )
        _, _, delta_blowout, _ = update_ratings(
            1500, 1500, home_won=True, margin=21, sport='cfb', neutral_site=True,
        )
        self.assertGreater(delta_blowout, delta_close)

    def test_loss_decreases_home_rating(self):
        new_h, new_a, _, _ = update_ratings(
            1500, 1500, home_won=False, margin=7, sport='cfb', neutral_site=True,
        )
        self.assertLess(new_h, 1500)
        self.assertGreater(new_a, 1500)

    def test_k_factor_scales_delta(self):
        # MLB K=4 is much smaller than CFB K=20, so a same-result update
        # produces a smaller magnitude delta in MLB.
        _, _, delta_cfb, _ = update_ratings(
            1500, 1500, home_won=True, margin=1, sport='cfb', neutral_site=True,
        )
        _, _, delta_mlb, _ = update_ratings(
            1500, 1500, home_won=True, margin=1, sport='mlb', neutral_site=True,
        )
        self.assertGreater(abs(delta_cfb), abs(delta_mlb))


class ScaleConversionTests(TestCase):
    def test_baseline_elo_maps_to_legacy_baseline(self):
        # 1500 elo -> 50 legacy (matches Team.rating default).
        self.assertAlmostEqual(elo_to_legacy_scale(1500), 50.0, places=4)

    def test_strong_team_maps_to_above_50(self):
        # Post-2026-04-28 calibration: divisor 25 → 13, so a 200-point Elo
        # gap projects to 200/13 ≈ 15.38 legacy points (was 8 before).
        self.assertAlmostEqual(elo_to_legacy_scale(1700), 50 + 200 / 13, places=3)

    def test_weak_team_maps_to_below_50(self):
        self.assertAlmostEqual(elo_to_legacy_scale(1300), 50 - 200 / 13, places=3)


class FeatureFlagFallbackTests(TestCase):
    """team_rating_for_model gating — requirement #4 of the original Phase 2 spec."""

    def setUp(self):
        # Build a minimal CFB team. Slug must be unique even within the
        # SQLite in-memory test DB.
        from apps.cfb.models import Conference, Team
        self.conference = Conference.objects.create(
            name='Test Conf', slug=f'tc-{timezone.now().timestamp()}',
        )
        self.team = Team.objects.create(
            name='T', slug=f't-{id(self)}',
            conference=self.conference, rating=55.0,
        )

    @override_settings(USE_DYNAMIC_RATINGS=False)
    def test_flag_off_returns_static_rating(self):
        # Even with elo_rating set, flag-off keeps legacy behavior.
        self.team.elo_rating = 1700.0
        self.team.save()
        self.assertAlmostEqual(team_rating_for_model(self.team), 55.0)

    @override_settings(USE_DYNAMIC_RATINGS=True)
    def test_flag_on_with_no_elo_falls_back_to_static(self):
        # Flag flipped but team hasn't been rebuilt yet — fall back to
        # static rating rather than producing a hybrid.
        self.team.elo_rating = None
        self.team.save()
        self.assertAlmostEqual(team_rating_for_model(self.team), 55.0)

    @override_settings(USE_DYNAMIC_RATINGS=True)
    def test_flag_on_with_elo_uses_projection(self):
        self.team.elo_rating = 1700.0
        self.team.save()
        # Post-2026-04-28: 1700 → (1700 - 1500) / 13 + 50 ≈ 65.38.
        self.assertAlmostEqual(
            team_rating_for_model(self.team), 50 + 200 / 13, places=3,
        )

    @override_settings(USE_DYNAMIC_RATINGS=True)
    def test_flag_on_with_elo_at_baseline_matches_legacy_default(self):
        # 1500 → 50, which is exactly the default Team.rating value —
        # so a freshly-rebuilt team produces identical model output to
        # an unrebuilt one with default rating.
        self.team.elo_rating = 1500.0
        self.team.save()
        self.assertAlmostEqual(team_rating_for_model(self.team), 50.0)


class ProcessGameTests(TestCase):
    """End-to-end persistence: process_game writes history + updates teams."""

    def _make_settled_mlb_game(self, *, home_score=5, away_score=3):
        from apps.mlb.models import Conference, Game, Team
        league = Conference.objects.create(
            name='AL', slug=f'al-{timezone.now().timestamp()}',
        )
        home = Team.objects.create(
            name='Home', slug=f'h-{id(home_score)}', conference=league,
        )
        away = Team.objects.create(
            name='Away', slug=f'a-{id(away_score)}', conference=league,
        )
        game = Game.objects.create(
            home_team=home, away_team=away,
            first_pitch=timezone.now() - timedelta(days=1),
            status='final', home_score=home_score, away_score=away_score,
        )
        return game

    def test_process_game_updates_both_teams_and_writes_two_history_rows(self):
        game = self._make_settled_mlb_game()
        self.assertTrue(process_game('mlb', game))
        game.home_team.refresh_from_db()
        game.away_team.refresh_from_db()
        self.assertIsNotNone(game.home_team.elo_rating)
        self.assertIsNotNone(game.away_team.elo_rating)
        # MLB winner should rise, loser should fall.
        self.assertGreater(game.home_team.elo_rating, INITIAL_RATING)
        self.assertLess(game.away_team.elo_rating, INITIAL_RATING)
        # One history row per team for this game.
        self.assertEqual(
            TeamEloHistory.objects.filter(sport='mlb', mlb_game=game).count(),
            2,
        )

    def test_process_game_is_idempotent(self):
        # Calling twice is a no-op the second time — idempotence is the
        # foundation of update_elo_ratings safety on cron.
        game = self._make_settled_mlb_game()
        self.assertTrue(process_game('mlb', game))
        self.assertFalse(process_game('mlb', game))
        self.assertEqual(
            TeamEloHistory.objects.filter(sport='mlb', mlb_game=game).count(),
            2,
        )

    def test_process_game_skips_ties(self):
        game = self._make_settled_mlb_game(home_score=4, away_score=4)
        self.assertFalse(process_game('mlb', game))
        self.assertEqual(TeamEloHistory.objects.count(), 0)

    def test_process_game_skips_missing_scores(self):
        game = self._make_settled_mlb_game()
        game.home_score = None
        game.save()
        self.assertFalse(process_game('mlb', game))

    def test_history_record_no_margin_for_mlb(self):
        # MLB doesn't use margin in its update — history reflects that.
        game = self._make_settled_mlb_game(home_score=10, away_score=2)
        process_game('mlb', game)
        history = TeamEloHistory.objects.filter(sport='mlb', mlb_game=game)
        for row in history:
            self.assertIsNone(row.margin)
            self.assertEqual(row.margin_multiplier, 1.0)


class ResetSportTests(TestCase):
    def test_reset_only_targets_one_sport(self):
        from apps.cfb.models import Conference as CFBConf, Team as CFBTeam
        from apps.mlb.models import Conference as MLBConf, Team as MLBTeam

        cfb_conf = CFBConf.objects.create(name='B12', slug=f'b12-{timezone.now().timestamp()}')
        cfb_team = CFBTeam.objects.create(
            name='T', slug=f'cfbt-{id(self)}', conference=cfb_conf,
        )
        cfb_team.elo_rating = 1700
        cfb_team.save()

        mlb_conf = MLBConf.objects.create(name='AL', slug=f'al-{timezone.now().timestamp()}')
        mlb_team = MLBTeam.objects.create(
            name='T', slug=f'mlbt-{id(self)}', conference=mlb_conf,
        )
        mlb_team.elo_rating = 1600
        mlb_team.save()

        cleared = reset_sport('cfb')
        self.assertGreaterEqual(cleared, 1)

        cfb_team.refresh_from_db()
        mlb_team.refresh_from_db()
        self.assertIsNone(cfb_team.elo_rating)
        self.assertEqual(mlb_team.elo_rating, 1600)  # untouched


class RebuildIdempotenceTests(TestCase):
    """Calling rebuild twice produces the same final ratings."""

    def _setup_two_mlb_games(self):
        from apps.mlb.models import Conference, Game, Team
        league = Conference.objects.create(
            name='X', slug=f'x-{timezone.now().timestamp()}',
        )
        a = Team.objects.create(name='A', slug=f'aa-{id(self)}', conference=league)
        b = Team.objects.create(name='B', slug=f'bb-{id(self)}', conference=league)
        # Two games, chronological; second has the same teams in opposite order.
        now = timezone.now()
        Game.objects.create(
            home_team=a, away_team=b,
            first_pitch=now - timedelta(days=2),
            status='final', home_score=5, away_score=3,
        )
        Game.objects.create(
            home_team=b, away_team=a,
            first_pitch=now - timedelta(days=1),
            status='final', home_score=2, away_score=4,
        )
        return a, b

    def test_rebuild_twice_yields_same_final_ratings(self):
        from django.core.management import call_command
        a, b = self._setup_two_mlb_games()

        call_command('rebuild_elo_ratings', '--sport', 'mlb', verbosity=0)
        a.refresh_from_db(); b.refresh_from_db()
        first_a, first_b = a.elo_rating, b.elo_rating
        first_history_count = TeamEloHistory.objects.filter(sport='mlb').count()

        call_command('rebuild_elo_ratings', '--sport', 'mlb', verbosity=0)
        a.refresh_from_db(); b.refresh_from_db()
        self.assertAlmostEqual(a.elo_rating, first_a, places=6)
        self.assertAlmostEqual(b.elo_rating, first_b, places=6)
        # History row count is identical — no doubling-up.
        self.assertEqual(
            TeamEloHistory.objects.filter(sport='mlb').count(),
            first_history_count,
        )

    def test_update_after_rebuild_is_no_op(self):
        from django.core.management import call_command
        self._setup_two_mlb_games()

        call_command('rebuild_elo_ratings', '--sport', 'mlb', verbosity=0)
        before = TeamEloHistory.objects.filter(sport='mlb').count()

        call_command('update_elo_ratings', '--sport', 'mlb', verbosity=0)
        after = TeamEloHistory.objects.filter(sport='mlb').count()
        self.assertEqual(before, after)


class ModelServiceIntegrationTests(TestCase):
    """The four sport model_services pick up Elo via team_rating_for_model.

    Smoke-level: verify that flipping the flag actually changes the
    house probability when elo_rating differs from rating, and that with
    the flag off the value is identical to before the integration.
    """

    def _make_cfb_game(self):
        from apps.cfb.models import Conference, Team, Game
        conf = Conference.objects.create(name='X', slug=f'x-{timezone.now().timestamp()}')
        h = Team.objects.create(
            name='H', slug=f'cfh-{id(self)}', conference=conf, rating=55.0,
        )
        a = Team.objects.create(
            name='A', slug=f'cfa-{id(self)}', conference=conf, rating=45.0,
        )
        game = Game.objects.create(
            home_team=h, away_team=a,
            kickoff=timezone.now() + timedelta(hours=2),
            neutral_site=True,  # remove HFA so the comparison is clean
        )
        return game, h, a

    @override_settings(USE_DYNAMIC_RATINGS=False)
    def test_flag_off_uses_static_rating(self):
        from apps.cfb.services.model_service import _compute_win_prob, HOUSE_WEIGHTS
        game, h, a = self._make_cfb_game()
        # Set elo to something very different from the static rating —
        # if the flag is off it must be ignored.
        h.elo_rating = 2000  # huge — would project to 70 on legacy scale
        a.elo_rating = 1000  # huge — would project to 30 on legacy scale
        h.save(); a.save()

        prob = _compute_win_prob(game, [], HOUSE_WEIGHTS)
        # With static ratings 55 vs 45, this is a known value. Flag-off
        # path must be identical to pre-Elo behavior — here we just
        # verify it's NOT close to the dramatic 90%+ that the dynamic
        # ratings would produce.
        self.assertLess(prob, 0.85)

    @override_settings(USE_DYNAMIC_RATINGS=True)
    def test_flag_on_uses_elo_when_present(self):
        from apps.cfb.services.model_service import _compute_win_prob, HOUSE_WEIGHTS
        game, h, a = self._make_cfb_game()
        h.elo_rating = 2000  # projects to 70 on legacy scale
        a.elo_rating = 1000  # projects to 30 on legacy scale
        h.save(); a.save()

        prob = _compute_win_prob(game, [], HOUSE_WEIGHTS)
        # 40-point legacy-scale gap (70-30) feeds a sigmoid(40/15) ≈ 0.935.
        # That's well above the static-rating prob (~0.66 for 55 vs 45)
        # so the dynamic ratings clearly dominate when the flag is on.
        self.assertGreater(prob, 0.90)

    @override_settings(USE_DYNAMIC_RATINGS=True)
    def test_flag_on_falls_back_when_no_elo(self):
        # Flag flipped, team has no Elo yet — uses static rating.
        from apps.cfb.services.model_service import _compute_win_prob, HOUSE_WEIGHTS
        game, h, a = self._make_cfb_game()
        # No elo_rating set — both None.
        prob_no_elo = _compute_win_prob(game, [], HOUSE_WEIGHTS)

        # Compare against flag-off — must be identical.
        with override_settings(USE_DYNAMIC_RATINGS=False):
            prob_flag_off = _compute_win_prob(game, [], HOUSE_WEIGHTS)
        self.assertAlmostEqual(prob_no_elo, prob_flag_off, places=6)
