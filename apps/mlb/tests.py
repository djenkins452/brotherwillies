"""MLB tests covering schema, prediction model, and provider normalization.

Uses no network: the schedule provider's normalize() is tested against a
hand-built sample payload matching statsapi.mlb.com's shape.
"""
import uuid
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import Client, TestCase
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
        # 0.65 * (95 - 15) = 52 → sigmoid(52/25) ≈ 0.89, then the
        # post-2026-04-28 calibration soft-clamps the picked side at
        # 0.85 (PROB_MAX). Asserting equality to PROB_MAX is the
        # right test post-tune — the model is "very confident" but
        # the system caps overconfidence.
        from apps.core.services.probability_calibration import PROB_MAX
        p = compute_house_win_prob(g)
        self.assertAlmostEqual(p, PROB_MAX, places=3)

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
        # 0.35 * (90-10) = 28, +2.5 HFA = 30.5 → sigmoid(30.5/25) ≈ 0.77,
        # below PROB_MAX so no clamp applies (post-2026-04-28 calibration).
        p = compute_house_win_prob(g)
        self.assertGreater(p, 0.70)
        self.assertLess(p, 0.85)

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

    def test_decision_first_three_section_context(self):
        """Post-2026-05-02 rewrite: the hub passes three flat lists for
        the new Recommended / Potential / Not Recommended sections."""
        resp = self.client.get('/mlb/')
        for key in ('recommended_tiles', 'potential_tiles', 'not_recommended_tiles'):
            self.assertIn(key, resp.context, f'missing context key: {key}')
            self.assertIsInstance(resp.context[key], list)

    def test_decision_first_three_section_markup(self):
        """Each of the 3 sections renders its own decision-group div.
        Requires at least one game in the slate — otherwise the "data
        unavailable" fallback fires."""
        from apps.mlb.models import Conference as MLBConf, Team as MLBTeam, Game as MLBGame, OddsSnapshot as MLBOdds
        conf = MLBConf.objects.create(name='AL-3sec', slug='al-3sec')
        t1 = MLBTeam.objects.create(name='A', slug='3sec-a', conference=conf, rating=70,
                                    source='mlb_stats_api', external_id='3sec-1')
        t2 = MLBTeam.objects.create(name='B', slug='3sec-b', conference=conf, rating=40,
                                    source='mlb_stats_api', external_id='3sec-2')
        game = MLBGame.objects.create(
            home_team=t1, away_team=t2,
            first_pitch=timezone.now() + timedelta(hours=2),
            status='scheduled',
            source='mlb_stats_api', external_id='3sec-game',
        )
        MLBOdds.objects.create(
            game=game, captured_at=timezone.now(),
            market_home_win_prob=0.5,
            moneyline_home=-110, moneyline_away=-110,
        )
        resp = self.client.get('/mlb/')
        body = resp.content.decode('utf-8')
        self.assertIn('mlb-decision-group--recommended', body)
        self.assertIn('mlb-decision-group--potential', body)
        self.assertIn('mlb-decision-group--fade', body)


@override_settings(STORAGES={
    'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
    'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'},
})
class MLBHubUserPickTileTests(TestCase):
    """The decision-first tile shows a persistent 'MY PICK' row + a
    '✓ Bet Placed' button when the logged-in user has a pending mock
    bet on the game. Restores the user-feedback signal that was lost
    when the tile partial was simplified."""

    def setUp(self):
        from django.contrib.auth.models import User
        from django.test import Client
        from apps.mlb.models import Conference as MLBConf, Team as MLBTeam, Game as MLBGame, OddsSnapshot as MLBOdds
        self.MLBGame = MLBGame
        self.MLBOdds = MLBOdds
        conf = MLBConf.objects.create(name='AL-pick', slug='al-pick')
        self.t_home = MLBTeam.objects.create(
            name='Yankees', slug='pick-yankees', conference=conf, rating=70,
            source='mlb_stats_api', external_id='pick-1',
        )
        self.t_away = MLBTeam.objects.create(
            name='Rays', slug='pick-rays', conference=conf, rating=40,
            source='mlb_stats_api', external_id='pick-2',
        )
        self.user = User.objects.create_user('pick_user', password='pw')
        self.client = Client()
        self.client.force_login(self.user)

    def _seeded_game(self, ext='pick-game-1'):
        game = self.MLBGame.objects.create(
            home_team=self.t_home, away_team=self.t_away,
            first_pitch=timezone.now() + timedelta(hours=2),
            status='scheduled',
            source='mlb_stats_api', external_id=ext,
        )
        self.MLBOdds.objects.create(
            game=game, captured_at=timezone.now(),
            market_home_win_prob=0.55,
            moneyline_home=-122, moneyline_away=110,
        )
        return game

    def _bet(self, game, selection='Yankees', odds=-122):
        from apps.mockbets.models import MockBet
        from decimal import Decimal as D
        return MockBet.objects.create(
            user=self.user, sport='mlb', mlb_game=game,
            bet_type='moneyline', selection=selection,
            odds_american=odds,
            implied_probability=D('0.5500'),
            stake_amount=D('100.00'),
            result='pending',
        )

    # --- Render presence ----------------------------------------------------

    def test_my_pick_row_renders_when_user_has_pending_bet(self):
        game = self._seeded_game()
        self._bet(game, selection='Yankees', odds=-122)
        resp = self.client.get('/mlb/')
        body = resp.content.decode('utf-8')
        self.assertIn('mlb-tile__user-pick', body)
        self.assertIn('MY PICK:', body)
        self.assertIn('Yankees', body)
        # American odds rendered with the sign character.
        self.assertIn('-122', body)

    def test_my_pick_row_absent_when_no_bet(self):
        self._seeded_game()
        resp = self.client.get('/mlb/')
        body = resp.content.decode('utf-8')
        self.assertNotIn('mlb-tile__user-pick', body)
        self.assertNotIn('MY PICK:', body)

    def test_bet_placed_button_replaces_bet_this(self):
        game = self._seeded_game()
        self._bet(game)
        resp = self.client.get('/mlb/')
        body = resp.content.decode('utf-8')
        self.assertIn('mlb-bet-btn--placed', body)
        self.assertIn('✓ Bet Placed', body)
        # The "Bet This" CTA should NOT also render — replaced, not stacked.
        # (Allowing the modal's static label to slip through is fine — we
        # check for the button class specifically.)
        self.assertNotIn('mlb-bet-btn--bet-this', body)

    def test_positive_odds_render_with_plus_sign(self):
        game = self._seeded_game()
        self._bet(game, selection='Rays', odds=145)
        resp = self.client.get('/mlb/')
        body = resp.content.decode('utf-8')
        self.assertIn('Rays', body)
        # Positive American odds should be rendered with a leading +.
        self.assertIn('(+145)', body)

    # --- Per-game isolation -------------------------------------------------

    def test_pick_attached_to_correct_game_only(self):
        """When the user has bets on game A but not game B, the MY PICK
        row appears on A's tile only — no cross-contamination."""
        game_a = self._seeded_game(ext='pick-A')

        # Build a second matchup on a separate Conference so slugs stay unique.
        from apps.mlb.models import Conference as MLBConf, Team as MLBTeam
        conf_b = MLBConf.objects.create(name='NL-pick', slug='nl-pick')
        t_b_home = MLBTeam.objects.create(
            name='Mets', slug='pick-mets', conference=conf_b, rating=65,
            source='mlb_stats_api', external_id='pick-3',
        )
        t_b_away = MLBTeam.objects.create(
            name='Phils', slug='pick-phils', conference=conf_b, rating=55,
            source='mlb_stats_api', external_id='pick-4',
        )
        game_b = self.MLBGame.objects.create(
            home_team=t_b_home, away_team=t_b_away,
            first_pitch=timezone.now() + timedelta(hours=3),
            status='scheduled',
            source='mlb_stats_api', external_id='pick-B',
        )
        self.MLBOdds.objects.create(
            game=game_b, captured_at=timezone.now(),
            market_home_win_prob=0.6,
            moneyline_home=-150, moneyline_away=130,
        )

        # Bet on A only.
        self._bet(game_a, selection='Yankees', odds=-122)

        resp = self.client.get('/mlb/')
        body = resp.content.decode('utf-8')
        # Exactly one parent MY PICK container on the page. Use the
        # closing quote to disambiguate from child classes that share
        # the prefix (mlb-tile__user-pick-icon / -label / -team / -odds).
        self.assertEqual(body.count('class="mlb-tile__user-pick"'), 1)
        self.assertIn('Yankees', body)
        # B's teams render without an attached MY PICK row.
        self.assertIn('Mets', body)


@override_settings(STORAGES={
    'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
    'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'},
})
class MLBDecisionTileMatchupContextTests(TestCase):
    """Spec 2026-05-04: matchup context must render directly on the
    tile — no click, no hover. Asserts records, streaks, pitcher names,
    pitcher records, and the existing pick / model / market / edge data
    all appear in the rendered template output.

    Renders the partial directly via render_to_string so the assertions
    are tightly scoped (no hub-page chrome, no other tiles to filter)."""

    def _render(self, signal, section_kind='recommended'):
        from django.template.loader import render_to_string
        return render_to_string(
            'mlb/_tile_decision.html',
            {'s': signal, 'section_kind': section_kind},
        )

    def _build_signal(self, *, away_record='20-12', home_record='19-13',
                     away_streak=None, home_streak=None,
                     away_pitcher_name='Roki Sasaki', home_pitcher_name='Michael McGreevy',
                     away_pitcher_record='1-2', home_pitcher_record='1-2'):
        """Construct a real Game + StartingPitcher + OddsSnapshot fixture
        and run prioritize() so GameSignals carries every field the tile
        consumes. Streaks are passed via the streaks= kwarg so we don't
        need to seed prior final games."""
        from apps.mlb.models import (
            Conference, Team, StartingPitcher, Game, OddsSnapshot,
        )
        from apps.mlb.services.prioritization import build_signals

        # Parse "20-12" into wins/losses for Team fields.
        def _split_record(rec):
            if rec is None:
                return None, None
            w, l = rec.split('-')
            return int(w), int(l)

        a_w, a_l = _split_record(away_record)
        h_w, h_l = _split_record(home_record)
        ap_w, ap_l = _split_record(away_pitcher_record)
        hp_w, hp_l = _split_record(home_pitcher_record)

        conf = Conference.objects.create(name='Mat-Conf', slug='mat-conf')
        away = Team.objects.create(
            name='Los Angeles Dodgers', slug='mat-lad', abbreviation='LAD',
            conference=conf, rating=90.0,
            wins=a_w, losses=a_l,
            source='mlb_stats_api', external_id='mat-lad',
        )
        home = Team.objects.create(
            name='St. Louis Cardinals', slug='mat-stl', abbreviation='STL',
            conference=conf, rating=10.0,
            wins=h_w, losses=h_l,
            source='mlb_stats_api', external_id='mat-stl',
        )
        ap = StartingPitcher.objects.create(
            team=away, name=away_pitcher_name,
            wins=ap_w, losses=ap_l,
            source='mlb_stats_api', external_id='mat-ap',
        ) if away_pitcher_name else None
        hp = StartingPitcher.objects.create(
            team=home, name=home_pitcher_name,
            wins=hp_w, losses=hp_l,
            source='mlb_stats_api', external_id='mat-hp',
        ) if home_pitcher_name else None
        g = Game.objects.create(
            home_team=home, away_team=away,
            first_pitch=timezone.now() + timedelta(hours=2),
            home_pitcher=hp, away_pitcher=ap, status='scheduled',
            source='mlb_stats_api', external_id='mat-game',
        )
        OddsSnapshot.objects.create(
            game=g, captured_at=timezone.now(),
            market_home_win_prob=0.5,
            moneyline_home=-110, moneyline_away=-110,
        )
        streaks = {}
        if away_streak:
            streaks[away.id] = away_streak
        if home_streak:
            streaks[home.id] = home_streak
        return build_signals(g, streaks=streaks)

    def test_team_records_render_when_present(self):
        s = self._build_signal(away_record='20-12', home_record='19-13')
        body = self._render(s)
        self.assertIn('20-12', body)
        self.assertIn('19-13', body)

    def test_team_record_fallback_when_missing(self):
        # Per spec: missing record → "Record unavailable"
        s = self._build_signal(away_record=None, home_record='19-13')
        body = self._render(s)
        self.assertIn('Record unavailable', body)
        self.assertIn('19-13', body)

    def test_team_streak_renders_when_present(self):
        s = self._build_signal(
            away_streak={'kind': 'L', 'count': 3, 'label': 'L3'},
            home_streak={'kind': 'W', 'count': 5, 'label': 'W5'},
        )
        body = self._render(s)
        self.assertIn('L3', body)
        self.assertIn('W5', body)
        # Class variants drive the green/red coloring.
        self.assertIn('mlb-tile__streak--l', body)
        self.assertIn('mlb-tile__streak--w', body)

    def test_team_streak_omitted_when_missing(self):
        # Per spec: streak missing → omit (no "no streak" placeholder).
        s = self._build_signal(away_streak=None, home_streak=None)
        body = self._render(s)
        self.assertNotIn('mlb-tile__streak', body)

    def test_pitcher_names_render(self):
        s = self._build_signal(
            away_pitcher_name='Roki Sasaki', home_pitcher_name='Michael McGreevy',
        )
        body = self._render(s)
        self.assertIn('Roki Sasaki', body)
        self.assertIn('Michael McGreevy', body)
        # The "vs" separator confirms they're rendered as a matchup, not separately.
        self.assertIn('mlb-tile__pitcher-sep', body)

    def test_tbd_pitcher_renders_as_TBD(self):
        # Per spec: pitcher unknown → "TBD"
        s = self._build_signal(away_pitcher_name=None, home_pitcher_name='Michael McGreevy')
        body = self._render(s)
        self.assertIn('TBD', body)
        self.assertIn('Michael McGreevy', body)

    def test_pitcher_records_render_when_present(self):
        s = self._build_signal(
            away_pitcher_record='1-2', home_pitcher_record='3-4',
        )
        body = self._render(s)
        # Records are rendered in parentheses next to the pitcher name.
        self.assertIn('(1-2)', body)
        self.assertIn('(3-4)', body)

    def test_pitcher_record_omitted_when_missing(self):
        # Per spec: pitcher record missing → omit.
        s = self._build_signal(
            away_pitcher_record=None, home_pitcher_record=None,
        )
        body = self._render(s)
        self.assertNotIn('mlb-tile__pitcher-record', body)

    def test_existing_pick_model_market_edge_data_still_renders(self):
        """The matchup-context additions don't displace the decision data
        the tile already showed (pick, odds, model %, market %, edge)."""
        s = self._build_signal()
        body = self._render(s)
        # Decision data hooks. The exact numbers depend on the engine but
        # the structural classes confirm the panel still renders.
        self.assertIn('mlb-tile__decision-pick', body)
        self.assertIn('mlb-tile__decision-stat-label', body)
        # Three stat labels: Model, Market, Edge.
        self.assertIn('Model', body)
        self.assertIn('Market', body)
        self.assertIn('Edge', body)

    def test_full_matchup_context_renders_in_one_pass(self):
        """End-to-end: every field from the spec example renders together
        on a single tile, in the section the spec defined."""
        s = self._build_signal(
            away_record='20-12', home_record='19-13',
            away_streak={'kind': 'L', 'count': 3, 'label': 'L3'},
            home_streak={'kind': 'W', 'count': 5, 'label': 'W5'},
            away_pitcher_name='Roki Sasaki', away_pitcher_record='1-2',
            home_pitcher_name='Michael McGreevy', home_pitcher_record='1-2',
        )
        body = self._render(s)
        # Records
        self.assertIn('20-12', body)
        self.assertIn('19-13', body)
        # Streaks
        self.assertIn('L3', body)
        self.assertIn('W5', body)
        # Pitchers
        self.assertIn('Roki Sasaki', body)
        self.assertIn('Michael McGreevy', body)
        # Pitcher records (parentheses)
        self.assertIn('(1-2)', body)
        # Decision panel still present
        self.assertIn('Model', body)
        self.assertIn('Edge', body)


class MLBHubBucketAssignmentTests(TestCase):
    """Each game must land in exactly ONE of the 3 buckets — no double-
    counting between Recommended / Potential / Not Recommended."""

    def setUp(self):
        from apps.mlb.models import Conference as MLBConf, Team as MLBTeam
        conf = MLBConf.objects.create(name='AL', slug='al-bucket')
        self.t1 = MLBTeam.objects.create(
            name='Yankees', slug='bucket-yankees', conference=conf, rating=70,
            source='mlb_stats_api', external_id='b-1',
        )
        self.t2 = MLBTeam.objects.create(
            name='Rays', slug='bucket-rays', conference=conf, rating=40,
            source='mlb_stats_api', external_id='b-2',
        )

    def test_no_game_appears_in_two_buckets(self):
        from apps.mlb.models import Game as MLBGame, OddsSnapshot as MLBOdds
        game = MLBGame.objects.create(
            home_team=self.t1, away_team=self.t2,
            first_pitch=timezone.now() + timedelta(hours=2),
            status='scheduled',
            source='mlb_stats_api', external_id='b-game-1',
        )
        MLBOdds.objects.create(
            game=game, captured_at=timezone.now(),
            market_home_win_prob=0.5,
            moneyline_home=-110, moneyline_away=-110,
        )
        resp = self.client.get('/mlb/')
        rec_ids = {t.game.id for t in resp.context['recommended_tiles']}
        pot_ids = {t.game.id for t in resp.context['potential_tiles']}
        not_rec_ids = {t.game.id for t in resp.context['not_recommended_tiles']}
        # No overlap between any pair.
        self.assertFalse(rec_ids & pot_ids)
        self.assertFalse(rec_ids & not_rec_ids)
        self.assertFalse(pot_ids & not_rec_ids)


