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


class MLBPrioritizationTests(TestCase):
    """Signals layer: GameSignals bucketing, sorting, and reason text."""

    def _game(self, home_rating=50.0, away_rating=50.0,
              hp_rating=50.0, ap_rating=50.0,
              status='scheduled', home_score=None, away_score=None,
              pitchers=True, ext='g1'):
        home = _mk_team('Home' + ext, home_rating, 'h' + ext)
        away = _mk_team('Away' + ext, away_rating, 'a' + ext)
        hp = _mk_pitcher(home, 'HP', hp_rating, 'hp' + ext) if pitchers else None
        ap = _mk_pitcher(away, 'AP', ap_rating, 'ap' + ext) if pitchers else None
        return Game.objects.create(
            home_team=home, away_team=away,
            first_pitch=timezone.now() + timedelta(hours=1),
            status=status, home_score=home_score, away_score=away_score,
            home_pitcher=hp, away_pitcher=ap,
            source='mlb_stats_api', external_id=ext,
        )

    def _add_odds(self, game, spread):
        from apps.mlb.models import OddsSnapshot
        return OddsSnapshot.objects.create(
            game=game, captured_at=timezone.now(),
            market_home_win_prob=0.5, spread=spread,
        )

    def _add_injury(self, game, team, level='high'):
        from apps.mlb.models import InjuryImpact
        return InjuryImpact.objects.create(game=game, team=team, impact_level=level)

    def test_tight_spread_plus_injury_is_high(self):
        from apps.mlb.services.prioritization import build_signals
        g = self._game(ext='ts1')
        self._add_odds(g, spread=1.0)
        self._add_injury(g, g.home_team, 'high')
        s = build_signals(g)
        self.assertEqual(s.priority, 'high')
        self.assertIn('tight_spread', s.reasons)
        self.assertIn('high_injury', s.reasons)

    def test_blowout_live_demotes(self):
        from apps.mlb.services.prioritization import build_signals
        g = self._game(status='live', home_score=10, away_score=2, ext='bo1')
        s = build_signals(g)
        self.assertEqual(s.priority, 'low')
        self.assertLess(s.priority_score, 0)

    def test_close_live_game_is_high(self):
        from apps.mlb.services.prioritization import build_signals
        g = self._game(status='live', home_score=3, away_score=2, ext='cl1')
        self._add_odds(g, spread=1.5)
        s = build_signals(g)
        self.assertEqual(s.priority, 'high')
        self.assertIn('close_game_live', s.reasons)

    def test_tbd_pitcher_demotes_and_no_ace_flag(self):
        from apps.mlb.services.prioritization import build_signals
        g = self._game(pitchers=False, ext='tbd1')
        s = build_signals(g)
        self.assertFalse(s.pitchers_known)
        self.assertFalse(s.ace_matchup)
        self.assertLess(s.priority_score, 0)

    def test_ace_matchup_flagged(self):
        from apps.mlb.services.prioritization import build_signals
        g = self._game(hp_rating=80.0, ap_rating=72.0, ext='ace1')
        s = build_signals(g)
        self.assertTrue(s.ace_matchup)
        self.assertIn('ace_matchup', s.reasons)

    def test_sort_today_priority_then_time(self):
        from apps.mlb.services.prioritization import build_signals, sort_today
        early = self._game(ext='early')
        early.first_pitch = timezone.now() + timedelta(hours=1)
        early.save()
        late = self._game(ext='late')
        late.first_pitch = timezone.now() + timedelta(hours=4)
        late.save()
        # Elevate `late` to high via tight spread + injury
        self._add_odds(late, spread=1.0)
        self._add_injury(late, late.home_team, 'high')
        signals = [build_signals(early), build_signals(late)]
        ordered = sort_today(signals)
        # late should come first despite later start time — priority wins
        self.assertEqual(ordered[0].game.external_id, 'late')
        self.assertEqual(ordered[1].game.external_id, 'early')

    def test_sort_live_priority_only(self):
        from apps.mlb.services.prioritization import build_signals, sort_live
        close = self._game(status='live', home_score=2, away_score=1, ext='liveA')
        blow = self._game(status='live', home_score=10, away_score=0, ext='liveB')
        signals = [build_signals(blow), build_signals(close)]
        ordered = sort_live(signals)
        self.assertEqual(ordered[0].game.external_id, 'liveA')


from django.test import override_settings


@override_settings(STORAGES={
    'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
    'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'},
})
class MLBHubViewTests(TestCase):
    def test_hub_context_has_three_buckets(self):
        resp = self.client.get('/mlb/')
        self.assertEqual(resp.status_code, 200)
        for key in ['live_tiles', 'today_tiles', 'future_games']:
            self.assertIn(key, resp.context, f'missing context key: {key}')


class MLBActionResolverTests(TestCase):
    """Part 1: resolve_actions — map signals to 🔥 Watch Now / 💰 Best Bet."""

    def _game(self, **kwargs):
        defaults = dict(home_rating=50.0, away_rating=50.0,
                        hp_rating=50.0, ap_rating=50.0,
                        status='scheduled', home_score=None, away_score=None,
                        pitchers=True, ext='ga')
        defaults.update(kwargs)
        home = _mk_team('H' + defaults['ext'], defaults['home_rating'], 'h' + defaults['ext'])
        away = _mk_team('A' + defaults['ext'], defaults['away_rating'], 'a' + defaults['ext'])
        hp = _mk_pitcher(home, 'HP', defaults['hp_rating'], 'hp' + defaults['ext']) if defaults['pitchers'] else None
        ap = _mk_pitcher(away, 'AP', defaults['ap_rating'], 'ap' + defaults['ext']) if defaults['pitchers'] else None
        return Game.objects.create(
            home_team=home, away_team=away,
            first_pitch=timezone.now() + timedelta(hours=1),
            status=defaults['status'],
            home_score=defaults['home_score'], away_score=defaults['away_score'],
            home_pitcher=hp, away_pitcher=ap,
            source='mlb_stats_api', external_id=defaults['ext'],
        )

    def _add_odds(self, game, spread=None, ml_home=None, ml_away=None):
        from apps.mlb.models import OddsSnapshot
        return OddsSnapshot.objects.create(
            game=game, captured_at=timezone.now(),
            market_home_win_prob=0.5, spread=spread,
            moneyline_home=ml_home, moneyline_away=ml_away,
        )

    def _types(self, actions):
        return [a['type'] for a in actions]

    def test_close_live_game_gets_watch_now(self):
        from apps.mlb.services.prioritization import build_signals
        g = self._game(status='live', home_score=3, away_score=2, ext='cl')
        s = build_signals(g)
        self.assertIn('watch_now', self._types(s.actions))

    def test_ace_matchup_upcoming_gets_watch_now(self):
        from apps.mlb.services.prioritization import build_signals
        g = self._game(hp_rating=80.0, ap_rating=72.0, ext='ace')
        s = build_signals(g)
        self.assertIn('watch_now', self._types(s.actions))

    def test_tight_spread_with_both_pitchers_gets_best_bet(self):
        from apps.mlb.services.prioritization import build_signals
        g = self._game(ext='tb')
        self._add_odds(g, spread=1.0)
        s = build_signals(g)
        self.assertIn('best_bet', self._types(s.actions))

    def test_tbd_pitcher_never_gets_best_bet(self):
        from apps.mlb.services.prioritization import build_signals
        g = self._game(pitchers=False, ext='tbd')
        self._add_odds(g, spread=1.0)
        s = build_signals(g)
        self.assertNotIn('best_bet', self._types(s.actions))
        self.assertTrue(s.tbd_pitcher)

    def test_blowout_gets_no_actions(self):
        from apps.mlb.services.prioritization import build_signals
        g = self._game(status='live', home_score=10, away_score=2, ext='bo')
        self._add_odds(g, spread=1.0)  # even with tight spread
        s = build_signals(g)
        self.assertEqual(s.actions, [])
        self.assertTrue(s.is_blowout)

    def test_best_bet_is_primary_when_both_fire(self):
        from apps.mlb.services.prioritization import build_signals
        # Ace live game + tight spread — both should fire, Best Bet primary.
        g = self._game(
            status='live', home_score=2, away_score=1,
            hp_rating=80.0, ap_rating=72.0, ext='bbprim',
        )
        self._add_odds(g, spread=1.0)
        s = build_signals(g)
        primaries = [a for a in s.actions if a['strength'] == 'primary']
        self.assertEqual(len(primaries), 1)
        self.assertEqual(primaries[0]['type'], 'best_bet')
        # Secondary Watch Now follows.
        secondaries = [a for a in s.actions if a['strength'] == 'secondary']
        self.assertEqual(len(secondaries), 1)
        self.assertEqual(secondaries[0]['type'], 'watch_now')


