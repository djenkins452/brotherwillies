"""MLB tests covering schema, prediction model, and provider normalization.

Uses no network: the schedule provider's normalize() is tested against a
hand-built sample payload matching statsapi.mlb.com's shape.
"""
from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from apps.mlb.models import Conference, Game, StartingPitcher, Team
from apps.mlb.services.model_service import (
    HOUSE_MODEL_VERSION,
    compute_data_confidence,
    compute_game_data,
    compute_house_win_prob,
)


def _mk_team(name='Yankees', rating=50.0, ext='147'):
    conf, _ = Conference.objects.get_or_create(slug='al-east', defaults={'name': 'AL East'})
    return Team.objects.create(
        name=name, slug=name.lower(), conference=conf,
        rating=rating, source='mlb_stats_api', external_id=ext,
    )


def _mk_pitcher(team, name='Ace', rating=80.0, ext='p1'):
    return StartingPitcher.objects.create(
        team=team, name=name, rating=rating,
        source='mlb_stats_api', external_id=ext,
        era=2.5, whip=1.0, k_per_9=9.0,
    )


class MLBSmokeTests(TestCase):
    def test_app_installed(self):
        from django.apps import apps
        self.assertTrue(apps.is_installed('apps.mlb'))


class MLBPitcherStatsWinLossTests(TestCase):
    """Verify the pitcher_stats provider parses + persists W/L."""

    def test_normalize_extracts_wins_and_losses(self):
        from apps.datahub.providers.mlb.pitcher_stats_provider import (
            MLBPitcherStatsProvider,
        )
        provider = MLBPitcherStatsProvider.__new__(MLBPitcherStatsProvider)
        raw = [{
            'id': 99999,
            'pitchHand': {'code': 'R'},
            'stats': [{
                'group': {'displayName': 'pitching'},
                'splits': [{'stat': {
                    'era': '2.15', 'whip': '0.92',
                    'strikeoutsPer9Inn': '11.4',
                    'inningsPitched': '25.0',
                    'wins': 3, 'losses': 1,
                }}],
            }],
        }]
        records = provider.normalize(raw)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]['wins'], 3)
        self.assertEqual(records[0]['losses'], 1)

    def test_persist_writes_wins_and_losses_to_pitcher(self):
        from apps.datahub.providers.mlb.pitcher_stats_provider import (
            MLBPitcherStatsProvider,
        )
        provider = MLBPitcherStatsProvider.__new__(MLBPitcherStatsProvider)
        team = _mk_team('Dodgers', 55.0, '119')
        StartingPitcher.objects.create(
            team=team, name='Shohei Ohtani', source='mlb_stats_api',
            external_id='660271',
        )
        provider.persist([{
            'external_id': '660271',
            'throws': 'L',
            'era': 2.15, 'whip': 0.92, 'k_per_9': 11.4,
            'innings_pitched': 25.0,
            'wins': 3, 'losses': 1,
        }])
        p = StartingPitcher.objects.get(external_id='660271')
        self.assertEqual(p.wins, 3)
        self.assertEqual(p.losses, 1)

    def test_normalize_handles_missing_wins_losses(self):
        from apps.datahub.providers.mlb.pitcher_stats_provider import (
            MLBPitcherStatsProvider,
        )
        provider = MLBPitcherStatsProvider.__new__(MLBPitcherStatsProvider)
        raw = [{
            'id': 12345,
            'pitchHand': {'code': 'L'},
            'stats': [{
                'group': {'displayName': 'pitching'},
                'splits': [{'stat': {
                    'era': '3.00', 'whip': '1.10',
                    'strikeoutsPer9Inn': '9.0',
                    'inningsPitched': '10.0',
                    # No wins / losses keys
                }}],
            }],
        }]
        records = provider.normalize(raw)
        self.assertEqual(len(records), 1)
        self.assertIsNone(records[0]['wins'])
        self.assertIsNone(records[0]['losses'])


