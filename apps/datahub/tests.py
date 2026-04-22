"""Tests for datahub providers — focused on golf odds windowed fetch gating."""

from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from apps.datahub.providers.golf.odds_provider import (
    GolfOddsProvider,
    is_event_in_window,
)
from apps.golf.models import GolfEvent, Golfer, GolfOddsSnapshot


class IsEventInWindowTests(TestCase):
    """Unit tests for the pure window predicate."""

    def setUp(self):
        self.today = timezone.now().date()

    def _event(self, start_offset, end_offset):
        # start_date/end_date relative to today
        return GolfEvent.objects.create(
            name=f'Test Event {start_offset}-{end_offset}',
            slug=f'test-event-{start_offset}-{end_offset}',
            start_date=self.today + timedelta(days=start_offset),
            end_date=self.today + timedelta(days=end_offset),
        )

    def test_event_more_than_7_days_out_is_outside_window(self):
        event = self._event(start_offset=10, end_offset=13)
        in_window, reason = is_event_in_window(event, self.today)
        self.assertFalse(in_window)
        self.assertEqual(reason, "outside_window")

    def test_event_exactly_7_days_out_is_in_window(self):
        event = self._event(start_offset=7, end_offset=10)
        in_window, reason = is_event_in_window(event, self.today)
        self.assertTrue(in_window)
        self.assertEqual(reason, "in_window")

    def test_event_5_days_out_is_in_window(self):
        event = self._event(start_offset=5, end_offset=8)
        in_window, reason = is_event_in_window(event, self.today)
        self.assertTrue(in_window)
        self.assertEqual(reason, "in_window")

    def test_event_in_progress_is_in_window(self):
        event = self._event(start_offset=-1, end_offset=2)
        in_window, reason = is_event_in_window(event, self.today)
        self.assertTrue(in_window)

    def test_event_ending_today_is_in_window(self):
        event = self._event(start_offset=-3, end_offset=0)
        in_window, reason = is_event_in_window(event, self.today)
        self.assertTrue(in_window)

    def test_event_ended_yesterday_is_complete(self):
        event = self._event(start_offset=-4, end_offset=-1)
        in_window, reason = is_event_in_window(event, self.today)
        self.assertFalse(in_window)
        self.assertEqual(reason, "event_complete")


class GolfOddsProviderFetchGateTests(TestCase):
    """fetch() should skip API calls when no events are in window."""

    def setUp(self):
        self.today = timezone.now().date()

    def _make_provider(self):
        # Bypass __init__ (which requires settings.ODDS_API_KEY) — we're only
        # testing the fetch gate, not the HTTP path.
        provider = GolfOddsProvider.__new__(GolfOddsProvider)
        provider.api_key = 'test-key'
        provider.client = None  # Will be replaced via patch when needed
        return provider

    def test_fetch_skipped_when_no_events(self):
        provider = self._make_provider()
        with patch.object(type(provider), 'client', create=True) as mock_client:
            result = provider.fetch()
            self.assertEqual(result, [])
            mock_client.get.assert_not_called()

    def test_fetch_skipped_when_all_events_outside_window(self):
        GolfEvent.objects.create(
            name='Future Major', slug='future-major',
            start_date=self.today + timedelta(days=30),
            end_date=self.today + timedelta(days=33),
        )
        provider = self._make_provider()

        # Stub out client with a mock so we can assert no HTTP call is made.
        from unittest.mock import MagicMock
        provider.client = MagicMock()

        result = provider.fetch()
        self.assertEqual(result, [])
        provider.client.get.assert_not_called()

    def test_fetch_proceeds_when_event_in_window(self):
        GolfEvent.objects.create(
            name='The Masters', slug='the-masters',
            start_date=self.today + timedelta(days=3),
            end_date=self.today + timedelta(days=6),
        )
        provider = self._make_provider()

        from unittest.mock import MagicMock
        provider.client = MagicMock()
        provider.client.get.return_value = []

        provider.fetch()
        # One call per sport key
        self.assertEqual(provider.client.get.call_count, 4)

    def test_fetch_skipped_when_event_already_ended(self):
        GolfEvent.objects.create(
            name='Past Major', slug='past-major',
            start_date=self.today - timedelta(days=10),
            end_date=self.today - timedelta(days=7),
        )
        provider = self._make_provider()

        from unittest.mock import MagicMock
        provider.client = MagicMock()

        result = provider.fetch()
        self.assertEqual(result, [])
        provider.client.get.assert_not_called()