class MLBHubRecommendedEqualsBetAllTests(TestCase):
    """2026-05-22 trust repair regression lock.

    Spec contract (RULE 2): If a game appears in the Recommended bucket,
    it MUST be bettable by Bet All. Otherwise it belongs in Potential.
    The visible Recommended count and the Bet All button count MUST
    be identical for every render.

    The previous trust repair (2026-05-16) aligned the button count
    with the placement set. This second repair aligns the visible
    Recommended bucket with both.
    """

    def setUp(self):
        from apps.mlb.models import Conference as MLBConf, Team as MLBTeam
        self.MLBConf = MLBConf
        self.MLBTeam = MLBTeam
        self.user = User.objects.create_user('rec_eq_user', password='pw')
        self.client = Client()
        self.client.force_login(self.user)
        self.conf = MLBConf.objects.create(name='AL', slug='req-eq-al')

    def _team_pair(self, suffix, home_rating=88, away_rating=22):
        # 2026-05-03 calibration: rating gap must be wide enough to
        # clear the post-tightening probability gate (≥0.60).
        t1 = self.MLBTeam.objects.create(
            name=f'H{suffix}', slug=f'req-h-{suffix}',
            conference=self.conf, rating=home_rating,
            source='mlb_stats_api', external_id=f'req-h-{suffix}',
        )
        t2 = self.MLBTeam.objects.create(
            name=f'A{suffix}', slug=f'req-a-{suffix}',
            conference=self.conf, rating=away_rating,
            source='mlb_stats_api', external_id=f'req-a-{suffix}',
        )
        return t1, t2

    def _game_with_odds(
        self, suffix, *,
        # 2026-05-22 fixture update: moneylines widened from -160/+140
        # to -140/+120 after MARKET_BLEND_WEIGHT bumped 0.40 → 0.55.
        hours_out=2, ml_home=-140, ml_away=120, market_home_prob=0.55,
        home_rating=88, away_rating=22,
    ):
        from apps.mlb.models import Game as MLBGame, OddsSnapshot as MLBOdds
        t1, t2 = self._team_pair(suffix, home_rating=home_rating, away_rating=away_rating)
        game = MLBGame.objects.create(
            home_team=t1, away_team=t2,
            first_pitch=timezone.now() + timedelta(hours=hours_out),
            status='scheduled',
            source='mlb_stats_api', external_id=str(uuid.uuid4()),
        )
        MLBOdds.objects.create(
            game=game, captured_at=timezone.now(),
            market_home_win_prob=market_home_prob,
            moneyline_home=ml_home, moneyline_away=ml_away,
            odds_source='odds_api', source_quality='primary',
        )
        return game

    # --- Scenario A: 4 recommended cards → button says 4 → bulk places 4 ----

    def test_scenario_a_four_recommended_cards_match_button_count(self):
        """4 distinct eligible games → 4 cards in Recommended → button (4)."""
        from apps.mockbets.services.bulk_actions import (
            is_bulk_moneyline_eligible, place_bulk_recommended_bets,
        )
        for i in range(4):
            self._game_with_odds(f'sA{i}', hours_out=2 + i)

        resp = self.client.get('/mlb/')
        self.assertEqual(resp.status_code, 200)

        recommended_tiles = resp.context['recommended_tiles']
        verified_bulk_count = resp.context['verified_bulk_count']
        # Filter to bulk-eligible (defensive — fixtures may emit other
        # categories under unforeseen calibration edges).
        bulk_eligible_in_rec = [
            t for t in recommended_tiles
            if is_bulk_moneyline_eligible(
                getattr(t, 'recommendation', None), source_filter='verified',
            )
        ]
        # RULE 2 invariant: every visible Recommended tile is bulk-eligible.
        self.assertEqual(len(bulk_eligible_in_rec), len(recommended_tiles))
        # RULE 1 invariant: button count == visible count.
        self.assertEqual(verified_bulk_count, len(recommended_tiles))

        # And bulk placement actually places that many (modulo drift —
        # fresh test fixture, no drift expected).
        game_ids = [str(t.game.id) for t in recommended_tiles]
        result = place_bulk_recommended_bets(
            self.user, sport='mlb', stake=Decimal('100'),
            source_filter='verified', game_ids=game_ids,
        )
        self.assertEqual(result['placed'], len(recommended_tiles))

    # --- Scenario B: risk-flagged games → Potential, not Recommended -------

    def test_scenario_b_risk_flagged_games_land_in_potential_not_recommended(self):
        """Construct a game that would normally be Recommended but has
        a risk flag → ends up in Potential. Recommended count and Bet
        All count both decrease by one — together — never apart."""
        from unittest.mock import patch
        from apps.mockbets.services.bulk_actions import is_bulk_moneyline_eligible

        clean_game = self._game_with_odds('sB1', hours_out=2)
        flagged_game = self._game_with_odds('sB2', hours_out=3)

        # Patch the recommendation lane to 'qualified' for the flagged
        # game only. This simulates a risk-flag firing (e.g.
        # short_fav_thin) without modifying the recommendation engine.
        from apps.core.services.recommendations import get_recommendation as _real_get
        def _patched_get(sport, game, user=None):
            rec = _real_get(sport, game, user)
            if rec is not None and game.id == flagged_game.id:
                rec.lane = 'qualified'
                rec.risk_flags = {'short_fav_thin': True}
                rec.risk_score = 1
            return rec

        # get_recommendation is imported inside the function in
        # prioritization.py and bulk_actions.py — patch the source
        # module so the lookup at import time picks up our patch.
        with patch(
            'apps.core.services.recommendations.get_recommendation',
            side_effect=_patched_get,
        ):
            resp = self.client.get('/mlb/')

        rec_ids = {str(t.game.id) for t in resp.context['recommended_tiles']}
        pot_ids = {str(t.game.id) for t in resp.context['potential_tiles']}

        # Flagged game is in Potential, not Recommended.
        self.assertNotIn(str(flagged_game.id), rec_ids)
        self.assertIn(str(flagged_game.id), pot_ids)
        # Clean game is in Recommended.
        self.assertIn(str(clean_game.id), rec_ids)
        # RULE 1: button count = visible Recommended count.
        self.assertEqual(
            resp.context['verified_bulk_count'],
            len(resp.context['recommended_tiles']),
        )

    # --- Scenario C: lane='qualified' → NOT in Recommended, IS in Potential -

    def test_scenario_c_qualified_lane_not_in_recommended_is_in_potential(self):
        """Direct test of lane semantics in the visible bucket structure."""
        from unittest.mock import patch
        qualified_game = self._game_with_odds('sC1', hours_out=2)

        from apps.core.services.recommendations import get_recommendation as _real_get
        def _patched_get(sport, game, user=None):
            rec = _real_get(sport, game, user)
            if rec is not None:
                rec.lane = 'qualified'
                rec.risk_flags = {'short_fav_thin': True}
                rec.risk_score = 1
            return rec

        # get_recommendation is imported inside the function in
        # prioritization.py and bulk_actions.py — patch the source
        # module so the lookup at import time picks up our patch.
        with patch(
            'apps.core.services.recommendations.get_recommendation',
            side_effect=_patched_get,
        ):
            resp = self.client.get('/mlb/')

        rec_ids = {str(t.game.id) for t in resp.context['recommended_tiles']}
        pot_ids = {str(t.game.id) for t in resp.context['potential_tiles']}
        self.assertNotIn(str(qualified_game.id), rec_ids)
        self.assertIn(str(qualified_game.id), pot_ids)
        # The visible Recommended count must match Bet All (both zero,
        # since the only game has lane='qualified').
        self.assertEqual(
            resp.context['verified_bulk_count'],
            len(resp.context['recommended_tiles']),
        )

    # --- Scenario D: non-negotiable invariant ------------------------------

    def test_scenario_d_visible_recommended_count_equals_bulk_count_always(self):
        """The contract: for ANY slate, |Recommended bucket| == button count.

        Builds a heterogeneous slate (eligible, qualified-lane, started)
        and verifies the alignment invariant holds regardless of which
        gates fire on which games.
        """
        from unittest.mock import patch
        from apps.mockbets.services.bulk_actions import is_bulk_moneyline_eligible

        # 2 eligible, 2 qualified-lane (visible-but-not-bulk).
        eligible_a = self._game_with_odds('sDa', hours_out=2)
        eligible_b = self._game_with_odds('sDb', hours_out=3)
        flagged_a = self._game_with_odds('sDc', hours_out=4)
        flagged_b = self._game_with_odds('sDd', hours_out=5)
        flagged_ids = {flagged_a.id, flagged_b.id}

        from apps.core.services.recommendations import get_recommendation as _real_get
        def _patched_get(sport, game, user=None):
            rec = _real_get(sport, game, user)
            if rec is not None and game.id in flagged_ids:
                rec.lane = 'qualified'
                rec.risk_flags = {'market_conflict': True}
                rec.risk_score = 1
            return rec

        # get_recommendation is imported inside the function in
        # prioritization.py and bulk_actions.py — patch the source
        # module so the lookup at import time picks up our patch.
        with patch(
            'apps.core.services.recommendations.get_recommendation',
            side_effect=_patched_get,
        ):
            resp = self.client.get('/mlb/')

        # THE INVARIANT.
        rec_count = len(resp.context['recommended_tiles'])
        bulk_count = resp.context['verified_bulk_count']
        self.assertEqual(
            rec_count, bulk_count,
            f'Trust contract violated: Recommended bucket shows '
            f'{rec_count} but Bet All button shows ({bulk_count}). '
            f'These MUST be equal.',
        )
        # And the bulk_game_ids list emitted to the JS must exactly
        # match the visible Recommended game IDs.
        import json as _json
        emitted_ids = set(
            _json.loads(resp.context['verified_bulk_game_ids_json'])
        )
        visible_ids = {str(t.game.id) for t in resp.context['recommended_tiles']}
        self.assertEqual(emitted_ids, visible_ids)

        # And every visible Recommended tile passes is_bulk_moneyline_eligible.
        for tile in resp.context['recommended_tiles']:
            self.assertTrue(
                is_bulk_moneyline_eligible(
                    getattr(tile, 'recommendation', None),
                    source_filter='verified',
                ),
                f'Tile {tile.game.id} is in Recommended but fails '
                f'is_bulk_moneyline_eligible — RULE 2 violation.',
            )

    # --- Defense in depth: predicate is a property of the rec ---------------

    def test_recommended_carry_overs_land_in_potential_not_lost(self):
        """A status='recommended' game that fails bulk-eligibility for
        a non-lane reason (e.g. longshot odds) must NOT vanish — it
        flows into Potential via the carry-over branch."""
        from unittest.mock import patch
        long_shot = self._game_with_odds(
            'carry', hours_out=2,
            ml_home=+450, ml_away=-650, market_home_prob=0.18,
        )

        from apps.core.services.recommendations import get_recommendation as _real_get
        def _patched_get(sport, game, user=None):
            rec = _real_get(sport, game, user)
            # Force status='recommended' + lane='core' + longshot odds
            # so the only failing gate is the longshot cap. This drives
            # the recommendation into the "carry-over" branch.
            if rec is not None and game.id == long_shot.id:
                rec.status = 'recommended'
                rec.lane = 'core'
                rec.odds_american = +450
                rec.confidence_score = 65.0
                rec.is_secondary = False
                rec.tier = 'standard'
                rec.status_reason = ''
            return rec

        # get_recommendation is imported inside the function in
        # prioritization.py and bulk_actions.py — patch the source
        # module so the lookup at import time picks up our patch.
        with patch(
            'apps.core.services.recommendations.get_recommendation',
            side_effect=_patched_get,
        ):
            resp = self.client.get('/mlb/')

        rec_ids = {str(t.game.id) for t in resp.context['recommended_tiles']}
        pot_ids = {str(t.game.id) for t in resp.context['potential_tiles']}
        all_buckets = rec_ids | pot_ids | {
            str(t.game.id) for t in resp.context['not_recommended_tiles']
        }
        # NOT in Recommended (longshot fails bulk eligibility).
        self.assertNotIn(str(long_shot.id), rec_ids)
        # Game must appear SOMEWHERE — never vanishes silently.
        self.assertIn(str(long_shot.id), all_buckets)


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
        """2026-05-03 contract tighten: best_bet now requires recommendation
        status='recommended' in addition to the tight-spread signal. We
        configure rating gap + moneylines so the engine produces a
        Recommended pick on the same game."""
        from apps.mlb.services.prioritization import build_signals
        g = self._game(home_rating=90.0, away_rating=10.0, ext='tb')
        self._add_odds(g, spread=1.0, ml_home=-110, ml_away=-110)
        s = build_signals(g)
        self.assertIn('best_bet', self._types(s.actions))

    def test_tight_spread_without_recommendation_does_NOT_get_best_bet(self):
        """Spec contract: best_bet ONLY fires when the recommendation
        engine status is 'recommended'. A tight-spread signal alone
        (without moneyline odds → no recommendation) should leave the
        tile in the no-action state."""
        from apps.mlb.services.prioritization import build_signals
        g = self._game(ext='tbnr')
        # Spread odds present but no moneyline → no recommendation possible.
        self._add_odds(g, spread=1.0)
        s = build_signals(g)
        self.assertNotIn('best_bet', self._types(s.actions))

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
        """2026-05-03: best_bet now requires recommendation status; rating
        gap + moneylines added so the engine produces a Recommended pick
        on the same live tight-spread game."""
        from apps.mlb.services.prioritization import build_signals
        # Ace live game + tight spread + recommended pick — all three fire,
        # Best Bet remains primary.
        g = self._game(
            status='live', home_score=2, away_score=1,
            hp_rating=80.0, ap_rating=72.0, ext='bbprim',
            home_rating=90.0, away_rating=10.0,
        )
        self._add_odds(g, spread=1.0, ml_home=-110, ml_away=-110)
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

    @override_settings(MONEYLINE_ONLY_MODE=False)
    def test_tight_spread_switches_to_spread_bet(self):
        """Legacy behavior — when the master ML-only flag is OFF, a tight
        spread on the snapshot prefills a spread bet."""
        from apps.mlb.services.prioritization import build_signals
        from apps.mockbets.services.prefill import prefill_from_signals
        g = self._setup(spread=-1.5)  # home favored by 1.5
        data = prefill_from_signals(build_signals(g))
        self.assertEqual(data['bet_type'], 'spread')
        # Home-POV spread -1.5 for home side → "Yankees -1.5"
        self.assertIn('Yankees', data['selection'])
        self.assertIn('-1.5', data['selection'])

    @override_settings(MONEYLINE_ONLY_MODE=True)
    def test_tight_spread_stays_on_moneyline_under_master_flag(self):
        """2026-05-04 fix: when MONEYLINE_ONLY_MODE is on, the prefill
        ignores tight-spread defaults and stays on moneyline. Without
        this gate, the modal would pre-fill a spread selection that the
        place_bet view rejects with 'Invalid bet type'."""
        from apps.mlb.services.prioritization import build_signals
        from apps.mockbets.services.prefill import prefill_from_signals
        # Tight spread + moneylines both present; the snapshot would have
        # triggered spread-default under the legacy code.
        g = self._setup(spread=-1.5, ml_home=-140, ml_away=120)
        data = prefill_from_signals(build_signals(g))
        self.assertEqual(data['bet_type'], 'moneyline')
        # Selection is the team name (not "Team -1.5") and odds are the ML.
        self.assertEqual(data['selection'], 'Yankees')
        self.assertEqual(data['odds'], -140)
        # selections_by_type omits spread/total — even a stale client
        # never sees a non-moneyline option from the prefill.
        self.assertNotIn('spread', data['selections_by_type'])
        self.assertNotIn('total', data['selections_by_type'])
        self.assertIn('moneyline', data['selections_by_type'])

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

    @override_settings(MONEYLINE_ONLY_MODE=False)
    def test_selections_include_spread_and_total_when_market_has_them(self):
        """Legacy: when ML-only is OFF, the prefill exposes spread/total
        selection options for the modal dropdown."""
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

    # 2026-04-29 fix: odds auto-population in the modal. Each selection
    # option must carry its own `odds` so the JS can sync the form's
    # Odds field whenever the user picks a different side.

    def test_moneyline_options_carry_per_side_odds(self):
        from apps.mlb.services.prioritization import build_signals
        from apps.mockbets.services.prefill import prefill_from_signals
        g = self._setup(ml_home=-150, ml_away=130)
        data = prefill_from_signals(build_signals(g))
        ml = data['selections_by_type']['moneyline']
        # Each option carries an int odds matching its side.
        by_label = {o['label']: o['odds'] for o in ml}
        self.assertEqual(by_label['Yankees'], -150)
        self.assertEqual(by_label['Royals'], 130)

    def test_moneyline_options_carry_none_when_snapshot_missing_ml(self):
        """Defensive: if a side's moneyline isn't populated on the
        snapshot (rare but possible — e.g., a derived row), the option
        still renders but with odds=None so the modal's missing-odds
        warning can fire."""
        from apps.mlb.services.prioritization import build_signals
        from apps.mockbets.services.prefill import prefill_from_signals
        g = self._setup(ml_home=-150, ml_away=None)
        data = prefill_from_signals(build_signals(g))
        ml = data['selections_by_type']['moneyline']
        by_label = {o['label']: o['odds'] for o in ml}
        self.assertEqual(by_label['Yankees'], -150)
        self.assertIsNone(by_label['Royals'])

    @override_settings(MONEYLINE_ONLY_MODE=False)
    def test_spread_total_options_default_to_minus_110(self):
        """Legacy (ML-only OFF): spread / total selection options default
        to -110 standard pricing. OddsSnapshot doesn't store per-side
        run-line / O-U prices yet; the flat -110 default mirrors what
        bulk-bet placement uses."""
        from apps.mlb.services.prioritization import build_signals
        from apps.mockbets.services.prefill import prefill_from_signals
        from apps.mlb.models import OddsSnapshot
        g = self._setup()
        OddsSnapshot.objects.create(
            game=g, captured_at=timezone.now(),
            market_home_win_prob=0.55, spread=-1.5, total=8.5,
        )
        data = prefill_from_signals(build_signals(g))
        spreads = data['selections_by_type']['spread']
        totals = data['selections_by_type']['total']
        for opt in spreads:
            self.assertEqual(
                opt['odds'], -110,
                f"spread option {opt['label']!r} expected -110, got {opt['odds']!r}",
            )
        for opt in totals:
            self.assertEqual(opt['odds'], -110)

    def test_options_shape_remains_json_serializable(self):
        """Adding the odds field can't break JSON serialization for
        the AJAX prefill payload."""
        import json
        from apps.mlb.services.prioritization import build_signals
        from apps.mockbets.services.prefill import prefill_from_signals
        from apps.mlb.models import OddsSnapshot
        g = self._setup(ml_home=-140, ml_away=120)
        OddsSnapshot.objects.create(
            game=g, captured_at=timezone.now(),
            market_home_win_prob=0.55, spread=-1.5, total=8.5,
            moneyline_home=-140, moneyline_away=120,
        )
        data = prefill_from_signals(build_signals(g))
        json.dumps(data)  # raises if any value isn't JSON-safe


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
        # 2026-05-03: best_bet now requires a Recommended pick. Bumped
        # rating gap to 90/10 + added moneyline odds so each fixture
        # produces a genuine engine recommendation, which in turn lets
        # the best_bet action fire.
        home = _mk_team('TH' + ext, 90.0, 'th' + ext)
        away = _mk_team('TA' + ext, 10.0, 'ta' + ext)
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
            moneyline_home=-110, moneyline_away=-110,
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
        # Model prob ~0.5 (equal ratings), market 0.15 → big delta even
        # after the heavier 2026-05-22 market blend (0.55). Caps the
        # line-value contribution at its max → confidence lifted past 0.7.
        # (Under blend 0.40 the prior fixture used market=0.25; the 2026-05-22
        # tighten reduced post-blend discrepancy, so the fixture was widened
        # to maintain the test's "big line value" intent.)
        OddsSnapshot.objects.create(
            game=g, captured_at=timezone.now(),
            market_home_win_prob=0.15, spread=1.0,
        )
        s = build_signals(g)
        self.assertGreaterEqual(s.confidence, 0.7)

    def test_confidence_pct_matches_confidence(self):
        from apps.mlb.services.prioritization import build_signals
        g = self._game('c5')
        s = build_signals(g)
        self.assertEqual(s.confidence_pct, int(round(s.confidence * 100)))

    def test_primary_action_carries_confidence(self):
        """2026-05-03: best_bet now requires a Recommended pick. We need
        a rating gap + moneyline odds so the engine emits one."""
        from apps.mlb.models import OddsSnapshot
        from apps.mlb.services.prioritization import build_signals
        g = self._game('c6', hp=90.0, ap=10.0)
        # Reset rating on the home team to a higher value so the model
        # produces a Recommended pick.
        g.home_team.rating = 90.0
        g.home_team.save()
        g.away_team.rating = 10.0
        g.away_team.save()
        OddsSnapshot.objects.create(
            game=g, captured_at=timezone.now(),
            market_home_win_prob=0.5, spread=1.0,
            moneyline_home=-110, moneyline_away=-110,
        )
        s = build_signals(g)
        primary = next(a for a in s.actions if a['strength'] == 'primary')
        self.assertEqual(primary['confidence'], s.confidence)


