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
