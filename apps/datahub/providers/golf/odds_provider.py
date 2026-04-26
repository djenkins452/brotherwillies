"""Golf Odds Provider — fetches outright winner odds from The Odds API."""

import logging
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from apps.golf.models import GolfEvent, Golfer, GolfOddsSnapshot
from apps.datahub.providers.base import AbstractProvider
from apps.datahub.providers.client import APIClient
from apps.datahub.providers.name_utils import normalize_golfer_name

logger = logging.getLogger(__name__)

ODDS_API_BASE = 'https://api.the-odds-api.com'

# The Odds API uses per-tournament keys for golf (not a single PGA Tour key)
GOLF_SPORT_KEYS = [
    'golf_masters_tournament_winner',
    'golf_pga_championship_winner',
    'golf_the_open_championship_winner',
    'golf_us_open_winner',
]

# Fetch window: start 7 days before event start_date, end at event end_date
FETCH_WINDOW_DAYS_BEFORE = 7


def is_event_in_window(event, today=None):
    """Return (bool, reason) — whether odds should be fetched/persisted for this event today."""
    if today is None:
        today = timezone.now().date()

    fetch_start = event.start_date - timedelta(days=FETCH_WINDOW_DAYS_BEFORE)
    fetch_end = event.end_date

    if today < fetch_start:
        return False, "outside_window"
    if today > fetch_end:
        return False, "event_complete"
    return True, "in_window"


def american_to_prob(ml):
    """Convert American moneyline to implied probability."""
    if ml is None:
        return None
    if ml > 0:
        return 100.0 / (ml + 100.0)
    else:
        return abs(ml) / (abs(ml) + 100.0)


class GolfOddsProvider(AbstractProvider):
    sport = 'golf'
    data_type = 'odds'

    def __init__(self):
        api_key = settings.ODDS_API_KEY
        if not api_key:
            raise ValueError("ODDS_API_KEY not configured")
        self.api_key = api_key
        self.client = APIClient(
            base_url=ODDS_API_BASE,
            rate_limit_delay=1.0,
        )

    def fetch(self):
        """Fetch golf outright odds from all active golf markets.

        Gates the API calls to the fetch window: odds are fetched only if at
        least one GolfEvent is currently within [start_date - 7d, end_date].
        """
        today = timezone.now().date()

        # Candidate events: anything not already well past its end date.
        # is_event_in_window does the authoritative filtering below.
        candidate_events = GolfEvent.objects.filter(
            end_date__gte=today - timedelta(days=FETCH_WINDOW_DAYS_BEFORE),
        )

        in_window_events = [
            e for e in candidate_events if is_event_in_window(e, today)[0]
        ]

        if not in_window_events:
            logger.info(
                "golf_odds_fetch_skipped_no_events date=%s candidates=%d",
                today, candidate_events.count(),
            )
            return []

        logger.info(
            "golf_odds_fetch_started date=%s events_in_window=%s",
            today,
            [e.name for e in in_window_events],
        )

        all_events = []
        for sport_key in GOLF_SPORT_KEYS:
            try:
                events = self.client.get(
                    f'/v4/sports/{sport_key}/odds/',
                    params={
                        'apiKey': self.api_key,
                        'regions': 'us',
                        'markets': 'outrights',
                        'oddsFormat': 'american',
                    },
                )
                if events:
                    all_events.extend(events)
            except Exception as e:
                logger.info(f"No active odds for {sport_key}: {e}")
                continue

        logger.info(
            "golf_odds_fetch_completed date=%s raw_events=%d",
            today, len(all_events),
        )
        return all_events

    def normalize(self, raw):
        """Transform Odds API golf response into standardized dicts."""
        normalized = []
        for event in raw:
            event_name = event.get('description', '') or event.get('home_team', '')

            for bookmaker in event.get('bookmakers', []):
                sportsbook = bookmaker.get('title', 'unknown')

                for market in bookmaker.get('markets', []):
                    if market.get('key') != 'outrights':
                        continue

                    for outcome in market.get('outcomes', []):
                        golfer_name = normalize_golfer_name(outcome.get('name', ''))
                        odds = outcome.get('price')
                        if not golfer_name or odds is None:
                            continue

                        prob = american_to_prob(odds)

                        normalized.append({
                            'event_name': event_name,
                            'golfer_name': golfer_name,
                            'sportsbook': sportsbook,
                            'outright_odds': odds,
                            'implied_prob': prob,
                        })

        return normalized

    def persist(self, normalized):
        """Create GolfOddsSnapshot records (append-only).

        Enforces the fetch window at persist time for data integrity, and a
        once-per-day guard per event to avoid duplicate daily inserts when the
        scheduler runs more than once in a day.
        """
        created = 0
        skipped = 0
        now = timezone.now()
        today = now.date()

        # Cache current/upcoming events
        upcoming_events = {e.name.lower(): e for e in GolfEvent.objects.filter(
            end_date__gte=today,
        )}

        # Cache of events that have already been persisted today (prevents
        # duplicate daily inserts across repeated scheduler runs).
        already_fetched_today = set()

        for item in normalized:
            # Match event by name (fuzzy)
            event = _match_event(item['event_name'], upcoming_events)
            if not event:
                skipped += 1
                continue

            # Window gate (data safety — mirrors fetch-level gating)
            in_window, reason = is_event_in_window(event, today)
            if not in_window:
                logger.info(
                    "golf_odds_persist_skipped_window event=%s date=%s reason=%s",
                    event.name, today, reason,
                )
                skipped += 1
                continue

            # Once-per-day guard (no schema change — use captured_at date)
            if event.id not in already_fetched_today:
                exists_today = GolfOddsSnapshot.objects.filter(
                    event=event,
                    captured_at__date=today,
                ).exists()
                if exists_today:
                    already_fetched_today.add(event.id)

            if event.id in already_fetched_today:
                logger.info(
                    "golf_odds_persist_skipped_duplicate event=%s date=%s",
                    event.name, today,
                )
                skipped += 1
                continue

            # Match or create golfer
            golfer, _ = Golfer.objects.get_or_create(
                name=item['golfer_name'],
            )

            GolfOddsSnapshot.objects.create(
                event=event,
                golfer=golfer,
                captured_at=now,
                sportsbook=item['sportsbook'],
                outright_odds=item['outright_odds'],
                implied_prob=item['implied_prob'] or 0.0,
            )
            # NOTE: Movement intelligence is intentionally NOT wired for golf.
            # Two structural reasons:
            #   1. Schema differs — GolfOddsSnapshot has outright_odds +
            #      implied_prob (per golfer), no moneyline_home/away/
            #      spread/total. The current is_significant() and
            #      compute_movement_score() helpers in
            #      apps.core.services.odds_movement key off the latter.
            #   2. The provider above already dedupes to "one row per event
            #      per day," so even with a working detector there'd be
            #      almost no within-day signal to read. Loosening dedup
            #      AND adding a golf-flavored significance/score path is a
            #      larger change than this commit's scope.
            # When we extend movement to golf, the right move is to add
            # is_significant_outright() + compute_outright_movement_score()
            # in odds_movement.py and call apply_movement_intelligence on
            # an outright-aware code path, plus revisit the dedup gate.
            created += 1

        return {'status': 'ok', 'created': created, 'skipped': skipped}


def _match_event(api_name, events_by_name):
    """Try to match an API event name to our DB events."""
    if not api_name:
        return None
    key = api_name.lower().strip()
    if key in events_by_name:
        return events_by_name[key]
    # Try partial match
    for db_name, event in events_by_name.items():
        if key in db_name or db_name in key:
            return event
    return None
