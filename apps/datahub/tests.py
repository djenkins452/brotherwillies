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


# =========================================================================
# MLB team alias coverage + odds persist robustness
# =========================================================================
# We had a prod incident where 15 of 16 MLB games silently lost their
# odds because team-name normalization missed a variant. These tests
# enforce the contract:
#   1. Every one of the 30 MLB franchises has at least one alias.
#   2. Every common name variant resolves to the right canonical name.
#   3. Fuzzy matching catches surprises that the alias dict missed.
#   4. The persist function logs WHY each row was dropped.
#   5. Coverage below 50% logs at ERROR so the Ops dashboard sees it.

import logging as _stdlib_logging
from unittest.mock import patch as _patch_for_alias_tests, MagicMock as _MagicMock

from apps.datahub.providers.mlb.name_aliases import (
    CANONICAL_MLB_TEAMS,
    MLB_TEAM_ALIASES,
    fuzzy_match_to_canonical,
    normalize_mlb_team_name,
)


class MlbAliasCoverageTests(TestCase):
    """One canonical name per franchise; multiple aliases each. The dict
    is the first-line defense — every well-known variant must resolve
    correctly without needing the fuzzy fallback."""

    def test_all_30_franchises_present(self):
        # 30 active franchises. The Athletics show up under their
        # current single-word brand; Oakland Athletics is an alias.
        self.assertEqual(len(CANONICAL_MLB_TEAMS), 30)

    def test_each_canonical_is_self_alias(self):
        # The canonical full name itself must normalize to itself.
        for canonical in CANONICAL_MLB_TEAMS:
            self.assertEqual(
                normalize_mlb_team_name(canonical), canonical,
                f'canonical {canonical!r} did not self-resolve',
            )

    def test_nickname_only_resolves_correctly(self):
        # The Odds API has been observed to send nicknames-only on certain
        # endpoints — every nickname-only form must hit the right canonical.
        nickname_to_canonical = {
            'Diamondbacks': 'Arizona Diamondbacks',
            'Braves': 'Atlanta Braves',
            'Orioles': 'Baltimore Orioles',
            'Red Sox': 'Boston Red Sox',
            'Cubs': 'Chicago Cubs',
            'White Sox': 'Chicago White Sox',
            'Reds': 'Cincinnati Reds',
            'Guardians': 'Cleveland Guardians',
            'Rockies': 'Colorado Rockies',
            'Tigers': 'Detroit Tigers',
            'Astros': 'Houston Astros',
            'Royals': 'Kansas City Royals',
            'Angels': 'Los Angeles Angels',
            'Dodgers': 'Los Angeles Dodgers',
            'Marlins': 'Miami Marlins',
            'Brewers': 'Milwaukee Brewers',
            'Twins': 'Minnesota Twins',
            'Mets': 'New York Mets',
            'Yankees': 'New York Yankees',
            'Phillies': 'Philadelphia Phillies',
            'Pirates': 'Pittsburgh Pirates',
            'Padres': 'San Diego Padres',
            'Giants': 'San Francisco Giants',
            'Mariners': 'Seattle Mariners',
            'Cardinals': 'St. Louis Cardinals',
            'Rays': 'Tampa Bay Rays',
            'Rangers': 'Texas Rangers',
            'Blue Jays': 'Toronto Blue Jays',
            'Nationals': 'Washington Nationals',
        }
        for nickname, canonical in nickname_to_canonical.items():
            self.assertEqual(
                normalize_mlb_team_name(nickname), canonical,
                f'nickname {nickname!r} did not resolve to {canonical!r}',
            )

    def test_common_short_forms(self):
        cases = [
            ('NY Yankees', 'New York Yankees'),
            ('NY Mets', 'New York Mets'),
            ('LA Dodgers', 'Los Angeles Dodgers'),
            ('LA Angels', 'Los Angeles Angels'),
            ('SF Giants', 'San Francisco Giants'),
            ('SD Padres', 'San Diego Padres'),
            ('Chi Cubs', 'Chicago Cubs'),
            ('Chi White Sox', 'Chicago White Sox'),
            ('AZ Diamondbacks', 'Arizona Diamondbacks'),
            ('D-Backs', 'Arizona Diamondbacks'),
            ('Tampa Bay', 'Tampa Bay Rays'),
            ('Kansas City', 'Kansas City Royals'),
            ('St Louis', 'St. Louis Cardinals'),
            ('Saint Louis Cardinals', 'St. Louis Cardinals'),
        ]
        for input_name, expected in cases:
            self.assertEqual(
                normalize_mlb_team_name(input_name), expected,
                f'{input_name!r} did not resolve to {expected!r}',
            )

    def test_three_letter_abbreviations(self):
        # Some APIs/feeds send 3-letter abbreviations only.
        cases = [
            ('NYY', 'New York Yankees'),
            ('NYM', 'New York Mets'),
            ('LAD', 'Los Angeles Dodgers'),
            ('LAA', 'Los Angeles Angels'),
            ('BOS', 'Boston Red Sox'),
            ('STL', 'St. Louis Cardinals'),
            ('TBR', 'Tampa Bay Rays'),
            ('CHC', 'Chicago Cubs'),
            ('CWS', 'Chicago White Sox'),
        ]
        for abbr, expected in cases:
            self.assertEqual(
                normalize_mlb_team_name(abbr), expected,
                f'{abbr!r} did not resolve to {expected!r}',
            )

    def test_athletics_relocation_variants_all_resolve(self):
        # Athletics rebrand — Oakland → Sacramento (interim) → Athletics-only.
        # All three forms map to the canonical "Athletics".
        for variant in ('Oakland Athletics', 'Sacramento Athletics',
                        'Athletics', 'Oakland', "A's"):
            self.assertEqual(normalize_mlb_team_name(variant), 'Athletics')

    def test_unknown_input_passes_through_unchanged(self):
        # Defensive: unknown name returns trimmed input, not None or ''.
        self.assertEqual(normalize_mlb_team_name('Some Team'), 'Some Team')
        self.assertEqual(normalize_mlb_team_name('  Mets  '), 'New York Mets')

    def test_empty_input_safe(self):
        self.assertEqual(normalize_mlb_team_name(''), '')
        self.assertEqual(normalize_mlb_team_name(None), '')


