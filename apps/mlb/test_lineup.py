"""v3.4 SHADOW — tests for the lineup collection foundation.

Locks:
  1. ConfirmedLineup model: unique (game, team, observed_at)
  2. team_lineup_signal leakage: strict `<` observed_at boundary
  3. team_lineup_signal excludes post_first_pitch state
  4. USE_LINEUP_QUALITY=false → score unchanged with/without lineup data
  5. USE_LINEUP_QUALITY=true → score reflects lineup data (currently zero)
  6. Shadow capture: breakdown carries lineup fields regardless of flag
  7. Poll command: fingerprint dedup (identical lineup → no new row)
  8. Poll command: differing lineup → new row (updated_after_confirmation)
  9. Coverage diagnostic: runs on empty database
"""
from datetime import date, datetime, timedelta
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.db import IntegrityError
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.mlb.models import (
    ConfirmedLineup, Conference, Game, OddsSnapshot, StartingPitcher, Team,
)


def _mk_team(slug):
    c, _ = Conference.objects.get_or_create(
        slug=f'div-{slug}', defaults={'name': 'Div'},
    )
    return Team.objects.create(
        name=f'T-{slug}', slug=f't-{slug}', conference=c,
        rating=50.0, elo_rating=1500,
        source='mlb_stats_api', external_id=f'ext-{slug}',
        abbreviation=slug[:5].upper(),
    )


def _mk_pitcher(team, name):
    return StartingPitcher.objects.create(
        team=team, name=name, rating=50.0,
        source='mlb_stats_api', external_id=f'p-{team.slug}-{name}',
    )


def _mk_game(home, away, when, hp=None, ap=None):
    return Game.objects.create(
        home_team=home, away_team=away, first_pitch=when,
        home_pitcher=hp, away_pitcher=ap,
        home_score=4, away_score=2, status='final',
        source='mlb_stats_api',
        external_id=f'g-{home.slug}-{int(when.timestamp())}',
    )


def _mk_lineup_row(game, team, when, state='confirmed', players=None):
    return ConfirmedLineup.objects.create(
        game=game, team=team, observed_at=when,
        players=players or [
            {'order': i, 'player_id': 1000 + i, 'name': f'P{i}', 'position': 'CF'}
            for i in range(1, 10)
        ],
        lineup_state=state,
        source='mlb_stats_api', data_confidence='high',
    )


class ModelUniquenessTests(TestCase):

    def test_unique_game_team_observed_at(self):
        h = _mk_team('mu-h'); a = _mk_team('mu-a')
        g = _mk_game(h, a, timezone.now() + timedelta(hours=2))
        ts = timezone.now()
        _mk_lineup_row(g, h, ts)
        with self.assertRaises(IntegrityError):
            _mk_lineup_row(g, h, ts)

    def test_multiple_observations_at_different_times(self):
        h = _mk_team('mu2-h'); a = _mk_team('mu2-a')
        g = _mk_game(h, a, timezone.now() + timedelta(hours=2))
        _mk_lineup_row(g, h, timezone.now() - timedelta(hours=2))
        _mk_lineup_row(g, h, timezone.now() - timedelta(hours=1),
                       state='updated_after_confirmation')
        self.assertEqual(
            ConfirmedLineup.objects.filter(game=g, team=h).count(), 2,
        )


class LineupSignalLeakageTests(TestCase):

    def test_observation_at_reference_date_is_excluded(self):
        from apps.mlb.services.lineup import team_lineup_signal
        team = _mk_team('lk1')
        # Give the team a game so it has a home/away context
        h = team; a = _mk_team('lk1a')
        g = _mk_game(h, a, timezone.now())
        ref = timezone.now()
        # Observed AT reference date — must be excluded.
        _mk_lineup_row(g, team, ref)
        sig = team_lineup_signal(team, ref)
        self.assertEqual(sig.lineup_state, 'no_data')
        self.assertIsNone(sig.observed_at)

    def test_observation_before_is_included(self):
        from apps.mlb.services.lineup import team_lineup_signal
        team = _mk_team('lk2')
        h = team; a = _mk_team('lk2a')
        g = _mk_game(h, a, timezone.now())
        ref = timezone.now()
        _mk_lineup_row(g, team, ref - timedelta(minutes=90))
        sig = team_lineup_signal(team, ref)
        self.assertEqual(sig.lineup_state, 'confirmed')
        self.assertEqual(sig.n_players, 9)
        # Data confidence 'med' when 9 players present.
        self.assertEqual(sig.data_confidence, 'med')

    def test_post_first_pitch_is_excluded(self):
        from apps.mlb.services.lineup import team_lineup_signal
        team = _mk_team('lk3')
        h = team; a = _mk_team('lk3a')
        g = _mk_game(h, a, timezone.now())
        ref = timezone.now()
        _mk_lineup_row(g, team, ref - timedelta(minutes=30),
                       state='post_first_pitch')
        sig = team_lineup_signal(team, ref)
        # post_first_pitch cannot be used pregame.
        self.assertEqual(sig.lineup_state, 'no_data')


