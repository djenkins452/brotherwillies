"""CBB Schedule Provider — fetches games from ESPN + CBBD APIs.

ESPN provides current/upcoming games (reliable, no auth needed).
CBBD provides season history (3000-game cap, oldest first).
"""

import logging
from datetime import timedelta

from django.conf import settings
from django.utils import timezone
from django.utils.text import slugify

from apps.cbb.models import Conference, Team, Game
from apps.datahub.providers.base import AbstractProvider
from apps.datahub.providers.client import APIClient
from apps.datahub.providers.name_utils import normalize_team_name

logger = logging.getLogger(__name__)

ESPN_BASE = 'https://site.api.espn.com'
CBBD_BASE_URL = 'https://api.collegebasketballdata.com'


class CBBScheduleProvider(AbstractProvider):
    sport = 'cbb'
    data_type = 'schedule'

    def __init__(self):
        self.espn_client = APIClient(
            base_url=ESPN_BASE,
            rate_limit_delay=0.5,
        )
        # CBBD client (optional, for historical data)
        api_key = settings.CBBD_API_KEY
        self.cbbd_client = None
        if api_key:
            self.cbbd_client = APIClient(
                base_url=CBBD_BASE_URL,
                headers={
                    'Authorization': f'Bearer {api_key}',
                    'Accept': 'application/json',
                },
                rate_limit_delay=1.0,
            )

    def fetch(self):
        """Fetch games from ESPN scoreboard (today + upcoming days)."""
        now = timezone.now()
        all_games = []

        # Fetch yesterday (catches late-night games) + today + next 7 days
        for offset in range(-1, 8):
            date = now + timedelta(days=offset)
            date_str = date.strftime('%Y%m%d')
            try:
                data = self.espn_client.get(
                    '/apis/site/v2/sports/basketball/mens-college-basketball/scoreboard',
                    params={'dates': date_str, 'groups': '50', 'limit': 200},
                )
                events = data.get('events', [])
                all_games.extend(events)
                logger.info(f"ESPN CBB {date_str}: {len(events)} events")
            except Exception as e:
                logger.warning(f"ESPN CBB {date_str} failed: {e}")

        return all_games

    def normalize(self, raw):
        """Transform ESPN event records into standardized dicts."""
        normalized = []
        for event in raw:
            competitions = event.get('competitions', [])
            if not competitions:
                continue
            comp = competitions[0]

            home_team = ''
            away_team = ''
            home_conf = ''
            away_conf = ''
            for competitor in comp.get('competitors', []):
                team_data = competitor.get('team', {})
                name = team_data.get('displayName', '') or team_data.get('name', '')
                name = normalize_team_name(name)
                conf = ''
                # ESPN sometimes nests conference in groups
                groups = team_data.get('groups', {})
                if isinstance(groups, dict):
                    conf = groups.get('shortName', '') or groups.get('name', '')

                if competitor.get('homeAway') == 'home':
                    home_team = name
                    home_conf = conf
                else:
                    away_team = name
                    away_conf = conf

            if not home_team or not away_team:
                continue

            start_date = event.get('date', '')
            if not start_date:
                continue

            status_data = event.get('status', {}).get('type', {})
            espn_state = status_data.get('state', '')
            if espn_state == 'post':
                status = 'final'
            elif espn_state == 'in':
                status = 'live'
            else:
                status = 'scheduled'

            neutral_site = comp.get('neutralSite', False)

            normalized.append({
                'home_team': home_team,
                'away_team': away_team,
                'home_conference': home_conf,
                'away_conference': away_conf,
                'start_date': start_date,
                'status': status,
                'neutral_site': bool(neutral_site),
            })

        return normalized

    def persist(self, normalized):
        """Upsert conferences, teams, and games."""
        created = 0
        updated = 0

        for item in normalized:
            # Upsert conferences
            for conf_name in [item['home_conference'], item['away_conference']]:
                if conf_name:
                    Conference.objects.get_or_create(
                        slug=slugify(conf_name),
                        defaults={'name': conf_name},
                    )

            # Upsert teams
            for team_name, conf_name in [
                (item['home_team'], item['home_conference']),
                (item['away_team'], item['away_conference']),
            ]:
                conf = None
                if conf_name:
                    conf = Conference.objects.filter(slug=slugify(conf_name)).first()
                if not conf:
                    conf, _ = Conference.objects.get_or_create(
                        slug='independent',
                        defaults={'name': 'Independent'},
                    )
                Team.objects.get_or_create(
                    slug=slugify(team_name),
                    defaults={'name': team_name, 'conference': conf},
                )

            # Look up teams
            home = Team.objects.filter(slug=slugify(item['home_team'])).first()
            away = Team.objects.filter(slug=slugify(item['away_team'])).first()
            if not home or not away:
                logger.warning(
                    f"Could not resolve teams: {item['home_team']} vs {item['away_team']}"
                )
                continue

            # Parse tipoff time
            from django.utils.dateparse import parse_datetime
            tipoff = parse_datetime(item['start_date'])
            if tipoff is None:
                continue
            if timezone.is_naive(tipoff):
                tipoff = timezone.make_aware(tipoff)

            # Match existing game: same teams, within ±1 day
            existing = Game.objects.filter(
                home_team=home,
                away_team=away,
                tipoff__date__gte=(tipoff - timedelta(days=1)).date(),
                tipoff__date__lte=(tipoff + timedelta(days=1)).date(),
            ).first()

            if existing:
                changed = False
                if existing.tipoff != tipoff:
                    existing.tipoff = tipoff
                    changed = True
                if existing.status != item['status']:
                    existing.status = item['status']
                    changed = True
                if changed:
                    existing.save()
                    updated += 1
            else:
                Game.objects.create(
                    home_team=home,
                    away_team=away,
                    tipoff=tipoff,
                    neutral_site=item['neutral_site'],
                    status=item['status'],
                )
                created += 1

        return {'status': 'ok', 'created': created, 'updated': updated}
