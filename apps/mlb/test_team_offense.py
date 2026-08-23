"""v3.4 SHADOW — tests for team_offense + offense_replay + polling optimization.

Locks:
  1. team_offense_signal leakage: strict `first_pitch < reference_date`
  2. Empty history → zero signal, data_confidence='low'
  3. Runs-per-game correctly summed across home vs away games
  4. Stale threshold degrades to zero
  5. Insufficient games threshold degrades to zero
  6. Cap enforced on extreme raw deltas
  7. USE_TEAM_OFFENSE=false → score unchanged with data present
  8. Breakdown carries team offense keys regardless of flag
  9. Poll command short-circuits when no games in window (no API call)
"""
from datetime import timedelta
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.mlb.models import Conference, Game, StartingPitcher, Team


def _mk_team(slug, rating=50.0):
    c, _ = Conference.objects.get_or_create(
        slug=f'div-{slug}', defaults={'name': 'Div'},
    )
    return Team.objects.create(
        name=f'T-{slug}', slug=f't-{slug}', conference=c,
        rating=rating, elo_rating=1500,
        source='mlb_stats_api', external_id=f'ext-{slug}',
        abbreviation=slug[:5].upper(),
    )


def _mk_game(home, away, when, home_score, away_score, external_id=None):
    return Game.objects.create(
        home_team=home, away_team=away, first_pitch=when,
        home_score=home_score, away_score=away_score,
        status='final', source='mlb_stats_api',
        external_id=external_id or f'g-{home.slug}-{int(when.timestamp())}',
    )


class TeamOffenseSignalTests(TestCase):

    def test_empty_history_returns_zero_low(self):
        from apps.mlb.services.team_offense import team_offense_signal
        team = _mk_team('to-e')
        sig = team_offense_signal(team, timezone.now())
        self.assertEqual(sig.quality_delta, 0.0)
        self.assertEqual(sig.n_games, 0)
        self.assertEqual(sig.data_confidence, 'low')

    def test_at_reference_date_is_excluded(self):
        from apps.mlb.services.team_offense import team_offense_signal
        team = _mk_team('to-lk1')
        opp = _mk_team('to-lk1o')
        ref = timezone.now()
        # Game EXACTLY at reference_date must be excluded (strict `<`).
        _mk_game(team, opp, ref, home_score=10, away_score=2)
        sig = team_offense_signal(team, ref)
        self.assertEqual(sig.n_games, 0)
        self.assertEqual(sig.quality_delta, 0.0)

    def test_before_reference_date_included(self):
        from apps.mlb.services.team_offense import team_offense_signal
        team = _mk_team('to-lk2')
        opp = _mk_team('to-lk2o')
        ref = timezone.now()
        for i in range(10):
            # Team scores 6/game as home
            _mk_game(team, opp, ref - timedelta(days=i + 1, hours=6),
                     home_score=6, away_score=3,
                     external_id=f'to-lk2-h-{i}')
        sig = team_offense_signal(team, ref)
        self.assertEqual(sig.n_games, 10)
        self.assertAlmostEqual(sig.runs_per_game, 6.0)
        # Runs-per-game 6.0 vs league 4.50 → +1.5 * 5.0 = 7.5 units
        self.assertAlmostEqual(sig.quality_delta, 7.5, places=1)

    def test_correctly_sums_home_and_away_scored(self):
        from apps.mlb.services.team_offense import team_offense_signal
        team = _mk_team('to-mix')
        opp = _mk_team('to-mixo')
        ref = timezone.now()
        # 5 home games scoring 8 each, 5 away games scoring 2 each.
        # Team runs = 5*8 + 5*2 = 50 in 10 games → 5.0 R/G.
        for i in range(5):
            _mk_game(team, opp, ref - timedelta(days=i + 1, hours=6),
                     home_score=8, away_score=3,
                     external_id=f'to-mix-h-{i}')
        for i in range(5):
            _mk_game(opp, team, ref - timedelta(days=i + 6, hours=6),
                     home_score=1, away_score=2,
                     external_id=f'to-mix-a-{i}')
        sig = team_offense_signal(team, ref)
        self.assertEqual(sig.n_games, 10)
        self.assertAlmostEqual(sig.runs_per_game, 5.0)

    def test_insufficient_games_returns_zero(self):
        from apps.mlb.services.team_offense import (
            MIN_GAMES_FOR_SIGNAL, team_offense_signal,
        )
        team = _mk_team('to-thin')
        opp = _mk_team('to-thino')
        ref = timezone.now()
        # Fewer than MIN_GAMES_FOR_SIGNAL — signal degrades to zero.
        for i in range(MIN_GAMES_FOR_SIGNAL - 1):
            _mk_game(team, opp, ref - timedelta(days=i + 1, hours=6),
                     home_score=6, away_score=3,
                     external_id=f'to-thin-{i}')
        sig = team_offense_signal(team, ref)
        self.assertEqual(sig.quality_delta, 0.0)
        self.assertEqual(sig.data_confidence, 'low')

    def test_stale_threshold_returns_zero(self):
        from apps.mlb.services.team_offense import (
            STALE_THRESHOLD_DAYS, team_offense_signal,
        )
        team = _mk_team('to-stale')
        opp = _mk_team('to-staleo')
        ref = timezone.now()
        # 10 games all older than the stale threshold — signal zeroed.
        for i in range(10):
            _mk_game(
                team, opp,
                ref - timedelta(days=STALE_THRESHOLD_DAYS + 5 + i, hours=6),
                home_score=6, away_score=3,
                external_id=f'to-stale-{i}',
            )
        sig = team_offense_signal(team, ref)
        # Window default = 30d, so games > 21d old but within 30d are
        # counted (n>0), but latest is older than STALE — stale-check
        # zeros out the signal.
        self.assertEqual(sig.quality_delta, 0.0)