class MlbFuzzyMatchTests(TestCase):
    """Fuzzy fallback runs only when the alias dict misses. Tests the
    contract: known nicknames inside arbitrary input → correct match;
    nothing matches → None (don't guess wildly)."""

    def test_substring_match_finds_known_nickname(self):
        # An imaginary API rebrand: "ATH (Athletics)" → should still hit.
        self.assertEqual(fuzzy_match_to_canonical('ATH (Athletics)'), 'Athletics')

    def test_input_substring_of_canonical(self):
        # API trims to "Yankees" — already in alias dict, but the fuzzy
        # path must also handle it as a safety net.
        self.assertEqual(fuzzy_match_to_canonical('Yankees'), 'New York Yankees')

    def test_two_word_nickname_still_matches(self):
        self.assertEqual(fuzzy_match_to_canonical('Boston Red Sox FC'), 'Boston Red Sox')
        self.assertEqual(fuzzy_match_to_canonical('Chicago White Sox B'), 'Chicago White Sox')
        self.assertEqual(fuzzy_match_to_canonical('Toronto Blue Jays Ltd'), 'Toronto Blue Jays')

    def test_garbage_returns_none(self):
        self.assertIsNone(fuzzy_match_to_canonical('Not A Team'))
        self.assertIsNone(fuzzy_match_to_canonical('xyz'))
        self.assertIsNone(fuzzy_match_to_canonical(''))
        self.assertIsNone(fuzzy_match_to_canonical(None))

    def test_short_input_does_not_substring_match(self):
        # 'a' is a substring of half the canonicals — must not match.
        self.assertIsNone(fuzzy_match_to_canonical('a'))
        self.assertIsNone(fuzzy_match_to_canonical('e'))


