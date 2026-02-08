"""CFB Schedule Provider — fetches games from the CFBD API."""

import logging
from datetime import timedelta

from django.conf import settings
from django.utils import timezone
from django.utils.text import slugify

from apps.cfb.models import Conference, Team, Game
from apps.datahub.providers.base import AbstractProvider
from apps.datahub.providers.client import APIClient
from apps.datahub.providers.name_utils import normalize_team_name
from apps.datahub.team_colors import get_team_color

logger = logging.getLogger(__name__)

CFBD_BASE_URL = 'https://api.collegefootballdata.com'


class CFBScheduleProvider(AbstractProvider):
    sport = 'cfb'
    data_type = 'schedule'

    def __init__(self):
        api_key = settings.CFBD_API_KEY
        if not api_key:
            raise ValueError("CFBD_API_KEY not configured")
        self.client = APIClient(
            base_url=CFBD_BASE_URL,
            headers={
                'Authorization': f'Bearer {api_key}',
                'Accept': 'application/json',
            },
            rate_limit_delay=1.0,
        )

    def fetch(self):
        """Fetch current season games from CFBD API."""
        now = timezone.now()
        year = now.year
        return self.client.get('/games', params={
            'year': year,
            'division': 'fbs',
        })

    def normalize(self, raw):
        """Transform CFBD game records into standardized dicts."""
        normalized = []
        for g in raw:
            home_team = g.get('home_team', '')
            away_team = g.get('away_team', '')
            if not home_team or not away_team:
                continue

            home_conf = g.get('home_conference', '')
            away_conf = g.get('away_conference', '')
            start_date = g.get('start_date', '')
            if not start_date:
                continue

            home_points = g.get('home_points')
            away_points = g.get('away_points')
            if g.get('completed', False):
                status = 'final'
            elif home_points is not None and away_points is not None:
                status = 'final'
            else:
                status = 'scheduled'

            normalized.append({
                'home_team': normalize_team_name(home_team),
                'away_team': normalize_team_name(away_team),
                'home_conference': home_conf or '',
                'away_conference': away_conf or '',
                'start_date': start_date,
                'status': status,
                'neutral_site': bool(g.get('neutral_site', False)),
                'home_score': home_points,
                'away_score': away_points,
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
                team_slug = slugify(team_name)
                color = get_team_color(team_slug, 'cfb')
                defaults = {'name': team_name, 'conference': conf}
                if color:
                    defaults['primary_color'] = color
                team_obj, team_created = Team.objects.get_or_create(
                    slug=team_slug, defaults=defaults,
                )
                if not team_created and not team_obj.primary_color and color:
                    team_obj.primary_color = color
                    team_obj.save(update_fields=['primary_color'])

            # Look up teams
            home = Team.objects.filter(slug=slugify(item['home_team'])).first()
            away = Team.objects.filter(slug=slugify(item['away_team'])).first()
            if not home or not away:
                logger.warning(
                    f"Could not resolve teams: {item['home_team']} vs {item['away_team']}"
                )
                continue

            # Parse kickoff time
            from django.utils.dateparse import parse_datetime
            kickoff = parse_datetime(item['start_date'])
            if kickoff is None:
                continue
            if timezone.is_naive(kickoff):
                kickoff = timezone.make_aware(kickoff)

            # Match existing game: same teams, within ±1 day
            existing = Game.objects.filter(
                home_team=home,
                away_team=away,
                kickoff__date__gte=(kickoff - timedelta(days=1)).date(),
                kickoff__date__lte=(kickoff + timedelta(days=1)).date(),
            ).first()

            if existing:
                changed = False
                if existing.kickoff != kickoff:
                    existing.kickoff = kickoff
                    changed = True
                if existing.status != item['status']:
                    existing.status = item['status']
                    changed = True
                if item.get('home_score') is not None and existing.home_score != item['home_score']:
                    existing.home_score = item['home_score']
                    changed = True
                if item.get('away_score') is not None and existing.away_score != item['away_score']:
                    existing.away_score = item['away_score']
                    changed = True
                if changed:
                    existing.save()
                    updated += 1
            else:
                Game.objects.create(
                    home_team=home,
                    away_team=away,
                    kickoff=kickoff,
                    neutral_site=item['neutral_site'],
                    status=item['status'],
                    home_score=item.get('home_score'),
                    away_score=item.get('away_score'),
                )
                created += 1

        return {'status': 'ok', 'created': created, 'updated': updated}
