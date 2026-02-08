"""Golf Odds Provider — fetches outright winner odds from The Odds API."""

import logging

from django.conf import settings
from django.utils import timezone

from apps.golf.models import GolfEvent, Golfer, GolfOddsSnapshot
from apps.datahub.providers.base import AbstractProvider
from apps.datahub.providers.client import APIClient
from apps.datahub.providers.name_utils import normalize_golfer_name

logger = logging.getLogger(__name__)

ODDS_API_BASE = 'https://api.the-odds-api.com'
GOLF_SPORT_KEY = 'golf_pga_tour'


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
        """Fetch PGA Tour outright odds from The Odds API."""
        return self.client.get(
            f'/v4/sports/{GOLF_SPORT_KEY}/odds/',
            params={
                'apiKey': self.api_key,
                'regions': 'us',
                'markets': 'outrights',
                'oddsFormat': 'american',
            },
        )

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
        """Create GolfOddsSnapshot records (append-only)."""
        created = 0
        skipped = 0
        now = timezone.now()

        # Cache current/upcoming events
        upcoming_events = {e.name.lower(): e for e in GolfEvent.objects.filter(
            end_date__gte=now.date(),
        )}

        for item in normalized:
            # Match event by name (fuzzy)
            event = _match_event(item['event_name'], upcoming_events)
            if not event:
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
