"""MLB Team Record Provider — fetches season W/L for every team.

Single call to statsapi.mlb.com /v1/standings with AL (103) and NL (104)
league IDs returns every team's current record. Upserts onto existing Team
rows matched by (source, external_id).

Runs on its own cadence (recommended: once per refresh cycle).
"""
import logging
from datetime import datetime

from django.conf import settings
from django.utils import timezone

from apps.datahub.providers.base import AbstractProvider
from apps.datahub.providers.client import APIClient
from apps.mlb.models import Team

logger = logging.getLogger(__name__)

SOURCE = 'mlb_stats_api'


class MLBTeamRecordProvider(AbstractProvider):
    sport = 'mlb'
    data_type = 'team_record'

    def __init__(self):
        self.client = APIClient(
            base_url=settings.MLB_STATSAPI_BASE_URL,
            rate_limit_delay=0.3,
        )

    def fetch(self):
        season = datetime.utcnow().year
        try:
            data = self.client.get(
                '/v1/standings',
                params={
                    'leagueId': '103,104',
                    'season': season,
                    'standingsTypes': 'regularSeason',
                },
            )
        except Exception as e:
            logger.warning(f"MLB team record fetch failed: {e}")
            return []
        return data.get('records', []) or []

    def normalize(self, raw):
        records = []
        for block in raw:
            for entry in block.get('teamRecords', []) or []:
                team = entry.get('team') or {}
                ext_id = str(team.get('id') or '')
                if not ext_id:
                    continue
                wins = entry.get('wins')
                losses = entry.get('losses')
                try:
                    wins = int(wins) if wins is not None else None
                except (ValueError, TypeError):
                    wins = None
                try:
                    losses = int(losses) if losses is not None else None
                except (ValueError, TypeError):
                    losses = None
                records.append({
                    'external_id': ext_id,
                    'wins': wins,
                    'losses': losses,
                })
        return records

    def persist(self, normalized):
        updated = 0
        skipped = 0
        for item in normalized:
            try:
                team = Team.objects.get(
                    source=SOURCE, external_id=item['external_id'],
                )
            except Team.DoesNotExist:
                skipped += 1
                continue
            team.wins = item['wins']
            team.losses = item['losses']
            team.save(update_fields=['wins', 'losses'])
            updated += 1
        return {'status': 'ok', 'updated': updated, 'skipped': skipped}