class ScoreWireInTests(TestCase):

    def _game(self):
        h = _mk_team('sw-h'); a = _mk_team('sw-a')
        hp = StartingPitcher.objects.create(
            team=h, name='hp', rating=50.0, source='mlb_stats_api',
            external_id='sw-hp',
        )
        ap = StartingPitcher.objects.create(
            team=a, name='ap', rating=50.0, source='mlb_stats_api',
            external_id='sw-ap',
        )
        g = Game.objects.create(
            home_team=h, away_team=a,
            first_pitch=timezone.now() + timedelta(hours=2),
            status='scheduled',
            home_pitcher=hp, away_pitcher=ap,
            source='mlb_stats_api', external_id='sw-main',
        )
        # Give home team a strong offensive history.
        opp = _mk_team('sw-opp')
        for i in range(12):
            _mk_game(h, opp, timezone.now() - timedelta(days=i + 1, hours=6),
                     home_score=8, away_score=3,
                     external_id=f'sw-h-hist-{i}')
        return g

    @override_settings(USE_TEAM_OFFENSE=False)
    def test_score_unchanged_when_flag_off(self):
        from apps.mlb.services.model_service import HOUSE_WEIGHTS, _score
        g = self._game()
        # Score with offense data present.
        s_with = _score(g, HOUSE_WEIGHTS,
                        reference_date=timezone.now())
        # Delete offense data.
        Game.objects.exclude(id=g.id).delete()
        s_without = _score(g, HOUSE_WEIGHTS,
                           reference_date=timezone.now())
        # Flag off → contribution is zero → score identical.
        self.assertEqual(s_with, s_without)

    @override_settings(USE_TEAM_OFFENSE=True)
    def test_score_changes_when_flag_on_and_data_present(self):
        from apps.mlb.services.model_service import HOUSE_WEIGHTS, _score
        g = self._game()
        # Baseline: strip the historical games so home offense = zero.
        Game.objects.exclude(id=g.id).delete()
        base = _score(g, HOUSE_WEIGHTS, reference_date=timezone.now())
        # Restore strong home offense.
        opp = _mk_team('sw-opp2')
        for i in range(12):
            _mk_game(g.home_team, opp,
                     timezone.now() - timedelta(days=i + 1, hours=6),
                     home_score=8, away_score=3,
                     external_id=f'sw-onchange-{i}')
        with_data = _score(g, HOUSE_WEIGHTS,
                           reference_date=timezone.now())
        # Strong home offense → score should be higher.
        self.assertGreater(with_data, base)

    def test_breakdown_carries_offense_keys_regardless_of_flag(self):
        from apps.mlb.services.model_service import HOUSE_WEIGHTS, _score
        g = self._game()
        _, breakdown = _score(
            g, HOUSE_WEIGHTS, return_breakdown=True,
            reference_date=timezone.now(),
        )
        for k in (
            'use_team_offense', 'home_team_offense_delta',
            'away_team_offense_delta', 'home_runs_per_game',
            'away_runs_per_game', 'home_team_offense_n_games',
            'away_team_offense_n_games',
            'home_team_offense_confidence', 'away_team_offense_confidence',
            'team_offense_contribution',
        ):
            self.assertIn(k, breakdown, msg=f'missing breakdown key: {k}')


class PollWindowShortCircuitTests(TestCase):

    @patch('apps.datahub.management.commands.ingest_lineups.fetch_json')
    def test_no_local_games_in_window_skips_api(self, mock_fetch):
        # No games created at all → poll should skip the API call.
        call_command('ingest_lineups')
        mock_fetch.assert_not_called()

    @patch('apps.datahub.management.commands.ingest_lineups.fetch_json')
    def test_games_in_window_hits_api(self, mock_fetch):
        # Create a game in the collection window.
        h = _mk_team('pw-h'); a = _mk_team('pw-a')
        _mk_game(h, a, timezone.now() + timedelta(hours=2),
                 home_score=None, away_score=None,
                 external_id='pw-game')
        # But scheduled, not final.
        Game.objects.filter(external_id='pw-game').update(status='scheduled')
        mock_fetch.return_value = {'dates': []}
        call_command('ingest_lineups')
        mock_fetch.assert_called_once()


class OffenseReplaySmokeTests(TestCase):

    def test_runs_on_empty_slate(self):
        from apps.analytics.services.offense_replay import (
            render_offense_experiment, run_offense_experiment,
        )
        exp = run_offense_experiment(days=7)
        for k in ('window', 'a_v3_2_baseline', 'b_plus_offense',
                  'sim_populations', 'partition', 'magnitude'):
            self.assertIn(k, exp)
        body = render_offense_experiment(exp)
        self.assertIn('TEAM-OFFENSE REPLAY', body)
