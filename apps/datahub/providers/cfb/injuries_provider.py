"""CFB Injuries Provider — fetches injury data from ESPN Public API."""

import logging
from datetime import timedelta

from django.db.models import Q
from django.utils import timezone
from django.utils.text import slugify

from apps.cfb.models import Game, Team, InjuryImpact
from apps.datahub.providers.base import AbstractProvider
from apps.datahub.providers.client import APIClient
from apps.datahub.providers.name_utils import normalize_team_name

logger = logging.getLogger(__name__)

ESPN_BASE = 'https://site.api.espn.com'

STATUS_TO_IMPACT = {
    'out': 'high',
    'doubtful': 'high',
    'questionable': 'med',
    'probable': 'low',
    'day-to-day': 'med',
}


class CFBInjuriesProvider(AbstractProvider):
    sport = 'cfb'
    data_type = 'injuries'

    def __init__(self):
        self.client = APIClient(
            base_url=ESPN_BASE,
            rate_limit_delay=1.0,
        )

    def fetch(self):
        """Fetch CFB injuries from ESPN scoreboard."""
        return self.client.get(
            '/apis/site/v2/sports/football/college-football/scoreboard',
        )

    def normalize(self, raw):
        """Extract injury data from ESPN scoreboard response."""
        normalized = []
        events = raw.get('events', [])

        for event in events:
            for competition in event.get('competitions', []):
                for competitor in competition.get('competitors', []):
                    team_data = competitor.get('team', {})
                    team_name = team_data.get('displayName', '') or team_data.get('name', '')
                    team_name = normalize_team_name(team_name)

                    injuries = competitor.get('injuries', [])
                    for inj in injuries:
                        status = (inj.get('status', '') or '').lower()
                        impact = STATUS_TO_IMPACT.get(status, 'low')
                        player = inj.get('athlete', {}).get('displayName', 'Unknown')
                        detail = ''
                        if isinstance(inj.get('details'), dict):
                            detail = inj['details'].get('detail', '')

                        note = f"{player}: {status}"
                        if detail:
                            note += f" ({detail})"

                        normalized.append({
                            'team_name': team_name,
                            'impact_level': impact,
                            'notes': note,
                            'event_date': event.get('date', ''),
                        })

        return normalized

    def persist(self, normalized):
        """Upsert injury records for upcoming games."""
        created = 0
        updated = 0
        skipped = 0
        now = timezone.now()

        for item in normalized:
            team = Team.objects.filter(slug=slugify(item['team_name'])).first()
            if not team:
                skipped += 1
                continue

            # Find upcoming games for this team within next 7 days
            games = Game.objects.filter(
                status='scheduled',
                kickoff__gte=now,
                kickoff__lte=now + timedelta(days=7),
            ).filter(
                Q(home_team=team) | Q(away_team=team),
            )

            for game in games:
                obj, was_created = InjuryImpact.objects.update_or_create(
                    game=game,
                    team=team,
                    defaults={
                        'impact_level': item['impact_level'],
                        'notes': item['notes'],
                    },
                )
                if was_created:
                    created += 1
                else:
                    updated += 1

        return {'status': 'ok', 'created': created, 'updated': updated, 'skipped': skipped}