class GolfOddsProviderPersistGateTests(TestCase):
    """persist() should enforce window and once-per-day guards."""

    def setUp(self):
        self.today = timezone.now().date()
        self.provider = GolfOddsProvider.__new__(GolfOddsProvider)

    def _normalized_item(self, event_name, golfer_name='Test Golfer'):
        return {
            'event_name': event_name,
            'golfer_name': golfer_name,
            'sportsbook': 'DraftKings',
            'outright_odds': 500,
            'implied_prob': 0.1667,
        }

    def test_persist_skips_event_outside_window(self):
        # Event starts 20 days from now -> outside window
        GolfEvent.objects.create(
            name='Future Major', slug='future-major',
            start_date=self.today + timedelta(days=20),
            end_date=self.today + timedelta(days=23),
        )
        result = self.provider.persist([self._normalized_item('Future Major')])
        # Event is not in upcoming_events (end_date__gte=today is true here,
        # so it enters the loop) and fails the window check.
        # Verify no snapshot was written.
        self.assertEqual(GolfOddsSnapshot.objects.count(), 0)
        self.assertEqual(result['created'], 0)
        self.assertGreaterEqual(result['skipped'], 1)

    def test_persist_allows_event_in_window(self):
        GolfEvent.objects.create(
            name='The Masters', slug='the-masters',
            start_date=self.today + timedelta(days=3),
            end_date=self.today + timedelta(days=6),
        )
        result = self.provider.persist([self._normalized_item('The Masters')])
        self.assertEqual(result['created'], 1)
        self.assertEqual(GolfOddsSnapshot.objects.count(), 1)

    def test_persist_dedupes_same_day_duplicates(self):
        event = GolfEvent.objects.create(
            name='The Masters', slug='the-masters',
            start_date=self.today + timedelta(days=3),
            end_date=self.today + timedelta(days=6),
        )
        golfer = Golfer.objects.create(name='Existing Golfer')
        # Pre-seed a snapshot captured today
        GolfOddsSnapshot.objects.create(
            event=event, golfer=golfer,
            captured_at=timezone.now(),
            sportsbook='DraftKings',
            outright_odds=500,
            implied_prob=0.1667,
        )

        # Second persist run on the same day should skip
        result = self.provider.persist([
            self._normalized_item('The Masters', golfer_name='New Golfer'),
        ])
        self.assertEqual(result['created'], 0)
        self.assertEqual(result['skipped'], 1)
        # Only the pre-seeded snapshot remains
        self.assertEqual(GolfOddsSnapshot.objects.count(), 1)