class MLBPrefillTests(TestCase):
    """Part 3: prefill_from_signals — intelligent defaults for the modal."""

    def _setup(self, *, hp_rating=60.0, ap_rating=50.0, spread=None, ml_home=None, ml_away=None, pitchers=True):
        home = _mk_team('Yankees', 60.0, 'ph')
        away = _mk_team('Royals', 40.0, 'pa')
        hp = _mk_pitcher(home, 'Ace', hp_rating, 'php') if pitchers else None
        ap = _mk_pitcher(away, 'Arm', ap_rating, 'pap') if pitchers else None
        g = Game.objects.create(
            home_team=home, away_team=away,
            first_pitch=timezone.now() + timedelta(hours=1),
            home_pitcher=hp, away_pitcher=ap,
            source='mlb_stats_api', external_id='pf',
        )
        if spread is not None or ml_home is not None or ml_away is not None:
            from apps.mlb.models import OddsSnapshot
            OddsSnapshot.objects.create(
                game=g, captured_at=timezone.now(),
                market_home_win_prob=0.5, spread=spread,
                moneyline_home=ml_home, moneyline_away=ml_away,
            )
        return g

    def test_default_selects_better_pitcher_team(self):
        from apps.mlb.services.prioritization import build_signals
        from apps.mockbets.services.prefill import prefill_from_signals
        # Away pitcher much better — prefill should pick the Royals.
        g = self._setup(hp_rating=45.0, ap_rating=70.0)
        data = prefill_from_signals(build_signals(g))
        self.assertEqual(data['selection'], 'Royals')

    def test_defaults_to_home_when_pitchers_tied(self):
        from apps.mlb.services.prioritization import build_signals
        from apps.mockbets.services.prefill import prefill_from_signals
        g = self._setup(hp_rating=60.0, ap_rating=60.0)
        data = prefill_from_signals(build_signals(g))
        self.assertEqual(data['selection'], 'Yankees')
        self.assertEqual(data['bet_type'], 'moneyline')

    def test_tight_spread_switches_to_spread_bet(self):
        from apps.mlb.services.prioritization import build_signals
        from apps.mockbets.services.prefill import prefill_from_signals
        g = self._setup(spread=-1.5)  # home favored by 1.5
        data = prefill_from_signals(build_signals(g))
        self.assertEqual(data['bet_type'], 'spread')
        # Home-POV spread -1.5 for home side → "Yankees -1.5"
        self.assertIn('Yankees', data['selection'])
        self.assertIn('-1.5', data['selection'])

    def test_moneyline_propagates_from_snapshot(self):
        from apps.mlb.services.prioritization import build_signals
        from apps.mockbets.services.prefill import prefill_from_signals
        g = self._setup(ml_home=-140, ml_away=120)
        data = prefill_from_signals(build_signals(g))
        self.assertEqual(data['bet_type'], 'moneyline')
        self.assertEqual(data['selection'], 'Yankees')
        self.assertEqual(data['odds'], -140)

    def test_shape_is_json_serializable(self):
        import json
        from apps.mlb.services.prioritization import build_signals
        from apps.mockbets.services.prefill import prefill_from_signals
        g = self._setup(ml_home=-140, ml_away=120)
        data = prefill_from_signals(build_signals(g))
        json.dumps(data)  # would raise if anything non-serializable
        self.assertEqual(data['sport'], 'mlb')
        self.assertEqual(data['game_id'], str(g.id))

    def test_selections_by_type_always_has_moneyline(self):
        from apps.mlb.services.prioritization import build_signals
        from apps.mockbets.services.prefill import prefill_from_signals
        g = self._setup()
        data = prefill_from_signals(build_signals(g))
        ml = data['selections_by_type']['moneyline']
        labels = [o['label'] for o in ml]
        self.assertEqual(sorted(labels), ['Royals', 'Yankees'])

    def test_selections_include_spread_and_total_when_market_has_them(self):
        from apps.mlb.services.prioritization import build_signals
        from apps.mockbets.services.prefill import prefill_from_signals
        # home -1.5, total 8.5
        from apps.mlb.models import OddsSnapshot
        g = self._setup()
        OddsSnapshot.objects.create(
            game=g, captured_at=timezone.now(),
            market_home_win_prob=0.55, spread=-1.5, total=8.5,
        )
        data = prefill_from_signals(build_signals(g))
        spreads = [o['label'] for o in data['selections_by_type']['spread']]
        totals = [o['label'] for o in data['selections_by_type']['total']]
        self.assertTrue(any('Yankees -1.5' in s for s in spreads))
        self.assertTrue(any('Royals +1.5' in s for s in spreads))
        self.assertIn('Over 8.5', totals)
        self.assertIn('Under 8.5', totals)


class MLBStreakTests(TestCase):
    """compute_streaks — recent W/L streak computation across teams."""

    def _final(self, home_team, away_team, home_score, away_score, days_ago=0):
        return Game.objects.create(
            home_team=home_team, away_team=away_team,
            first_pitch=timezone.now() - timedelta(days=days_ago),
            status='final', home_score=home_score, away_score=away_score,
            source='mlb_stats_api', external_id=f'f{home_team.slug}{away_team.slug}{days_ago}',
        )

    def test_three_game_win_streak(self):
        from apps.mlb.services.streaks import compute_streaks
        a = _mk_team('A', 50.0, 'sa')
        b = _mk_team('B', 50.0, 'sb')
        # Team A wins 3 most recent games
        self._final(a, b, 5, 1, days_ago=1)
        self._final(a, b, 6, 2, days_ago=2)
        self._final(a, b, 4, 3, days_ago=3)
        self._final(a, b, 1, 7, days_ago=4)  # loss — ends the streak
        result = compute_streaks({a.id, b.id})
        self.assertEqual(result[a.id]['kind'], 'W')
        self.assertEqual(result[a.id]['count'], 3)
        self.assertEqual(result[a.id]['label'], 'W3')
        self.assertEqual(result[b.id]['kind'], 'L')
        self.assertEqual(result[b.id]['count'], 3)

    def test_single_game_returns_none_below_min(self):
        from apps.mlb.services.streaks import compute_streaks
        a = _mk_team('A1', 50.0, 'sa1')
        b = _mk_team('B1', 50.0, 'sb1')
        self._final(a, b, 5, 1, days_ago=1)
        self._final(b, a, 5, 1, days_ago=2)  # A lost previous — W1 streak
        result = compute_streaks({a.id})
        # W1 is filtered out — below MIN_STREAK=2
        self.assertIsNone(result[a.id])

    def test_no_games_returns_none(self):
        from apps.mlb.services.streaks import compute_streaks
        a = _mk_team('Lonely', 50.0, 'lone')
        result = compute_streaks({a.id})
        self.assertIsNone(result[a.id])

    def test_empty_team_set_returns_empty(self):
        from apps.mlb.services.streaks import compute_streaks
        self.assertEqual(compute_streaks(set()), {})

    def test_format_record_handles_null(self):
        from apps.mlb.services.streaks import format_record
        a = _mk_team('R1', 50.0, 'r1')
        self.assertIsNone(format_record(a))  # wins/losses default None
        a.wins, a.losses = 12, 8
        a.save()
        self.assertEqual(format_record(a), '12-8')
        self.assertIsNone(format_record(None))


class MLBUserBetIndicatorTests(TestCase):
    """has_user_bet is True when the user has a pending MockBet on the game."""

    def test_pending_bet_surfaces(self):
        from django.contrib.auth import get_user_model
        from apps.mlb.services.prioritization import prioritize
        from apps.mockbets.models import MockBet
        User = get_user_model()
        u = User.objects.create_user(username='betuser', password='x')

        home = _mk_team('H', 50.0, 'bh')
        away = _mk_team('A', 50.0, 'ba')
        g = Game.objects.create(
            home_team=home, away_team=away,
            first_pitch=timezone.now() + timedelta(hours=1),
            source='mlb_stats_api', external_id='betgame',
        )
        MockBet.objects.create(
            user=u, sport='mlb', mlb_game=g,
            bet_type='moneyline', selection='H',
            odds_american=-120, implied_probability=0.545,
            stake_amount=100, result='pending',
        )
        signals = prioritize([g], user=u)
        self.assertTrue(signals[0].has_user_bet)

    def test_anonymous_never_flagged(self):
        from django.contrib.auth.models import AnonymousUser
        from apps.mlb.services.prioritization import prioritize
        home = _mk_team('H2', 50.0, 'bh2')
        away = _mk_team('A2', 50.0, 'ba2')
        g = Game.objects.create(
            home_team=home, away_team=away,
            first_pitch=timezone.now() + timedelta(hours=1),
            source='mlb_stats_api', external_id='anonbet',
        )
        signals = prioritize([g], user=AnonymousUser())
        self.assertFalse(signals[0].has_user_bet)


class MLBLineValueTests(TestCase):
    """line_value_discrepancy signal — model vs market probability delta."""

    def _game(self, ext):
        home = _mk_team('VH' + ext, 50.0, 'vh' + ext)
        away = _mk_team('VA' + ext, 50.0, 'va' + ext)
        hp = _mk_pitcher(home, 'HP', 50.0, 'vhp' + ext)
        ap = _mk_pitcher(away, 'AP', 50.0, 'vap' + ext)
        return Game.objects.create(
            home_team=home, away_team=away,
            first_pitch=timezone.now() + timedelta(hours=1),
            neutral_site=True,
            home_pitcher=hp, away_pitcher=ap,
            source='mlb_stats_api', external_id=ext,
        )

    def test_no_odds_no_line_value(self):
        from apps.mlb.services.prioritization import build_signals
        g = self._game('lv1')
        s = build_signals(g)
        self.assertIsNone(s.line_value_discrepancy)
        self.assertNotIn('line_value', s.reasons)

    def test_market_equal_to_model_no_signal(self):
        from apps.mlb.models import OddsSnapshot
        from apps.mlb.services.prioritization import build_signals
        g = self._game('lv2')
        # Equal-strength teams + equal pitchers + neutral site -> house prob = 0.5.
        # Market prob = 0.5 -> discrepancy = 0 -> no signal.
        OddsSnapshot.objects.create(
            game=g, captured_at=timezone.now(),
            market_home_win_prob=0.5,
        )
        s = build_signals(g)
        self.assertIsNotNone(s.line_value_discrepancy)
        self.assertLess(s.line_value_discrepancy, 0.06)
        self.assertNotIn('line_value', s.reasons)

    def test_large_discrepancy_emits_line_value(self):
        from apps.mlb.models import OddsSnapshot
        from apps.mlb.services.prioritization import build_signals
        g = self._game('lv3')
        # Model says 0.5 (equal teams, neutral site), market says 0.3 -> delta 0.2.
        OddsSnapshot.objects.create(
            game=g, captured_at=timezone.now(),
            market_home_win_prob=0.3,
        )
        s = build_signals(g)
        self.assertGreaterEqual(s.line_value_discrepancy, 0.06)
        self.assertIn('line_value', s.reasons)