class MLBTeamRecordProviderTests(TestCase):
    """Verify the team_record provider parses /v1/standings and upserts."""

    def test_normalize_extracts_records_per_team(self):
        from apps.datahub.providers.mlb.team_record_provider import (
            MLBTeamRecordProvider,
        )
        provider = MLBTeamRecordProvider.__new__(MLBTeamRecordProvider)
        raw = [{
            'teamRecords': [
                {'team': {'id': 119}, 'wins': 14, 'losses': 7},
                {'team': {'id': 109}, 'wins': 9, 'losses': 12},
            ],
        }]
        records = provider.normalize(raw)
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0]['external_id'], '119')
        self.assertEqual(records[0]['wins'], 14)
        self.assertEqual(records[0]['losses'], 7)

    def test_persist_upserts_wins_and_losses_to_team(self):
        from apps.datahub.providers.mlb.team_record_provider import (
            MLBTeamRecordProvider,
        )
        provider = MLBTeamRecordProvider.__new__(MLBTeamRecordProvider)
        team = _mk_team('Dodgers', 55.0, '119')
        provider.persist([{'external_id': '119', 'wins': 14, 'losses': 7}])
        team.refresh_from_db()
        self.assertEqual(team.wins, 14)
        self.assertEqual(team.losses, 7)

    def test_persist_skips_unknown_team(self):
        from apps.datahub.providers.mlb.team_record_provider import (
            MLBTeamRecordProvider,
        )
        provider = MLBTeamRecordProvider.__new__(MLBTeamRecordProvider)
        result = provider.persist([{'external_id': '9999', 'wins': 1, 'losses': 1}])
        self.assertEqual(result['updated'], 0)
        self.assertEqual(result['skipped'], 1)


class MLBPredictionModelTests(TestCase):
    def test_equal_teams_equal_pitchers_neutral_site_is_5050(self):
        home = _mk_team('Home', 50.0, '1')
        away = _mk_team('Away', 50.0, '2')
        hp = _mk_pitcher(home, 'H', 50.0, 'p1')
        ap = _mk_pitcher(away, 'A', 50.0, 'p2')
        g = Game.objects.create(
            home_team=home, away_team=away,
            first_pitch=timezone.now() + timedelta(hours=1),
            neutral_site=True,
            home_pitcher=hp, away_pitcher=ap,
            source='mlb_stats_api', external_id='g1',
        )
        p = compute_house_win_prob(g)
        self.assertAlmostEqual(p, 0.5, places=3)

    def test_pitcher_advantage_drives_probability(self):
        home = _mk_team('Home', 50.0, '1')
        away = _mk_team('Away', 50.0, '2')
        hp = _mk_pitcher(home, 'Ace', 95.0, 'p1')
        ap = _mk_pitcher(away, 'Bum', 15.0, 'p2')
        g = Game.objects.create(
            home_team=home, away_team=away,
            first_pitch=timezone.now() + timedelta(hours=1),
            neutral_site=True,
            home_pitcher=hp, away_pitcher=ap,
            source='mlb_stats_api', external_id='g1',
        )
        # 0.65 * (95 - 15) = 52 -> sigmoid(52/15) = ~0.97
        p = compute_house_win_prob(g)
        self.assertGreater(p, 0.95)

    def test_missing_pitcher_drops_confidence_to_low(self):
        home = _mk_team('Home', 90.0, '1')
        away = _mk_team('Away', 10.0, '2')
        g = Game.objects.create(
            home_team=home, away_team=away,
            first_pitch=timezone.now() + timedelta(hours=1),
            home_pitcher=None, away_pitcher=None,
            source='mlb_stats_api', external_id='g1',
        )
        # Confidence low regardless of other factors
        self.assertEqual(compute_data_confidence(g), 'low')
        # And pitcher_diff = 0, so only team rating + HFA drives prob.
        # 0.35 * (90-10) = 28, +2.5 HFA = 30.5 -> sigmoid(30.5/15) ~= 0.88
        p = compute_house_win_prob(g)
        self.assertGreater(p, 0.85)
        self.assertLess(p, 0.95)

    def test_compute_game_data_shape(self):
        home = _mk_team('Home', 60.0, '1')
        away = _mk_team('Away', 40.0, '2')
        hp = _mk_pitcher(home, 'H', 70.0, 'p1')
        ap = _mk_pitcher(away, 'A', 50.0, 'p2')
        g = Game.objects.create(
            home_team=home, away_team=away,
            first_pitch=timezone.now() + timedelta(hours=1),
            home_pitcher=hp, away_pitcher=ap,
            source='mlb_stats_api', external_id='g1',
        )
        d = compute_game_data(g)
        for key in ['market_prob', 'house_prob', 'house_edge', 'confidence',
                    'confidence_class', 'model_version', 'line_movement', 'injuries']:
            self.assertIn(key, d, f'missing key: {key}')
        self.assertEqual(d['model_version'], HOUSE_MODEL_VERSION)