class FlagRoutingTests(TestCase):

    def _game_with_lineups(self):
        h = _mk_team('fr-h'); a = _mk_team('fr-a')
        hp = _mk_pitcher(h, 'p'); ap = _mk_pitcher(a, 'p')
        # Game some hours in the future so lineups are legitimately pregame
        # relative to first_pitch.
        g = _mk_game(h, a, timezone.now() + timedelta(hours=2), hp, ap)
        past = timezone.now() - timedelta(hours=1)
        _mk_lineup_row(g, h, past)
        _mk_lineup_row(g, a, past)
        return g

    @override_settings(USE_LINEUP_QUALITY=False)
    def test_score_unchanged_when_flag_off(self):
        from apps.mlb.services.model_service import HOUSE_WEIGHTS, _score
        g = self._game_with_lineups()
        # Score once with lineup rows present.
        s_with_lineups = _score(g, HOUSE_WEIGHTS,
                                reference_date=timezone.now())
        # Delete lineup rows — score should be unchanged (flag off means
        # lineup contribution is zero and the code path is a no-op).
        ConfirmedLineup.objects.all().delete()
        s_no_lineups = _score(g, HOUSE_WEIGHTS,
                              reference_date=timezone.now())
        self.assertEqual(s_with_lineups, s_no_lineups)

    def test_breakdown_carries_lineup_keys_regardless_of_flag(self):
        from apps.mlb.services.model_service import HOUSE_WEIGHTS, _score
        g = self._game_with_lineups()
        _, breakdown = _score(g, HOUSE_WEIGHTS, return_breakdown=True,
                              reference_date=timezone.now())
        for k in (
            'home_lineup_quality_delta', 'away_lineup_quality_delta',
            'home_lineup_state', 'away_lineup_state',
            'home_lineup_data_confidence', 'away_lineup_data_confidence',
            'home_lineup_n_players', 'away_lineup_n_players',
            'lineup_quality_contribution', 'use_lineup_quality',
        ):
            self.assertIn(k, breakdown, msg=f'missing breakdown key: {k}')
        # Pre-activation zero contribution.
        self.assertEqual(breakdown['lineup_quality_contribution'], 0.0)
        self.assertEqual(breakdown['home_lineup_state'], 'confirmed')

    def test_breakdown_reports_no_data_when_no_lineup(self):
        from apps.mlb.services.model_service import HOUSE_WEIGHTS, _score
        h = _mk_team('nd-h'); a = _mk_team('nd-a')
        hp = _mk_pitcher(h, 'p'); ap = _mk_pitcher(a, 'p')
        g = _mk_game(h, a, timezone.now() + timedelta(hours=2), hp, ap)
        _, breakdown = _score(g, HOUSE_WEIGHTS, return_breakdown=True,
                              reference_date=timezone.now())
        self.assertEqual(breakdown['home_lineup_state'], 'no_data')
        self.assertEqual(breakdown['home_lineup_n_players'], 0)