class MlbOddsPersistLoggingTests(TestCase):
    """The persist function's logs are the prod debugger. Every skip
    must include enough info to identify whether the failure is alias
    coverage, missing schedule entry, or downstream — without re-running."""

    def setUp(self):
        from apps.datahub.providers.mlb.odds_provider import MLBOddsProvider
        from apps.mlb.models import Conference, Game, Team
        self.provider = MLBOddsProvider.__new__(MLBOddsProvider)
        # Skip the __init__ that requires ODDS_API_KEY for these unit tests.
        conf = Conference.objects.create(name='AL East', slug='al-east')
        self.home = Team.objects.create(
            name='New York Yankees', slug='new-york-yankees', conference=conf,
        )
        self.away = Team.objects.create(
            name='Boston Red Sox', slug='boston-red-sox', conference=conf,
        )
        self.game = Game.objects.create(
            home_team=self.home, away_team=self.away,
            first_pitch=timezone.now() + timedelta(hours=2),
        )

    def _item(self, **kw):
        defaults = {
            'home_team': 'New York Yankees',
            'away_team': 'Boston Red Sox',
            'commence_time': self.game.first_pitch.isoformat().replace('+00:00', 'Z'),
            'sportsbook': 'DraftKings',
            'moneyline_home': -150,
            'moneyline_away': 130,
            'spread': -1.5,
            'total': 8.5,
        }
        defaults.update(kw)
        return defaults

    def test_skip_log_includes_normalized_names(self):
        with self.assertLogs('apps.datahub.providers.mlb.odds_provider', level='INFO') as cm:
            result = self.provider.persist([
                self._item(home_team='Mystery Team', away_team='Other Mystery'),
            ])
        # The skip line must call out the canonical attempts so a log
        # reader can tell alias-miss from DB-miss without rerunning.
        joined = '\n'.join(cm.output)
        self.assertIn('mlb_odds_persist_skip', joined)
        self.assertIn('reason=no_team_match', joined)
        self.assertIn('normalized_home=', joined)
        self.assertIn('normalized_away=', joined)
        self.assertEqual(result['skipped'], 1)
        self.assertEqual(result['created'], 0)

    def test_summary_log_emitted_at_end(self):
        with self.assertLogs('apps.datahub.providers.mlb.odds_provider', level='INFO') as cm:
            self.provider.persist([self._item()])
        joined = '\n'.join(cm.output)
        self.assertIn('mlb_odds_persist_summary', joined)
        self.assertIn('matchups_seen=', joined)
        self.assertIn('matchups_matched=', joined)
        self.assertIn('coverage_pct=', joined)

    def test_low_coverage_logs_at_error_level(self):
        # Build 5 matchups: 1 with valid teams, 4 with mystery teams.
        # That's 20% coverage — below the 50% threshold and >= 4-game
        # minimum, so the summary must be ERROR.
        items = [self._item()]
        for i in range(4):
            items.append(self._item(
                home_team=f'Mystery Home {i}',
                away_team=f'Mystery Away {i}',
            ))
        with self.assertLogs('apps.datahub.providers.mlb.odds_provider', level='ERROR') as cm:
            self.provider.persist(items)
        joined = '\n'.join(cm.output)
        self.assertIn('mlb_odds_persist_summary', joined)
        # The ERROR level itself is the signal — assertLogs with level=ERROR
        # only captures records at or above that level, so reaching here
        # already proves an ERROR record was emitted.

    def test_normal_coverage_does_not_log_error(self):
        # All matchups match → no ERROR record for the summary.
        items = [self._item() for _ in range(5)]
        with self.assertLogs('apps.datahub.providers.mlb.odds_provider', level='INFO') as cm:
            self.provider.persist(items)
        # Filter to ERROR-level lines about the summary.
        errors = [r for r in cm.records
                  if r.levelno >= _stdlib_logging.ERROR
                  and 'mlb_odds_persist_summary' in r.getMessage()]
        self.assertEqual(errors, [])

    def test_coverage_check_skipped_for_small_slates(self):
        # Only 2 matchups, both fail — below 50% coverage but under the
        # 4-matchup minimum so we don't false-alarm on early-season days.
        items = [
            self._item(home_team='Mystery A', away_team='Mystery B'),
            self._item(home_team='Mystery C', away_team='Mystery D'),
        ]
        with self.assertLogs('apps.datahub.providers.mlb.odds_provider', level='INFO') as cm:
            self.provider.persist(items)
        errors = [r for r in cm.records
                  if r.levelno >= _stdlib_logging.ERROR
                  and 'mlb_odds_persist_summary' in r.getMessage()]
        self.assertEqual(errors, [])

    def test_coverage_returned_in_result(self):
        items = [self._item() for _ in range(3)] + [
            self._item(home_team='Mystery Team', away_team='Other'),
        ]
        result = self.provider.persist(items)
        self.assertEqual(result['matchups_seen'], 2)  # 2 distinct matchups
        self.assertEqual(result['matchups_matched'], 1)
        self.assertAlmostEqual(result['coverage_pct'], 50.0, places=1)


