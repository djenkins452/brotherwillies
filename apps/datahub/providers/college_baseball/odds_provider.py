"""College Baseball Odds Provider — Odds API.

The Odds API's `baseball_ncaa` market is sparse (few books carry it
outside marquee matchups). This provider is defensive: if no events
come back or no matches resolve, it writes zero snapshots and reports
the result — the UI then shows "Odds unavailable" per product spec.
"""
import logging
from datetime import timedelta

from django.conf import settings
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.utils.text import slugify

from apps.college_baseball.models import Game, OddsSnapshot, Team
from apps.datahub.providers.base import AbstractProvider
from apps.datahub.providers.client import APIClient

logger = logging.getLogger(__name__)

ODDS_API_BASE = 'https://api.the-odds-api.com'
CB_SPORT_KEY = 'baseball_ncaa'


def american_to_prob(ml):
    if ml is None:
        return None
    if ml > 0:
        return 100.0 / (ml + 100.0)
    return abs(ml) / (abs(ml) + 100.0)


def _find_team(name):
    if not name:
        return None
    slug = slugify(name)
    return (
        Team.objects.filter(name__iexact=name).first()
        or Team.objects.filter(slug=slug).first()
    )


class CollegeBaseballOddsProvider(AbstractProvider):
    sport = 'college_baseball'
    data_type = 'odds'

    def __init__(self):
        api_key = settings.ODDS_API_KEY
        if not api_key:
            raise ValueError("ODDS_API_KEY not configured")
        self.api_key = api_key
        self.client = APIClient(base_url=ODDS_API_BASE, rate_limit_delay=1.0)

    def fetch(self):
        try:
            return self.client.get(
                f'/v4/sports/{CB_SPORT_KEY}/odds/',
                params={
                    'apiKey': self.api_key,
                    'regions': 'us',
                    'markets': 'h2h,spreads,totals',
                    'oddsFormat': 'american',
                },
            )
        except Exception as e:
            # baseball_ncaa can legitimately return 404 out of season.
            logger.warning(f"College baseball odds fetch returned error: {e}")
            return []

    def normalize(self, raw):
        normalized = []
        for event in raw or []:
            home = event.get('home_team', '')
            away = event.get('away_team', '')
            commence = event.get('commence_time', '')
            if not home or not away:
                continue
            for bm in event.get('bookmakers', []) or []:
                record = {
                    'home_team': home,
                    'away_team': away,
                    'commence_time': commence,
                    'sportsbook': bm.get('title', 'unknown'),
                    'moneyline_home': None,
                    'moneyline_away': None,
                    'spread': None,
                    'total': None,
                }
                for market in bm.get('markets', []) or []:
                    key = market.get('key')
                    outcomes = market.get('outcomes') or []
                    if key == 'h2h':
                        for o in outcomes:
                            if o.get('name') == home:
                                record['moneyline_home'] = o.get('price')
                            else:
                                record['moneyline_away'] = o.get('price')
                    elif key == 'spreads':
                        for o in outcomes:
                            if o.get('name') == home:
                                record['spread'] = o.get('point')
                    elif key == 'totals':
                        for o in outcomes:
                            if o.get('name') == 'Over':
                                record['total'] = o.get('point')
                normalized.append(record)
        return normalized

    def persist(self, normalized):
        created = skipped = 0
        now = timezone.now()
        for item in normalized:
            home = _find_team(item['home_team'])
            away = _find_team(item['away_team'])
            if not home or not away:
                skipped += 1
                continue

            commence = parse_datetime(item.get('commence_time') or '')
            if commence and timezone.is_naive(commence):
                commence = timezone.make_aware(commence)

            qs = Game.objects.filter(home_team=home, away_team=away)
            if commence:
                qs = qs.filter(
                    first_pitch__date__gte=(commence - timedelta(days=1)).date(),
                    first_pitch__date__lte=(commence + timedelta(days=1)).date(),
                )
            game = qs.order_by('first_pitch').first()
            if not game:
                skipped += 1
                continue

            home_prob = american_to_prob(item.get('moneyline_home'))
            if home_prob is None:
                skipped += 1
                continue

            snapshot = OddsSnapshot.objects.create(
                game=game,
                captured_at=now,
                sportsbook=item['sportsbook'],
                market_home_win_prob=home_prob,
                spread=item.get('spread'),
                total=item.get('total'),
                moneyline_home=item.get('moneyline_home'),
                moneyline_away=item.get('moneyline_away'),
            )
            from apps.core.services.odds_movement import apply_movement_intelligence
            apply_movement_intelligence(OddsSnapshot, snapshot)
            created += 1
        return {'status': 'ok', 'created': created, 'skipped': skipped}