class MLBScheduleProviderNormalizeTests(TestCase):
    """Verify normalize() shape against a minimal statsapi payload."""

    SAMPLE_GAME = {
        'gamePk': 12345,
        'gameDate': '2026-04-19T17:35:00Z',
        'status': {'detailedState': 'Scheduled'},
        'teams': {
            'home': {
                'team': {'id': 147, 'name': 'New York Yankees', 'abbreviation': 'NYY',
                         'division': {'name': 'American League East'}},
                'score': None,
                'probablePitcher': {'id': 9001, 'fullName': 'Ace Hurler'},
            },
            'away': {
                'team': {'id': 118, 'name': 'Kansas City Royals', 'abbreviation': 'KC',
                         'division': {'name': 'American League Central'}},
                'score': None,
                'probablePitcher': {'id': 9002, 'fullName': 'Rookie Arm'},
            },
        },
    }

    def test_normalize_extracts_game_fields(self):
        from apps.datahub.providers.mlb.schedule_provider import MLBScheduleProvider
        with patch.object(MLBScheduleProvider, '__init__', return_value=None):
            p = MLBScheduleProvider()
            rec_list = p.normalize([self.SAMPLE_GAME])
        self.assertEqual(len(rec_list), 1)
        r = rec_list[0]
        self.assertEqual(r['external_id'], '12345')
        self.assertEqual(r['detailed_state'], 'Scheduled')
        self.assertEqual(r['home_team']['id'], 147)
        self.assertEqual(r['away_team']['id'], 118)
        self.assertEqual(r['home_pitcher']['id'], 9001)
        self.assertEqual(r['away_pitcher']['id'], 9002)

    def test_normalize_skips_bad_rows(self):
        from apps.datahub.providers.mlb.schedule_provider import MLBScheduleProvider
        with patch.object(MLBScheduleProvider, '__init__', return_value=None):
            p = MLBScheduleProvider()
            # Missing team IDs should be dropped
            out = p.normalize([{'gameDate': '2026-04-19T00:00:00Z', 'teams': {}}])
        self.assertEqual(out, [])


class MLBPitcherRatingTests(TestCase):
    def test_elite_pitcher_gets_high_rating(self):
        from apps.datahub.providers.mlb.pitcher_stats_provider import compute_pitcher_rating
        r = compute_pitcher_rating(era=2.0, whip=0.95, k_per_9=11.0)
        self.assertGreater(r, 75)

    def test_bad_pitcher_gets_low_rating(self):
        from apps.datahub.providers.mlb.pitcher_stats_provider import compute_pitcher_rating
        r = compute_pitcher_rating(era=8.0, whip=1.8, k_per_9=5.0)
        self.assertLess(r, 25)

    def test_missing_stat_returns_none(self):
        from apps.datahub.providers.mlb.pitcher_stats_provider import compute_pitcher_rating
        self.assertIsNone(compute_pitcher_rating(era=None, whip=1.0, k_per_9=8.0))
        self.assertIsNone(compute_pitcher_rating(era=3.0, whip=None, k_per_9=8.0))
        self.assertIsNone(compute_pitcher_rating(era=3.0, whip=1.0, k_per_9=None))