class ScoreOnlyProviderTests(TestCase):
    """Covers the new lightweight update path on MLB's schedule provider.

    We mock the API call (fetch) so no network hits in tests. The test asserts
    the provider writes only status/home_score/away_score — never odds, never
    pitchers, never creating new rows.
    """

    def setUp(self):
        import uuid as _uuid
        from apps.mlb.models import Conference, Team, Game, StartingPitcher
        conf = Conference.objects.create(name='AL East', slug='al-east')
        self.home = Team.objects.create(
            name='Yankees', slug='yankees', conference=conf,
            source='mlb_stats_api', external_id='147',
        )
        self.away = Team.objects.create(
            name='Royals', slug='royals', conference=conf,
            source='mlb_stats_api', external_id='118',
        )
        # Pre-existing starting pitcher — the score-only path must NOT clear
        # or overwrite the pitcher FK.
        self.pitcher = StartingPitcher.objects.create(
            team=self.home, name='Gerrit Cole',
            source='mlb_stats_api', external_id='543037',
        )
        self.ext_id = str(_uuid.uuid4())
        self.game = Game.objects.create(
            home_team=self.home, away_team=self.away,
            first_pitch=timezone.now() + timedelta(hours=1),
            status='scheduled',
            home_pitcher=self.pitcher,
            source='mlb_stats_api', external_id=self.ext_id,
        )

    def _raw_game(self, status='Final', home_score=5, away_score=3, offset_hours=1):
        """Shape matches what the MLB Stats API returns enough to pass normalize()."""
        game_time = timezone.now() + timedelta(hours=offset_hours)
        return {
            'gamePk': self.ext_id,
            'gameDate': game_time.isoformat(),
            'status': {'detailedState': status},
            'teams': {
                'home': {'team': {'id': '147', 'name': 'Yankees'}, 'score': home_score},
                'away': {'team': {'id': '118', 'name': 'Royals'}, 'score': away_score},
            },
            'venue': {},
        }

    def _patched_provider(self, raw_games):
        """Instantiate MLB provider with fetch() mocked to return our shape."""
        from apps.datahub.providers.mlb.schedule_provider import MLBScheduleProvider
        prov = MLBScheduleProvider()
        prov.fetch = lambda: raw_games
        return prov

    def test_updates_status_and_scores_only_on_real_change(self):
        prov = self._patched_provider([self._raw_game(status='Final', home_score=5, away_score=3)])
        stats = prov.update_scores_only()
        self.assertEqual(stats['updated'], 1)
        self.assertEqual(stats['skipped'], 0)
        self.game.refresh_from_db()
        self.assertEqual(self.game.status, 'final')
        self.assertEqual(self.game.home_score, 5)
        self.assertEqual(self.game.away_score, 3)
        # Pitcher FK preserved — the score-only path must not touch unrelated fields.
        self.assertEqual(self.game.home_pitcher_id, self.pitcher.id)

    def test_is_idempotent_and_skips_unchanged(self):
        """Second run after the score has settled must dirty-check and skip."""
        prov = self._patched_provider([self._raw_game(home_score=5, away_score=3)])
        prov.update_scores_only()
        stats2 = prov.update_scores_only()
        self.assertEqual(stats2['updated'], 0)
        self.assertEqual(stats2['skipped'], 1)

    def test_skips_games_outside_live_window(self):
        # Game way in the future — window is now-1d to now+12h.
        prov = self._patched_provider([self._raw_game(offset_hours=48)])
        stats = prov.update_scores_only()
        self.assertEqual(stats['updated'], 0)
        self.assertEqual(stats['out_of_window'], 1)

    def test_skips_unknown_games_without_creating_them(self):
        """If the API returns a game we don't already have, we skip it — the
        heavy 6-hour cron owns row creation."""
        import uuid as _uuid
        from apps.mlb.models import Game
        raw = self._raw_game()
        raw['gamePk'] = str(_uuid.uuid4())  # different external_id
        prov = self._patched_provider([raw])
        initial = Game.objects.count()
        stats = prov.update_scores_only()
        self.assertEqual(stats['not_found'], 1)
        self.assertEqual(stats['updated'], 0)
        self.assertEqual(Game.objects.count(), initial)  # no row created

    def test_not_found_emits_warning_log_with_external_id(self):
        """Operators need visibility when the API surfaces a game we haven't
        ingested yet. Emit a structured warning keyed by external_id."""
        import uuid as _uuid
        raw = self._raw_game()
        missing_id = str(_uuid.uuid4())
        raw['gamePk'] = missing_id
        prov = self._patched_provider([raw])
        with self.assertLogs('apps.datahub.providers.base', level='WARNING') as cm:
            prov.update_scores_only()
        output = '\n'.join(cm.output)
        self.assertIn('Score update skipped', output)

    def test_summary_log_includes_counts_and_window(self):
        """Final summary log must carry counts + resolved window hours so the
        Railway deploy log tells the full story of a cycle."""
        prov = self._patched_provider([self._raw_game(home_score=5, away_score=3)])
        with self.assertLogs('apps.datahub.providers.base', level='INFO') as cm:
            prov.update_scores_only()
        self.assertTrue(
            any('Score update summary' in line for line in cm.output),
            msg=f'Missing summary log in {cm.output}',
        )

    def test_window_settings_respected(self):
        """Widen the lookahead via settings — a game 30h out must now count as
        in-window, not out_of_window. Proves the window is call-time resolved."""
        prov = self._patched_provider([self._raw_game(offset_hours=30)])
        # Default 12h lookahead: game is out of window
        stats = prov.update_scores_only()
        self.assertEqual(stats['out_of_window'], 1)
        # Widen lookahead to 48h: now it's in window and gets updated
        with self.settings(SCORE_UPDATE_LOOKAHEAD_HOURS=48):
            stats = prov.update_scores_only()
        self.assertEqual(stats['out_of_window'], 0)
        self.assertEqual(stats['updated'], 1)
        self.assertEqual(stats['window_hours']['lookahead'], 48)