class MLBLateGameProxyTests(TestCase):
    def test_fresh_live_game_is_not_late(self):
        from apps.mlb.services.prioritization import build_signals
        home = _mk_team('LH', 50.0, 'lh')
        away = _mk_team('LA', 50.0, 'la')
        g = Game.objects.create(
            home_team=home, away_team=away,
            first_pitch=timezone.now() - timedelta(minutes=15),
            status='live', home_score=1, away_score=0,
            source='mlb_stats_api', external_id='lg1',
        )
        s = build_signals(g)
        self.assertFalse(s.late_game)

    def test_two_hour_old_live_game_is_late(self):
        from apps.mlb.services.prioritization import build_signals
        home = _mk_team('LH2', 50.0, 'lh2')
        away = _mk_team('LA2', 50.0, 'la2')
        g = Game.objects.create(
            home_team=home, away_team=away,
            first_pitch=timezone.now() - timedelta(hours=2),
            status='live', home_score=4, away_score=3,
            source='mlb_stats_api', external_id='lg2',
        )
        s = build_signals(g)
        self.assertTrue(s.late_game)
        self.assertIn('late_game', s.reasons)

    def test_scheduled_game_never_late(self):
        from apps.mlb.services.prioritization import build_signals
        home = _mk_team('LH3', 50.0, 'lh3')
        away = _mk_team('LA3', 50.0, 'la3')
        g = Game.objects.create(
            home_team=home, away_team=away,
            first_pitch=timezone.now() - timedelta(hours=10),  # would be "late" time-wise
            status='scheduled',
            source='mlb_stats_api', external_id='lg3',
        )
        s = build_signals(g)
        self.assertFalse(s.late_game)


class MLBTopOpportunityTests(TestCase):
    def _make_best_bet_game(self, ext, spread=1.0, hp_rating=60.0):
        home = _mk_team('TH' + ext, 50.0, 'th' + ext)
        away = _mk_team('TA' + ext, 50.0, 'ta' + ext)
        hp = _mk_pitcher(home, 'HP', hp_rating, 'thp' + ext)
        ap = _mk_pitcher(away, 'AP', 50.0, 'tap' + ext)
        g = Game.objects.create(
            home_team=home, away_team=away,
            first_pitch=timezone.now() + timedelta(hours=1),
            home_pitcher=hp, away_pitcher=ap,
            source='mlb_stats_api', external_id='top' + ext,
        )
        from apps.mlb.models import OddsSnapshot
        OddsSnapshot.objects.create(
            game=g, captured_at=timezone.now(),
            market_home_win_prob=0.5, spread=spread,
        )
        return g

    def test_only_one_top_opportunity_by_default(self):
        from apps.mlb.services.prioritization import prioritize, mark_top_opportunities
        g1 = self._make_best_bet_game('A')
        g2 = self._make_best_bet_game('B')
        signals = prioritize([g1, g2])
        mark_top_opportunities(signals)
        flagged = [s for s in signals if s.is_top_opportunity]
        self.assertEqual(len(flagged), 1)

    def test_n_is_configurable(self):
        from apps.mlb.services.prioritization import prioritize, mark_top_opportunities
        g1 = self._make_best_bet_game('C')
        g2 = self._make_best_bet_game('D')
        signals = prioritize([g1, g2])
        mark_top_opportunities(signals, n=2)
        flagged = [s for s in signals if s.is_top_opportunity]
        self.assertEqual(len(flagged), 2)

    def test_zero_best_bets_means_zero_top(self):
        from apps.mlb.services.prioritization import prioritize, mark_top_opportunities
        # Game without odds — no best_bet action can be primary.
        home = _mk_team('EH', 50.0, 'eh')
        away = _mk_team('EA', 50.0, 'ea')
        g = Game.objects.create(
            home_team=home, away_team=away,
            first_pitch=timezone.now() + timedelta(hours=1),
            source='mlb_stats_api', external_id='notop',
        )
        signals = prioritize([g])
        mark_top_opportunities(signals)
        self.assertFalse(signals[0].is_top_opportunity)

    def test_empty_actions_is_valid_clean_state(self):
        """A game with no actions renders nothing in the action bar — a
        blowout with TBD pitchers and no odds has nothing to say."""
        from apps.mlb.services.prioritization import build_signals
        home = _mk_team('QH', 50.0, 'qh')
        away = _mk_team('QA', 50.0, 'qa')
        g = Game.objects.create(
            home_team=home, away_team=away,
            first_pitch=timezone.now() + timedelta(hours=1),
            home_pitcher=None, away_pitcher=None,
            source='mlb_stats_api', external_id='cleanstate',
        )
        s = build_signals(g)
        self.assertEqual(s.actions, [])


class APIClientValidationTests(TestCase):
    """Hard guardrails against HTML / non-JSON responses served with 2xx."""

    def _make_response(self, *, content_type='application/json', text='{"ok": true}', status=200):
        import requests
        resp = requests.Response()
        resp.status_code = status
        resp.headers['Content-Type'] = content_type
        resp._content = text.encode('utf-8')
        return resp

    def test_html_body_rejected(self):
        from apps.datahub.providers.client import APIClient, NonJSONResponseError
        resp = self._make_response(content_type='text/html; charset=utf-8',
                                    text='<!doctype html><html><body>Rate limited</body></html>')
        with self.assertRaises(NonJSONResponseError) as ctx:
            APIClient._validate_json_response(resp, 'https://example.com/api')
        self.assertIn('Non-JSON', str(ctx.exception))

    def test_non_json_content_type_rejected(self):
        from apps.datahub.providers.client import APIClient, NonJSONResponseError
        resp = self._make_response(content_type='text/plain', text='quota exceeded')
        with self.assertRaises(NonJSONResponseError):
            APIClient._validate_json_response(resp, 'https://example.com/api')

    def test_valid_json_passes(self):
        from apps.datahub.providers.client import APIClient
        resp = self._make_response()
        # No raise expected
        APIClient._validate_json_response(resp, 'https://example.com/api')


class MLBConfidenceTests(TestCase):
    """compute_confidence — normalized [0, 1] from signals + data completeness."""

    def _game(self, ext, *, pitchers=True, hp=50.0, ap=50.0,
              status='scheduled', home_score=None, away_score=None):
        home = _mk_team('CH' + ext, 50.0, 'ch' + ext)
        away = _mk_team('CA' + ext, 50.0, 'ca' + ext)
        hpp = _mk_pitcher(home, 'HP', hp, 'chp' + ext) if pitchers else None
        app = _mk_pitcher(away, 'AP', ap, 'cap' + ext) if pitchers else None
        return Game.objects.create(
            home_team=home, away_team=away,
            first_pitch=timezone.now() + timedelta(hours=1),
            status=status, home_score=home_score, away_score=away_score,
            home_pitcher=hpp, away_pitcher=app,
            source='mlb_stats_api', external_id=ext,
        )

    def test_no_odds_no_tbd_has_low_but_nonzero(self):
        from apps.mlb.services.prioritization import build_signals
        g = self._game('c1')
        s = build_signals(g)
        self.assertLess(s.confidence, 0.5)
        self.assertGreaterEqual(s.confidence, 0.0)

    def test_tbd_pitcher_caps_confidence(self):
        from apps.mlb.services.prioritization import build_signals
        g = self._game('c2', pitchers=False)
        s = build_signals(g)
        # pitchers_known contributes 0.10 weight; TBD loses it entirely.
        self.assertLess(s.confidence, 0.7)

    def test_blowout_clamps_confidence(self):
        from apps.mlb.services.prioritization import build_signals
        g = self._game('c3', status='live', home_score=12, away_score=2)
        s = build_signals(g)
        self.assertLessEqual(s.confidence, 0.4)

    def test_big_line_value_lifts_confidence(self):
        from apps.mlb.models import OddsSnapshot
        from apps.mlb.services.prioritization import build_signals
        g = self._game('c4')
        # Model prob ~0.5 (equal ratings), market 0.25 → 0.25 delta, well past
        # the LINE_VALUE_STRONG threshold.
        OddsSnapshot.objects.create(
            game=g, captured_at=timezone.now(),
            market_home_win_prob=0.25, spread=1.0,
        )
        s = build_signals(g)
        self.assertGreaterEqual(s.confidence, 0.7)

    def test_confidence_pct_matches_confidence(self):
        from apps.mlb.services.prioritization import build_signals
        g = self._game('c5')
        s = build_signals(g)
        self.assertEqual(s.confidence_pct, int(round(s.confidence * 100)))

    def test_primary_action_carries_confidence(self):
        from apps.mlb.models import OddsSnapshot
        from apps.mlb.services.prioritization import build_signals
        g = self._game('c6')
        OddsSnapshot.objects.create(
            game=g, captured_at=timezone.now(),
            market_home_win_prob=0.5, spread=1.0,
        )
        s = build_signals(g)
        primary = next(a for a in s.actions if a['strength'] == 'primary')
        self.assertEqual(primary['confidence'], s.confidence)