class MlbDebugMatchingFlagTests(TestCase):
    """When DEBUG_ODDS_MATCHING is on, every API team name we see is
    echoed at INFO so we can mine real-world variants and grow the
    alias dict. When off, no extra noise."""

    def setUp(self):
        from apps.datahub.providers.mlb.odds_provider import MLBOddsProvider
        from apps.mlb.models import Conference, Game, Team
        self.provider = MLBOddsProvider.__new__(MLBOddsProvider)
        conf = Conference.objects.create(name='AL East', slug='al-east')
        self.home = Team.objects.create(
            name='New York Yankees', slug='new-york-yankees', conference=conf,
        )
        self.away = Team.objects.create(
            name='Boston Red Sox', slug='boston-red-sox', conference=conf,
        )
        self.game = Game.objects.create(
            home_team=self.home, away_team=self.away,
            first_pitch=timezone.now() + timedelta(hours=2),
        )

    def _item(self):
        return {
            'home_team': 'New York Yankees',
            'away_team': 'Boston Red Sox',
            'commence_time': self.game.first_pitch.isoformat().replace('+00:00', 'Z'),
            'sportsbook': 'DraftKings',
            'moneyline_home': -150, 'moneyline_away': 130,
            'spread': -1.5, 'total': 8.5,
        }

    def test_debug_flag_emits_per_item_log(self):
        with self.settings(DEBUG_ODDS_MATCHING=True):
            with self.assertLogs('apps.datahub.providers.mlb.odds_provider', level='INFO') as cm:
                self.provider.persist([self._item()])
        debug_lines = [r for r in cm.records
                       if 'mlb_odds_persist_debug' in r.getMessage()]
        self.assertEqual(len(debug_lines), 1)

    def test_debug_flag_off_emits_no_per_item_log(self):
        with self.settings(DEBUG_ODDS_MATCHING=False):
            with self.assertLogs('apps.datahub.providers.mlb.odds_provider', level='INFO') as cm:
                self.provider.persist([self._item()])
        debug_lines = [r for r in cm.records
                       if 'mlb_odds_persist_debug' in r.getMessage()]
        self.assertEqual(debug_lines, [])


class MlbFuzzyRecoveryEndToEndTests(TestCase):
    """When the alias dict misses but the team is in DB under a recognizable
    name, the fuzzy fallback inside _find_team must recover the match."""

    def setUp(self):
        from apps.datahub.providers.mlb.odds_provider import MLBOddsProvider
        from apps.mlb.models import Conference, Team
        self.provider = MLBOddsProvider.__new__(MLBOddsProvider)
        conf = Conference.objects.create(name='AL East', slug='al-east')
        # DB stores canonical full names — typical MLB Stats API output.
        Team.objects.create(name='New York Yankees', slug='new-york-yankees', conference=conf)
        Team.objects.create(name='Boston Red Sox', slug='boston-red-sox', conference=conf)

    def test_fuzzy_recovers_on_unusual_api_format(self):
        # Imagine the API sends an unfamiliar formatting our alias dict
        # didn't anticipate — fuzzy substring should still recover.
        from apps.datahub.providers.mlb.odds_provider import _find_team
        # 'New York Yankees Inc' is not in the alias dict; substring match
        # against canonical 'New York Yankees' should still find the team.
        team = _find_team('New York Yankees Inc')
        self.assertIsNotNone(team)
        self.assertEqual(team.name, 'New York Yankees')


# =========================================================================
# Per-game ESPN gap-fill — every game must end up with odds (API or ESPN)
# =========================================================================

import logging as _stdlib_logging_for_gap_tests