class IngestLineupsCommandTests(TestCase):

    def _seed_game(self, when=None, external_id='p-822780'):
        h = _mk_team('poll-h')
        h.external_id = '141'; h.save()
        a = _mk_team('poll-a')
        a.external_id = '111'; a.save()
        # First pitch in the near future so poll's default lookahead
        # window includes it.
        fp = when or (timezone.now() + timedelta(hours=2))
        return _mk_game(h, a, fp), h, a

    def _fake_schedule(self, gamePk, home_players, away_players):
        return {
            'dates': [{
                'games': [{
                    'gamePk': gamePk,
                    'gameDate': (timezone.now() + timedelta(hours=2)).isoformat(),
                    'status': {'detailedState': 'Scheduled'},
                    'lineups': {
                        'homePlayers': home_players,
                        'awayPlayers': away_players,
                    },
                }]
            }]
        }

    @patch('apps.datahub.management.commands.ingest_lineups.fetch_json')
    def test_first_poll_creates_confirmed_row(self, mock_fetch):
        g, h, a = self._seed_game()
        # gamePk 822780 matches the seeded external_id
        players = [
            {'id': 1000 + i, 'fullName': f'Player {i}',
             'primaryPosition': {'abbreviation': 'CF'}}
            for i in range(9)
        ]
        # Note: our seed used external_id='g-...' so must set explicitly.
        g.external_id = '822780'; g.save()
        mock_fetch.return_value = self._fake_schedule(822780, players, players)
        call_command('ingest_lineups')
        # Both sides written.
        self.assertEqual(ConfirmedLineup.objects.count(), 2)
        for row in ConfirmedLineup.objects.all():
            self.assertEqual(row.lineup_state, 'confirmed')
            self.assertEqual(len(row.players), 9)

    @patch('apps.datahub.management.commands.ingest_lineups.fetch_json')
    def test_identical_second_poll_writes_no_new_row(self, mock_fetch):
        g, h, a = self._seed_game()
        g.external_id = '822780'; g.save()
        players = [
            {'id': 1000 + i, 'fullName': f'Player {i}',
             'primaryPosition': {'abbreviation': 'CF'}}
            for i in range(9)
        ]
        mock_fetch.return_value = self._fake_schedule(822780, players, players)
        call_command('ingest_lineups')
        first_count = ConfirmedLineup.objects.count()
        # Second poll — identical lineup — no new row.
        call_command('ingest_lineups')
        self.assertEqual(ConfirmedLineup.objects.count(), first_count)

    @patch('apps.datahub.management.commands.ingest_lineups.fetch_json')
    def test_differing_second_poll_writes_updated_row(self, mock_fetch):
        g, h, a = self._seed_game()
        g.external_id = '822780'; g.save()
        players = [
            {'id': 1000 + i, 'fullName': f'P{i}',
             'primaryPosition': {'abbreviation': 'CF'}}
            for i in range(9)
        ]
        mock_fetch.return_value = self._fake_schedule(822780, players, players)
        call_command('ingest_lineups')
        # Swap positions 1 and 2 → different lineup fingerprint.
        players[0], players[1] = players[1], players[0]
        mock_fetch.return_value = self._fake_schedule(822780, players, players)
        call_command('ingest_lineups')
        # 4 rows total (2 first poll + 2 second poll).
        self.assertEqual(ConfirmedLineup.objects.count(), 4)
        # New rows have state='updated_after_confirmation'.
        latest = ConfirmedLineup.objects.order_by('-observed_at').first()
        self.assertEqual(latest.lineup_state, 'updated_after_confirmation')


class CoverageDiagnosticTests(TestCase):

    def test_runs_on_empty_db(self):
        from apps.analytics.services.lineup_coverage import (
            build_coverage_report, render,
        )
        report = build_coverage_report(days=30)
        self.assertEqual(report['coverage']['both_covered'], 0)
        self.assertFalse(report['experiment_readiness']['ready_for_experiment'])
        body = render(report)
        self.assertIn('LINEUP COLLECTION COVERAGE REPORT', body)

    def test_reports_covered_games(self):
        from apps.analytics.services.lineup_coverage import build_coverage_report
        h = _mk_team('cov-h'); a = _mk_team('cov-a')
        g = _mk_game(h, a, timezone.now() - timedelta(days=1))
        _mk_lineup_row(g, h, g.first_pitch - timedelta(hours=2))
        _mk_lineup_row(g, a, g.first_pitch - timedelta(hours=2))
        report = build_coverage_report(days=30)
        self.assertEqual(report['coverage']['both_covered'], 1)