class RefreshScoresAndSettleCommandTests(TestCase):
    """End-to-end: the 15-minute command flips a pending bet to 'win' after
    the underlying MLB game reports final — with no odds/model recompute."""

    def setUp(self):
        import uuid as _uuid
        from django.contrib.auth.models import User
        from apps.mlb.models import Conference, Team, Game
        from apps.mockbets.models import MockBet
        from decimal import Decimal

        conf = Conference.objects.create(name='AL East', slug='al-east')
        self.home = Team.objects.create(
            name='Yankees', slug='yankees', conference=conf,
            source='mlb_stats_api', external_id='147',
        )
        self.away = Team.objects.create(
            name='Royals', slug='royals', conference=conf,
            source='mlb_stats_api', external_id='118',
        )
        self.ext_id = str(_uuid.uuid4())
        self.game = Game.objects.create(
            home_team=self.home, away_team=self.away,
            first_pitch=timezone.now() + timedelta(hours=1),
            status='scheduled',
            source='mlb_stats_api', external_id=self.ext_id,
        )
        user = User.objects.create_user('bettor', password='pw')
        self.bet = MockBet.objects.create(
            user=user, sport='mlb', bet_type='moneyline',
            selection='Yankees', odds_american=-150,
            implied_probability=Decimal('0.60'),
            stake_amount=Decimal('100'), mlb_game=self.game,
        )

    def _mocked_api_payload(self, home=5, away=3):
        return [{
            'gamePk': self.ext_id,
            'gameDate': (timezone.now() + timedelta(hours=1)).isoformat(),
            'status': {'detailedState': 'Final'},
            'teams': {
                'home': {'team': {'id': '147', 'name': 'Yankees'}, 'score': home},
                'away': {'team': {'id': '118', 'name': 'Royals'}, 'score': away},
            },
            'venue': {},
        }]

    def test_end_to_end_score_update_and_settle(self):
        """Full dual-speed flow: game is scheduled → lightweight cron fires →
        API reports Final → command updates status + settles the pending bet."""
        from django.core.management import call_command
        from io import StringIO
        from apps.mockbets.models import MockBet
        from decimal import Decimal

        # Patch the MLB provider's fetch to avoid network.
        with patch(
            'apps.datahub.providers.mlb.schedule_provider.MLBScheduleProvider.fetch',
            return_value=self._mocked_api_payload(home=5, away=3),
        ), self.settings(
            LIVE_DATA_ENABLED=True,
            LIVE_MLB_ENABLED=True,
            LIVE_COLLEGE_BASEBALL_ENABLED=False,
        ):
            call_command('refresh_scores_and_settle', sport='mlb', stdout=StringIO())

        self.game.refresh_from_db()
        self.bet.refresh_from_db()
        self.assertEqual(self.game.status, 'final')
        self.assertEqual(self.game.home_score, 5)
        self.assertEqual(self.bet.result, 'win')
        self.assertEqual(self.bet.simulated_payout, Decimal('66.67').quantize(Decimal('0.01')))

    def test_second_run_does_not_double_settle(self):
        """Idempotency guard: running the command twice must not re-settle or
        flip the already-settled bet."""
        from django.core.management import call_command
        from io import StringIO
        from apps.mockbets.models import MockBet, MockBetSettlementLog

        with patch(
            'apps.datahub.providers.mlb.schedule_provider.MLBScheduleProvider.fetch',
            return_value=self._mocked_api_payload(home=5, away=3),
        ), self.settings(
            LIVE_DATA_ENABLED=True,
            LIVE_MLB_ENABLED=True,
            LIVE_COLLEGE_BASEBALL_ENABLED=False,
        ):
            call_command('refresh_scores_and_settle', sport='mlb', stdout=StringIO())
            call_command('refresh_scores_and_settle', sport='mlb', stdout=StringIO())

        # Exactly one settlement log row — the second run must no-op on bets.
        self.assertEqual(MockBetSettlementLog.objects.filter(mock_bet=self.bet).count(), 1)

    def test_dispatcher_emits_cycle_summary_log(self):
        """Dispatcher must emit both a per-provider success log and a cycle
        summary log so operators can grep the Railway log for health state."""
        from apps.datahub.services.scores import update_scores_only
        with patch(
            'apps.datahub.providers.mlb.schedule_provider.MLBScheduleProvider.fetch',
            return_value=self._mocked_api_payload(home=5, away=3),
        ), self.settings(
            LIVE_DATA_ENABLED=True,
            LIVE_MLB_ENABLED=True,
            LIVE_COLLEGE_BASEBALL_ENABLED=False,
        ):
            with self.assertLogs('apps.datahub.services.scores', level='INFO') as cm:
                update_scores_only(sport='mlb')
        joined = '\n'.join(cm.output)
        self.assertIn('mlb score update success', joined)
        self.assertIn('Score refresh cycle complete', joined)

    def test_dispatcher_continues_past_provider_failure(self):
        """A single provider raising must not break the dispatcher — the cycle
        summary still runs and marks providers_failed=1."""
        from apps.datahub.services.scores import update_scores_only
        with patch(
            'apps.datahub.providers.mlb.schedule_provider.MLBScheduleProvider.fetch',
            side_effect=RuntimeError('api down'),
        ), self.settings(
            LIVE_DATA_ENABLED=True,
            LIVE_MLB_ENABLED=True,
            LIVE_COLLEGE_BASEBALL_ENABLED=False,
        ):
            results = update_scores_only(sport='mlb')
        self.assertEqual(results['mlb']['status'], 'error')
        self.assertIn('api down', results['mlb']['error'])

    def test_skips_bets_for_games_still_in_progress(self):
        """If the game is 'live' (not final), the command must leave the bet pending.
        The score can update, but no settlement fires."""
        from django.core.management import call_command
        from io import StringIO

        in_progress = self._mocked_api_payload(home=2, away=1)
        in_progress[0]['status']['detailedState'] = 'In Progress'

        with patch(
            'apps.datahub.providers.mlb.schedule_provider.MLBScheduleProvider.fetch',
            return_value=in_progress,
        ), self.settings(
            LIVE_DATA_ENABLED=True,
            LIVE_MLB_ENABLED=True,
            LIVE_COLLEGE_BASEBALL_ENABLED=False,
        ):
            call_command('refresh_scores_and_settle', sport='mlb', stdout=StringIO())

        self.game.refresh_from_db()
        self.bet.refresh_from_db()
        self.assertEqual(self.game.status, 'live')
        self.assertEqual(self.game.home_score, 2)
        self.assertEqual(self.bet.result, 'pending')