class _MlbGapFillFixture:
    """Shared MLB setup: 3 games, 6 teams, all in the upcoming window.

    Mixed-slate tests pre-seed primary OddsSnapshot rows for some games
    (simulating "primary covered them") and leave others empty
    (simulating "primary missed them"). The gap-fill flow should then
    fill ONLY the empty ones from the (mocked) ESPN scoreboard.
    """

    def make(self):
        from apps.mlb.models import Conference, Game, OddsSnapshot, Team
        self.OddsSnapshot = OddsSnapshot
        conf = Conference.objects.create(name='AL East', slug='al-east')

        # Build 3 distinct matchups with full canonical names so the
        # alias dict + DB iexact lookup succeed.
        teams = {}
        for n, slug in [
            ('New York Yankees', 'new-york-yankees'),
            ('Boston Red Sox', 'boston-red-sox'),
            ('Los Angeles Dodgers', 'los-angeles-dodgers'),
            ('San Francisco Giants', 'san-francisco-giants'),
            ('Chicago Cubs', 'chicago-cubs'),
            ('St. Louis Cardinals', 'st-louis-cardinals'),
        ]:
            teams[n] = Team.objects.create(name=n, slug=slug, conference=conf)
        self.teams = teams

        # All three games start in the next ~2-6 hours so they all sit
        # inside the gap-detector's 36h upcoming window.
        first_pitch = timezone.now() + timedelta(hours=3)
        self.game_a = Game.objects.create(
            home_team=teams['New York Yankees'], away_team=teams['Boston Red Sox'],
            first_pitch=first_pitch,
        )
        self.game_b = Game.objects.create(
            home_team=teams['Los Angeles Dodgers'], away_team=teams['San Francisco Giants'],
            first_pitch=first_pitch + timedelta(minutes=15),
        )
        self.game_c = Game.objects.create(
            home_team=teams['Chicago Cubs'], away_team=teams['St. Louis Cardinals'],
            first_pitch=first_pitch + timedelta(minutes=30),
        )
        return self


def _seed_primary_snapshot(game, sportsbook='DraftKings', minutes_ago=5):
    """Simulate a row left by the primary Odds API path."""
    from apps.mlb.models import OddsSnapshot
    return OddsSnapshot.objects.create(
        game=game,
        captured_at=timezone.now() - timedelta(minutes=minutes_ago),
        sportsbook=sportsbook,
        market_home_win_prob=0.55,
        moneyline_home=-150, moneyline_away=130,
        odds_source='odds_api', source_quality='primary',
    )


def _espn_payload(home, away, commence_dt, ml_home=-130, ml_away=110):
    """Minimal ESPN scoreboard event shaped like the real payload."""
    return {
        'date': commence_dt.isoformat().replace('+00:00', 'Z'),
        'competitions': [{
            'date': commence_dt.isoformat().replace('+00:00', 'Z'),
            'competitors': [
                {'homeAway': 'home', 'team': {'displayName': home}},
                {'homeAway': 'away', 'team': {'displayName': away}},
            ],
            'odds': [{
                'provider': {'name': 'DraftKings'},
                'spread': -1.5,
                'overUnder': 8.5,
                'homeTeamOdds': {'moneyLine': ml_home},
                'awayTeamOdds': {'moneyLine': ml_away},
            }],
        }],
    }


class MlbGapDetectionTests(TestCase):
    """The gap detector underlies the whole feature: it must correctly
    distinguish 'has fresh primary odds' from 'needs fallback'."""

    def setUp(self):
        self.fx = _MlbGapFillFixture().make()

    def test_no_snapshots_means_all_games_are_gaps(self):
        from apps.datahub.management.commands.ingest_odds import (
            _find_mlb_games_without_fresh_odds,
        )
        upcoming, gaps = _find_mlb_games_without_fresh_odds(180)
        self.assertEqual(len(upcoming), 3)
        self.assertEqual(set(gaps), {self.fx.game_a.pk, self.fx.game_b.pk, self.fx.game_c.pk})

    def test_fresh_snapshot_removes_game_from_gaps(self):
        from apps.datahub.management.commands.ingest_odds import (
            _find_mlb_games_without_fresh_odds,
        )
        _seed_primary_snapshot(self.fx.game_a)  # 5 minutes ago — fresh
        upcoming, gaps = _find_mlb_games_without_fresh_odds(180)
        self.assertEqual(len(upcoming), 3)
        self.assertEqual(set(gaps), {self.fx.game_b.pk, self.fx.game_c.pk})

    def test_stale_snapshot_does_not_count(self):
        from apps.datahub.management.commands.ingest_odds import (
            _find_mlb_games_without_fresh_odds,
        )
        # Snapshot from 4 hours ago — older than the 180-min freshness
        # window. Game must still appear in the gap list.
        _seed_primary_snapshot(self.fx.game_a, minutes_ago=240)
        _, gaps = _find_mlb_games_without_fresh_odds(180)
        self.assertIn(self.fx.game_a.pk, gaps)

    def test_far_future_games_excluded_from_window(self):
        # A game starting in 5 days is NOT in the upcoming window even
        # though it has no odds — gap-fill should ignore it (we don't
        # waste an ESPN call on games not playing soon).
        from apps.mlb.models import Game
        far_game = Game.objects.create(
            home_team=self.fx.teams['New York Yankees'],
            away_team=self.fx.teams['Los Angeles Dodgers'],
            first_pitch=timezone.now() + timedelta(days=5),
        )
        from apps.datahub.management.commands.ingest_odds import (
            _find_mlb_games_without_fresh_odds,
        )
        upcoming, gaps = _find_mlb_games_without_fresh_odds(180)
        self.assertNotIn(far_game.pk, upcoming)
        self.assertNotIn(far_game.pk, gaps)