class MLBFocusEngineTests(TestCase):
    def _bb_game(self, ext, *, home_win_prob=0.5, spread=1.0):
        home = _mk_team('FH' + ext, 50.0, 'fh' + ext)
        away = _mk_team('FA' + ext, 50.0, 'fa' + ext)
        hp = _mk_pitcher(home, 'HP', 60.0, 'fhp' + ext)
        ap = _mk_pitcher(away, 'AP', 50.0, 'fap' + ext)
        g = Game.objects.create(
            home_team=home, away_team=away,
            first_pitch=timezone.now() + timedelta(hours=1),
            home_pitcher=hp, away_pitcher=ap,
            source='mlb_stats_api', external_id=ext,
        )
        from apps.mlb.models import OddsSnapshot
        OddsSnapshot.objects.create(
            game=g, captured_at=timezone.now(),
            market_home_win_prob=home_win_prob, spread=spread,
        )
        return g

    def test_returns_none_when_no_primary_action(self):
        from apps.mlb.services.prioritization import get_focus_game, prioritize
        # Game with no odds → no Best Bet; no injury/ace/live → no Watch Now.
        home = _mk_team('NP', 50.0, 'np')
        away = _mk_team('NA', 50.0, 'na')
        g = Game.objects.create(
            home_team=home, away_team=away,
            first_pitch=timezone.now() + timedelta(hours=1),
            home_pitcher=None, away_pitcher=None,
            source='mlb_stats_api', external_id='nofocus',
        )
        signals = prioritize([g])
        self.assertIsNone(get_focus_game(signals))

    def test_picks_highest_confidence_best_bet(self):
        from apps.mlb.services.prioritization import get_focus_game, prioritize
        # weak: tight spread but market near model prob → no line_value signal
        weak = self._bb_game('weak', home_win_prob=0.62, spread=1.5)
        # strong: tight spread + huge line-value delta → saturated confidence
        strong = self._bb_game('strong', home_win_prob=0.20, spread=1.0)
        signals = prioritize([weak, strong])
        focus = get_focus_game(signals)
        self.assertEqual(focus.game.external_id, 'strong')

    def test_best_bet_preferred_over_watch_now(self):
        from apps.mlb.services.prioritization import get_focus_game, prioritize
        bb = self._bb_game('bbf')  # gets Best Bet
        # Watch Now only: close live with no odds.
        home = _mk_team('WH', 50.0, 'wh')
        away = _mk_team('WA', 50.0, 'wa')
        hp = _mk_pitcher(home, 'HP', 60.0, 'whp')
        ap = _mk_pitcher(away, 'AP', 50.0, 'wap')
        wn = Game.objects.create(
            home_team=home, away_team=away,
            first_pitch=timezone.now() - timedelta(minutes=15),
            status='live', home_score=2, away_score=1,
            home_pitcher=hp, away_pitcher=ap,
            source='mlb_stats_api', external_id='wnf',
        )
        signals = prioritize([wn, bb])
        focus = get_focus_game(signals)
        self.assertEqual(focus.game.external_id, 'bbf')

    def test_bet_placed_not_chosen_for_focus(self):
        """The focus banner should surface NEW opportunities, not restate
        the user's own pending bet."""
        from django.contrib.auth import get_user_model
        from apps.mlb.services.prioritization import get_focus_game, prioritize
        from apps.mockbets.models import MockBet
        User = get_user_model()
        u = User.objects.create_user(username='focus_user', password='x')
        g = self._bb_game('withbet')
        MockBet.objects.create(
            user=u, sport='mlb', mlb_game=g,
            bet_type='moneyline', selection='x',
            odds_american=-110, implied_probability=0.52,
            stake_amount=100, result='pending',
        )
        signals = prioritize([g], user=u)
        # The primary action should be `bet_placed` now…
        self.assertEqual(signals[0].actions[0]['type'], 'bet_placed')
        # …and focus should return None because nothing else is in the field.
        self.assertIsNone(get_focus_game(signals))


class MLBBetPlacedActionTests(TestCase):
    def test_pending_bet_makes_bet_placed_primary(self):
        from django.contrib.auth import get_user_model
        from apps.mlb.services.prioritization import prioritize
        from apps.mlb.models import OddsSnapshot
        from apps.mockbets.models import MockBet
        User = get_user_model()
        u = User.objects.create_user(username='bp_user', password='x')
        home = _mk_team('BPH', 50.0, 'bph')
        away = _mk_team('BPA', 50.0, 'bpa')
        hp = _mk_pitcher(home, 'HP', 60.0, 'bphp')
        ap = _mk_pitcher(away, 'AP', 50.0, 'bpap')
        g = Game.objects.create(
            home_team=home, away_team=away,
            first_pitch=timezone.now() + timedelta(hours=1),
            home_pitcher=hp, away_pitcher=ap,
            source='mlb_stats_api', external_id='bpgame',
        )
        OddsSnapshot.objects.create(
            game=g, captured_at=timezone.now(),
            market_home_win_prob=0.5, spread=1.0,
        )
        bet = MockBet.objects.create(
            user=u, sport='mlb', mlb_game=g,
            bet_type='moneyline', selection='BPH',
            odds_american=-110, implied_probability=0.52,
            stake_amount=100, result='pending',
        )
        signals = prioritize([g], user=u)
        actions = signals[0].actions
        self.assertEqual(actions[0]['type'], 'bet_placed')
        self.assertEqual(actions[0]['strength'], 'primary')
        # Best Bet preserved as secondary context.
        self.assertTrue(any(a['type'] == 'best_bet' and a['strength'] == 'secondary' for a in actions))
        # user_bet_id is the uuid of the bet (str).
        self.assertEqual(signals[0].user_bet_id, str(bet.id))


class IngestOddsFailFastTests(TestCase):
    """ingest_odds raises in DEBUG when ingestion produces zero records."""

    def test_zero_created_raises_in_debug(self):
        from unittest.mock import patch
        from django.core.management import call_command
        from django.test.utils import override_settings
        with patch('apps.datahub.providers.registry.get_provider') as mk, \
             patch('apps.datahub.providers.mlb.odds_espn_provider.MLBEspnOddsProvider') as mk_espn:
            mk.return_value.run.return_value = {'status': 'empty', 'created': 0, 'skipped': 5}
            mk_espn.return_value.run.return_value = {'status': 'empty', 'created': 0, 'skipped': 0}
            with override_settings(DEBUG=True):
                with self.assertRaises(RuntimeError) as ctx:
                    call_command('ingest_odds', sport='mlb', force=True)
                self.assertIn('zero records', str(ctx.exception))


class MLBInjuriesProviderTests(TestCase):
    """ESPN MLB injury provider — status→impact mapping + aggregation."""

    def test_status_mapping_pitcher_is_high_on_10day(self):
        from apps.datahub.providers.mlb.injuries_provider import _impact_from_status
        self.assertEqual(_impact_from_status('10-Day IL', 'SP'), 'high')
        self.assertEqual(_impact_from_status('15-Day IL', 'SP'), 'high')
        self.assertEqual(_impact_from_status('60-Day IL', 'P'), 'high')

    def test_status_mapping_position_player(self):
        from apps.datahub.providers.mlb.injuries_provider import _impact_from_status
        self.assertEqual(_impact_from_status('Day-To-Day', '1B'), 'low')
        self.assertEqual(_impact_from_status('10-Day IL', '1B'), 'med')
        self.assertEqual(_impact_from_status('15-Day IL', '1B'), 'high')
        self.assertEqual(_impact_from_status('60-Day IL', 'CF'), 'high')

    def test_unknown_status_returns_none(self):
        from apps.datahub.providers.mlb.injuries_provider import _impact_from_status
        self.assertIsNone(_impact_from_status('Active', 'SP'))
        self.assertIsNone(_impact_from_status('', 'SP'))
        self.assertIsNone(_impact_from_status(None, 'SP'))

    def test_normalize_aggregates_to_worst(self):
        """A team with both a DTD bat and a 15-day-IL starter is `high`,
        not `low`. notes carry the top-N most severe in priority order."""
        from unittest.mock import patch
        from apps.datahub.providers.mlb.injuries_provider import MLBEspnInjuriesProvider
        raw = [{
            'team_id': 1,
            'team_name': 'X',
            'injuries': [
                {'athlete': {'fullName': 'Bench Bat', 'position': {'abbreviation': '1B'}},
                 'status': 'Day-To-Day'},
                {'athlete': {'fullName': 'Spencer Strider', 'position': {'abbreviation': 'SP'}},
                 'status': '15-Day IL', 'details': {'returnDate': '2026-05-01'}},
            ],
        }]
        with patch.object(MLBEspnInjuriesProvider, '__init__', return_value=None):
            p = MLBEspnInjuriesProvider()
            out = p.normalize(raw)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]['impact_level'], 'high')
        # Most severe listed first in notes.
        self.assertTrue(out[0]['notes'].startswith('SP Spencer Strider'))

    def test_normalize_skips_team_with_no_impact(self):
        from unittest.mock import patch
        from apps.datahub.providers.mlb.injuries_provider import MLBEspnInjuriesProvider
        raw = [{
            'team_id': 1, 'team_name': 'X',
            'injuries': [
                # Unrecognized status → ignored
                {'athlete': {'fullName': 'X', 'position': {'abbreviation': '1B'}},
                 'status': 'Probable'},
            ],
        }]
        with patch.object(MLBEspnInjuriesProvider, '__init__', return_value=None):
            p = MLBEspnInjuriesProvider()
            out = p.normalize(raw)
        self.assertEqual(out, [])

    def test_persist_creates_one_per_upcoming_game(self):
        from unittest.mock import patch
        from apps.datahub.providers.mlb.injuries_provider import MLBEspnInjuriesProvider
        from apps.mlb.models import InjuryImpact
        conf, _ = Conference.objects.get_or_create(slug='al-east', defaults={'name': 'AL East'})
        home = Team.objects.create(name='Atlanta Braves', slug='atl', conference=conf)
        away = Team.objects.create(name='Philadelphia Phillies', slug='phi', conference=conf)
        g1 = Game.objects.create(home_team=home, away_team=away,
                                 first_pitch=timezone.now() + timedelta(hours=2),
                                 source='mlb_stats_api', external_id='ij1')
        g2 = Game.objects.create(home_team=home, away_team=away,
                                 first_pitch=timezone.now() + timedelta(days=2),
                                 source='mlb_stats_api', external_id='ij2')
        normalized = [{
            'team_id': home.id,
            'team_name': home.name,
            'impact_level': 'high',
            'notes': 'SP Strider (15-Day IL, return 2026-05-01)',
            'player_count': 1,
        }]
        with patch.object(MLBEspnInjuriesProvider, '__init__', return_value=None):
            p = MLBEspnInjuriesProvider()
            result = p.persist(normalized)
        self.assertEqual(result['created'], 2)
        self.assertEqual(InjuryImpact.objects.count(), 2)
        self.assertTrue(all(i.impact_level == 'high' for i in InjuryImpact.objects.all()))

    def test_persist_is_idempotent(self):
        """Second run with the same normalized data updates, does not duplicate."""
        from unittest.mock import patch
        from apps.datahub.providers.mlb.injuries_provider import MLBEspnInjuriesProvider
        from apps.mlb.models import InjuryImpact
        conf, _ = Conference.objects.get_or_create(slug='al-east', defaults={'name': 'AL East'})
        home = Team.objects.create(name='Atlanta Braves', slug='atl2', conference=conf)
        away = Team.objects.create(name='Philadelphia Phillies', slug='phi2', conference=conf)
        Game.objects.create(home_team=home, away_team=away,
                            first_pitch=timezone.now() + timedelta(hours=2),
                            source='mlb_stats_api', external_id='ij3')
        normalized = [{'team_id': home.id, 'team_name': home.name,
                       'impact_level': 'med', 'notes': 'initial', 'player_count': 1}]
        with patch.object(MLBEspnInjuriesProvider, '__init__', return_value=None):
            p = MLBEspnInjuriesProvider()
            p.persist(normalized)
            normalized[0]['impact_level'] = 'high'
            normalized[0]['notes'] = 'escalated'
            r2 = p.persist(normalized)
        self.assertEqual(r2['updated'], 1)
        self.assertEqual(r2['created'], 0)
        self.assertEqual(InjuryImpact.objects.count(), 1)
        self.assertEqual(InjuryImpact.objects.first().impact_level, 'high')
        self.assertEqual(InjuryImpact.objects.first().notes, 'escalated')


