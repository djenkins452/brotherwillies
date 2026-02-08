"""Golf Schedule Provider — fetches events and field from ESPN Public API."""

import logging
from datetime import datetime

from django.utils.text import slugify

from apps.golf.models import GolfEvent, Golfer
from apps.datahub.providers.base import AbstractProvider
from apps.datahub.providers.client import APIClient
from apps.datahub.providers.name_utils import normalize_golfer_name

logger = logging.getLogger(__name__)

ESPN_BASE = 'https://site.api.espn.com'


class GolfScheduleProvider(AbstractProvider):
    sport = 'golf'
    data_type = 'schedule'

    def __init__(self):
        self.client = APIClient(
            base_url=ESPN_BASE,
            rate_limit_delay=1.0,
        )

    def fetch(self):
        """Fetch PGA Tour scoreboard from ESPN."""
        return self.client.get(
            '/apis/site/v2/sports/golf/pga/scoreboard',
        )

    def normalize(self, raw):
        """Transform ESPN golf data into standardized dicts."""
        normalized = []
        events = raw.get('events', [])

        for event in events:
            event_id = event.get('id', '')
            event_name = event.get('name', '')
            if not event_name:
                continue

            # Parse dates
            start_str = event.get('date', '')
            end_str = event.get('endDate', start_str)

            start_date = _parse_date(start_str)
            end_date = _parse_date(end_str)
            if not start_date:
                continue
            if not end_date:
                end_date = start_date

            # Extract competitors (golfers in the field)
            golfers = []
            for competition in event.get('competitions', []):
                for competitor in competition.get('competitors', []):
                    athlete = competitor.get('athlete', {})
                    golfer_name = athlete.get('displayName', '')
                    golfer_id = athlete.get('id', '')
                    if golfer_name:
                        golfers.append({
                            'name': normalize_golfer_name(golfer_name),
                            'external_id': str(golfer_id),
                        })

            normalized.append({
                'external_id': str(event_id),
                'name': event_name,
                'start_date': start_date,
                'end_date': end_date,
                'golfers': golfers,
            })

        return normalized

    def persist(self, normalized):
        """Upsert golf events and golfers."""
        events_created = 0
        events_updated = 0
        golfers_created = 0

        for item in normalized:
            # Upsert event
            slug = slugify(item['name'])
            event, was_created = GolfEvent.objects.update_or_create(
                external_id=item['external_id'],
                defaults={
                    'name': item['name'],
                    'slug': slug,
                    'start_date': item['start_date'],
                    'end_date': item['end_date'],
                },
            )
            if was_created:
                events_created += 1
            else:
                events_updated += 1

            # Upsert golfers
            for g in item.get('golfers', []):
                _, g_created = Golfer.objects.get_or_create(
                    external_id=g['external_id'],
                    defaults={'name': g['name']},
                )
                if g_created:
                    golfers_created += 1

        return {
            'status': 'ok',
            'events_created': events_created,
            'events_updated': events_updated,
            'golfers_created': golfers_created,
        }


def _parse_date(date_str):
    """Parse an ISO date string to a date object."""
    if not date_str:
        return None
    try:
        return datetime.fromisoformat(date_str.replace('Z', '+00:00')).date()
    except (ValueError, TypeError):
        return None