class MlbEspnTargetFilterTests(TestCase):
    """When persist is given target_game_ids, it must skip games NOT in
    the set (so we don't double-write for games primary already covered)."""

    def setUp(self):
        self.fx = _MlbGapFillFixture().make()

    def test_filter_skips_non_target_games(self):
        from apps.datahub.providers.mlb.odds_espn_provider import MLBEspnOddsProvider
        provider = MLBEspnOddsProvider.__new__(MLBEspnOddsProvider)
        commence = self.fx.game_a.first_pitch
        normalized = [
            {
                'home_team': 'New York Yankees', 'away_team': 'Boston Red Sox',
                'commence_time': commence.isoformat().replace('+00:00', 'Z'),
                'sportsbook': 'DraftKings',
                'moneyline_home': -130, 'moneyline_away': 110,
                'spread': -1.5, 'total': 8.5,
            },
            {
                'home_team': 'Los Angeles Dodgers', 'away_team': 'San Francisco Giants',
                'commence_time': self.fx.game_b.first_pitch.isoformat().replace('+00:00', 'Z'),
                'sportsbook': 'DraftKings',
                'moneyline_home': -150, 'moneyline_away': 130,
                'spread': -1.5, 'total': 8.5,
            },
        ]
        # Only game_b is in the target set.
        result = provider.persist(normalized, target_game_ids={self.fx.game_b.pk})
        self.assertEqual(result['created'], 1)
        # game_a's row was skipped because it's not in target_game_ids.
        self.assertEqual(result['skip_reasons'].get('not_in_target_set'), 1)
        # The DB confirms — only game_b has a fresh ESPN row.
        from apps.mlb.models import OddsSnapshot
        self.assertEqual(
            OddsSnapshot.objects.filter(game=self.fx.game_a).count(), 0,
        )
        self.assertEqual(
            OddsSnapshot.objects.filter(game=self.fx.game_b).count(), 1,
        )

    def test_filter_none_preserves_original_whole_slate_behavior(self):
        # No filter → ESPN persists for every matched game (the original
        # whole-slate fallback contract).
        from apps.datahub.providers.mlb.odds_espn_provider import MLBEspnOddsProvider
        provider = MLBEspnOddsProvider.__new__(MLBEspnOddsProvider)
        normalized = [{
            'home_team': 'New York Yankees', 'away_team': 'Boston Red Sox',
            'commence_time': self.fx.game_a.first_pitch.isoformat().replace('+00:00', 'Z'),
            'sportsbook': 'DraftKings',
            'moneyline_home': -130, 'moneyline_away': 110,
            'spread': -1.5, 'total': 8.5,
        }]
        result = provider.persist(normalized)  # no filter
        self.assertEqual(result['created'], 1)