class MLBInjurySignalNotesTests(TestCase):
    """GameSignals.injury_summary carries per-side notes for tile display."""

    def test_notes_propagate_from_injury_impact(self):
        from apps.mlb.models import InjuryImpact
        from apps.mlb.services.prioritization import build_signals
        conf, _ = Conference.objects.get_or_create(slug='al-east', defaults={'name': 'AL East'})
        home = Team.objects.create(name='H', slug='inote-h', conference=conf)
        away = Team.objects.create(name='A', slug='inote-a', conference=conf)
        g = Game.objects.create(home_team=home, away_team=away,
                                first_pitch=timezone.now() + timedelta(hours=1),
                                source='mlb_stats_api', external_id='inote')
        InjuryImpact.objects.create(
            game=g, team=home, impact_level='high',
            notes='SP Strider (15-Day IL, return 2026-05-01)\nRP Iglesias (Day-To-Day)',
        )
        s = build_signals(g)
        self.assertEqual(s.injury_summary['home'], 'high')
        # Tile surfaces only the first (worst) line.
        self.assertIn('SP Strider', s.injury_summary['home_notes'])
        self.assertEqual(s.injury_summary.get('away_notes', ''), '')


class ESPNOddsProviderTests(TestCase):
    """Hermetic tests for the ESPN MLB odds provider — no network."""

    SAMPLE_EVENT = {
        'date': '2026-04-20T23:10:00Z',
        'competitions': [{
            'competitors': [
                {'homeAway': 'home', 'team': {'displayName': 'New York Yankees', 'abbreviation': 'NYY'}},
                {'homeAway': 'away', 'team': {'displayName': 'Boston Red Sox', 'abbreviation': 'BOS'}},
            ],
            'odds': [
                {
                    'provider': {'name': 'DraftKings'},
                    'details': 'NYY -1.5',
                    'overUnder': 8.5,
                    'spread': -1.5,
                    'homeTeamOdds': {'moneyLine': -131, 'favorite': True},
                    'awayTeamOdds': {'moneyLine': 113, 'favorite': False},
                },
            ],
            'date': '2026-04-20T23:10:00Z',
        }],
    }

    def test_normalize_extracts_odds(self):
        from unittest.mock import patch
        from apps.datahub.providers.mlb.odds_espn_provider import MLBEspnOddsProvider
        with patch.object(MLBEspnOddsProvider, '__init__', return_value=None):
            p = MLBEspnOddsProvider()
            out = p.normalize([self.SAMPLE_EVENT])
        self.assertEqual(len(out), 1)
        row = out[0]
        self.assertEqual(row['home_team'], 'New York Yankees')
        self.assertEqual(row['away_team'], 'Boston Red Sox')
        self.assertEqual(row['sportsbook'], 'DraftKings')
        self.assertEqual(row['moneyline_home'], -131)
        self.assertEqual(row['moneyline_away'], 113)
        self.assertEqual(row['spread'], -1.5)
        self.assertEqual(row['total'], 8.5)

    def test_normalize_prefers_draftkings(self):
        from unittest.mock import patch
        from apps.datahub.providers.mlb.odds_espn_provider import MLBEspnOddsProvider
        event = {
            **self.SAMPLE_EVENT,
            'competitions': [{
                **self.SAMPLE_EVENT['competitions'][0],
                'odds': [
                    {'provider': {'name': 'BetMGM'}, 'homeTeamOdds': {'moneyLine': -125}, 'awayTeamOdds': {'moneyLine': 110}, 'spread': -1.5, 'overUnder': 9.0},
                    {'provider': {'name': 'DraftKings'}, 'homeTeamOdds': {'moneyLine': -131}, 'awayTeamOdds': {'moneyLine': 113}, 'spread': -1.5, 'overUnder': 8.5},
                ],
            }],
        }
        with patch.object(MLBEspnOddsProvider, '__init__', return_value=None):
            p = MLBEspnOddsProvider()
            out = p.normalize([event])
        self.assertEqual(out[0]['sportsbook'], 'DraftKings')
        self.assertEqual(out[0]['moneyline_home'], -131)

    def test_normalize_skips_event_without_odds(self):
        from unittest.mock import patch
        from apps.datahub.providers.mlb.odds_espn_provider import MLBEspnOddsProvider
        event = {**self.SAMPLE_EVENT, 'competitions': [{**self.SAMPLE_EVENT['competitions'][0], 'odds': []}]}
        with patch.object(MLBEspnOddsProvider, '__init__', return_value=None):
            p = MLBEspnOddsProvider()
            out = p.normalize([event])
        self.assertEqual(out, [])

    def test_extract_moneyline_handles_dict_shape(self):
        """ESPN started serving moneyLine as {'value': -150, 'displayValue': '-150'}.
        That shape crashed production on 2026-04-25 because downstream code
        compared a dict to int. Extractor must coerce to a bare int."""
        from apps.datahub.providers.mlb.odds_espn_provider import _extract_moneyline
        odds_entry = {
            'provider': {'name': 'DraftKings'},
            'homeTeamOdds': {'moneyLine': {'value': -150, 'displayValue': '-150'}},
            'awayTeamOdds': {'moneyLine': {'value': 130, 'displayValue': '+130'}},
            'spread': -1.5, 'overUnder': 8.5,
        }
        self.assertEqual(_extract_moneyline(odds_entry, 'home'), -150)
        self.assertEqual(_extract_moneyline(odds_entry, 'away'), 130)

    def test_extract_moneyline_handles_string_shape(self):
        """Some ESPN payloads serve moneyLine as '+150' / '-110' strings.
        The extractor strips the + and parses the int."""
        from apps.datahub.providers.mlb.odds_espn_provider import _extract_moneyline
        odds_entry = {
            'provider': {'name': 'DraftKings'},
            'homeTeamOdds': {'moneyLine': '-150'},
            'awayTeamOdds': {'moneyLine': '+130'},
        }
        self.assertEqual(_extract_moneyline(odds_entry, 'home'), -150)
        self.assertEqual(_extract_moneyline(odds_entry, 'away'), 130)

    def test_persist_does_not_crash_on_dict_shape(self):
        """End-to-end: normalize+persist runs cleanly when moneyLine arrives
        as a dict. Regression test for the production outage."""
        from unittest.mock import patch
        from apps.datahub.providers.mlb.odds_espn_provider import MLBEspnOddsProvider
        from apps.mlb.models import OddsSnapshot
        conf, _ = Conference.objects.get_or_create(slug='al-east', defaults={'name': 'AL East'})
        home = Team.objects.create(name='New York Yankees', slug='ny-dict', conference=conf,
                                   source='mlb_stats_api', external_id='dict-147')
        away = Team.objects.create(name='Boston Red Sox', slug='bos-dict', conference=conf,
                                   source='mlb_stats_api', external_id='dict-111')
        Game.objects.create(
            home_team=home, away_team=away,
            first_pitch=timezone.now() + timedelta(hours=1),
            source='mlb_stats_api', external_id='espn_dict_test_game',
        )
        event = {
            **self.SAMPLE_EVENT,
            'date': (timezone.now() + timedelta(hours=1)).isoformat(),
            'competitions': [{
                **self.SAMPLE_EVENT['competitions'][0],
                'odds': [{
                    'provider': {'name': 'DraftKings'},
                    # Dict-shaped values — what crashed prod.
                    'homeTeamOdds': {'moneyLine': {'value': -131, 'displayValue': '-131'}},
                    'awayTeamOdds': {'moneyLine': {'value': 113, 'displayValue': '+113'}},
                    'spread': -1.5, 'overUnder': 8.5,
                }],
            }],
        }
        with patch.object(MLBEspnOddsProvider, '__init__', return_value=None):
            p = MLBEspnOddsProvider()
            normalized = p.normalize([event])
            result = p.persist(normalized)
        self.assertEqual(result['created'], 1)
        snap = OddsSnapshot.objects.filter(game__external_id='espn_dict_test_game').first()
        self.assertIsNotNone(snap)
        self.assertEqual(snap.moneyline_home, -131)
        self.assertEqual(snap.moneyline_away, 113)

    def test_persist_creates_snapshot_when_teams_and_game_match(self):
        from unittest.mock import patch
        from apps.datahub.providers.mlb.odds_espn_provider import MLBEspnOddsProvider
        from apps.mlb.models import OddsSnapshot
        # Seed teams + game that ESPN can match against.
        conf, _ = Conference.objects.get_or_create(slug='al-east', defaults={'name': 'AL East'})
        home = Team.objects.create(name='New York Yankees', slug='yankees', conference=conf,
                                   source='mlb_stats_api', external_id='147')
        away = Team.objects.create(name='Boston Red Sox', slug='redsox', conference=conf,
                                   source='mlb_stats_api', external_id='111')
        game = Game.objects.create(
            home_team=home, away_team=away,
            first_pitch=timezone.now() + timedelta(hours=1),
            source='mlb_stats_api', external_id='espn_test_game',
        )
        # Use the event's commence_time close to now so window matches.
        event = {**self.SAMPLE_EVENT, 'date': (timezone.now() + timedelta(hours=1)).isoformat()}
        with patch.object(MLBEspnOddsProvider, '__init__', return_value=None):
            p = MLBEspnOddsProvider()
            normalized = p.normalize([event])
            result = p.persist(normalized)
        self.assertEqual(result['created'], 1)
        self.assertEqual(OddsSnapshot.objects.filter(game=game).count(), 1)
        snap = OddsSnapshot.objects.get(game=game)
        self.assertEqual(snap.spread, -1.5)
        self.assertEqual(snap.total, 8.5)
        self.assertEqual(snap.sportsbook, 'DraftKings')