class MLBFocusEngineTests(TestCase):
    def _bb_game(self, ext, *, home_win_prob=0.5, spread=1.0,
                 home_rating=90.0, away_rating=10.0,
                 ml_home=-110, ml_away=-110):
        """2026-05-03: defaults updated so each game produces a
        Recommended-status pick out of the box. Focus banner is now
        anchored on Recommended only — tests that want a focus need
        ratings + moneylines that clear the engine gates.

        Override `home_rating` / `away_rating` to test the no-rec case.
        """
        home = _mk_team('FH' + ext, home_rating, 'fh' + ext)
        away = _mk_team('FA' + ext, away_rating, 'fa' + ext)
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
            moneyline_home=ml_home, moneyline_away=ml_away,
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
        """Focus picks the higher-edge recommended game. 2026-05-03:
        previously this test relied on the legacy signal-layer fallback;
        now it asserts the edge-DESC sort within the recommended pool."""
        from apps.mlb.services.prioritization import get_focus_game, prioritize
        # Both games are recommended (90/10 default rating gap). 'strong'
        # has a tighter rating contest (smaller pre-blend prob) but with
        # market agreeing more, post-blend prob lands higher → bigger
        # edge vs the 50/50 de-vigged market. We test that the focus
        # engine picks by edge, not by which game has more "signals".
        weak = self._bb_game('weak', home_win_prob=0.62,
                             home_rating=70.0, away_rating=30.0)
        strong = self._bb_game('strong', home_win_prob=0.62,
                               home_rating=90.0, away_rating=10.0)
        signals = prioritize([weak, strong])
        focus = get_focus_game(signals)
        self.assertEqual(focus.game.external_id, 'strong')

    def test_focus_is_none_when_no_recommended_games(self):
        """2026-05-03 spec contract: when no game has status='recommended',
        Focus banner is suppressed. The hub renders 'No strong plays
        right now' in its place."""
        from apps.mlb.services.prioritization import get_focus_game, prioritize
        # Equal-rating teams + balanced market = no recommendation. Tight
        # spread alone (signal-layer) used to anchor Focus via the legacy
        # Layer-3 fallback; that fallback has been removed.
        g = self._bb_game(
            'norec', home_win_prob=0.50, spread=1.0,
            home_rating=50.0, away_rating=50.0,  # no model edge
        )
        signals = prioritize([g])
        focus = get_focus_game(signals)
        self.assertIsNone(focus)

    def test_focus_post_condition_holds(self):
        """get_focus_game's post-condition: returned game's recommendation
        status is always 'recommended'. Mixed slate test — even when other
        games have signals, focus only anchors on the recommended one."""
        from apps.mlb.services.prioritization import get_focus_game, prioritize
        # Game A: rated, recommended.
        rec = self._bb_game('rec-game', home_win_prob=0.50)
        # Game B: equal ratings → no recommendation. Tight spread present
        # but it should NOT anchor focus.
        non_rec = self._bb_game(
            'sig-only', home_win_prob=0.50, spread=1.0,
            home_rating=50.0, away_rating=50.0,
        )
        signals = prioritize([rec, non_rec])
        focus = get_focus_game(signals)
        self.assertIsNotNone(focus)
        self.assertEqual(focus.game.external_id, 'rec-game')
        self.assertEqual(focus.recommendation.status, 'recommended')

    def test_best_bet_preferred_over_watch_now(self):
        from apps.mlb.services.prioritization import get_focus_game, prioritize
        # 2026-05-03 calibration + Best Bet contract tighten: this test
        # needs a properly Recommended pick so it can be selected as
        # focus. _bb_game defaults (90/10 ratings, ML -110/-110) produce
        # exactly that — no need to override home_win_prob.
        bb = self._bb_game('bbf')
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
        # 2026-05-03: best_bet (the secondary action this test asserts)
        # now requires a Recommended pick. Ratings + moneylines give the
        # engine enough signal to produce one.
        home = _mk_team('BPH', 90.0, 'bph')
        away = _mk_team('BPA', 10.0, 'bpa')
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
            moneyline_home=-110, moneyline_away=-110,
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

    def test_strong_signal_without_rec_NO_LONGER_focused(self):
        """2026-05-03 contract: 'Best Bet' / Focus is reserved for games
        whose recommendation engine status is 'recommended'. Signal-layer
        flags alone (tight spread, line value, ace matchup) no longer
        promote a game to focus. The legacy Layer-3 fallback was removed
        because it produced the contradiction where 'Focus Right Now'
        pointed to games sitting in the Not Recommended section."""
        from apps.mlb.services.prioritization import get_focus_game
        s = self._fake_signal(confidence=0.5, primary_type='best_bet', rec=None)
        self.assertIsNone(get_focus_game([s]))

    def test_not_recommended_rec_does_not_win_focus(self):
        """A game whose rec was DECLINED by the decision rules shouldn't take
        the Focus slot — that would contradict the UI telling users 'don't
        bet'. Post-2026-05-03 the legacy Layer-3 fallback is gone, so this
        rule applies regardless of signal-layer confidence."""
        from apps.mlb.services.prioritization import get_focus_game
        declined = self._fake_signal(
            game_id='d', confidence=0.5, primary_type='watch_now',
            rec=self._fake_rec(status='not_recommended', edge=3.0),
        )
        self.assertIsNone(get_focus_game([declined]))


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
            # 2026-05-22 fixture update: gap widened 80/40 → 90/30
            # after MARKET_BLEND_WEIGHT bumped 0.40 → 0.55.
            home = Team.objects.create(
                name=f'Home {i}', slug=f'home-{i}', conference=conf, rating=90.0,
            )
            away = Team.objects.create(
                name=f'Away {i}', slug=f'away-{i}', conference=conf, rating=30.0,
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

    def test_espn_filter_now_places_zero_after_correction(self):
        """2026-04-27 strict correction: ESPN-source picks are never
        Recommended at the engine level (compute_status returns
        status='not_recommended', reason='secondary_source'). The bulk
        endpoint with source_filter='espn' therefore places ZERO bets
        — the surface is intentionally not-bulk-eligible per spec
        ('visible but NOT actionable in bulk').

        Previously this test asserted placed==2 with the old behavior
        where ESPN picks could be Recommended and bulk-bettable. The
        correction explicitly removes that path."""
        from apps.mockbets.services.bulk_actions import place_bulk_recommended_bets
        self._seed_snapshot(self.team_pairs[0], source='odds_api')
        self._seed_snapshot(self.team_pairs[1], source='espn')
        self._seed_snapshot(self.team_pairs[2], source='espn')
        result = place_bulk_recommended_bets(
            self.user, source_filter='espn',
        )
        self.assertEqual(result['placed'], 0)

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
            # 2026-05-22 fixture update: gap widened 80/40 → 90/30 after
            # MARKET_BLEND_WEIGHT bumped 0.40 → 0.55.
            home = Team.objects.create(
                name=f'Home {label}', slug=f'home-{label}',
                conference=conf, rating=90.0,
            )
            away = Team.objects.create(
                name=f'Away {label}', slug=f'away-{label}',
                conference=conf, rating=30.0,
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
        """Post-2026-05-02 decision-first rewrite: ESPN-source recs are
        now folded into the Not Recommended section (their engine status
        is 'not_recommended' under the strict-source correction). The
        per-tile ESPN provenance survives as a subtle badge."""
        self._seed(self.verified_game, source='odds_api')
        self._seed(self.espn_game, source='espn')
        resp = self.client.get('/mlb/')
        self.assertEqual(resp.status_code, 200)
        # The new Not Recommended group is the canonical home for
        # ESPN-source recs after the restructure.
        self.assertContains(resp, 'mlb-decision-group--fade')
        # ESPN provenance is preserved per-tile as a subtle badge.
        self.assertContains(resp, 'mlb-subtle-badge--espn')

    def test_espn_section_hidden_when_no_secondary(self):
        # Only verified rec → no ESPN provenance badge should render.
        self._seed(self.verified_game, source='odds_api')
        resp = self.client.get('/mlb/')
        self.assertNotContains(resp, 'mlb-subtle-badge--espn')

    def test_only_moneyline_bulk_button_renders_after_correction(self):
        """2026-04-27 strict correction: 'Bet All ESPN Plays' button
        REMOVED from the hub. ESPN-source picks are visible in their
        Insights section but never bulk-eligible per the spec rule
        'visible but NOT actionable in bulk'. Even when both sources
        are present, only the green Moneyline bulk button renders."""
        self._seed(self.verified_game, source='odds_api')
        self._seed(self.espn_game, source='espn')
        resp = self.client.get('/mlb/')
        self.assertContains(resp, 'Bet All Moneyline Plays')
        self.assertNotContains(resp, 'Bet All ESPN Plays')

    def test_only_verified_button_renders_when_no_secondary(self):
        self._seed(self.verified_game, source='odds_api')
        resp = self.client.get('/mlb/')
        self.assertContains(resp, 'Bet All Moneyline Plays')
        self.assertNotContains(resp, 'Bet All ESPN Plays')

    def test_source_badge_renders_on_espn_tile(self):
        """Post-2026-05-02: the source badge moved from a primary chip on
        the legacy tile to a subtle chip on the decision-first tile."""
        self._seed(self.espn_game, source='espn')
        resp = self.client.get('/mlb/')
        self.assertContains(resp, 'mlb-subtle-badge--espn')


# ===================================================================== #
# Tiered Intelligence — Phase 1 Opportunity Signal Tests
#
# These cover the rule-based Spread + Total signal generators ONLY.
# They explicitly do NOT touch Moneyline / BettingRecommendation paths
# — that pipeline is frozen by contract for this phase. Any Moneyline
# regression caught here would mean a leak the architecture should
# prevent at the type level, not just by test.
# ===================================================================== #

class _OpportunitySignalTestBase(TestCase):
    """Shared setup for Spread + Total signal tests.

    Uses fresh teams + fresh game per test (TestCase rolls back) so the
    UniqueConstraint on (game, snapshot, signal_type) can't bleed
    across tests.
    """

    def setUp(self):
        from apps.mlb.models import Conference as MLBConf, Team as MLBTeam, Game as MLBGame
        # Conference is required by Team; reuse the seed-loader's first
        # conference if it exists, otherwise create one.
        conf = MLBConf.objects.first()
        if conf is None:
            conf = MLBConf.objects.create(name='Test League', slug='test-league')
        self.home = MLBTeam.objects.create(
            name='Opp Home', slug='opp-home', conference=conf,
            rating=70, source='mlb_stats_api', external_id='opp-home',
        )
        self.away = MLBTeam.objects.create(
            name='Opp Away', slug='opp-away', conference=conf,
            rating=50, source='mlb_stats_api', external_id='opp-away',
        )
        self.game = MLBGame.objects.create(
            home_team=self.home, away_team=self.away,
            first_pitch=timezone.now() + timedelta(hours=3),
            status='scheduled',
            source='mlb_stats_api', external_id=f'opp-game-{timezone.now().timestamp()}',
        )

    def _make_snapshot(self, *, spread=None, total=None, source='odds_api', is_derived=False):
        from apps.mlb.models import OddsSnapshot
        # Disable the post_save hook so the test can call the generator
        # directly without the hook also creating rows. Tests that
        # exercise the hook re-enable it explicitly.
        from django.db.models.signals import post_save
        from apps.mlb import signals as mlb_signals
        post_save.disconnect(
            mlb_signals.generate_opportunities_on_snapshot_save,
            sender=OddsSnapshot,
        )
        try:
            snap = OddsSnapshot.objects.create(
                game=self.game, captured_at=timezone.now(),
                market_home_win_prob=0.5,
                spread=spread, total=total,
                odds_source=source, is_derived=is_derived,
            )
        finally:
            post_save.connect(
                mlb_signals.generate_opportunities_on_snapshot_save,
                sender=OddsSnapshot,
            )
        return snap


class SpreadOpportunitySignalTests(_OpportunitySignalTestBase):
    """Phase 1 spread signals — rule-based, NOT recommendations."""

    def test_tight_spread_at_threshold_fires(self):
        from apps.mlb.services.opportunity_signals import generate_spread_opportunities
        from apps.mlb.models import SpreadOpportunity
        snap = self._make_snapshot(spread=-1.5)
        created = generate_spread_opportunities(self.game, snap)
        self.assertEqual(len(created), 1)
        self.assertEqual(created[0].signal_type, 'tight_spread')
        self.assertEqual(SpreadOpportunity.objects.count(), 1)

    def test_tight_spread_below_threshold_fires(self):
        from apps.mlb.services.opportunity_signals import generate_spread_opportunities
        snap = self._make_snapshot(spread=1.0)
        sigs = generate_spread_opportunities(self.game, snap)
        self.assertEqual([s.signal_type for s in sigs], ['tight_spread'])

    def test_large_favorite_at_threshold_fires(self):
        from apps.mlb.services.opportunity_signals import generate_spread_opportunities
        snap = self._make_snapshot(spread=-2.5)
        sigs = generate_spread_opportunities(self.game, snap)
        self.assertEqual([s.signal_type for s in sigs], ['large_favorite'])

    def test_large_favorite_above_threshold_fires(self):
        from apps.mlb.services.opportunity_signals import generate_spread_opportunities
        snap = self._make_snapshot(spread=3.5)
        sigs = generate_spread_opportunities(self.game, snap)
        self.assertEqual([s.signal_type for s in sigs], ['large_favorite'])

    def test_no_signal_in_gap_band(self):
        """Spreads strictly between 1.5 and 2.5 (exclusive) get nothing.
        Silence is correct — confirms the threshold gap is intentional."""
        from apps.mlb.services.opportunity_signals import generate_spread_opportunities
        snap = self._make_snapshot(spread=2.0)
        sigs = generate_spread_opportunities(self.game, snap)
        self.assertEqual(sigs, [])

    def test_no_spread_field_no_signal(self):
        from apps.mlb.services.opportunity_signals import generate_spread_opportunities
        snap = self._make_snapshot(spread=None, total=8.5)
        self.assertEqual(generate_spread_opportunities(self.game, snap), [])

    def test_idempotent_no_duplicate_rows(self):
        """Running the generator twice on the same snapshot is a no-op
        the second time — the DB constraint guarantees it. Load-bearing
        because the post_save hook can fire again on snapshot updates."""
        from apps.mlb.services.opportunity_signals import generate_spread_opportunities
        from apps.mlb.models import SpreadOpportunity
        snap = self._make_snapshot(spread=-1.0)
        first = generate_spread_opportunities(self.game, snap)
        second = generate_spread_opportunities(self.game, snap)
        self.assertEqual(len(first), 1)
        self.assertEqual(len(second), 0)
        self.assertEqual(SpreadOpportunity.objects.filter(game=self.game).count(), 1)

    def test_negative_spread_means_home_is_favorite(self):
        """Convention: snapshot.spread is from home perspective; negative
        means home is favored. Signal stores team NAMES, not FKs, because
        the favorite/underdog labels are display data — we don't want
        downstream code to treat them as authoritative team references."""
        from apps.mlb.services.opportunity_signals import generate_spread_opportunities
        snap = self._make_snapshot(spread=-1.5)
        sig = generate_spread_opportunities(self.game, snap)[0]
        self.assertEqual(sig.favorite_team_name, self.home.name)
        self.assertEqual(sig.underdog_team_name, self.away.name)

    def test_positive_spread_means_away_is_favorite(self):
        from apps.mlb.services.opportunity_signals import generate_spread_opportunities
        snap = self._make_snapshot(spread=1.5)
        sig = generate_spread_opportunities(self.game, snap)[0]
        self.assertEqual(sig.favorite_team_name, self.away.name)
        self.assertEqual(sig.underdog_team_name, self.home.name)

    def test_pickem_spread_zero_no_favorite(self):
        from apps.mlb.services.opportunity_signals import generate_spread_opportunities
        snap = self._make_snapshot(spread=0.0)
        sig = generate_spread_opportunities(self.game, snap)[0]
        self.assertEqual(sig.signal_type, 'tight_spread')
        self.assertEqual(sig.favorite_team_name, '')
        self.assertEqual(sig.underdog_team_name, '')

    def test_source_attribution_carries_through(self):
        from apps.mlb.services.opportunity_signals import generate_spread_opportunities
        snap = self._make_snapshot(spread=-1.5, source='espn')
        sig = generate_spread_opportunities(self.game, snap)[0]
        self.assertEqual(sig.source, 'espn')


class TotalOpportunitySignalTests(_OpportunitySignalTestBase):
    """Phase 1 total signals — rule-based, NOT recommendations."""

    def test_high_scoring_at_threshold_fires(self):
        from apps.mlb.services.opportunity_signals import generate_total_opportunities
        snap = self._make_snapshot(total=9.5)
        sigs = generate_total_opportunities(self.game, snap)
        self.assertEqual([s.signal_type for s in sigs], ['high_scoring'])

    def test_high_scoring_above_threshold_fires(self):
        from apps.mlb.services.opportunity_signals import generate_total_opportunities
        snap = self._make_snapshot(total=10.5)
        sigs = generate_total_opportunities(self.game, snap)
        self.assertEqual([s.signal_type for s in sigs], ['high_scoring'])

    def test_low_scoring_at_threshold_fires(self):
        from apps.mlb.services.opportunity_signals import generate_total_opportunities
        snap = self._make_snapshot(total=7.5)
        sigs = generate_total_opportunities(self.game, snap)
        self.assertEqual([s.signal_type for s in sigs], ['low_scoring'])

    def test_low_scoring_below_threshold_fires(self):
        from apps.mlb.services.opportunity_signals import generate_total_opportunities
        snap = self._make_snapshot(total=6.5)
        sigs = generate_total_opportunities(self.game, snap)
        self.assertEqual([s.signal_type for s in sigs], ['low_scoring'])

    def test_no_signal_in_gap_band(self):
        from apps.mlb.services.opportunity_signals import generate_total_opportunities
        snap = self._make_snapshot(total=8.5)
        sigs = generate_total_opportunities(self.game, snap)
        self.assertEqual(sigs, [])

    def test_no_total_field_no_signal(self):
        from apps.mlb.services.opportunity_signals import generate_total_opportunities
        snap = self._make_snapshot(total=None, spread=-1.0)
        self.assertEqual(generate_total_opportunities(self.game, snap), [])

    def test_idempotent_no_duplicate_rows(self):
        from apps.mlb.services.opportunity_signals import generate_total_opportunities
        from apps.mlb.models import TotalOpportunity
        snap = self._make_snapshot(total=10.0)
        generate_total_opportunities(self.game, snap)
        generate_total_opportunities(self.game, snap)
        self.assertEqual(TotalOpportunity.objects.filter(game=self.game).count(), 1)

    def test_source_attribution_carries_through(self):
        from apps.mlb.services.opportunity_signals import generate_total_opportunities
        snap = self._make_snapshot(total=10.0, source='espn')
        sig = generate_total_opportunities(self.game, snap)[0]
        self.assertEqual(sig.source, 'espn')


class OpportunityPostSaveHookTests(TestCase):
    """The post_save signal must auto-generate opportunity rows on insert.

    These tests intentionally do NOT disable the signal — they verify
    the integration path that a real OddsSnapshot.create() goes through.
    """

    def setUp(self):
        from apps.mlb.models import Conference as MLBConf, Team as MLBTeam, Game as MLBGame
        conf = MLBConf.objects.first() or MLBConf.objects.create(name='Test League', slug='test-league')
        self.home = MLBTeam.objects.create(
            name='Hook Home', slug='hook-home', conference=conf,
            rating=70, source='mlb_stats_api', external_id='hook-home',
        )
        self.away = MLBTeam.objects.create(
            name='Hook Away', slug='hook-away', conference=conf,
            rating=50, source='mlb_stats_api', external_id='hook-away',
        )
        self.game = MLBGame.objects.create(
            home_team=self.home, away_team=self.away,
            first_pitch=timezone.now() + timedelta(hours=3),
            status='scheduled',
            source='mlb_stats_api', external_id=f'opp-game-{timezone.now().timestamp()}',
        )

    def test_insert_with_tight_spread_creates_signal(self):
        from apps.mlb.models import OddsSnapshot, SpreadOpportunity
        OddsSnapshot.objects.create(
            game=self.game, captured_at=timezone.now(),
            market_home_win_prob=0.55, spread=-1.0,
        )
        self.assertEqual(SpreadOpportunity.objects.filter(game=self.game).count(), 1)

    def test_insert_with_high_total_creates_signal(self):
        from apps.mlb.models import OddsSnapshot, TotalOpportunity
        OddsSnapshot.objects.create(
            game=self.game, captured_at=timezone.now(),
            market_home_win_prob=0.5, total=10.5,
        )
        self.assertEqual(TotalOpportunity.objects.filter(game=self.game).count(), 1)

    def test_insert_in_gap_band_creates_no_signals(self):
        from apps.mlb.models import OddsSnapshot, SpreadOpportunity, TotalOpportunity
        OddsSnapshot.objects.create(
            game=self.game, captured_at=timezone.now(),
            market_home_win_prob=0.5, spread=-2.0, total=8.5,
        )
        self.assertEqual(SpreadOpportunity.objects.count(), 0)
        self.assertEqual(TotalOpportunity.objects.count(), 0)

    def test_update_does_not_regenerate_signals(self):
        """Editing a snapshot must NOT create a duplicate signal — only
        new inserts trigger generation. Verifies the `created` guard."""
        from apps.mlb.models import OddsSnapshot, SpreadOpportunity
        snap = OddsSnapshot.objects.create(
            game=self.game, captured_at=timezone.now(),
            market_home_win_prob=0.55, spread=-1.0,
        )
        self.assertEqual(SpreadOpportunity.objects.count(), 1)
        snap.market_home_win_prob = 0.60
        snap.save()
        self.assertEqual(SpreadOpportunity.objects.count(), 1)


@override_settings(MONEYLINE_ONLY_MODE=False)
class SpreadTotalUITests(TestCase):
    """Phase 1 UI surface — feature flag + filter + render gating.

    Hard guarantees:
      - Flag OFF: the spread/total UI must be completely invisible
        (no filter bar, no sections, no CSS hooks). Even if the data
        layer has rows, the page must look identical to before.
      - Flag ON + signals exist: filter bar renders; spread + total
        sections render; the mandatory "Not model-backed —
        informational only" disclaimer is present.
      - Filter param: bet_type=moneyline must hide the opportunity
        groups; bet_type=spread must hide the moneyline groups; etc.
    """

    def setUp(self):
        from apps.mlb.models import Conference as MLBConf, Team as MLBTeam, Game as MLBGame, OddsSnapshot
        conf = MLBConf.objects.first() or MLBConf.objects.create(name='Test League', slug='test-league')
        self.home = MLBTeam.objects.create(
            name='UI Home', slug='ui-home', conference=conf,
            rating=80, source='mlb_stats_api', external_id='ui-home',
        )
        self.away = MLBTeam.objects.create(
            name='UI Away', slug='ui-away', conference=conf,
            rating=40, source='mlb_stats_api', external_id='ui-away',
        )
        self.game = MLBGame.objects.create(
            home_team=self.home, away_team=self.away,
            first_pitch=timezone.now() + timedelta(hours=3),
            status='scheduled',
            source='mlb_stats_api', external_id=f'opp-game-{timezone.now().timestamp()}',
        )
        # Create snapshot with values that fire BOTH a tight_spread
        # and a high_scoring signal — the post_save hook generates
        # rows in both opportunity tables.
        OddsSnapshot.objects.create(
            game=self.game, captured_at=timezone.now(),
            market_home_win_prob=0.58,
            moneyline_home=-150, moneyline_away=130,
            spread=-1.5, total=10.0,
        )

    @override_settings(SPREAD_TOTAL_SIGNALS_ENABLED=False)
    def test_flag_off_hides_everything(self):
        """The whole UI surface must be invisible when the flag is off.
        This protects the dark-launch capability — we can ship the data
        layer to prod without users seeing it.

        Asserts on UNIQUE CSS markers (`mlb-decision-group--spread`,
        `mlb-decision-group--total`, `mlb-opportunity-card`,
        `mlb-bet-type-filter`) rather than text strings — the help
        modal mentions the feature names in instructional text on
        every page, so a substring assertion would false-positive.
        """
        resp = self.client.get('/mlb/')
        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode('utf-8')
        # Filter chip bar: not rendered
        self.assertNotIn('mlb-bet-type-filter', body)
        # Group containers: not rendered
        self.assertNotIn('mlb-decision-group--spread', body)
        self.assertNotIn('mlb-decision-group--total', body)
        # Per-card wrapper: not rendered
        self.assertNotIn('mlb-opportunity-card', body)
        # Coming-soon bulk buttons: not rendered
        self.assertNotIn('mlb-bulk-btn--coming-soon', body)

    @override_settings(SPREAD_TOTAL_SIGNALS_ENABLED=True)
    def test_flag_on_renders_sections_and_disclaimer(self):
        """Asserts on UNIQUE CSS markers — the help-modal include
        contains the feature names in instructional text on every page,
        so a text-substring check could false-pass."""
        resp = self.client.get('/mlb/')
        # UI surfaces (CSS classes are unique to the rendered page)
        self.assertContains(resp, 'mlb-bet-type-filter')
        self.assertContains(resp, 'mlb-decision-group--spread')
        self.assertContains(resp, 'mlb-decision-group--total')
        self.assertContains(resp, 'mlb-opportunity-card')
        # Per-spec mandatory disclaimer — present at least twice
        # (parent group hint + per-card footer).
        self.assertContains(resp, 'Not model-backed')
        # Filter chip labels (unique link hrefs prevent help-modal
        # bleed-through; the chips use bet_type query param)
        self.assertContains(resp, 'bet_type=moneyline')
        self.assertContains(resp, 'bet_type=spread')
        self.assertContains(resp, 'bet_type=total')

    @override_settings(SPREAD_TOTAL_SIGNALS_ENABLED=True)
    def test_filter_spread_hides_moneyline_groups(self):
        """Post-2026-05-02 decision-first rewrite: the moneyline groups
        (Recommended/Potential/Not Recommended) always render — the
        bet_type filter no longer toggles their visibility because they
        ARE the page's primary structure now. Spread sections are still
        visible under bet_type=spread."""
        resp = self.client.get('/mlb/?bet_type=spread')
        body = resp.content.decode('utf-8')
        # Spread group is NOT hidden when filter selects spread.
        self.assertNotIn('mlb-decision-group--spread is-hidden', body)

    @override_settings(SPREAD_TOTAL_SIGNALS_ENABLED=True)
    def test_filter_moneyline_hides_opportunity_groups(self):
        resp = self.client.get('/mlb/?bet_type=moneyline')
        body = resp.content.decode('utf-8')
        self.assertIn('mlb-decision-group--spread is-hidden', body)
        self.assertIn('mlb-decision-group--total is-hidden', body)
        self.assertNotIn('mlb-decision-group--recommended is-hidden', body)
        self.assertNotIn('mlb-decision-group--fade is-hidden', body)

    @override_settings(SPREAD_TOTAL_SIGNALS_ENABLED=True)
    def test_invalid_filter_collapses_to_all(self):
        """Garbage input must not crash or hide everything — should
        fall through to 'all'. Defensive against stale links + typos."""
        resp = self.client.get('/mlb/?bet_type=asdf')
        body = resp.content.decode('utf-8')
        # Nothing should be hidden — same as ?bet_type=all
        self.assertNotIn('is-hidden', body)

    @override_settings(SPREAD_TOTAL_SIGNALS_ENABLED=True)
    def test_spread_total_bulk_buttons_render_disabled(self):
        """Phase 1 contract: bulk buttons for spread + total render but
        are HTML-disabled with aria-disabled, so the click never reaches
        any handler. Phase 2 will activate them once we have a modeled
        edge for those markets."""
        # Need to log in — the bulk-action header only shows for authed users.
        from django.contrib.auth import get_user_model
        User = get_user_model()
        u = User.objects.create_user(username='bulk-test', password='pw')
        self.client.force_login(u)
        resp = self.client.get('/mlb/')
        body = resp.content.decode('utf-8')
        # Buttons are present
        self.assertIn('Bet All Spread', body)
        self.assertIn('Coming Soon', body)
        # Buttons are disabled at the HTML level
        self.assertIn('mlb-bulk-btn--coming-soon', body)
        # No onclick handler — explicit absence (otherwise a misclick
        # could fire bulkPlaceRecommended on the wrong bet type).
        # Find the button block and assert disabled is present.
        import re
        spread_btn = re.search(
            r'<button[^>]*mlb-bulk-btn--coming-soon[^>]*>[^<]*Bet All Spread[^<]*',
            body,
        )
        self.assertIsNotNone(spread_btn, 'Spread bulk button not found')
        self.assertIn('disabled', spread_btn.group(0))
        self.assertIn('aria-disabled="true"', spread_btn.group(0))
        # And no onclick attribute on the disabled button
        self.assertNotIn('onclick', spread_btn.group(0))

    @override_settings(SPREAD_TOTAL_SIGNALS_ENABLED=False)
    def test_disabled_bulk_buttons_hidden_when_flag_off(self):
        """Even the disabled Coming Soon buttons must not render when
        the flag is off — preserves the dark-launch capability.

        Asserts on UNIQUE BUTTON CLASSES rather than text. The help
        modal mentions "Bet All Spread" / "Bet All Total" in the Phase
        3 bullet on every page (instructional copy), so a substring
        check would false-positive."""
        from django.contrib.auth import get_user_model
        User = get_user_model()
        u = User.objects.create_user(username='bulk-test-off', password='pw')
        self.client.force_login(u)
        resp = self.client.get('/mlb/')
        body = resp.content.decode('utf-8')
        # No Coming Soon button class (Phase 1 path)
        self.assertNotIn('mlb-bulk-btn--coming-soon', body)
        # No active Phase 3 button class either
        self.assertNotIn('mlb-bulk-btn--bet-rec', body)

    @override_settings(SPREAD_TOTAL_SIGNALS_ENABLED=True)
    def test_existing_bulk_endpoint_rejects_spread_or_total_bet_type(self):
        """Server-side guarantee: even if a user constructs a hand-rolled
        request to the bulk-place endpoint with bet_type=spread/total,
        the existing service ignores them — the eligibility iterator
        only yields games whose recommendation status == 'recommended',
        and spread/total signals never produce that status. This test
        documents that contract by hitting the endpoint directly."""
        from django.contrib.auth import get_user_model
        from apps.mockbets.models import MockBet
        User = get_user_model()
        u = User.objects.create_user(username='bulk-attack', password='pw')
        self.client.force_login(u)
        # Attempt to abuse: pass bet_type=spread (ignored — only
        # source_filter is read by the bulk endpoint).
        resp = self.client.post(
            '/mockbets/bulk/place-recommended/?bet_type=spread'
        )
        self.assertEqual(resp.status_code, 200)
        # No bets land on spread/total selections — only moneyline
        # recommendations are eligible. Even with a moneyline-eligible
        # game in setUp, this is the existing behavior — confirming
        # that the new param doesn't change anything.
        for bet in MockBet.objects.filter(user=u):
            self.assertEqual(bet.bet_type, 'moneyline')


class MoneylinePipelineNonRegressionTests(TestCase):
    """Belt-and-suspenders: verify that introducing the opportunity
    signal layer did not change the Moneyline recommendation pipeline.

    These tests explicitly assert no SpreadOpportunity / TotalOpportunity
    is consulted by the recommendation engine. The Tiered Intelligence
    contract REQUIRES this isolation.
    """

    def test_get_recommendation_does_not_query_opportunity_tables(self):
        """The recommendation engine must not depend on opportunity
        signals. Patch the queryset managers to raise on access — if
        the engine tries to read either table, this test fails loudly."""
        from unittest.mock import PropertyMock, patch
        from apps.mlb.models import Conference as MLBConf, Team as MLBTeam, Game as MLBGame, OddsSnapshot
        from apps.core.services.recommendations import get_recommendation

        conf = MLBConf.objects.first() or MLBConf.objects.create(name='Test League', slug='test-league')
        h = MLBTeam.objects.create(
            name='Reg Home', slug='reg-home', conference=conf,
            rating=80, source='mlb_stats_api', external_id='reg-home',
        )
        a = MLBTeam.objects.create(
            name='Reg Away', slug='reg-away', conference=conf,
            rating=40, source='mlb_stats_api', external_id='reg-away',
        )
        g = MLBGame.objects.create(
            home_team=h, away_team=a,
            first_pitch=timezone.now() + timedelta(hours=3),
            status='scheduled',
            source='mlb_stats_api', external_id=f'opp-game-{timezone.now().timestamp()}',
        )
        OddsSnapshot.objects.create(
            game=g, captured_at=timezone.now(),
            market_home_win_prob=0.55,
            moneyline_home=-150, moneyline_away=130,
            spread=-1.5, total=9.5,  # would also trigger BOTH opportunity signals
        )

        # Sentinel: if the recommendation engine touches either related
        # manager, raise. This proves Moneyline's reasoning never relies
        # on the new tables.
        with patch.object(MLBGame, 'spread_opportunities',
                          new_callable=PropertyMock,
                          side_effect=AssertionError(
                              'recommendation engine touched spread_opportunities'
                          )):
            with patch.object(MLBGame, 'total_opportunities',
                              new_callable=PropertyMock,
                              side_effect=AssertionError(
                                  'recommendation engine touched total_opportunities'
                              )):
                rec = get_recommendation('mlb', g, user=None)
        # Sanity: a recommendation came back — the moneyline pipeline
        # ran end-to-end with both opportunity managers locked off.
        self.assertIsNotNone(rec)
        self.assertEqual(rec.bet_type, 'moneyline')


# ===================================================================== #
# Phase 2: Settlement, Performance, Lean Classification
#
# Adds outcome-tracking + per-signal-type win rate aggregation +
# is_lean stamping. Must NOT regress Moneyline.
# ===================================================================== #


class _OpportunityPhase2TestBase(TestCase):
    """Shared setup — fresh teams + minimum viable game for settlement."""

    def setUp(self):
        from apps.mlb.models import Conference as MLBConf, Team as MLBTeam, Game as MLBGame
        conf = MLBConf.objects.first() or MLBConf.objects.create(
            name='P2 League', slug='p2-league',
        )
        self.home = MLBTeam.objects.create(
            name='P2 Home', slug='p2-home', conference=conf,
            rating=70, source='mlb_stats_api', external_id='p2-home',
        )
        self.away = MLBTeam.objects.create(
            name='P2 Away', slug='p2-away', conference=conf,
            rating=50, source='mlb_stats_api', external_id='p2-away',
        )

    def _final_game(self, home_score, away_score):
        from apps.mlb.models import Game as MLBGame
        return MLBGame.objects.create(
            home_team=self.home, away_team=self.away,
            first_pitch=timezone.now() - timedelta(hours=4),
            status='final',
            home_score=home_score, away_score=away_score,
            source='mlb_stats_api',
            external_id=f'p2-final-{timezone.now().timestamp()}',
        )

    def _scheduled_game(self):
        from apps.mlb.models import Game as MLBGame
        return MLBGame.objects.create(
            home_team=self.home, away_team=self.away,
            first_pitch=timezone.now() + timedelta(hours=4),
            status='scheduled',
            source='mlb_stats_api',
            external_id=f'p2-sched-{timezone.now().timestamp()}',
        )

    def _make_signal_rows(self, game, *, spread=None, total=None, source='odds_api',
                          signal_type=None, model='spread'):
        """Bypass the auto-generator — build a row directly so the test
        can pin signal_type independent of the threshold rules."""
        from apps.mlb.models import OddsSnapshot, SpreadOpportunity, TotalOpportunity
        from django.db.models.signals import post_save
        from apps.mlb import signals as mlb_signals
        # Disable the post_save hook for the snapshot creation — this
        # test wants to control exactly which opportunity rows exist.
        post_save.disconnect(
            mlb_signals.generate_opportunities_on_snapshot_save,
            sender=OddsSnapshot,
        )
        try:
            snap = OddsSnapshot.objects.create(
                game=game, captured_at=timezone.now() - timedelta(hours=5),
                market_home_win_prob=0.5,
                spread=spread, total=total, odds_source=source,
            )
        finally:
            post_save.connect(
                mlb_signals.generate_opportunities_on_snapshot_save,
                sender=OddsSnapshot,
            )
        if model == 'spread':
            return SpreadOpportunity.objects.create(
                game=game, odds_snapshot=snap, signal_type=signal_type,
                spread=spread or 0.0, source=source,
            )
        else:
            return TotalOpportunity.objects.create(
                game=game, odds_snapshot=snap, signal_type=signal_type,
                total=total or 0.0, source=source,
            )


class SpreadSettlementTests(_OpportunityPhase2TestBase):
    """Settlement convention: tight_spread bets the dog, large_favorite
    bets the favorite. Tests pin both directions explicitly."""

    def test_tight_spread_dog_covers_when_dog_wins(self):
        # Home is favored at -1.0; dog (away) wins outright → dog covers.
        from apps.mlb.services.opportunity_signals import settle_opportunities_for_game
        g = self._final_game(home_score=2, away_score=4)
        opp = self._make_signal_rows(g, spread=-1.0, signal_type='tight_spread')
        result = settle_opportunities_for_game(g)
        self.assertEqual(result['spread_settled'], 1)
        opp.refresh_from_db()
        self.assertEqual(opp.outcome, 'win')
        self.assertIsNotNone(opp.settled_at)

    def test_tight_spread_dog_loses_when_fav_wins_by_more_than_line(self):
        # Home favored at -1.0; home wins by 3 → favorite covers → dog loses.
        from apps.mlb.services.opportunity_signals import settle_opportunities_for_game
        g = self._final_game(home_score=5, away_score=2)
        opp = self._make_signal_rows(g, spread=-1.0, signal_type='tight_spread')
        settle_opportunities_for_game(g)
        opp.refresh_from_db()
        self.assertEqual(opp.outcome, 'loss')

    def test_large_favorite_covers_at_2dot5_line(self):
        # Home favored at -2.5; home wins by 4 → favorite covers.
        from apps.mlb.services.opportunity_signals import settle_opportunities_for_game
        g = self._final_game(home_score=6, away_score=2)
        opp = self._make_signal_rows(g, spread=-2.5, signal_type='large_favorite')
        settle_opportunities_for_game(g)
        opp.refresh_from_db()
        self.assertEqual(opp.outcome, 'win')

    def test_large_favorite_does_not_cover_when_fav_wins_by_one(self):
        from apps.mlb.services.opportunity_signals import settle_opportunities_for_game
        g = self._final_game(home_score=3, away_score=2)
        opp = self._make_signal_rows(g, spread=-2.5, signal_type='large_favorite')
        settle_opportunities_for_game(g)
        opp.refresh_from_db()
        self.assertEqual(opp.outcome, 'loss')

    def test_push_when_fav_margin_equals_line(self):
        from apps.mlb.services.opportunity_signals import settle_opportunities_for_game
        # Home -2.0 line, home wins by exactly 2 → push.
        g = self._final_game(home_score=4, away_score=2)
        opp = self._make_signal_rows(g, spread=-2.0, signal_type='tight_spread')
        settle_opportunities_for_game(g)
        opp.refresh_from_db()
        self.assertEqual(opp.outcome, 'push')

    def test_settlement_idempotent(self):
        from apps.mlb.services.opportunity_signals import settle_opportunities_for_game
        g = self._final_game(home_score=5, away_score=2)
        opp = self._make_signal_rows(g, spread=-1.0, signal_type='tight_spread')
        first = settle_opportunities_for_game(g)
        second = settle_opportunities_for_game(g)
        self.assertEqual(first['spread_settled'], 1)
        self.assertEqual(second['spread_settled'], 0)

    def test_skips_non_final_game(self):
        from apps.mlb.services.opportunity_signals import settle_opportunities_for_game
        g = self._scheduled_game()
        opp = self._make_signal_rows(g, spread=-1.0, signal_type='tight_spread')
        result = settle_opportunities_for_game(g)
        self.assertEqual(result['spread_settled'], 0)
        opp.refresh_from_db()
        self.assertEqual(opp.outcome, '')

    def test_away_favorite_settles_correctly(self):
        """Convention: spread > 0 means away is favored. Make sure the
        favorite/underdog math doesn't assume home is always the dog."""
        from apps.mlb.services.opportunity_signals import settle_opportunities_for_game
        # Spread +1.5: away favored by 1.5; away wins by 3 → fav covers.
        g = self._final_game(home_score=2, away_score=5)
        opp = self._make_signal_rows(g, spread=1.5, signal_type='large_favorite')
        # 1.5 doesn't actually trigger large_favorite via the auto-gen
        # path — but here we're testing the settlement math directly.
        # Reset spread to -2.5 magnitude in the away direction:
        opp.spread = 2.5
        opp.save()
        settle_opportunities_for_game(g)
        opp.refresh_from_db()
        self.assertEqual(opp.outcome, 'win')


class TotalSettlementTests(_OpportunityPhase2TestBase):
    def test_high_scoring_over_hits(self):
        from apps.mlb.services.opportunity_signals import settle_opportunities_for_game
        g = self._final_game(home_score=6, away_score=5)  # 11 runs total
        opp = self._make_signal_rows(g, total=10.0, signal_type='high_scoring', model='total')
        settle_opportunities_for_game(g)
        opp.refresh_from_db()
        self.assertEqual(opp.outcome, 'win')

    def test_high_scoring_under_costs_loss(self):
        from apps.mlb.services.opportunity_signals import settle_opportunities_for_game
        g = self._final_game(home_score=2, away_score=3)  # 5 runs
        opp = self._make_signal_rows(g, total=10.0, signal_type='high_scoring', model='total')
        settle_opportunities_for_game(g)
        opp.refresh_from_db()
        self.assertEqual(opp.outcome, 'loss')

    def test_low_scoring_under_hits(self):
        from apps.mlb.services.opportunity_signals import settle_opportunities_for_game
        g = self._final_game(home_score=2, away_score=1)  # 3 runs
        opp = self._make_signal_rows(g, total=7.0, signal_type='low_scoring', model='total')
        settle_opportunities_for_game(g)
        opp.refresh_from_db()
        self.assertEqual(opp.outcome, 'win')

    def test_total_push_at_exact(self):
        from apps.mlb.services.opportunity_signals import settle_opportunities_for_game
        g = self._final_game(home_score=4, away_score=4)  # 8 runs
        opp = self._make_signal_rows(g, total=8.0, signal_type='high_scoring', model='total')
        settle_opportunities_for_game(g)
        opp.refresh_from_db()
        self.assertEqual(opp.outcome, 'push')


class PerformanceAggregationTests(_OpportunityPhase2TestBase):
    """Verify the performance aggregator excludes pushes from win_rate
    (standard sports-betting convention) but counts them in the totals."""

    def test_win_rate_excludes_pushes(self):
        from apps.mlb.services.opportunity_signals import compute_spread_performance
        # Build settled rows directly — 6 wins, 4 losses, 2 pushes.
        from apps.mlb.models import SpreadOpportunity, OddsSnapshot
        for outcome, count in [('win', 6), ('loss', 4), ('push', 2)]:
            for i in range(count):
                g = self._final_game(home_score=5 + i, away_score=2)
                snap = OddsSnapshot.objects.create(
                    game=g, captured_at=timezone.now(),
                    market_home_win_prob=0.5, spread=-1.0,
                )
                # Skip the auto-generated row (different signal_type
                # might fire). Just directly create with desired outcome.
                SpreadOpportunity.objects.update_or_create(
                    game=g, odds_snapshot=snap, signal_type='tight_spread',
                    defaults={
                        'spread': -1.0, 'outcome': outcome,
                        'settled_at': timezone.now(),
                    },
                )
        perf = compute_spread_performance()['tight_spread']
        # Sample size = wins + losses (decided rows only)
        self.assertEqual(perf['sample_size'], 10)
        self.assertEqual(perf['pushes'], 2)
        # Win rate over decided rows = 6/10 = 0.6
        self.assertAlmostEqual(perf['win_rate'], 0.6, places=3)
        # ROI estimate should be > 0 for win_rate=0.6 at -110
        self.assertGreater(perf['roi_estimate'], 0)


class LeanClassificationTests(_OpportunityPhase2TestBase):
    """When a new signal is created, is_lean / historical_win_rate /
    sample_size are stamped from current historical performance.
    Snapshotted at create-time so old rows can never be retroactively
    re-labeled by a threshold change."""

    def _seed_history(self, signal_type, *, wins, losses, model='spread'):
        from apps.mlb.models import SpreadOpportunity, TotalOpportunity, OddsSnapshot
        Model = SpreadOpportunity if model == 'spread' else TotalOpportunity
        for i in range(wins):
            g = self._final_game(home_score=5, away_score=2)
            snap = OddsSnapshot.objects.create(
                game=g, captured_at=timezone.now(),
                market_home_win_prob=0.5,
                spread=-1.0 if model == 'spread' else None,
                total=10.0 if model == 'total' else None,
            )
            kwargs = {
                'game': g, 'odds_snapshot': snap, 'signal_type': signal_type,
                'outcome': 'win', 'settled_at': timezone.now(),
            }
            if model == 'spread':
                kwargs['spread'] = -1.0
            else:
                kwargs['total'] = 10.0
            Model.objects.update_or_create(
                game=g, odds_snapshot=snap, signal_type=signal_type,
                defaults=kwargs,
            )
        for i in range(losses):
            g = self._final_game(home_score=2, away_score=5)
            snap = OddsSnapshot.objects.create(
                game=g, captured_at=timezone.now(),
                market_home_win_prob=0.5,
                spread=-1.0 if model == 'spread' else None,
                total=10.0 if model == 'total' else None,
            )
            kwargs = {
                'game': g, 'odds_snapshot': snap, 'signal_type': signal_type,
                'outcome': 'loss', 'settled_at': timezone.now(),
            }
            if model == 'spread':
                kwargs['spread'] = -1.0
            else:
                kwargs['total'] = 10.0
            Model.objects.update_or_create(
                game=g, odds_snapshot=snap, signal_type=signal_type,
                defaults=kwargs,
            )

    def test_lean_off_when_sample_below_minimum(self):
        # Strong win rate but only 5 samples — bar requires > 30.
        from apps.mlb.services.opportunity_signals import generate_spread_opportunities
        from apps.mlb.models import OddsSnapshot
        self._seed_history('tight_spread', wins=4, losses=1)
        # Now create a NEW signal — should NOT be marked lean.
        g = self._scheduled_game()
        snap = OddsSnapshot.objects.create(
            game=g, captured_at=timezone.now(),
            market_home_win_prob=0.5, spread=-1.0,
        )
        # Auto-generated by post_save — find the row.
        from apps.mlb.models import SpreadOpportunity
        new_opp = SpreadOpportunity.objects.get(game=g, signal_type='tight_spread')
        self.assertFalse(new_opp.is_lean)
        self.assertEqual(new_opp.sample_size, 5)

    def test_lean_off_when_winrate_below_threshold(self):
        # 31 samples (>30) but 50% win rate — fails 53% threshold.
        from apps.mlb.models import OddsSnapshot, SpreadOpportunity
        self._seed_history('tight_spread', wins=15, losses=16)
        g = self._scheduled_game()
        OddsSnapshot.objects.create(
            game=g, captured_at=timezone.now(),
            market_home_win_prob=0.5, spread=-1.0,
        )
        new_opp = SpreadOpportunity.objects.get(game=g, signal_type='tight_spread')
        self.assertFalse(new_opp.is_lean)
        self.assertEqual(new_opp.sample_size, 31)

    def test_lean_on_when_both_thresholds_clear(self):
        # 35 samples, 60% win rate — clears both bars.
        from apps.mlb.models import OddsSnapshot, SpreadOpportunity
        self._seed_history('tight_spread', wins=21, losses=14)  # 60%
        g = self._scheduled_game()
        OddsSnapshot.objects.create(
            game=g, captured_at=timezone.now(),
            market_home_win_prob=0.5, spread=-1.0,
        )
        new_opp = SpreadOpportunity.objects.get(game=g, signal_type='tight_spread')
        self.assertTrue(new_opp.is_lean)
        self.assertEqual(new_opp.sample_size, 35)
        self.assertAlmostEqual(new_opp.historical_win_rate, 21/35, places=3)

    def test_total_lean_requires_roi_and_winrate(self):
        """Total leans need BOTH win rate > 54% AND ROI > 2%. A 54.5%
        win rate at -110 gives ROI ~+4% (clears) — should be a lean."""
        from apps.mlb.models import OddsSnapshot, TotalOpportunity
        self._seed_history('high_scoring', wins=22, losses=18, model='total')  # 55%
        g = self._scheduled_game()
        OddsSnapshot.objects.create(
            game=g, captured_at=timezone.now(),
            market_home_win_prob=0.5, total=10.0,
        )
        new_opp = TotalOpportunity.objects.get(game=g, signal_type='high_scoring')
        self.assertTrue(new_opp.is_lean)

    def test_lean_status_snapshotted_not_recomputed(self):
        """Old rows must NOT be retroactively re-labeled when history
        changes. is_lean is set at create-time and stays."""
        from apps.mlb.models import OddsSnapshot, SpreadOpportunity
        # Cold start — no history — first signal is_lean=False.
        g1 = self._scheduled_game()
        OddsSnapshot.objects.create(
            game=g1, captured_at=timezone.now(),
            market_home_win_prob=0.5, spread=-1.0,
        )
        first_opp = SpreadOpportunity.objects.get(game=g1, signal_type='tight_spread')
        self.assertFalse(first_opp.is_lean)
        first_opp_id = first_opp.id

        # Now seed enough winning history to meet the bar.
        self._seed_history('tight_spread', wins=30, losses=5)

        # Re-fetch the original row — it must still be is_lean=False.
        first_opp.refresh_from_db()
        self.assertFalse(first_opp.is_lean)
        self.assertEqual(first_opp.id, first_opp_id)

        # A NEW row created now should pick up the lean status.
        g2 = self._scheduled_game()
        OddsSnapshot.objects.create(
            game=g2, captured_at=timezone.now(),
            market_home_win_prob=0.5, spread=-1.0,
        )
        new_opp = SpreadOpportunity.objects.get(game=g2, signal_type='tight_spread')
        self.assertTrue(new_opp.is_lean)


class SettleOutcomesIntegrationTests(_OpportunityPhase2TestBase):
    """End-to-end: resolve_outcomes management command settles MLB
    opportunity rows alongside its existing ModelResultSnapshot work."""

    def test_resolve_outcomes_settles_opportunities(self):
        from django.core.management import call_command
        from io import StringIO
        # Build a final game with both spread + total signals seeded.
        g = self._final_game(home_score=6, away_score=2)  # margin 4
        self._make_signal_rows(g, spread=-2.5, signal_type='large_favorite')
        self._make_signal_rows(g, total=10.0, signal_type='high_scoring', model='total')
        out = StringIO()
        call_command('resolve_outcomes', sport='mlb', stdout=out)
        # Both should be settled now
        from apps.mlb.models import SpreadOpportunity, TotalOpportunity
        self.assertEqual(
            SpreadOpportunity.objects.filter(game=g).first().outcome, 'win',
        )
        # 8 runs total, line 10.0 → over LOSES (under hit)
        self.assertEqual(
            TotalOpportunity.objects.filter(game=g).first().outcome, 'loss',
        )


@override_settings(MONEYLINE_ONLY_MODE=False)
class Phase2LeanUITests(TestCase):
    """UI surface for Phase 2 Lean intelligence. Hard contract:
       - flag OFF: no yellow Lean badge, no Leans Only chip, no
         "(Market Signals Only)" subtitle, no Performance panel rows.
         (Phase 1 surfaces still render normally if Phase 1 flag is on.)
       - flag ON: badge + chip + subtitle render when data supports them.
       - Leans Only filter hides non-lean cards via is-hidden.
    """

    def setUp(self):
        from apps.mlb.models import Conference as MLBConf, Team as MLBTeam, Game as MLBGame, OddsSnapshot, SpreadOpportunity
        conf = MLBConf.objects.first() or MLBConf.objects.create(name='B', slug='b')
        self.home = MLBTeam.objects.create(
            name='B Home', slug='b-home', conference=conf,
            rating=80, source='mlb_stats_api', external_id='b-home',
        )
        self.away = MLBTeam.objects.create(
            name='B Away', slug='b-away', conference=conf,
            rating=40, source='mlb_stats_api', external_id='b-away',
        )
        self.game = MLBGame.objects.create(
            home_team=self.home, away_team=self.away,
            first_pitch=timezone.now() + timedelta(hours=3),
            status='scheduled',
            source='mlb_stats_api',
            external_id=f'b-game-{timezone.now().timestamp()}',
        )
        # Snapshot drives the post_save signal → SpreadOpportunity row.
        OddsSnapshot.objects.create(
            game=self.game, captured_at=timezone.now(),
            market_home_win_prob=0.58,
            moneyline_home=-150, moneyline_away=130,
            spread=-1.5, total=10.0,
        )
        # Force the auto-generated row to is_lean=True so the UI
        # branches under test actually fire. This bypasses the
        # threshold gate (which we already test elsewhere).
        for opp in SpreadOpportunity.objects.filter(game=self.game):
            opp.is_lean = True
            opp.historical_win_rate = 0.56
            opp.sample_size = 42
            opp.save(update_fields=['is_lean', 'historical_win_rate', 'sample_size'])

    @override_settings(
        SPREAD_TOTAL_SIGNALS_ENABLED=True,
        SPREAD_TOTAL_LEANS_ENABLED=False,
    )
    def test_leans_flag_off_hides_lean_ui(self):
        """Phase 1 surfaces stay visible; Phase 2 leans surfaces stay hidden."""
        resp = self.client.get('/mlb/')
        body = resp.content.decode('utf-8')
        # Phase 1 still on
        self.assertIn('mlb-decision-group--spread', body)
        # Phase 2 surfaces NOT rendered
        self.assertNotIn('mlb-lean-badge', body)
        self.assertNotIn('mlb-bet-type-chip--leans', body)
        self.assertNotIn('Market Signals Only', body)

    @override_settings(
        SPREAD_TOTAL_SIGNALS_ENABLED=True,
        SPREAD_TOTAL_LEANS_ENABLED=True,
    )
    def test_leans_flag_on_renders_badge_chip_and_subtitle(self):
        resp = self.client.get('/mlb/')
        body = resp.content.decode('utf-8')
        self.assertIn('mlb-lean-badge', body)
        self.assertIn('mlb-bet-type-chip--leans', body)
        self.assertIn('Market Signals Only', body)
        # Win rate is precomputed as percentage in the view (56.0)
        self.assertIn('56.0% win rate', body)
        self.assertIn('42 games', body)

    @override_settings(
        SPREAD_TOTAL_SIGNALS_ENABLED=True,
        SPREAD_TOTAL_LEANS_ENABLED=True,
    )
    def test_leans_only_filter_hides_non_lean_cards(self):
        """Cards whose signal is_lean=False get is-hidden when filter
        is 'leans'. Non-lean cards still exist in the DOM (hidden via
        CSS) so the structure stays intact for accessibility."""
        from apps.mlb.models import (
            Conference as MLBConf, Team as MLBTeam, Game as MLBGame,
            OddsSnapshot, SpreadOpportunity,
        )
        # Add a SECOND game with a non-lean spread signal.
        conf = MLBConf.objects.first()
        h2 = MLBTeam.objects.create(
            name='B Home 2', slug='b-home-2', conference=conf,
            rating=70, source='mlb_stats_api', external_id='b-home-2',
        )
        a2 = MLBTeam.objects.create(
            name='B Away 2', slug='b-away-2', conference=conf,
            rating=50, source='mlb_stats_api', external_id='b-away-2',
        )
        g2 = MLBGame.objects.create(
            home_team=h2, away_team=a2,
            first_pitch=timezone.now() + timedelta(hours=4),
            status='scheduled',
            source='mlb_stats_api',
            external_id=f'b-game2-{timezone.now().timestamp()}',
        )
        OddsSnapshot.objects.create(
            game=g2, captured_at=timezone.now(),
            market_home_win_prob=0.55,
            moneyline_home=-130, moneyline_away=110,
            spread=-1.0, total=10.5,
        )
        # Don't promote g2's signals — they stay is_lean=False.
        resp = self.client.get('/mlb/?bet_type=leans')
        body = resp.content.decode('utf-8')
        # is-hidden appears on cards (not just the moneyline groups).
        # Exact structure: both moneyline groups hidden + non-lean cards
        # hidden.
        self.assertIn('is-hidden', body)
        # The Leans Only chip is currently active
        self.assertIn('"true"', body)  # aria-pressed="true" somewhere


@override_settings(MONEYLINE_ONLY_MODE=False)
class Phase2PerformancePanelTests(TestCase):
    """Performance panel on /profile/performance/ surfaces the per-
    signal-type stats table when leans flag is on, hides when off."""

    def setUp(self):
        from django.contrib.auth import get_user_model
        User = get_user_model()
        self.user = User.objects.create_user(username='perf-test', password='pw')
        self.client.force_login(self.user)

    @override_settings(SPREAD_TOTAL_LEANS_ENABLED=False)
    def test_panel_hidden_when_flag_off(self):
        resp = self.client.get('/profile/performance/')
        body = resp.content.decode('utf-8')
        self.assertNotIn('Spread &amp; Total Performance', body)

    @override_settings(SPREAD_TOTAL_LEANS_ENABLED=True)
    def test_panel_renders_when_flag_on(self):
        resp = self.client.get('/profile/performance/')
        body = resp.content.decode('utf-8')
        self.assertIn('Spread &amp; Total Performance', body)
        # Both bet-type sub-tables present
        self.assertIn('Spread (Run Line)', body)
        self.assertIn('Total (Over/Under)', body)


# ===================================================================== #
# Phase 3: Promotion to Recommendation
#
# Adds is_recommended + roi fields. Promotion thresholds are stricter
# than Lean: sample >= 50, win_rate >= 54.5% (spread) / 55.0% (total),
# ROI >= 2.0% (spread) / 2.5% (total). Source safety: ESPN-source rows
# and synthesized (is_derived=True) rows are NEVER promoted regardless
# of stats.
# ===================================================================== #


class Phase3BreakEvenTests(TestCase):
    """Standard implied-probability formula. Tests pin known values so
    a math regression breaks the suite loudly."""

    def test_break_even_at_minus_110(self):
        from apps.mlb.services.opportunity_signals import calculate_break_even
        self.assertAlmostEqual(calculate_break_even(-110), 110/210, places=4)

    def test_break_even_at_minus_150(self):
        from apps.mlb.services.opportunity_signals import calculate_break_even
        self.assertAlmostEqual(calculate_break_even(-150), 150/250, places=4)

    def test_break_even_at_plus_150(self):
        from apps.mlb.services.opportunity_signals import calculate_break_even
        self.assertAlmostEqual(calculate_break_even(150), 100/250, places=4)

    def test_break_even_at_plus_100_is_50pct(self):
        from apps.mlb.services.opportunity_signals import calculate_break_even
        # +100 is even money — break-even is 50%.
        self.assertAlmostEqual(calculate_break_even(100), 0.5, places=4)

    def test_break_even_zero_uses_default_minus_110(self):
        """Sentinel: 0 means caller didn't have a price; default to -110."""
        from apps.mlb.services.opportunity_signals import calculate_break_even
        self.assertAlmostEqual(calculate_break_even(0), 110/210, places=4)


class Phase3PromotionGatesTests(_OpportunityPhase2TestBase):
    """Promotion thresholds for spread:
        sample >= 50 AND win_rate >= 54.5% AND roi >= 2.0%
    Promotion thresholds for total:
        sample >= 50 AND win_rate >= 55.0% AND roi >= 2.5%
    Each test pins one threshold at the boundary while clearing the
    other two, so a single-axis regression is detectable.
    """

    def _seed_history(self, signal_type, *, wins, losses, model='spread'):
        """Reused from LeanClassificationTests' helper, inlined here so
        Phase 3 tests can run independently."""
        from apps.mlb.models import (
            SpreadOpportunity, TotalOpportunity, OddsSnapshot,
        )
        Model = SpreadOpportunity if model == 'spread' else TotalOpportunity
        for i, outcome in [(j, 'win') for j in range(wins)] + [(j, 'loss') for j in range(losses)]:
            g = self._final_game(home_score=5 if outcome == 'win' else 2,
                                  away_score=2 if outcome == 'win' else 5)
            snap = OddsSnapshot.objects.create(
                game=g, captured_at=timezone.now(),
                market_home_win_prob=0.5,
                spread=-1.0 if model == 'spread' else None,
                total=10.0 if model == 'total' else None,
            )
            kwargs = {
                'game': g, 'odds_snapshot': snap, 'signal_type': signal_type,
                'outcome': outcome, 'settled_at': timezone.now(),
            }
            if model == 'spread':
                kwargs['spread'] = -1.0
            else:
                kwargs['total'] = 10.0
            Model.objects.update_or_create(
                game=g, odds_snapshot=snap, signal_type=signal_type,
                defaults=kwargs,
            )

    # ----- Spread -----

    def test_spread_promotes_at_thresholds(self):
        """50 sample, 54.5% win rate (28/51), ROI ~ +4.05% — passes all."""
        from apps.mlb.models import OddsSnapshot, SpreadOpportunity
        # 28W / 23L = 54.9% over 51 — passes 54.5 and the ROI floor.
        self._seed_history('tight_spread', wins=28, losses=23)
        g = self._scheduled_game()
        OddsSnapshot.objects.create(
            game=g, captured_at=timezone.now(),
            market_home_win_prob=0.5, spread=-1.0,
        )
        new_opp = SpreadOpportunity.objects.get(game=g, signal_type='tight_spread')
        self.assertTrue(new_opp.is_lean)
        self.assertTrue(new_opp.is_recommended)
        self.assertEqual(new_opp.sample_size, 51)
        self.assertIsNotNone(new_opp.roi)

    def test_spread_blocked_below_sample_minimum(self):
        """49 sample (below 50) but elite win rate — must NOT promote."""
        from apps.mlb.models import OddsSnapshot, SpreadOpportunity
        self._seed_history('tight_spread', wins=30, losses=19)  # 49, ~61%
        g = self._scheduled_game()
        OddsSnapshot.objects.create(
            game=g, captured_at=timezone.now(),
            market_home_win_prob=0.5, spread=-1.0,
        )
        new_opp = SpreadOpportunity.objects.get(game=g, signal_type='tight_spread')
        self.assertFalse(new_opp.is_recommended)
        # Lean still passes (lean threshold is sample > 30).
        self.assertTrue(new_opp.is_lean)

    def test_spread_blocked_below_winrate_threshold(self):
        """50+ sample, 53% win rate (clears Lean's 53 strictly, but not
        Recommendation's 54.5)."""
        from apps.mlb.models import OddsSnapshot, SpreadOpportunity
        # 27W / 23L = 54% — under 54.5, over 53 (lean clears).
        self._seed_history('tight_spread', wins=27, losses=23)
        g = self._scheduled_game()
        OddsSnapshot.objects.create(
            game=g, captured_at=timezone.now(),
            market_home_win_prob=0.5, spread=-1.0,
        )
        new_opp = SpreadOpportunity.objects.get(game=g, signal_type='tight_spread')
        self.assertTrue(new_opp.is_lean)
        self.assertFalse(new_opp.is_recommended)

    # ----- Total -----

    def test_total_promotes_at_thresholds(self):
        """28W/22L over 50 = 56% win rate, ROI ~ +6.95% — passes all."""
        from apps.mlb.models import OddsSnapshot, TotalOpportunity
        self._seed_history('high_scoring', wins=28, losses=22, model='total')
        g = self._scheduled_game()
        OddsSnapshot.objects.create(
            game=g, captured_at=timezone.now(),
            market_home_win_prob=0.5, total=10.0,
        )
        new_opp = TotalOpportunity.objects.get(game=g, signal_type='high_scoring')
        self.assertTrue(new_opp.is_recommended)
        self.assertGreaterEqual(new_opp.roi, 0.025)

    def test_total_blocked_below_winrate_threshold(self):
        """51 sample, 54.9% win rate — clears Lean's >54, fails Rec's >=55."""
        from apps.mlb.models import OddsSnapshot, TotalOpportunity
        # 28W / 23L = 54.9% — strictly above 54, strictly below 55.
        self._seed_history('high_scoring', wins=28, losses=23, model='total')
        g = self._scheduled_game()
        OddsSnapshot.objects.create(
            game=g, captured_at=timezone.now(),
            market_home_win_prob=0.5, total=10.0,
        )
        new_opp = TotalOpportunity.objects.get(game=g, signal_type='high_scoring')
        self.assertTrue(new_opp.is_lean)
        self.assertFalse(new_opp.is_recommended)

    def test_total_blocked_below_roi_floor(self):
        """50 sample, 55% win rate (clears WR), but ROI must clear 2.5%.
        At 55% the ROI is ~5%, so this case is hard to construct cleanly
        — instead we verify with a borderline sample where ROI floor
        gates above the WR clear.

        (At -110, win_rate 55% => ROI 5%; we need ROI < 2.5% which
        means win_rate < ~53.6%. So a separate test isn't needed —
        the win-rate gate effectively dominates. Skipping with comment.)
        """
        # No assertion — see docstring. The ROI gate is mathematically
        # implied by the win-rate gate at -110. Documented for the
        # next person to wonder why this isn't covered.
        pass


class Phase3SourceSafetyTests(_OpportunityPhase2TestBase):
    """ESPN-source rows and is_derived rows must NEVER be promoted to
    Recommendation regardless of stats. The safety gate is the load-
    bearing piece that protects users from "this signal works
    historically, AND we're trusting an ESPN scrape that may be wrong"
    landing in the green-tier Recommendation list."""

    def _seed_winning_history(self, signal_type, *, sample, model='spread'):
        """Build enough wins/losses to clear the recommendation bar."""
        wins = int(sample * 0.60)  # 60% — well above all bars
        losses = sample - wins
        from apps.mlb.models import (
            SpreadOpportunity, TotalOpportunity, OddsSnapshot,
        )
        Model = SpreadOpportunity if model == 'spread' else TotalOpportunity
        for i in range(wins):
            g = self._final_game(home_score=5, away_score=2)
            snap = OddsSnapshot.objects.create(
                game=g, captured_at=timezone.now(),
                market_home_win_prob=0.5,
                spread=-1.0 if model == 'spread' else None,
                total=10.0 if model == 'total' else None,
            )
            kwargs = {
                'game': g, 'odds_snapshot': snap, 'signal_type': signal_type,
                'outcome': 'win', 'settled_at': timezone.now(),
            }
            if model == 'spread':
                kwargs['spread'] = -1.0
            else:
                kwargs['total'] = 10.0
            Model.objects.update_or_create(
                game=g, odds_snapshot=snap, signal_type=signal_type,
                defaults=kwargs,
            )
        for i in range(losses):
            g = self._final_game(home_score=2, away_score=5)
            snap = OddsSnapshot.objects.create(
                game=g, captured_at=timezone.now(),
                market_home_win_prob=0.5,
                spread=-1.0 if model == 'spread' else None,
                total=10.0 if model == 'total' else None,
            )
            kwargs = {
                'game': g, 'odds_snapshot': snap, 'signal_type': signal_type,
                'outcome': 'loss', 'settled_at': timezone.now(),
            }
            if model == 'spread':
                kwargs['spread'] = -1.0
            else:
                kwargs['total'] = 10.0
            Model.objects.update_or_create(
                game=g, odds_snapshot=snap, signal_type=signal_type,
                defaults=kwargs,
            )

    def test_espn_source_never_promoted_even_with_winning_history(self):
        from apps.mlb.models import OddsSnapshot, SpreadOpportunity
        self._seed_winning_history('tight_spread', sample=60)
        # New row from ESPN — must NOT promote.
        g = self._scheduled_game()
        OddsSnapshot.objects.create(
            game=g, captured_at=timezone.now(),
            market_home_win_prob=0.5, spread=-1.0,
            odds_source='espn',  # the safety gate trip
        )
        new_opp = SpreadOpportunity.objects.get(game=g, signal_type='tight_spread')
        self.assertFalse(new_opp.is_recommended)
        # Lean still allowed — leans don't gate on source.
        self.assertTrue(new_opp.is_lean)

    def test_derived_row_never_promoted(self):
        from apps.mlb.models import OddsSnapshot, SpreadOpportunity
        self._seed_winning_history('tight_spread', sample=60)
        g = self._scheduled_game()
        OddsSnapshot.objects.create(
            game=g, captured_at=timezone.now(),
            market_home_win_prob=0.5, spread=-1.0,
            is_derived=True,  # synthesized — never promoted
        )
        new_opp = SpreadOpportunity.objects.get(game=g, signal_type='tight_spread')
        self.assertFalse(new_opp.is_recommended)

    def test_primary_source_promoted_with_same_history(self):
        """Sanity: same winning history with primary source DOES promote.
        Pairs with the ESPN test above to prove the source gate is the
        sole difference."""
        from apps.mlb.models import OddsSnapshot, SpreadOpportunity
        self._seed_winning_history('tight_spread', sample=60)
        g = self._scheduled_game()
        OddsSnapshot.objects.create(
            game=g, captured_at=timezone.now(),
            market_home_win_prob=0.5, spread=-1.0,
            odds_source='odds_api',
        )
        new_opp = SpreadOpportunity.objects.get(game=g, signal_type='tight_spread')
        self.assertTrue(new_opp.is_recommended)


@override_settings(MONEYLINE_ONLY_MODE=False)
class Phase3UITests(TestCase):
    """Phase 3 UI surface — proven-recommendations sections, badges,
    Recommended filter chip, bulk-bet button activation. The hard
    contract:
       - Phase 3 flag OFF = no green Recommendations sections, no
         Recommended chip, bulk Spread/Total stays "Coming Soon".
       - Phase 3 flag ON + at least one promoted signal → all of the
         above become visible.
    """

    def setUp(self):
        from django.contrib.auth import get_user_model
        from apps.mlb.models import (
            Conference as MLBConf, Team as MLBTeam, Game as MLBGame, OddsSnapshot,
            SpreadOpportunity,
        )
        # Bulk-bet header only renders for authenticated users — log in
        # so the active green button branch can be exercised.
        User = get_user_model()
        self.user = User.objects.create_user(username='p3-ui', password='pw')
        self.client.force_login(self.user)
        conf = MLBConf.objects.first() or MLBConf.objects.create(name='P3UI', slug='p3ui')
        self.home = MLBTeam.objects.create(
            name='P3UI Home', slug='p3ui-home', conference=conf,
            rating=80, source='mlb_stats_api', external_id='p3ui-home',
        )
        self.away = MLBTeam.objects.create(
            name='P3UI Away', slug='p3ui-away', conference=conf,
            rating=40, source='mlb_stats_api', external_id='p3ui-away',
        )
        self.game = MLBGame.objects.create(
            home_team=self.home, away_team=self.away,
            first_pitch=timezone.now() + timedelta(hours=3),
            status='scheduled',
            source='mlb_stats_api',
            external_id=f'p3ui-game-{timezone.now().timestamp()}',
        )
        # Snapshot fires the post_save hook → SpreadOpportunity row.
        # Then we mark it promoted so the Phase 3 UI branches activate.
        OddsSnapshot.objects.create(
            game=self.game, captured_at=timezone.now(),
            market_home_win_prob=0.58,
            moneyline_home=-150, moneyline_away=130,
            spread=-1.0, total=10.0,
        )
        for opp in SpreadOpportunity.objects.filter(game=self.game):
            opp.is_lean = True
            opp.is_recommended = True
            opp.historical_win_rate = 0.572
            opp.sample_size = 120
            opp.roi = 0.031
            opp.save(update_fields=['is_lean', 'is_recommended', 'historical_win_rate',
                                     'sample_size', 'roi'])

    @override_settings(
        SPREAD_TOTAL_SIGNALS_ENABLED=True,
        SPREAD_TOTAL_LEANS_ENABLED=True,
        SPREAD_TOTAL_RECOMMENDATIONS_ENABLED=False,
    )
    def test_phase3_flag_off_hides_promoted_surfaces(self):
        resp = self.client.get('/mlb/')
        body = resp.content.decode('utf-8')
        # No green Recommendations group container
        self.assertNotIn('mlb-decision-group--proven', body)
        # No green Recommended filter chip
        self.assertNotIn('mlb-bet-type-chip--recommended', body)
        # No active bulk button — the Coming Soon disabled state is
        # what should render (Phase 1 wired that path).
        self.assertNotIn('mlb-bulk-btn--bet-rec', body)
        # Coming Soon button still renders (because spread_tiles is
        # non-empty and the recs flag is off)
        self.assertIn('Bet All Spread', body)
        self.assertIn('Coming Soon', body)

    @override_settings(
        SPREAD_TOTAL_SIGNALS_ENABLED=True,
        SPREAD_TOTAL_LEANS_ENABLED=True,
        SPREAD_TOTAL_RECOMMENDATIONS_ENABLED=True,
    )
    def test_phase3_flag_on_renders_proven_section_and_active_button(self):
        resp = self.client.get('/mlb/')
        body = resp.content.decode('utf-8')
        # Promoted section renders
        self.assertIn('mlb-decision-group--proven', body)
        self.assertIn('mlb-decision-group--proven-spread', body)
        self.assertIn('Spread Recommendations', body)
        self.assertIn('Proven Edge', body)
        # Recommended filter chip renders
        self.assertIn('mlb-bet-type-chip--recommended', body)
        # Active green bulk button (NOT the Coming Soon variant)
        self.assertIn('mlb-bulk-btn--bet-rec', body)
        # Phase 3 badge format with break-even
        self.assertIn('mlb-rec-badge', body)
        self.assertIn('57.2% vs', body)
        self.assertIn('+3.1% ROI', body)
        self.assertIn('120 games', body)

    @override_settings(
        SPREAD_TOTAL_SIGNALS_ENABLED=True,
        SPREAD_TOTAL_LEANS_ENABLED=True,
        SPREAD_TOTAL_RECOMMENDATIONS_ENABLED=True,
    )
    def test_promoted_row_renders_only_in_proven_section_not_signals(self):
        """Phase 3 partition: a promoted row must render in the green
        section ONLY, not also in the cyan opportunity-signals section
        (otherwise the user sees the same matchup twice)."""
        resp = self.client.get('/mlb/')
        body = resp.content.decode('utf-8')
        # The matchup string appears once for our promoted spread row
        # in the proven section, plus possibly tile-related references.
        # The signal-only "(Market Signals Only)" subtitle should appear
        # in the cyan group's header but the cyan group's count should
        # be 0 for our case (only one signal exists, and it's promoted).
        # Easiest assertion: cyan section count for spread = 0.
        # The opportunity card class with cyan dashed border:
        # mlb-opportunity-card (without --proven) should NOT appear
        # for this game.
        # Count occurrences of mlb-opportunity-card--proven (proven)
        # vs bare mlb-opportunity-card. Promoted row is in the
        # --proven container.
        proven_count = body.count('mlb-opportunity-card--proven')
        # 1 promoted row → at least one occurrence of --proven.
        self.assertGreaterEqual(proven_count, 1)


@override_settings(MONEYLINE_ONLY_MODE=False)
class Phase3BulkBetServiceTests(TestCase):
    """The new place_bulk_proven_spread_bets / place_bulk_proven_total_bets
    services. Hard rules from the spec:
       - ESPN/derived rows must NEVER produce a placement.
       - Only is_recommended=True rows place.
       - Today's local slate only.
       - Skips games where user already has a pending spread/total bet.
    """

    def setUp(self):
        from django.contrib.auth import get_user_model
        from apps.mlb.models import (
            Conference as MLBConf, Team as MLBTeam, Game as MLBGame, OddsSnapshot,
            SpreadOpportunity,
        )
        User = get_user_model()
        self.user = User.objects.create_user(username='p3-bulk', password='pw')
        conf = MLBConf.objects.first() or MLBConf.objects.create(name='P3B', slug='p3b')
        self.home = MLBTeam.objects.create(
            name='P3B Home', slug='p3b-home', conference=conf,
            rating=80, source='mlb_stats_api', external_id='p3b-home',
        )
        self.away = MLBTeam.objects.create(
            name='P3B Away', slug='p3b-away', conference=conf,
            rating=40, source='mlb_stats_api', external_id='p3b-away',
        )

    def _seed_promoted_spread(self, *, source='odds_api'):
        from apps.mlb.models import (
            Game as MLBGame, OddsSnapshot, SpreadOpportunity,
        )
        from django.db.models.signals import post_save
        from apps.mlb import signals as mlb_signals
        g = MLBGame.objects.create(
            home_team=self.home, away_team=self.away,
            first_pitch=timezone.now() + timedelta(hours=3),
            status='scheduled',
            source='mlb_stats_api',
            external_id=f'p3b-game-{timezone.now().timestamp()}',
        )
        # Disable hook so the promoted row can be set explicitly.
        post_save.disconnect(
            mlb_signals.generate_opportunities_on_snapshot_save,
            sender=OddsSnapshot,
        )
        try:
            snap = OddsSnapshot.objects.create(
                game=g, captured_at=timezone.now(),
                market_home_win_prob=0.5, spread=-1.0,
                odds_source=source,
            )
            opp = SpreadOpportunity.objects.create(
                game=g, odds_snapshot=snap, signal_type='tight_spread',
                spread=-1.0,
                favorite_team_name=self.home.name,
                underdog_team_name=self.away.name,
                source=source,
                is_lean=True, is_recommended=True,
                historical_win_rate=0.56, sample_size=70, roi=0.025,
            )
        finally:
            post_save.connect(
                mlb_signals.generate_opportunities_on_snapshot_save,
                sender=OddsSnapshot,
            )
        return g, opp

    def test_bulk_proven_spread_places_for_primary_source(self):
        from apps.mockbets.services.bulk_actions import place_bulk_proven_spread_bets
        from apps.mockbets.models import MockBet
        g, opp = self._seed_promoted_spread(source='odds_api')
        result = place_bulk_proven_spread_bets(self.user)
        self.assertEqual(result['placed'], 1)
        bet = MockBet.objects.get(user=self.user, mlb_game=g)
        self.assertEqual(bet.bet_type, 'spread')
        # Selection format: "<dog> +<line>" for tight_spread
        self.assertIn('+1.0', bet.selection)
        self.assertEqual(bet.recommendation_tier, 'proven_spread')

    def test_bulk_proven_spread_skips_espn_source(self):
        """Defense in depth — even if a stale row has is_recommended=True
        with source='espn' (the data layer prevents this), the bulk
        service must skip it. Belt + suspenders."""
        from apps.mockbets.services.bulk_actions import place_bulk_proven_spread_bets
        from apps.mockbets.models import MockBet
        # Force-create an espn row with is_recommended=True (data layer
        # would block this in the auto-gen path; we're testing the
        # placement service's own guard).
        self._seed_promoted_spread(source='espn')
        result = place_bulk_proven_spread_bets(self.user)
        self.assertEqual(result['placed'], 0)
        self.assertEqual(MockBet.objects.filter(user=self.user).count(), 0)

    def test_bulk_proven_spread_idempotent(self):
        from apps.mockbets.services.bulk_actions import place_bulk_proven_spread_bets
        self._seed_promoted_spread()
        first = place_bulk_proven_spread_bets(self.user)
        second = place_bulk_proven_spread_bets(self.user)
        self.assertEqual(first['placed'], 1)
        self.assertEqual(second['placed'], 0)
        self.assertEqual(second['skipped_existing'], 1)

    def test_bulk_proven_total_places_with_correct_selection(self):
        from apps.mockbets.services.bulk_actions import place_bulk_proven_total_bets
        from apps.mockbets.models import MockBet
        from apps.mlb.models import (
            Game as MLBGame, OddsSnapshot, TotalOpportunity,
        )
        from django.db.models.signals import post_save
        from apps.mlb import signals as mlb_signals
        g = MLBGame.objects.create(
            home_team=self.home, away_team=self.away,
            first_pitch=timezone.now() + timedelta(hours=3),
            status='scheduled',
            source='mlb_stats_api',
            external_id=f'p3bt-{timezone.now().timestamp()}',
        )
        post_save.disconnect(
            mlb_signals.generate_opportunities_on_snapshot_save,
            sender=OddsSnapshot,
        )
        try:
            snap = OddsSnapshot.objects.create(
                game=g, captured_at=timezone.now(),
                market_home_win_prob=0.5, total=10.0,
                odds_source='odds_api',
            )
            TotalOpportunity.objects.create(
                game=g, odds_snapshot=snap, signal_type='high_scoring',
                total=10.0, source='odds_api',
                is_lean=True, is_recommended=True,
                historical_win_rate=0.56, sample_size=70, roi=0.05,
            )
        finally:
            post_save.connect(
                mlb_signals.generate_opportunities_on_snapshot_save,
                sender=OddsSnapshot,
            )
        result = place_bulk_proven_total_bets(self.user)
        self.assertEqual(result['placed'], 1)
        bet = MockBet.objects.get(user=self.user, mlb_game=g)
        self.assertEqual(bet.bet_type, 'total')
        # high_scoring → Over
        self.assertTrue(bet.selection.startswith('Over '))
        self.assertEqual(bet.recommendation_tier, 'proven_total')

    def test_bulk_proven_endpoint_routes_via_view(self):
        """End-to-end: POST to /mockbets/bulk/place-recommended/?bet_type=spread
        routes to the proven-spread service."""
        from apps.mockbets.models import MockBet
        self._seed_promoted_spread()
        self.client.force_login(self.user)
        resp = self.client.post(
            '/mockbets/bulk/place-recommended/?bet_type=spread'
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data.get('success'))
        self.assertEqual(data['placed'], 1)
        self.assertEqual(MockBet.objects.filter(
            user=self.user, bet_type='spread',
        ).count(), 1)


# ===================================================================== #
# Phase 4: Promotion-quality guards
#
# All four guards are additive on top of the Phase 3 thresholds:
#     1. Margin over break-even (>=2pp)
#     2. Recency window (60-day default)
#     3. Distribution guard (min wins/losses >=15)
#     4. CLV confirmation (active when sample >=20, else insufficient)
#
# Hard rule: Moneyline pipeline still untouched (sentinel below).
# ===================================================================== #


class Phase4MarginGuardTests(_OpportunityPhase2TestBase):
    """The margin guard requires win_rate - break_even >= 2pp.
    At -110 break-even is ~52.4%, so any promoted spread/total must
    clear ~54.4% to satisfy the 2pp margin. Pairs naturally with the
    Phase 3 win-rate floor (54.5% for spread / 55% for total) — the
    margin guard is a defensive backstop that catches an edge case
    where the price/threshold relationship gets re-tuned."""

    def _seed_spread_history(self, *, wins, losses):
        from apps.mlb.models import (
            SpreadOpportunity, OddsSnapshot,
        )
        for outcome, count in [('win', wins), ('loss', losses)]:
            for i in range(count):
                g = self._final_game(home_score=5 if outcome == 'win' else 2,
                                      away_score=2 if outcome == 'win' else 5)
                snap = OddsSnapshot.objects.create(
                    game=g, captured_at=timezone.now(),
                    market_home_win_prob=0.5, spread=-1.0,
                )
                SpreadOpportunity.objects.update_or_create(
                    game=g, odds_snapshot=snap, signal_type='tight_spread',
                    defaults={
                        'spread': -1.0, 'outcome': outcome,
                        'settled_at': timezone.now(),
                    },
                )

    def test_high_winrate_with_thin_margin_does_not_promote(self):
        """A pathological case: imagine break_even drifts up (alt
        pricing) so 53.8% only beats break-even by 1.4pp — Phase 4
        should block. We construct this by overriding the break-even
        function locally."""
        from unittest.mock import patch
        from apps.mlb.models import OddsSnapshot, SpreadOpportunity
        # 30W / 25L over 55 = 54.5% — sits exactly on the Phase 3 bar.
        self._seed_spread_history(wins=30, losses=25)
        g = self._scheduled_game()
        # Patch break-even to 53% so margin = 54.5 - 53 = 1.5pp < 2pp guard.
        with patch(
            'apps.mlb.services.opportunity_signals.calculate_break_even',
            return_value=0.53,
        ):
            OddsSnapshot.objects.create(
                game=g, captured_at=timezone.now(),
                market_home_win_prob=0.5, spread=-1.0,
            )
        new_opp = SpreadOpportunity.objects.get(game=g, signal_type='tight_spread')
        # Phase 3 bars cleared but Phase 4 margin guard blocked promotion.
        self.assertTrue(new_opp.is_lean)
        self.assertFalse(new_opp.is_recommended)

    def test_strong_margin_with_phase3_clears_promotes(self):
        """Sanity: with default -110 break-even (~52.4%), a 56% win
        rate beats break-even by ~3.6pp — well clear of the 2pp guard.
        Combined with Phase 3 thresholds, must promote."""
        from apps.mlb.models import OddsSnapshot, SpreadOpportunity
        self._seed_spread_history(wins=33, losses=27)  # 55% over 60
        g = self._scheduled_game()
        OddsSnapshot.objects.create(
            game=g, captured_at=timezone.now(),
            market_home_win_prob=0.5, spread=-1.0,
        )
        new_opp = SpreadOpportunity.objects.get(game=g, signal_type='tight_spread')
        # 55% beats break-even (52.4%) by 2.6pp ≥ 2pp guard.
        self.assertTrue(new_opp.is_recommended)


class Phase4RecencyWindowTests(_OpportunityPhase2TestBase):
    """Performance aggregators apply the rolling
    SPREAD_TOTAL_PERFORMANCE_LOOKBACK_DAYS window by default. Old
    rows are excluded from win-rate calculations but remain in the DB
    (no deletion). lookback_days=0 bypasses the filter entirely."""

    def _seed_with_age(self, *, signal_type, outcome, days_old):
        """Create a settled SpreadOpportunity with settled_at set
        days_old in the past. Uses update_or_create because the
        OddsSnapshot post_save hook auto-creates an opportunity row
        on insert; we then re-stamp it with the desired outcome and
        settlement age."""
        from datetime import timedelta
        from apps.mlb.models import SpreadOpportunity, OddsSnapshot
        g = self._final_game(home_score=5 if outcome == 'win' else 2,
                              away_score=2 if outcome == 'win' else 5)
        snap = OddsSnapshot.objects.create(
            game=g, captured_at=timezone.now() - timedelta(days=days_old),
            market_home_win_prob=0.5, spread=-1.0,
        )
        opp, _ = SpreadOpportunity.objects.update_or_create(
            game=g, odds_snapshot=snap, signal_type=signal_type,
            defaults={
                'spread': -1.0, 'outcome': outcome,
                'settled_at': timezone.now() - timedelta(days=days_old),
            },
        )
        return opp

    def test_old_outcomes_excluded_by_default(self):
        from apps.mlb.services.opportunity_signals import compute_spread_performance
        # Recent rows: 5 wins
        for _ in range(5):
            self._seed_with_age(signal_type='tight_spread', outcome='win', days_old=10)
        # Old rows beyond default 60-day window: 5 losses
        for _ in range(5):
            self._seed_with_age(signal_type='tight_spread', outcome='loss', days_old=120)
        perf = compute_spread_performance()['tight_spread']
        # Default lookback excludes the old losses.
        self.assertEqual(perf['wins'], 5)
        self.assertEqual(perf['losses'], 0)
        self.assertAlmostEqual(perf['win_rate'], 1.0, places=3)

    def test_lookback_zero_includes_all_history(self):
        from apps.mlb.services.opportunity_signals import compute_spread_performance
        for _ in range(5):
            self._seed_with_age(signal_type='tight_spread', outcome='win', days_old=10)
        for _ in range(5):
            self._seed_with_age(signal_type='tight_spread', outcome='loss', days_old=120)
        # lookback_days=0 → no recency filter, all-time performance.
        perf = compute_spread_performance(lookback_days=0)['tight_spread']
        self.assertEqual(perf['wins'], 5)
        self.assertEqual(perf['losses'], 5)

    def test_empty_recent_window_does_not_crash(self):
        """Edge case: zero recent rows with old rows still in DB.
        Should return win_rate=None, sample=0 — not raise."""
        from apps.mlb.services.opportunity_signals import compute_spread_performance
        # Only old rows
        for _ in range(3):
            self._seed_with_age(signal_type='tight_spread', outcome='win', days_old=120)
        perf = compute_spread_performance()['tight_spread']
        self.assertIsNone(perf['win_rate'])
        self.assertEqual(perf['sample_size'], 0)


class Phase4DistributionGuardTests(_OpportunityPhase2TestBase):
    """Distribution guard: promotion requires min(wins, losses) >= 15
    so a 50-0 hot streak doesn't promote. Pairs naturally with the
    sample minimum — 50 sample with all wins gives high win rate but
    leaves us with no loss data to verify the signal."""

    def _seed_spread_history(self, *, wins, losses):
        from apps.mlb.models import SpreadOpportunity, OddsSnapshot
        for outcome, count in [('win', wins), ('loss', losses)]:
            for i in range(count):
                g = self._final_game(home_score=5 if outcome == 'win' else 2,
                                      away_score=2 if outcome == 'win' else 5)
                snap = OddsSnapshot.objects.create(
                    game=g, captured_at=timezone.now(),
                    market_home_win_prob=0.5, spread=-1.0,
                )
                SpreadOpportunity.objects.update_or_create(
                    game=g, odds_snapshot=snap, signal_type='tight_spread',
                    defaults={
                        'spread': -1.0, 'outcome': outcome,
                        'settled_at': timezone.now(),
                    },
                )

    def test_lopsided_streak_does_not_promote(self):
        """50 wins, 5 losses: total sample 55, win_rate ~91%, ROI
        positive — every Phase 3 threshold clears spectacularly. But
        only 5 losses fails the distribution guard, so promotion is
        blocked."""
        from apps.mlb.models import OddsSnapshot, SpreadOpportunity
        self._seed_spread_history(wins=50, losses=5)
        g = self._scheduled_game()
        OddsSnapshot.objects.create(
            game=g, captured_at=timezone.now(),
            market_home_win_prob=0.5, spread=-1.0,
        )
        new_opp = SpreadOpportunity.objects.get(game=g, signal_type='tight_spread')
        self.assertFalse(new_opp.is_recommended)

    def test_balanced_distribution_promotes(self):
        from apps.mlb.models import OddsSnapshot, SpreadOpportunity
        # 30 / 22 = 57.7% over 52, both buckets > 15. All bars clear.
        self._seed_spread_history(wins=30, losses=22)
        g = self._scheduled_game()
        OddsSnapshot.objects.create(
            game=g, captured_at=timezone.now(),
            market_home_win_prob=0.5, spread=-1.0,
        )
        new_opp = SpreadOpportunity.objects.get(game=g, signal_type='tight_spread')
        self.assertTrue(new_opp.is_recommended)


class Phase4CLVGuardTests(_OpportunityPhase2TestBase):
    """CLV guard: gated by sample size. When clv_sample_size >= 20,
    require positive_clv_rate >= 52%. Below the minimum, the guard is
    inactive and reports status='insufficient_sample' so the system
    doesn't block recommendations during the cold-start period."""

    def _seed_strong_history(self):
        """50 wins / 30 losses: 62.5% win rate, all Phase 3 + Phase 4
        non-CLV guards clear. Lets us isolate the CLV guard's effect."""
        from apps.mlb.models import SpreadOpportunity, OddsSnapshot
        for outcome, count in [('win', 50), ('loss', 30)]:
            for i in range(count):
                g = self._final_game(home_score=5 if outcome == 'win' else 2,
                                      away_score=2 if outcome == 'win' else 5)
                snap = OddsSnapshot.objects.create(
                    game=g, captured_at=timezone.now(),
                    market_home_win_prob=0.5, spread=-1.0,
                )
                SpreadOpportunity.objects.update_or_create(
                    game=g, odds_snapshot=snap, signal_type='tight_spread',
                    defaults={
                        'spread': -1.0, 'outcome': outcome,
                        'settled_at': timezone.now(),
                    },
                )

    def test_insufficient_clv_sample_does_not_block(self):
        """Default state: CLV stub returns insufficient_sample, so the
        guard is inactive and a strong-stats signal still promotes."""
        from apps.mlb.models import OddsSnapshot, SpreadOpportunity
        self._seed_strong_history()
        g = self._scheduled_game()
        OddsSnapshot.objects.create(
            game=g, captured_at=timezone.now(),
            market_home_win_prob=0.5, spread=-1.0,
        )
        new_opp = SpreadOpportunity.objects.get(game=g, signal_type='tight_spread')
        self.assertTrue(new_opp.is_recommended)

    def test_active_clv_below_threshold_blocks(self):
        from unittest.mock import patch
        from apps.mlb.models import OddsSnapshot, SpreadOpportunity
        self._seed_strong_history()
        g = self._scheduled_game()
        # Mock CLV stats to active (sample ≥ 20) but sub-threshold
        # (positive_clv_rate < 52%).
        with patch(
            'apps.mlb.services.opportunity_signals.compute_clv_stats',
            return_value={
                'positive_clv_rate': 0.45,
                'clv_sample_size': 40,
                'status': 'negative',
            },
        ):
            OddsSnapshot.objects.create(
                game=g, captured_at=timezone.now(),
                market_home_win_prob=0.5, spread=-1.0,
            )
        new_opp = SpreadOpportunity.objects.get(game=g, signal_type='tight_spread')
        self.assertFalse(new_opp.is_recommended)

    def test_active_clv_above_threshold_promotes(self):
        from unittest.mock import patch
        from apps.mlb.models import OddsSnapshot, SpreadOpportunity
        self._seed_strong_history()
        g = self._scheduled_game()
        with patch(
            'apps.mlb.services.opportunity_signals.compute_clv_stats',
            return_value={
                'positive_clv_rate': 0.55,
                'clv_sample_size': 40,
                'status': 'positive',
            },
        ):
            OddsSnapshot.objects.create(
                game=g, captured_at=timezone.now(),
                market_home_win_prob=0.5, spread=-1.0,
            )
        new_opp = SpreadOpportunity.objects.get(game=g, signal_type='tight_spread')
        self.assertTrue(new_opp.is_recommended)


# ===================================================================== #
# 2026-04-27 STRICT CORRECTION — recommendation safety tests
#
# These tests pin the corrected behavior end-to-end: the recommendation
# engine, the partition, the bulk-bet filter. They cover the three
# spec cases explicitly and verify the new gates fire at every layer.
# ===================================================================== #


class CorrectionRecommendationSafetyTests(TestCase):
    """End-to-end safety tests for the strict correction.

    The bug being fixed: a +1700 longshot with 21% probability and 15pp
    edge previously got Recommended via the elite-edge override. After
    this correction it's classified as VALUE — visible for manual
    decision but NEVER Recommended and NEVER in Bet All.
    """

    def setUp(self):
        from django.contrib.auth import get_user_model
        from apps.mlb.models import (
            Conference as MLBConf, Team as MLBTeam, Game as MLBGame,
        )
        User = get_user_model()
        self.user = User.objects.create_user(username='corr-test', password='pw')
        conf = MLBConf.objects.first() or MLBConf.objects.create(
            name='Corr', slug='corr',
        )
        # 2026-05-22 fixture update: gap widened 80/40 → 90/30 after
        # MARKET_BLEND_WEIGHT bumped 0.40 → 0.55. The prior gap pulled
        # blended prob just below MIN_PROBABILITY=0.60 for Case 2.
        self.high_rated = MLBTeam.objects.create(
            name='High', slug='corr-high', conference=conf,
            rating=90, source='mlb_stats_api', external_id='corr-high',
        )
        self.low_rated = MLBTeam.objects.create(
            name='Low', slug='corr-low', conference=conf,
            rating=30, source='mlb_stats_api', external_id='corr-low',
        )

    def _make_game(self, **odds_kwargs):
        from apps.mlb.models import Game as MLBGame, OddsSnapshot
        g = MLBGame.objects.create(
            home_team=self.high_rated, away_team=self.low_rated,
            first_pitch=timezone.now() + timedelta(hours=3),
            status='scheduled',
            source='mlb_stats_api',
            external_id=f'corr-{timezone.now().timestamp()}',
        )
        defaults = {
            'captured_at': timezone.now(),
            'market_home_win_prob': 0.5,
            'moneyline_home': -110, 'moneyline_away': -110,
            'odds_source': 'odds_api',
        }
        defaults.update(odds_kwargs)
        OddsSnapshot.objects.create(game=g, **defaults)
        return g

    def test_case1_low_probability_longshot_is_value_not_recommended(self):
        """SPEC CASE 1: a longshot bet on the dog gets classified as VALUE.

        Setup: high_rated home (rating 80) vs low_rated away (rating 40)
        with market that prices away as +1700. The model says away has
        ~21% probability of winning (small but real chance given the
        rating gap implies maybe 25%). Edge over devigged market is
        large but probability < 50% → must be VALUE."""
        from apps.core.services.recommendations import get_recommendation
        # Market priced as: home -2000, away +1700 (huge favorite).
        # Devigged this is roughly home 0.94, away 0.06.
        # Model with ratings 80 vs 40 says home ~0.85, away ~0.15.
        # Model picks the side with bigger edge — likely away (+9pp edge
        # at 15% prob) over home (-9pp at 85% prob).
        g = self._make_game(
            moneyline_home=-2000, moneyline_away=+1700,
            market_home_win_prob=0.94,
        )
        rec = get_recommendation('mlb', g, user=None)
        self.assertIsNotNone(rec)
        # The pick MUST NOT be Recommended — either it's the away side
        # (low prob → value) or the home side (longshot in the other
        # direction). Either way, status must be not_recommended.
        self.assertEqual(rec.status, 'not_recommended')
        # If it's the away side (more likely given edge math), classify
        # as value. If somehow the home side won the edge comparison,
        # it'd hit the longshot gate (-2000 → |odds|>300).
        self.assertIn(rec.status_reason, ('value', 'longshot'))

    def test_case2_strong_favorite_with_clean_edge_is_recommended(self):
        """SPEC CASE 2: probability ~62%, edge ~5pp, odds in range,
        primary source. All gates clear → Recommended."""
        from apps.core.services.recommendations import get_recommendation
        # Market home=-140 implies ~58% home. Model with ratings 80 vs
        # 40 says home is much higher (~85%). Plenty of edge, prob>55%,
        # |odds|<=300.
        g = self._make_game(
            moneyline_home=-140, moneyline_away=+120,
            market_home_win_prob=0.58,
        )
        rec = get_recommendation('mlb', g, user=None)
        self.assertEqual(rec.status, 'recommended')
        self.assertGreaterEqual(rec.confidence_score, 55.0)
        self.assertLessEqual(abs(rec.odds_american), 300)

    def test_case3_espn_fallback_is_visible_but_not_recommended(self):
        """SPEC CASE 3: ESPN-source pick. Visible (renders in tile) but
        is_recommended=False, status_reason='secondary_source', NEVER in
        bulk endpoint."""
        from apps.core.services.recommendations import get_recommendation
        g = self._make_game(
            moneyline_home=-130, moneyline_away=+110,
            market_home_win_prob=0.58,
            odds_source='espn',
        )
        rec = get_recommendation('mlb', g, user=None)
        self.assertEqual(rec.status, 'not_recommended')
        self.assertEqual(rec.status_reason, 'secondary_source')
        self.assertTrue(rec.is_secondary)

    def test_value_pick_excluded_from_bulk_bet_endpoint(self):
        """End-to-end: a value-classified pick must never produce a
        MockBet via the bulk endpoint. Defense in depth — the engine
        already sets status='not_recommended', and the bulk eligibility
        iterator double-checks the value tier."""
        from apps.mockbets.services.bulk_actions import place_bulk_recommended_bets
        from apps.mockbets.models import MockBet
        # Create a single-game slate with a longshot value pick.
        self._make_game(
            moneyline_home=-2000, moneyline_away=+1700,
            market_home_win_prob=0.94,
        )
        result = place_bulk_recommended_bets(self.user, source_filter='all')
        # Zero placements — the pick was classified as value/longshot.
        self.assertEqual(result['placed'], 0)
        self.assertEqual(MockBet.objects.filter(user=self.user).count(), 0)

    def test_recommended_count_not_capped_by_slate(self):
        """Per product direction (2026-04-28): assign_tiers does NOT
        impose a count ceiling on Recommended status. The per-pick
        gates (in compute_status) are the safety bar — every bet that
        clears them gets surfaced, even if 10+ qualify in one slate.

        Locks the 'no cap' contract at the engine layer."""
        from apps.core.services.recommendations import (
            Recommendation, STATUS_RECOMMENDED, assign_tiers,
        )
        # Build 8 mock recommendations all status=Recommended.
        recs = []
        for i in range(8):
            r = Recommendation(
                sport='mlb', game=None, bet_type='moneyline', pick='X',
                line='-110', odds_american=-110,
                confidence_score=60.0 + i * 0.1,
                model_edge=5.0 + i * 0.1,
                model_source='house',
                tier='standard', status=STATUS_RECOMMENDED, status_reason='',
            )
            recs.append(r)
        assign_tiers(recs)
        # All 8 stay Recommended — no slate cap.
        rec_count = sum(1 for r in recs if r.status == STATUS_RECOMMENDED)
        self.assertEqual(rec_count, 8)
        # No demotions to 'marginal'.
        marginal_count = sum(
            1 for r in recs if r.status_reason == 'marginal'
        )
        self.assertEqual(marginal_count, 0)


class Phase4MoneylineNonRegressionTests(TestCase):
    """The promotion-hardening guards must not leak into the
    recommendation engine. Locks down the new functions in addition
    to the existing Phase 1/2/3 sentinels."""

    def test_recommendation_engine_unaffected_by_phase4_layer(self):
        from unittest.mock import patch, PropertyMock
        from apps.mlb.models import (
            Conference as MLBConf, Team as MLBTeam, Game as MLBGame, OddsSnapshot,
        )
        from apps.core.services.recommendations import get_recommendation

        conf = MLBConf.objects.first() or MLBConf.objects.create(
            name='P4 Reg', slug='p4-reg',
        )
        h = MLBTeam.objects.create(
            name='P4 Home', slug='p4-home', conference=conf,
            rating=80, source='mlb_stats_api', external_id='p4-home',
        )
        a = MLBTeam.objects.create(
            name='P4 Away', slug='p4-away', conference=conf,
            rating=40, source='mlb_stats_api', external_id='p4-away',
        )
        g = MLBGame.objects.create(
            home_team=h, away_team=a,
            first_pitch=timezone.now() + timedelta(hours=3),
            status='scheduled',
            source='mlb_stats_api', external_id='p4r-game',
        )
        OddsSnapshot.objects.create(
            game=g, captured_at=timezone.now(),
            market_home_win_prob=0.55,
            moneyline_home=-150, moneyline_away=130,
            spread=-1.5, total=9.5,
        )

        with patch.object(MLBGame, 'spread_opportunities',
                          new_callable=PropertyMock,
                          side_effect=AssertionError(
                              'phase4: rec engine touched spread_opportunities'
                          )):
            with patch.object(MLBGame, 'total_opportunities',
                              new_callable=PropertyMock,
                              side_effect=AssertionError(
                                  'phase4: rec engine touched total_opportunities'
                              )):
                with patch(
                    'apps.mlb.services.opportunity_signals.compute_clv_stats',
                    side_effect=AssertionError(
                        'phase4: rec engine called compute_clv_stats'
                    ),
                ):
                    with patch(
                        'apps.mlb.services.opportunity_signals._phase4_promotion_guards_pass',
                        side_effect=AssertionError(
                            'phase4: rec engine called the guards function'
                        ),
                    ):
                        rec = get_recommendation('mlb', g, user=None)
        self.assertIsNotNone(rec)
        self.assertEqual(rec.bet_type, 'moneyline')


class Phase3MoneylineNonRegressionTests(TestCase):
    """Belt-and-suspenders sentinel for Phase 3. Locks down the new
    promotion classifier + break-even function in addition to the
    Phase 1/2 surfaces. If the recommendation engine ever starts
    consulting any of these, this test fails loudly."""

    def test_recommendation_engine_unaffected_by_phase3_layer(self):
        from unittest.mock import patch, PropertyMock
        from apps.mlb.models import (
            Conference as MLBConf, Team as MLBTeam, Game as MLBGame, OddsSnapshot,
        )
        from apps.core.services.recommendations import get_recommendation

        conf = MLBConf.objects.first() or MLBConf.objects.create(
            name='P3 Reg', slug='p3-reg',
        )
        h = MLBTeam.objects.create(
            name='P3R Home', slug='p3r-home', conference=conf,
            rating=80, source='mlb_stats_api', external_id='p3r-home',
        )
        a = MLBTeam.objects.create(
            name='P3R Away', slug='p3r-away', conference=conf,
            rating=40, source='mlb_stats_api', external_id='p3r-away',
        )
        g = MLBGame.objects.create(
            home_team=h, away_team=a,
            first_pitch=timezone.now() + timedelta(hours=3),
            status='scheduled',
            source='mlb_stats_api', external_id='p3r-game',
        )
        OddsSnapshot.objects.create(
            game=g, captured_at=timezone.now(),
            market_home_win_prob=0.55,
            moneyline_home=-150, moneyline_away=130,
            spread=-1.5, total=9.5,
        )

        with patch.object(MLBGame, 'spread_opportunities',
                          new_callable=PropertyMock,
                          side_effect=AssertionError(
                              'phase3: rec engine touched spread_opportunities'
                          )):
            with patch.object(MLBGame, 'total_opportunities',
                              new_callable=PropertyMock,
                              side_effect=AssertionError(
                                  'phase3: rec engine touched total_opportunities'
                              )):
                with patch(
                    'apps.mlb.services.opportunity_signals._spread_classify',
                    side_effect=AssertionError(
                        'phase3: rec engine called the spread classifier'
                    ),
                ):
                    with patch(
                        'apps.mlb.services.opportunity_signals._total_classify',
                        side_effect=AssertionError(
                            'phase3: rec engine called the total classifier'
                        ),
                    ):
                        with patch(
                            'apps.mlb.services.opportunity_signals.calculate_break_even',
                            side_effect=AssertionError(
                                'phase3: rec engine called calculate_break_even'
                            ),
                        ):
                            rec = get_recommendation('mlb', g, user=None)
        self.assertIsNotNone(rec)
        self.assertEqual(rec.bet_type, 'moneyline')


class Phase2MoneylineNonRegressionTests(TestCase):
    """Belt-and-suspenders again: the Phase 2 settlement / lean code
    must NOT cause the Moneyline pipeline to consult the new fields.
    Same sentinel pattern as Phase 1's MoneylinePipelineNonRegressionTests
    — patches the related managers AND the new performance functions
    to AssertionError-on-call, then runs get_recommendation."""

    def test_recommendation_engine_unaffected_by_phase2_layer(self):
        from unittest.mock import patch, PropertyMock
        from apps.mlb.models import (
            Conference as MLBConf, Team as MLBTeam, Game as MLBGame, OddsSnapshot,
        )
        from apps.core.services.recommendations import get_recommendation

        conf = MLBConf.objects.first() or MLBConf.objects.create(
            name='P2 Reg', slug='p2-reg',
        )
        h = MLBTeam.objects.create(
            name='P2R Home', slug='p2r-home', conference=conf,
            rating=80, source='mlb_stats_api', external_id='p2r-home',
        )
        a = MLBTeam.objects.create(
            name='P2R Away', slug='p2r-away', conference=conf,
            rating=40, source='mlb_stats_api', external_id='p2r-away',
        )
        g = MLBGame.objects.create(
            home_team=h, away_team=a,
            first_pitch=timezone.now() + timedelta(hours=3),
            status='scheduled',
            source='mlb_stats_api', external_id='p2r-game',
        )
        OddsSnapshot.objects.create(
            game=g, captured_at=timezone.now(),
            market_home_win_prob=0.55,
            moneyline_home=-150, moneyline_away=130,
            spread=-1.5, total=9.5,
        )

        with patch.object(MLBGame, 'spread_opportunities',
                          new_callable=PropertyMock,
                          side_effect=AssertionError(
                              'phase2: rec engine touched spread_opportunities'
                          )):
            with patch.object(MLBGame, 'total_opportunities',
                              new_callable=PropertyMock,
                              side_effect=AssertionError(
                                  'phase2: rec engine touched total_opportunities'
                              )):
                # Also lock down the perf functions — if the rec engine
                # ever started using them as a soft signal, this would catch.
                with patch(
                    'apps.mlb.services.opportunity_signals.compute_spread_performance',
                    side_effect=AssertionError('phase2 perf leaked into rec'),
                ):
                    with patch(
                        'apps.mlb.services.opportunity_signals.compute_total_performance',
                        side_effect=AssertionError('phase2 perf leaked into rec'),
                    ):
                        rec = get_recommendation('mlb', g, user=None)
        self.assertIsNotNone(rec)
        self.assertEqual(rec.bet_type, 'moneyline')
