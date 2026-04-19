"""MLB Schedule Provider — fetches games + probable pitchers from statsapi.mlb.com.

MLB Stats API is free, requires no key, and exposes a stable schedule
endpoint. We hydrate `probablePitcher` in a single call so we get both
game rows and pitcher name/ID without an extra round-trip.

Pitcher season STATS are handled by MLBPitcherStatsProvider on its own
cadence — the schedule provider only writes pitcher name + external_id
so that game -> pitcher FKs can be set immediately.

Idempotency: games and pitchers and teams are keyed by (source, external_id).
"""
import logging
from datetime import timedelta

from django.conf import settings
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.utils.text import slugify

from apps.datahub.providers.base import AbstractProvider
from apps.datahub.providers.client import APIClient
from apps.datahub.team_colors import get_team_color
from apps.mlb.models import Conference, Game, StartingPitcher, Team

logger = logging.getLogger(__name__)

SOURCE = 'mlb_stats_api'

# MLB Stats API detailedState -> our status
STATUS_MAP = {
    'Scheduled': 'scheduled',
    'Pre-Game': 'scheduled',
    'Warmup': 'scheduled',
    'Delayed Start': 'scheduled',
    'In Progress': 'live',
    'Delayed': 'live',
    'Manager challenge': 'live',
    'Final': 'final',
    'Game Over': 'final',
    'Completed Early': 'final',
    'Postponed': 'postponed',
    'Cancelled': 'cancelled',
    'Suspended': 'postponed',
}


def _status_from_detailed(detailed_state):
    return STATUS_MAP.get(detailed_state, 'scheduled')


def _upsert_team(team_data):
    """Upsert a Team row from MLB Stats API team payload. Returns Team."""
    ext_id = str(team_data.get('id', ''))
    name = team_data.get('name') or team_data.get('teamName') or ''
    abbr = team_data.get('abbreviation', '') or ''
    if not ext_id or not name:
        return None

    # Conference = MLB division (e.g. "American League East"). Nested shape varies.
    division = team_data.get('division') or {}
    division_name = division.get('name') or 'MLB'
    conference, _ = Conference.objects.get_or_create(
        slug=slugify(division_name),
        defaults={'name': division_name},
    )

    slug = slugify(name)
    team, created = Team.objects.update_or_create(
        source=SOURCE, external_id=ext_id,
        defaults={
            'name': name,
            'slug': slug,
            'conference': conference,
            'abbreviation': abbr,
            'primary_color': get_team_color(slug, 'mlb') or '',
        },
    )
    return team


def _upsert_pitcher(pitcher_data, team):
    """Upsert a StartingPitcher from a hydrated probablePitcher dict."""
    if not pitcher_data or not team:
        return None
    ext_id = str(pitcher_data.get('id', ''))
    name = pitcher_data.get('fullName') or ''
    if not ext_id or not name:
        return None

    pitcher, _ = StartingPitcher.objects.update_or_create(
        source=SOURCE, external_id=ext_id,
        defaults={
            'name': name,
            'team': team,
        },
    )
    return pitcher


class MLBScheduleProvider(AbstractProvider):
    sport = 'mlb'
    data_type = 'schedule'

    def __init__(self):
        self.client = APIClient(
            base_url=settings.MLB_STATSAPI_BASE_URL,
            rate_limit_delay=0.3,
        )

    def fetch(self):
        now = timezone.now()
        start = (now - timedelta(days=1)).strftime('%Y-%m-%d')
        end = (now + timedelta(days=7)).strftime('%Y-%m-%d')
        try:
            data = self.client.get(
                '/v1/schedule',
                params={
                    'sportId': 1,
                    'startDate': start,
                    'endDate': end,
                    'hydrate': 'probablePitcher,team,linescore',
                },
            )
        except Exception as e:
            logger.error(f"MLB schedule fetch failed: {e}")
            return []
        dates = data.get('dates', []) or []
        games = []
        for d in dates:
            games.extend(d.get('games', []) or [])
        return games

    def normalize(self, raw):
        records = []
        for g in raw:
            teams = g.get('teams') or {}
            home = teams.get('home') or {}
            away = teams.get('away') or {}
            home_team = home.get('team') or {}
            away_team = away.get('team') or {}
            if not home_team.get('id') or not away_team.get('id'):
                continue
            if not g.get('gameDate'):
                continue

            record = {
                'external_id': str(g.get('gamePk') or ''),
                'game_date': g.get('gameDate'),
                'detailed_state': (g.get('status') or {}).get('detailedState', ''),
                'home_team': home_team,
                'away_team': away_team,
                'home_score': home.get('score'),
                'away_score': away.get('score'),
                'home_pitcher': home.get('probablePitcher') or {},
                'away_pitcher': away.get('probablePitcher') or {},
                'venue': g.get('venue') or {},
            }
            records.append(record)
        return records

    def persist(self, normalized):
        created = 0
        updated = 0
        skipped = 0
        now = timezone.now()

        for item in normalized:
            home = _upsert_team(item['home_team'])
            away = _upsert_team(item['away_team'])
            if not home or not away:
                skipped += 1
                continue

            first_pitch = parse_datetime(item['game_date'])
            if first_pitch is None:
                skipped += 1
                continue
            if timezone.is_naive(first_pitch):
                first_pitch = timezone.make_aware(first_pitch)

            status = _status_from_detailed(item['detailed_state'])
            home_pitcher = _upsert_pitcher(item.get('home_pitcher'), home)
            away_pitcher = _upsert_pitcher(item.get('away_pitcher'), away)

            defaults = {
                'home_team': home,
                'away_team': away,
                'first_pitch': first_pitch,
                'status': status,
                'home_score': item.get('home_score'),
                'away_score': item.get('away_score'),
                'home_pitcher': home_pitcher,
                'away_pitcher': away_pitcher,
                'pitchers_updated_at': now if (home_pitcher or away_pitcher) else None,
            }

            game, was_created = Game.objects.update_or_create(
                source=SOURCE,
                external_id=item['external_id'],
                defaults=defaults,
            )
            if was_created:
                created += 1
            else:
                updated += 1

        return {'status': 'ok', 'created': created, 'updated': updated, 'skipped': skipped}