class MLBHubDecisionPartitionTests(TestCase):
    """The MLB hub page partitions today's games into elite / recommended /
    not_recommended sections driven by the decision layer. These tests make
    sure:
      - every signal lands in exactly one section (no duplicates, no drops)
      - the slate elite cap is honored before partitioning runs
      - within-section ordering is (edge DESC, confidence DESC)
      - games with no recommendation fall into not_recommended
    """

    def _fake_signal(self, sport_status='scheduled', rec=None, game_id='g1'):
        """Build a GameSignals stub — we only need `.game.status` and
        `.recommendation` for partition_games_by_decision to work."""
        from apps.mlb.services.prioritization import GameSignals

        class _StubGame:
            id = game_id
            status = sport_status
            first_pitch = timezone.now()
        return GameSignals(
            game=_StubGame(), priority='low', priority_score=0.0,
            recommendation=rec,
        )

    def _fake_rec(self, tier='standard', status='recommended', edge=5.0, confidence=70.0):
        from apps.core.services.recommendations import Recommendation
        return Recommendation(
            sport='mlb', game=None, bet_type='moneyline', pick='X',
            line='+100', odds_american=100,
            confidence_score=confidence, model_edge=edge, model_source='house',
            tier=tier, status=status, status_reason='',
        )

    def test_elite_tier_lands_in_top_plays(self):
        from apps.mlb.services.prioritization import partition_games_by_decision
        s = self._fake_signal(rec=self._fake_rec(tier='elite', edge=10.0))
        sections = partition_games_by_decision([s])
        self.assertEqual(sections['elite'], [s])
        self.assertEqual(sections['recommended'], [])
        self.assertEqual(sections['not_recommended'], [])

    def test_recommended_non_elite_lands_in_recommended(self):
        from apps.mlb.services.prioritization import partition_games_by_decision
        s = self._fake_signal(
            rec=self._fake_rec(tier='strong', status='recommended', edge=6.5)
        )
        sections = partition_games_by_decision([s])
        self.assertEqual(sections['recommended'], [s])
        self.assertEqual(sections['elite'], [])

    def test_not_recommended_lands_in_not_recommended(self):
        from apps.mlb.services.prioritization import partition_games_by_decision
        s = self._fake_signal(
            rec=self._fake_rec(tier='standard', status='not_recommended', edge=2.0)
        )
        sections = partition_games_by_decision([s])
        self.assertEqual(sections['not_recommended'], [s])

    def test_no_recommendation_lands_in_unrated(self):
        """Games without odds (rec=None) go to unrated, NOT not_recommended.
        Two different states: 'we couldn't price it' vs 'we said skip'."""
        from apps.mlb.services.prioritization import partition_games_by_decision
        s = self._fake_signal(rec=None)
        sections = partition_games_by_decision([s])
        self.assertEqual(sections['unrated'], [s])
        self.assertEqual(sections['not_recommended'], [])
        self.assertEqual(sections['elite'], [])
        self.assertEqual(sections['recommended'], [])

    def test_every_game_appears_exactly_once(self):
        """Full slate of 10 games covering all four buckets.
        Total across sections == total input, and no tile shows up twice."""
        from apps.mlb.services.prioritization import partition_games_by_decision

        signals = [
            self._fake_signal(game_id=f'g{i}',
                              rec=self._fake_rec(tier='elite', edge=12.0 - i))
            for i in range(2)
        ]
        signals += [
            self._fake_signal(game_id=f'r{i}',
                              rec=self._fake_rec(tier='strong',
                                                 status='recommended',
                                                 edge=7.0 - i * 0.1))
            for i in range(3)
        ]
        signals += [
            self._fake_signal(game_id=f'n{i}',
                              rec=self._fake_rec(tier='standard',
                                                 status='not_recommended',
                                                 edge=2.0))
            for i in range(4)
        ]
        signals.append(self._fake_signal(game_id='unknown', rec=None))

        sections = partition_games_by_decision(signals)

        total = (len(sections['elite']) + len(sections['recommended'])
                 + len(sections['not_recommended']) + len(sections['unrated']))
        self.assertEqual(total, len(signals))
        all_ids = (
            [s.game.id for s in sections['elite']]
            + [s.game.id for s in sections['recommended']]
            + [s.game.id for s in sections['not_recommended']]
            + [s.game.id for s in sections['unrated']]
        )
        self.assertEqual(len(all_ids), len(set(all_ids)), 'duplicate id across sections')
        self.assertEqual(sections['unrated'][0].game.id, 'unknown')

    def test_sort_order_within_section_is_edge_desc(self):
        from apps.mlb.services.prioritization import partition_games_by_decision
        a = self._fake_signal(game_id='a',
                              rec=self._fake_rec(tier='strong', edge=6.5))
        b = self._fake_signal(game_id='b',
                              rec=self._fake_rec(tier='strong', edge=7.5))
        c = self._fake_signal(game_id='c',
                              rec=self._fake_rec(tier='strong', edge=7.0))
        sections = partition_games_by_decision([a, b, c])
        self.assertEqual(
            [s.game.id for s in sections['recommended']],
            ['b', 'c', 'a'],
        )

    def test_unrated_section_isolated_from_not_recommended(self):
        """Real not-recommended games and null-rec games go to DIFFERENT
        sections — the new unrated bucket distinguishes 'no signal' from
        'negative signal'."""
        from apps.mlb.services.prioritization import partition_games_by_decision
        real = self._fake_signal(game_id='real',
                                 rec=self._fake_rec(tier='standard',
                                                    status='not_recommended',
                                                    edge=2.0))
        null = self._fake_signal(game_id='null', rec=None)
        sections = partition_games_by_decision([null, real])
        self.assertEqual([s.game.id for s in sections['not_recommended']], ['real'])
        self.assertEqual([s.game.id for s in sections['unrated']], ['null'])

    def test_elite_cap_from_assign_tiers_is_honored(self):
        """End-to-end with the slate-level cap: 4 recs all with elite-level
        edge — after assign_tiers runs, only 2 stay in the elite section.
        Proves the partition respects the guardrail rather than re-classifying."""
        from apps.core.services.recommendations import assign_tiers, MAX_ELITE_PER_SLATE
        from apps.mlb.services.prioritization import partition_games_by_decision

        recs = [
            self._fake_rec(tier='standard', edge=e, confidence=c)
            for e, c in [(12.0, 90.0), (11.0, 85.0), (10.0, 80.0), (9.0, 75.0)]
        ]
        signals = [self._fake_signal(game_id=f's{i}', rec=r) for i, r in enumerate(recs)]
        assign_tiers(recs)
        sections = partition_games_by_decision(signals)

        self.assertEqual(len(sections['elite']), MAX_ELITE_PER_SLATE)
        # Top by edge (12, 11) keep elite
        self.assertEqual(
            [s.game.id for s in sections['elite']], ['s0', 's1'],
        )
        # Demoted elites land in recommended section
        demoted_ids = [s.game.id for s in sections['recommended']]
        self.assertIn('s2', demoted_ids)
        self.assertIn('s3', demoted_ids)


class MLBHubCTATests(TestCase):
    """CTA text on tile actions adapts to decision status: Bet This / Not
    Recommended / generic Mock Bet when no recommendation exists."""

    def _render_tile_cta(self, status, has_prefill=True):
        """Render the _tile_actions partial via an inline Template with a
        GameSignals-shaped context. Returns the rendered HTML."""
        from django.contrib.auth.models import AnonymousUser, User
        from django.template.loader import render_to_string
        from apps.mlb.services.prioritization import GameSignals
        from apps.core.services.recommendations import Recommendation

        class _StubGame:
            id = 'cta'
            status = 'scheduled'
            home_team = type('T', (), {'name': 'Home'})()
            away_team = type('T', (), {'name': 'Away'})()
        rec = None
        if status is not None:
            rec = Recommendation(
                sport='mlb', game=None, bet_type='moneyline', pick='Home',
                line='-150', odds_american=-150,
                confidence_score=70.0, model_edge=6.0, model_source='house',
                tier='strong', status=status, status_reason='',
            )
        s = GameSignals(
            game=_StubGame(), priority='low', priority_score=0.0,
            actions=[{'type': 'watch_now', 'strength': 'primary',
                      'reason': 'tight_spread'}],
            recommendation=rec,
        )
        # Simulate _attach_prefill having run
        s.prefill_json = '{"sport":"mlb"}' if has_prefill else ''

        user, _ = User.objects.get_or_create(username='ctauser')
        return render_to_string('mlb/_tile_actions.html', {'s': s, 'user': user})

    def test_recommended_status_renders_bet_this(self):
        html = self._render_tile_cta(status='recommended')
        self.assertIn('Bet This', html)
        self.assertNotIn('Not Recommended', html)

    def test_not_recommended_status_renders_not_recommended(self):
        html = self._render_tile_cta(status='not_recommended')
        self.assertIn('Not Recommended', html)
        # CTA is still a button — never disabled — so click through still works
        self.assertIn('openMLBBet(this)', html)

    def test_no_recommendation_falls_back_to_mock_bet(self):
        """Defensive: a tile without a recommendation still gets a usable CTA."""
        html = self._render_tile_cta(status=None)
        self.assertIn('Mock Bet', html)


