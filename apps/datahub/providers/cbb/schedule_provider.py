"""CBB Schedule Provider — fetches games from the CBBD API."""

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

CBBD_BASE_URL = 'https://api.collegebasketballdata.com'


class CBBScheduleProvider(AbstractProvider):
    sport = 'cbb'
    data_type = 'schedule'

    def __init__(self):
        api_key = settings.CBBD_API_KEY
        if not api_key:
            raise ValueError("CBBD_API_KEY not configured")
        self.client = APIClient(
            base_url=CBBD_BASE_URL,
            headers={
                'Authorization': f'Bearer {api_key}',
                'Accept': 'application/json',
            },
            rate_limit_delay=1.0,
        )

    def fetch(self):
        """Fetch current season games from CBBD API."""
        now = timezone.now()
        # CBB season spans two calendar years; use the later year
        year = now.year if now.month >= 10 else now.year
        season = year

        games = self.client.get('/games', params={
            'year': season,
            'division': 'D1',
        })
        return games

    def normalize(self, raw):
        """Transform CBBD game records into standardized dicts."""
        normalized = []
        for g in raw:
            home_team = g.get('homeTeam') or g.get('home_team', '')
            away_team = g.get('awayTeam') or g.get('away_team', '')
            if not home_team or not away_team:
                continue

            home_conf = g.get('homeConference') or g.get('home_conference', '')
            away_conf = g.get('awayConference') or g.get('away_conference', '')

            start_date = g.get('startDate') or g.get('start_date', '')
            if not start_date:
                continue

            home_score = g.get('homeScore') or g.get('home_score')
            away_score = g.get('awayScore') or g.get('away_score')
            status = 'final' if home_score is not None and away_score is not None else 'scheduled'

            normalized.append({
                'home_team': normalize_team_name(home_team),
                'away_team': normalize_team_name(away_team),
                'home_conference': home_conf,
                'away_conference': away_conf,
                'start_date': start_date,
                'status': status,
                'neutral_site': bool(g.get('neutralSite') or g.get('neutral_site', False)),
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