class MlbSourceMetadataTests(TestCase):
    """Snapshots must carry odds_source + source_quality so the UI and
    recommendation engine can tell primary apart from fallback."""

    def setUp(self):
        self.fx = _MlbGapFillFixture().make()

    def test_espn_snapshot_tagged_fallback(self):
        from apps.datahub.providers.mlb.odds_espn_provider import MLBEspnOddsProvider
        provider = MLBEspnOddsProvider.__new__(MLBEspnOddsProvider)
        normalized = [{
            'home_team': 'New York Yankees', 'away_team': 'Boston Red Sox',
            'commence_time': self.fx.game_a.first_pitch.isoformat().replace('+00:00', 'Z'),
            'sportsbook': 'DraftKings',
            'moneyline_home': -130, 'moneyline_away': 110,
            'spread': -1.5, 'total': 8.5,
        }]
        provider.persist(normalized)
        snap = self.fx.OddsSnapshot.objects.get(game=self.fx.game_a)
        self.assertEqual(snap.odds_source, 'espn')
        self.assertEqual(snap.source_quality, 'fallback')

    def test_primary_snapshot_tagged_primary(self):
        from apps.datahub.providers.mlb.odds_provider import MLBOddsProvider
        provider = MLBOddsProvider.__new__(MLBOddsProvider)
        normalized = [{
            'home_team': 'New York Yankees', 'away_team': 'Boston Red Sox',
            'commence_time': self.fx.game_a.first_pitch.isoformat().replace('+00:00', 'Z'),
            'sportsbook': 'DraftKings',
            'moneyline_home': -150, 'moneyline_away': 130,
            'spread': -1.5, 'total': 8.5,
        }]
        provider.persist(normalized)
        snap = self.fx.OddsSnapshot.objects.get(game=self.fx.game_a)
        self.assertEqual(snap.odds_source, 'odds_api')
        self.assertEqual(snap.source_quality, 'primary')