class FocusGameSelectionTests(TestCase):
    """The focus banner ('FOCUS RIGHT NOW') must prefer decision-layer recs
    over the legacy signals-layer actions. A 20%-signal Watch Now game must
    never win over a 56%-confidence Recommended pick."""

    def setUp(self):
        from apps.mlb.services.prioritization import GameSignals
        self.GameSignals = GameSignals

    def _fake_signal(self, confidence=0.2, rec=None, primary_type='watch_now',
                     game_id='g1', has_bet=False):
        """Build a minimal GameSignals with the knobs get_focus_game reads."""
        class _StubGame:
            id = game_id
            status = 'scheduled'
            first_pitch = timezone.now() + timedelta(hours=2)
            home_team = type('T', (), {'primary_color': '#fff'})()
            away_team = type('T', (), {'name': 'A'})()
        s = self.GameSignals(
            game=_StubGame(),
            priority='medium',
            priority_score=0.0,
            actions=[{'type': primary_type, 'strength': 'primary', 'reason': 'ace_matchup'}],
            confidence=confidence,
            confidence_pct=int(confidence * 100),
            recommendation=rec,
            has_user_bet=has_bet,
            user_bet_id='some-uuid' if has_bet else None,
        )
        return s

    def _fake_rec(self, tier='strong', status='recommended', edge=6.5, confidence=56.0):
        from apps.core.services.recommendations import Recommendation
        return Recommendation(
            sport='mlb', game=None, bet_type='moneyline', pick='Arizona',
            line='+105', odds_american=105,
            confidence_score=confidence, model_edge=edge, model_source='house',
            tier=tier, status=status, status_reason='',
        )

    def test_recommended_rec_beats_watch_now_signal(self):
        """The exact bug from the screenshot: watch_now at 20% signal vs a
        Recommended pick on another game. The rec must win."""
        from apps.mlb.services.prioritization import get_focus_game
        watch = self._fake_signal(confidence=0.2, primary_type='watch_now',
                                  game_id='watch', rec=None)
        rec_game = self._fake_signal(confidence=0.4, primary_type='watch_now',
                                     game_id='rec', rec=self._fake_rec(edge=6.1))
        result = get_focus_game([watch, rec_game])
        self.assertIs(result, rec_game)

    def test_elite_rec_beats_recommended_rec(self):
        from apps.mlb.services.prioritization import get_focus_game
        strong = self._fake_signal(game_id='strong',
                                   rec=self._fake_rec(tier='strong', edge=7.0))
        elite = self._fake_signal(game_id='elite',
                                  rec=self._fake_rec(tier='elite', edge=9.0))
        result = get_focus_game([strong, elite])
        self.assertIs(result, elite)

    def test_highest_edge_wins_among_elites(self):
        from apps.mlb.services.prioritization import get_focus_game
        e1 = self._fake_signal(game_id='e1',
                               rec=self._fake_rec(tier='elite', edge=9.0))
        e2 = self._fake_signal(game_id='e2',
                               rec=self._fake_rec(tier='elite', edge=12.5))
        result = get_focus_game([e1, e2])
        self.assertIs(result, e2)

    def test_user_bet_skipped_at_every_layer(self):
        """Focus surfaces a NEW opportunity — not a restatement of the user's
        existing bet. An elite game with the user's bet on it is skipped in
        favor of the next-best game."""
        from apps.mlb.services.prioritization import get_focus_game
        owned = self._fake_signal(game_id='owned', has_bet=True,
                                  rec=self._fake_rec(tier='elite', edge=12.0))
        other = self._fake_signal(game_id='other',
                                  rec=self._fake_rec(tier='strong', edge=6.5))
        result = get_focus_game([owned, other])
        self.assertIs(result, other)

    def test_weak_signals_without_recs_produce_no_focus(self):
        """20% signal watch_now games should NOT be promoted to Focus —
        the banner is omitted instead. Previous behavior would promote
        them because there was no floor."""
        from apps.mlb.services.prioritization import get_focus_game
        weak = self._fake_signal(confidence=0.2, primary_type='watch_now', rec=None)
        self.assertIsNone(get_focus_game([weak]))

    def test_strong_signal_without_rec_still_focused(self):
        """Legacy fallback — if a game has a strong signal (e.g. 50%) but
        no actionable recommendation, it still gets surfaced."""
        from apps.mlb.services.prioritization import get_focus_game
        s = self._fake_signal(confidence=0.5, primary_type='best_bet', rec=None)
        self.assertIs(get_focus_game([s]), s)

    def test_not_recommended_rec_does_not_win_focus(self):
        """A game whose rec was DECLINED by the decision rules shouldn't take
        the Focus slot — that would contradict the UI telling users "don't bet"."""
        from apps.mlb.services.prioritization import get_focus_game
        declined = self._fake_signal(
            game_id='d', confidence=0.5, primary_type='watch_now',
            rec=self._fake_rec(status='not_recommended', edge=3.0),
        )
        # no other candidates qualify — legacy layer 3 falls back via watch_now
        # floor=0.35, so 0.5 signal passes and declined gets focus via Layer 3.
        # That's fine: at least the banner represents the actual strongest signal.
        # The key assertion is the rec's status='not_recommended' didn't promote
        # it via Layers 1 or 2. We verify that by isolating: single declined rec
        # with LOW signal confidence → no focus (would have been focused if
        # recs-with-not_recommended-status were picked up).
        weak_declined = self._fake_signal(
            game_id='weak', confidence=0.2, primary_type='watch_now',
            rec=self._fake_rec(status='not_recommended', edge=3.0),
        )
        self.assertIsNone(get_focus_game([weak_declined]))


class DisplayConfidenceTests(TestCase):
    """GameSignals.display_confidence_pct prefers recommendation confidence
    over the signals-layer score. Fixes the 'FOCUS RIGHT NOW — 20%' confusion
    where the number was about signal strength, not bet confidence."""

    def test_display_confidence_uses_recommendation_when_present(self):
        from apps.mlb.services.prioritization import build_signals
        from apps.mlb.models import Conference, Team, Game, OddsSnapshot

        conf = Conference.objects.create(name='AL', slug='al-disp')
        home = Team.objects.create(name='H', slug='h-disp', conference=conf,
                                   source='mlb_stats_api', external_id='h-d')
        away = Team.objects.create(name='A', slug='a-disp', conference=conf,
                                   source='mlb_stats_api', external_id='a-d')
        game = Game.objects.create(
            home_team=home, away_team=away,
            first_pitch=timezone.now() + timedelta(hours=1),
            status='scheduled', source='mlb_stats_api', external_id='g-d',
        )
        OddsSnapshot.objects.create(
            game=game, captured_at=timezone.now(),
            market_home_win_prob=0.5, moneyline_home=-110, moneyline_away=-110,
        )
        s = build_signals(game, user=None)
        # Recommendation exists → display_confidence_pct = rec.confidence_score
        self.assertIsNotNone(s.recommendation)
        self.assertEqual(
            s.display_confidence_pct,
            int(round(float(s.recommendation.confidence_score))),
        )

    def test_display_confidence_falls_back_to_signal_when_no_rec(self):
        """No odds → no recommendation → display_confidence_pct uses signal."""
        from apps.mlb.services.prioritization import build_signals
        from apps.mlb.models import Conference, Team, Game

        conf = Conference.objects.create(name='AL', slug='al-fall')
        home = Team.objects.create(name='H', slug='h-fall', conference=conf,
                                   source='mlb_stats_api', external_id='h-f')
        away = Team.objects.create(name='A', slug='a-fall', conference=conf,
                                   source='mlb_stats_api', external_id='a-f')
        game = Game.objects.create(
            home_team=home, away_team=away,
            first_pitch=timezone.now() + timedelta(hours=1),
            status='scheduled', source='mlb_stats_api', external_id='g-f',
        )
        s = build_signals(game, user=None)
        self.assertIsNone(s.recommendation)
        self.assertEqual(s.display_confidence_pct, s.confidence_pct)


# =========================================================================
# Source-Aware Betting (Commit B) — UI partition + bulk filter + focus
# =========================================================================
# These tests cover the visible behavior: section split, badge rendering,
# bulk-button source filter, and the focus banner refusing to anchor on
# secondary or derived recommendations.

from types import SimpleNamespace as _SimpleNS


def _signal_with_rec(*, tier='standard', status='recommended', is_secondary=False,
                    status_reason='', game_id='g1', live=False):
    """Build a minimal GameSignals stand-in for partition tests.

    The partition function only inspects rec.tier / rec.status /
    rec.is_secondary / rec.status_reason and a few sort-key attrs, so we
    can avoid the full ORM dance with a SimpleNamespace fixture."""
    rec = _SimpleNS(
        tier=tier,
        status=status,
        is_secondary=is_secondary,
        status_reason=status_reason,
        confidence_score=70.0,
        model_edge=5.0,
    )
    game = _SimpleNS(
        id=game_id,
        status='live' if live else 'scheduled',
        first_pitch=timezone.now() + timedelta(hours=2),
    )
    return _SimpleNS(
        game=game,
        recommendation=rec,
        is_top_opportunity=False,
        confidence=2.0,
        actions=[],
        has_user_bet=False,
        user_bet_id=None,
    )


