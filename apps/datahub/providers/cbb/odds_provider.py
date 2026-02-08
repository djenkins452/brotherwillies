"""CBB Odds Provider — fetches odds from The Odds API."""

import logging
from datetime import timedelta

from django.conf import settings
from django.utils import timezone
from django.utils.text import slugify

from apps.cbb.models import Game, OddsSnapshot, Team
from apps.datahub.providers.base import AbstractProvider
from apps.datahub.providers.client import APIClient
from apps.datahub.providers.name_utils import normalize_team_name

logger = logging.getLogger(__name__)

ODDS_API_BASE = 'https://api.the-odds-api.com'
CBB_SPORT_KEY = 'basketball_ncaab'


def american_to_prob(ml):
    """Convert American moneyline to implied probability."""
    if ml is None:
        return None
    if ml > 0:
        return 100.0 / (ml + 100.0)
    else:
        return abs(ml) / (abs(ml) + 100.0)


class CBBOddsProvider(AbstractProvider):
    sport = 'cbb'
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
        """Fetch current CBB odds from The Odds API."""
        return self.client.get(
            f'/v4/sports/{CBB_SPORT_KEY}/odds/',
            params={
                'apiKey': self.api_key,
                'regions': 'us',
                'markets': 'h2h,spreads,totals',
                'oddsFormat': 'american',
            },
        )

    def normalize(self, raw):
        """Transform Odds API response into standardized dicts."""
        normalized = []
        for event in raw:
            home_team = normalize_team_name(event.get('home_team', ''))
            away_team = normalize_team_name(event.get('away_team', ''))
            commence_time = event.get('commence_time', '')

            if not home_team or not away_team:
                continue

            for bookmaker in event.get('bookmakers', []):
                sportsbook = bookmaker.get('title', 'unknown')
                record = {
                    'home_team': home_team,
                    'away_team': away_team,
                    'commence_time': commence_time,
                    'sportsbook': sportsbook,
                    'moneyline_home': None,
                    'moneyline_away': None,
                    'spread': None,
                    'total': None,
                }

                for market in bookmaker.get('markets', []):
                    key = market.get('key')
                    outcomes = market.get('outcomes', [])

                    if key == 'h2h':
                        for o in outcomes:
                            name = normalize_team_name(o.get('name', ''))
                            if name == home_team:
                                record['moneyline_home'] = o.get('price')
                            elif name == away_team:
                                record['moneyline_away'] = o.get('price')

                    elif key == 'spreads':
                        for o in outcomes:
                            name = normalize_team_name(o.get('name', ''))
                            if name == home_team:
                                record['spread'] = o.get('point')

                    elif key == 'totals':
                        for o in outcomes:
                            if o.get('name') == 'Over':
                                record['total'] = o.get('point')

                normalized.append(record)

        return normalized

    def persist(self, normalized):
        """Create OddsSnapshot records (append-only)."""
        created = 0
        skipped = 0
        now = timezone.now()

        for item in normalized:
            home = Team.objects.filter(slug=slugify(item['home_team'])).first()
            away = Team.objects.filter(slug=slugify(item['away_team'])).first()
            if not home or not away:
                skipped += 1
                continue

            # Find matching game within ±1 day
            from django.utils.dateparse import parse_datetime
            commence = parse_datetime(item['commence_time'])
            if commence and timezone.is_naive(commence):
                commence = timezone.make_aware(commence)

            game = Game.objects.filter(
                home_team=home,
                away_team=away,
            )
            if commence:
                game = game.filter(
                    tipoff__date__gte=(commence - timedelta(days=1)).date(),
                    tipoff__date__lte=(commence + timedelta(days=1)).date(),
                )
            game = game.first()

            if not game:
                skipped += 1
                continue

            # Compute implied probability from moneyline
            ml_home = item.get('moneyline_home')
            home_prob = american_to_prob(ml_home)
            if home_prob is None:
                skipped += 1
                continue

            OddsSnapshot.objects.create(
                game=game,
                captured_at=now,
                sportsbook=item['sportsbook'],
                market_home_win_prob=home_prob,
                spread=item.get('spread'),
                total=item.get('total'),
                moneyline_home=ml_home,
                moneyline_away=item.get('moneyline_away'),
            )
            created += 1

        return {'status': 'ok', 'created': created, 'skipped': skipped}