class MlbIngestOddsCommandGapFillTests(TestCase):
    """End-to-end: the management command runs primary, identifies gaps,
    runs ESPN with the gap filter, and emits the summary log."""

    def setUp(self):
        self.fx = _MlbGapFillFixture().make()

    def _run_command_with_mocks(self, primary_normalized, espn_events):
        """Run ingest_odds with both providers patched.

        primary_normalized: list of dicts the primary normalize() returns.
        espn_events: list of ESPN event dicts the espn fetch() returns.
        """
        from io import StringIO
        from django.core.management import call_command

        with patch(
            'apps.datahub.providers.mlb.odds_provider.MLBOddsProvider.fetch',
            return_value=primary_normalized,  # raw == normalized in stub
        ), patch(
            'apps.datahub.providers.mlb.odds_provider.MLBOddsProvider.normalize',
            side_effect=lambda raw: list(raw),
        ), patch(
            'apps.datahub.providers.mlb.odds_espn_provider.MLBEspnOddsProvider.fetch',
            return_value=espn_events,
        ), self.settings(
            LIVE_DATA_ENABLED=True,
            LIVE_MLB_ENABLED=True,
            ODDS_API_KEY='dummy-key-for-test',
        ):
            out = StringIO()
            call_command('ingest_odds', sport='mlb', stdout=out)
            return out.getvalue()

    def test_mixed_slate_espn_fills_only_gaps(self):
        # Primary returns odds for game_a only.
        primary = [{
            'home_team': 'New York Yankees', 'away_team': 'Boston Red Sox',
            'commence_time': self.fx.game_a.first_pitch.isoformat().replace('+00:00', 'Z'),
            'sportsbook': 'DraftKings',
            'moneyline_home': -150, 'moneyline_away': 130,
            'spread': -1.5, 'total': 8.5,
        }]
        # ESPN scoreboard returns ALL three games; gap-fill must restrict
        # the persist to b and c (a is not a gap because primary covered).
        espn_events = [
            _espn_payload('New York Yankees', 'Boston Red Sox', self.fx.game_a.first_pitch),
            _espn_payload('Los Angeles Dodgers', 'San Francisco Giants', self.fx.game_b.first_pitch),
            _espn_payload('Chicago Cubs', 'St. Louis Cardinals', self.fx.game_c.first_pitch),
        ]
        self._run_command_with_mocks(primary, espn_events)

        # game_a → 1 row, source=odds_api
        a_snaps = self.fx.OddsSnapshot.objects.filter(game=self.fx.game_a)
        self.assertEqual(a_snaps.count(), 1)
        self.assertEqual(a_snaps.first().odds_source, 'odds_api')
        self.assertEqual(a_snaps.first().source_quality, 'primary')

        # games b, c → 1 row each, source=espn
        for g in (self.fx.game_b, self.fx.game_c):
            snaps = self.fx.OddsSnapshot.objects.filter(game=g)
            self.assertEqual(snaps.count(), 1, msg=f'expected 1 row for {g}')
            self.assertEqual(snaps.first().odds_source, 'espn')
            self.assertEqual(snaps.first().source_quality, 'fallback')

    def test_all_primary_skips_espn_call(self):
        # Primary covers all 3 games; ESPN should NOT be called at all.
        primary = []
        for g in (self.fx.game_a, self.fx.game_b, self.fx.game_c):
            primary.append({
                'home_team': g.home_team.name, 'away_team': g.away_team.name,
                'commence_time': g.first_pitch.isoformat().replace('+00:00', 'Z'),
                'sportsbook': 'DraftKings',
                'moneyline_home': -150, 'moneyline_away': 130,
                'spread': -1.5, 'total': 8.5,
            })
        with patch(
            'apps.datahub.providers.mlb.odds_espn_provider.MLBEspnOddsProvider.fetch'
        ) as mock_espn_fetch:
            self._run_command_with_mocks(primary, [])
            mock_espn_fetch.assert_not_called()
        # Each game has exactly 1 primary snapshot.
        for g in (self.fx.game_a, self.fx.game_b, self.fx.game_c):
            snaps = self.fx.OddsSnapshot.objects.filter(game=g)
            self.assertEqual(snaps.count(), 1)
            self.assertEqual(snaps.first().odds_source, 'odds_api')

    def test_primary_zero_espn_fills_all(self):
        # Primary returns nothing — ALL games are gaps — ESPN fills all.
        # This is the "old whole-slate fallback" scenario; the new
        # per-game logic must preserve it.
        espn_events = [
            _espn_payload('New York Yankees', 'Boston Red Sox', self.fx.game_a.first_pitch),
            _espn_payload('Los Angeles Dodgers', 'San Francisco Giants', self.fx.game_b.first_pitch),
            _espn_payload('Chicago Cubs', 'St. Louis Cardinals', self.fx.game_c.first_pitch),
        ]
        self._run_command_with_mocks([], espn_events)
        for g in (self.fx.game_a, self.fx.game_b, self.fx.game_c):
            snaps = self.fx.OddsSnapshot.objects.filter(game=g)
            self.assertEqual(snaps.count(), 1, msg=f'expected ESPN fill for {g}')
            self.assertEqual(snaps.first().odds_source, 'espn')
            self.assertEqual(snaps.first().source_quality, 'fallback')

    def test_summary_log_emitted(self):
        # The required debug summary line: total / api / espn / missing.
        primary = [{
            'home_team': 'New York Yankees', 'away_team': 'Boston Red Sox',
            'commence_time': self.fx.game_a.first_pitch.isoformat().replace('+00:00', 'Z'),
            'sportsbook': 'DraftKings',
            'moneyline_home': -150, 'moneyline_away': 130,
            'spread': -1.5, 'total': 8.5,
        }]
        espn_events = [
            _espn_payload('Los Angeles Dodgers', 'San Francisco Giants', self.fx.game_b.first_pitch),
            _espn_payload('Chicago Cubs', 'St. Louis Cardinals', self.fx.game_c.first_pitch),
        ]
        with self.assertLogs('apps.datahub.management.commands.ingest_odds', level='INFO') as cm:
            self._run_command_with_mocks(primary, espn_events)
        joined = '\n'.join(cm.output)
        self.assertIn('mlb_odds_ingest_summary', joined)
        self.assertIn('upcoming_games=3', joined)
        self.assertIn('api_filled=1', joined)
        self.assertIn('espn_filled=2', joined)
        self.assertIn('still_missing=0', joined)

    def test_summary_logs_error_when_missing(self):
        # A game that ESPN can't fill (mismatched team names) must end up
        # in still_missing — and the summary line must be at ERROR level
        # so it surfaces in the Ops dashboard.
        primary = []
        espn_events = [
            _espn_payload('New York Yankees', 'Boston Red Sox', self.fx.game_a.first_pitch),
            # game_b and game_c have no ESPN entry → still_missing = 2
        ]
        with self.assertLogs('apps.datahub.management.commands.ingest_odds', level='ERROR') as cm:
            self._run_command_with_mocks(primary, espn_events)
        # Reaching here proves an ERROR record was emitted.
        joined = '\n'.join(cm.output)
        self.assertIn('mlb_odds_ingest_summary', joined)
        self.assertIn('still_missing=', joined)