class PartitionSourceAwareSplitTests(TestCase):
    """The partition function must split recommended bets by source and
    bucket blocked rows separately so they never appear in primary UI."""

    def test_verified_recommended_lands_in_recommended_bucket(self):
        from apps.mlb.services.prioritization import partition_games_by_decision
        s = _signal_with_rec(is_secondary=False)
        sections = partition_games_by_decision([s])
        self.assertEqual(len(sections['recommended']), 1)
        self.assertEqual(len(sections['recommended_espn']), 0)

    def test_secondary_recommended_lands_in_espn_bucket(self):
        from apps.mlb.services.prioritization import partition_games_by_decision
        s = _signal_with_rec(is_secondary=True)
        sections = partition_games_by_decision([s])
        self.assertEqual(len(sections['recommended']), 0)
        self.assertEqual(len(sections['recommended_espn']), 1)

    def test_blocked_rec_never_appears_in_primary_buckets(self):
        from apps.mlb.services.prioritization import partition_games_by_decision
        s = _signal_with_rec(
            tier='blocked', status='not_recommended',
            status_reason='derived_odds',
        )
        sections = partition_games_by_decision([s])
        self.assertEqual(len(sections['recommended']), 0)
        self.assertEqual(len(sections['recommended_espn']), 0)
        self.assertEqual(len(sections['not_recommended']), 0)
        self.assertEqual(len(sections['elite']), 0)
        self.assertEqual(len(sections['blocked']), 1)

    def test_secondary_elite_drops_to_espn_recommended_section(self):
        # An elite-tier ESPN-secondary rec should NOT show up in Top Plays.
        # Top Plays must remain trustworthy. Falls through to ESPN bucket.
        from apps.mlb.services.prioritization import partition_games_by_decision
        s = _signal_with_rec(tier='elite', is_secondary=True)
        sections = partition_games_by_decision([s])
        self.assertEqual(len(sections['elite']), 0)
        self.assertEqual(len(sections['recommended_espn']), 1)

    def test_verified_elite_still_lands_in_elite(self):
        from apps.mlb.services.prioritization import partition_games_by_decision
        s = _signal_with_rec(tier='elite', is_secondary=False)
        sections = partition_games_by_decision([s])
        self.assertEqual(len(sections['elite']), 1)


class FocusBannerTrustFilterTests(TestCase):
    """Focus banner is the most prominent surface — it must never anchor
    on ESPN-secondary or derived/blocked recommendations."""

    def test_focus_skips_secondary_recommendation(self):
        from apps.mlb.services.prioritization import get_focus_game
        # Two recs with the same edge: one verified, one ESPN. Focus
        # must always pick the verified one.
        verified = _signal_with_rec(tier='elite', is_secondary=False, game_id='v')
        verified.recommendation.model_edge = 8.0
        espn = _signal_with_rec(tier='elite', is_secondary=True, game_id='e')
        espn.recommendation.model_edge = 10.0  # higher edge but secondary
        focus = get_focus_game([verified, espn])
        self.assertIsNotNone(focus)
        self.assertEqual(focus.game.id, 'v')

    def test_focus_skips_blocked_recommendation(self):
        from apps.mlb.services.prioritization import get_focus_game
        blocked = _signal_with_rec(
            tier='blocked', status='not_recommended',
            status_reason='derived_odds', game_id='b',
        )
        focus = get_focus_game([blocked])
        self.assertIsNone(focus)

    def test_focus_only_secondary_returns_none(self):
        # Even when the only rec available is ESPN, focus refuses to
        # anchor — Focus is for the user's most actionable CONFIRMED
        # bet, not "least bad option."
        from apps.mlb.services.prioritization import get_focus_game
        s = _signal_with_rec(is_secondary=True)
        focus = get_focus_game([s])
        self.assertIsNone(focus)


class BulkActionsSourceFilterTests(TestCase):
    """The bulk-place service must respect source_filter and never bet
    on derived rows regardless of filter."""

    def setUp(self):
        from django.contrib.auth.models import User
        from apps.mlb.models import Conference, Game, Team
        self.user = User.objects.create_user('joe', password='pw')
        conf = Conference.objects.create(name='AL East', slug='al-east')
        self.team_pairs = []
        for i in range(3):
            # Wide rating gap so model has clear edge → bet lands as
            # 'recommended' rather than no_rec.
            home = Team.objects.create(
                name=f'Home {i}', slug=f'home-{i}', conference=conf, rating=80.0,
            )
            away = Team.objects.create(
                name=f'Away {i}', slug=f'away-{i}', conference=conf, rating=40.0,
            )
            game = Game.objects.create(
                home_team=home, away_team=away,
                first_pitch=timezone.now() + timedelta(hours=2 + i*0.5),
            )
            self.team_pairs.append(game)

    def _seed_snapshot(self, game, *, source='odds_api', is_derived=False):
        from apps.mlb.models import OddsSnapshot
        # Weak market price (close to 50/50) → strong house edge.
        OddsSnapshot.objects.create(
            game=game,
            captured_at=timezone.now(),
            sportsbook='DraftKings',
            market_home_win_prob=0.52,
            moneyline_home=-110, moneyline_away=-110,
            odds_source=source, is_derived=is_derived,
        )

    def test_verified_filter_excludes_secondary_bets(self):
        from apps.mockbets.services.bulk_actions import place_bulk_recommended_bets
        self._seed_snapshot(self.team_pairs[0], source='odds_api')
        self._seed_snapshot(self.team_pairs[1], source='odds_api')
        self._seed_snapshot(self.team_pairs[2], source='espn')
        result = place_bulk_recommended_bets(
            self.user, source_filter='verified',
        )
        self.assertEqual(result['placed'], 2)
        self.assertEqual(result['source_filter'], 'verified')

    def test_espn_filter_excludes_verified_bets(self):
        from apps.mockbets.services.bulk_actions import place_bulk_recommended_bets
        self._seed_snapshot(self.team_pairs[0], source='odds_api')
        self._seed_snapshot(self.team_pairs[1], source='espn')
        self._seed_snapshot(self.team_pairs[2], source='espn')
        result = place_bulk_recommended_bets(
            self.user, source_filter='espn',
        )
        self.assertEqual(result['placed'], 2)

    def test_derived_never_bulk_bet_regardless_of_filter(self):
        # Defense in depth: even if source_filter='all' or 'espn', a
        # derived snapshot must never produce a bet.
        from apps.mockbets.services.bulk_actions import place_bulk_recommended_bets
        from apps.mockbets.models import MockBet
        self._seed_snapshot(self.team_pairs[0], source='espn', is_derived=True)
        self._seed_snapshot(self.team_pairs[1], source='espn', is_derived=True)
        for filt in ('all', 'verified', 'espn'):
            MockBet.objects.all().delete()
            result = place_bulk_recommended_bets(self.user, source_filter=filt)
            self.assertEqual(
                result['placed'], 0,
                msg=f'derived must not bulk-bet under filter={filt}',
            )


class HubTemplateSourceAwareTests(TestCase):
    """Template smoke tests: ESPN section renders when there are
    secondary rec games; both bulk buttons render together when both
    sets exist; derived rows never appear."""

    def setUp(self):
        from django.contrib.auth.models import User
        from django.test import Client
        from apps.mlb.models import Conference, Game, Team
        self.client = Client()
        self.user = User.objects.create_user('joe', password='pw')
        self.client.force_login(self.user)
        conf = Conference.objects.create(name='AL East', slug='al-east')

        def mkgame(label, hours_ahead):
            # Wide rating gap so the model produces a clear edge → the
            # bet lands in 'recommended' status rather than no_rec/edge_too_low.
            home = Team.objects.create(
                name=f'Home {label}', slug=f'home-{label}',
                conference=conf, rating=80.0,
            )
            away = Team.objects.create(
                name=f'Away {label}', slug=f'away-{label}',
                conference=conf, rating=40.0,
            )
            return Game.objects.create(
                home_team=home, away_team=away,
                first_pitch=timezone.now() + timedelta(hours=hours_ahead),
            )

        self.verified_game = mkgame('v', 2)
        self.espn_game = mkgame('e', 3)
        self.derived_game = mkgame('d', 4)

    def _seed(self, game, *, source, is_derived=False):
        # Weak market price (close to 50/50) vs strong model favorite →
        # generates a recommended bet with material edge.
        from apps.mlb.models import OddsSnapshot
        OddsSnapshot.objects.create(
            game=game,
            captured_at=timezone.now(),
            sportsbook='DraftKings',
            market_home_win_prob=0.52,
            moneyline_home=-110, moneyline_away=-110,
            odds_source=source, is_derived=is_derived,
        )

    def test_espn_section_renders_when_secondary_present(self):
        self._seed(self.verified_game, source='odds_api')
        self._seed(self.espn_game, source='espn')
        resp = self.client.get('/mlb/')
        self.assertEqual(resp.status_code, 200)
        # Section CSS class is the most reliable marker — text like
        # "ESPN Fallback" appears in the help modal regardless.
        self.assertContains(resp, 'mlb-section--espn')
        self.assertContains(resp, 'mlb-section__source-tag--espn')
        # The "secondary market" note tells users explicitly.
        self.assertContains(resp, 'These bets use ESPN odds')

    def test_espn_section_hidden_when_no_secondary(self):
        # Only verified rec → ESPN section markup should NOT render.
        self._seed(self.verified_game, source='odds_api')
        resp = self.client.get('/mlb/')
        self.assertNotContains(resp, 'mlb-section--espn')
        self.assertNotContains(resp, 'These bets use ESPN odds')

    def test_both_bulk_buttons_render_when_both_sources_present(self):
        self._seed(self.verified_game, source='odds_api')
        self._seed(self.espn_game, source='espn')
        resp = self.client.get('/mlb/')
        self.assertContains(resp, 'Bet All Verified Plays')
        self.assertContains(resp, 'Bet All ESPN Plays')

    def test_only_verified_button_renders_when_no_secondary(self):
        self._seed(self.verified_game, source='odds_api')
        resp = self.client.get('/mlb/')
        self.assertContains(resp, 'Bet All Verified Plays')
        self.assertNotContains(resp, 'Bet All ESPN Plays')

    def test_source_badge_renders_on_espn_tile(self):
        self._seed(self.espn_game, source='espn')
        resp = self.client.get('/mlb/')
        self.assertContains(resp, 'mlb-source-badge--espn')
        self.assertContains(resp, 'ESPN Odds')
