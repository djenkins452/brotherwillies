"""MLB Pitcher Stats Provider — fetches season stats for known starting pitchers.

Runs on its own cadence (e.g., daily). Iterates all StartingPitcher rows
referenced by upcoming (scheduled) games and calls /people/{id}/stats to
retrieve ERA / WHIP / K/9 for the current season. Computes rating from
those stats and stores both raw + derived values.

If stats are missing (early season, call-up, etc.), the pitcher row's
rating remains at its existing value and stats_updated_at is NOT set —
the game's confidence score will reflect that downstream.

Rating formula (documented here for transparency):
    rating = 50
    rating += clip(4.0 - ERA, -3, 4) * 8          # lower ERA -> better
    rating += clip(1.2 - WHIP, -0.3, 0.5) * 30    # lower WHIP -> better
    rating += clip(K/9 - 8.0, -3, 5) * 2          # higher K/9 -> better
    rating = clamp(10, rating, 95)
"""
import logging
from datetime import datetime

from django.conf import settings
from django.db.models import Q
from django.utils import timezone

from apps.datahub.providers.base import AbstractProvider
from apps.datahub.providers.client import APIClient
from apps.mlb.models import Game, StartingPitcher

logger = logging.getLogger(__name__)

SOURCE = 'mlb_stats_api'


def _clip(x, lo, hi):
    return max(lo, min(hi, x))


def compute_pitcher_rating(era, whip, k_per_9):
    """Derive a 10-95 pitcher rating from season stats. Returns None if unusable."""
    if era is None or whip is None or k_per_9 is None:
        return None
    rating = 50.0
    rating += _clip(4.0 - era, -3.0, 4.0) * 8.0
    rating += _clip(1.2 - whip, -0.3, 0.5) * 30.0
    rating += _clip(k_per_9 - 8.0, -3.0, 5.0) * 2.0
    return _clip(rating, 10.0, 95.0)


class MLBPitcherStatsProvider(AbstractProvider):
    sport = 'mlb'
    data_type = 'pitcher_stats'

    def __init__(self):
        self.client = APIClient(
            base_url=settings.MLB_STATSAPI_BASE_URL,
            rate_limit_delay=0.3,
        )

    def _pitchers_of_interest(self):
        """Pitchers referenced by scheduled/live games — others are ignored."""
        now = timezone.now()
        pitcher_ids = set(
            Game.objects.filter(
                first_pitch__gte=now,
                status__in=['scheduled', 'live'],
            )
            .exclude(home_pitcher__isnull=True, away_pitcher__isnull=True)
            .values_list('home_pitcher_id', 'away_pitcher_id')
        )
        ids = set()
        for pair in pitcher_ids:
            for pid in pair:
                if pid:
                    ids.add(pid)
        return StartingPitcher.objects.filter(
            Q(id__in=ids),
            source=SOURCE,
        ).exclude(external_id='')

    def fetch(self):
        """Batch-fetch stats for relevant pitchers via /people endpoint."""
        pitchers = list(self._pitchers_of_interest())
        if not pitchers:
            return []

        season = datetime.utcnow().year
        results = []
        # /people supports multiple personIds comma-separated
        # Split in chunks of 40 to stay under URL length limits.
        for i in range(0, len(pitchers), 40):
            chunk = pitchers[i:i + 40]
            ids = ','.join(p.external_id for p in chunk)
            try:
                data = self.client.get(
                    '/v1/people',
                    params={
                        'personIds': ids,
                        'hydrate': f'stats(group=[pitching],type=season,season={season})',
                    },
                )
                people = data.get('people', []) or []
                results.extend(people)
            except Exception as e:
                logger.warning(f"MLB pitcher stats chunk failed: {e}")
        return results

    def normalize(self, raw):
        records = []
        for person in raw:
            ext_id = str(person.get('id') or '')
            if not ext_id:
                continue
            throws_code = (person.get('pitchHand') or {}).get('code', '') or ''

            era = whip = k_per_9 = ip = None
            wins = losses = None
            stats_blocks = person.get('stats') or []
            for block in stats_blocks:
                if block.get('group', {}).get('displayName') != 'pitching':
                    continue
                for split in block.get('splits', []) or []:
                    s = split.get('stat') or {}
                    try:
                        era = float(s['era']) if s.get('era') not in (None, '-.--', '') else era
                    except (ValueError, TypeError):
                        pass
                    try:
                        whip = float(s['whip']) if s.get('whip') not in (None, '-.--', '') else whip
                    except (ValueError, TypeError):
                        pass
                    try:
                        k_per_9 = (
                            float(s['strikeoutsPer9Inn'])
                            if s.get('strikeoutsPer9Inn') not in (None, '-.--', '') else k_per_9
                        )
                    except (ValueError, TypeError):
                        pass
                    try:
                        ip_val = s.get('inningsPitched')
                        ip = float(ip_val) if ip_val not in (None, '') else ip
                    except (ValueError, TypeError):
                        pass
                    try:
                        wins = int(s['wins']) if s.get('wins') not in (None, '') else wins
                    except (ValueError, TypeError):
                        pass
                    try:
                        losses = int(s['losses']) if s.get('losses') not in (None, '') else losses
                    except (ValueError, TypeError):
                        pass

            records.append({
                'external_id': ext_id,
                'throws': throws_code if throws_code in ('L', 'R', 'S') else '',
                'era': era,
                'whip': whip,
                'k_per_9': k_per_9,
                'innings_pitched': ip,
                'wins': wins,
                'losses': losses,
            })
        return records

    def persist(self, normalized):
        now = timezone.now()
        updated = 0
        skipped = 0
        for item in normalized:
            try:
                pitcher = StartingPitcher.objects.get(
                    source=SOURCE, external_id=item['external_id'],
                )
            except StartingPitcher.DoesNotExist:
                skipped += 1
                continue

            pitcher.throws = item['throws'] or pitcher.throws
            pitcher.era = item['era']
            pitcher.whip = item['whip']
            pitcher.k_per_9 = item['k_per_9']
            pitcher.innings_pitched = item['innings_pitched']
            pitcher.wins = item['wins']
            pitcher.losses = item['losses']
            rating = compute_pitcher_rating(item['era'], item['whip'], item['k_per_9'])
            if rating is not None:
                pitcher.rating = rating
                pitcher.stats_updated_at = now
            pitcher.save()
            updated += 1
        return {'status': 'ok', 'updated': updated, 'skipped': skipped}
