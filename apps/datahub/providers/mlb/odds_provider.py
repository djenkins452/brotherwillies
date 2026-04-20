"""MLB Odds Provider — fetches MLB odds from The Odds API (same API used for CFB/CBB).

Match strategy: match by home/away team name (normalized via our
normalize_team_name + mlb-specific alias table) within ±1 day of the
commence time. MLB games have unique start times per matchup on a given
day (same two teams rarely play twice the same day), so this is safe.
"""
import logging
from datetime import timedelta

from django.conf import settings
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.utils.text import slugify

from apps.datahub.providers.base import AbstractProvider
from apps.datahub.providers.client import APIClient
from apps.datahub.providers.mlb.name_aliases import normalize_mlb_team_name
from apps.mlb.models import Game, OddsSnapshot, Team

logger = logging.getLogger(__name__)

ODDS_API_BASE = 'https://api.the-odds-api.com'
MLB_SPORT_KEY = 'baseball_mlb'


def american_to_prob(ml):
    if ml is None:
        return None
    if ml > 0:
        return 100.0 / (ml + 100.0)
    return abs(ml) / (abs(ml) + 100.0)


def _find_team(name):
    if not name:
        return None
    canonical = normalize_mlb_team_name(name)
    return (
        Team.objects.filter(name__iexact=canonical).first()
        or Team.objects.filter(slug=slugify(canonical)).first()
    )


class MLBOddsProvider(AbstractProvider):
    sport = 'mlb'
    data_type = 'odds'

    def __init__(self):
        api_key = settings.ODDS_API_KEY
        if not api_key:
            raise ValueError("ODDS_API_KEY not configured")
        self.api_key = api_key
        self.client = APIClient(base_url=ODDS_API_BASE, rate_limit_delay=1.0)

    def fetch(self):
        data = self.client.get(
            f'/v4/sports/{MLB_SPORT_KEY}/odds/',
            params={
                'apiKey': self.api_key,
                'regions': 'us',
                'markets': 'h2h,spreads,totals',
                'oddsFormat': 'american',
            },
        )
        # Log a sample so deploy logs can diagnose unexpected shapes.
        if data:
            first = data[0] if isinstance(data, list) else data
            bm_count = len((first or {}).get('bookmakers') or [])
            logger.info(
                f"mlb_odds_fetch_sample events={len(data) if isinstance(data, list) else 1} "
                f"first_home={first.get('home_team')!r} first_away={first.get('away_team')!r} "
                f"first_commence={first.get('commence_time')!r} first_bookmaker_count={bm_count}"
            )
        else:
            logger.warning("mlb_odds_fetch_empty payload")
        return data

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
                            name = o.get('name', '')
                            if normalize_mlb_team_name(name) == normalize_mlb_team_name(home):
                                record['moneyline_home'] = o.get('price')
                            else:
                                record['moneyline_away'] = o.get('price')
                    elif key == 'spreads':
                        for o in outcomes:
                            name = o.get('name', '')
                            if normalize_mlb_team_name(name) == normalize_mlb_team_name(home):
                                record['spread'] = o.get('point')
                    elif key == 'totals':
                        for o in outcomes:
                            if o.get('name') == 'Over':
                                record['total'] = o.get('point')
                normalized.append(record)
        return normalized

    def persist(self, normalized):
        """Persist normalized odds rows. Skips (with structured reason log)
        when team/game match fails or moneyline is absent. Returns
        `status='empty'` when nothing was created — upstream fail-fast
        code relies on this to distinguish "ran but got nothing" from "ran
        successfully".
        """
        created = skipped = 0
        skip_reasons: dict[str, int] = {}
        now = timezone.now()
        for item in normalized:
            home = _find_team(item['home_team'])
            away = _find_team(item['away_team'])
            if not home or not away:
                skipped += 1
                reason = 'no_team_match'
                skip_reasons[reason] = skip_reasons.get(reason, 0) + 1
                logger.info(
                    f"mlb_odds_persist_skip reason={reason} "
                    f"home={item['home_team']!r} away={item['away_team']!r}"
                )
                continue

            commence = parse_datetime(item.get('commence_time') or '')
            if commence and timezone.is_naive(commence):
                commence = timezone.make_aware(commence)

            # Primary match: same teams, first_pitch within ±36h of commence.
            # The wider window + direct datetime comparison avoids the
            # timezone trap hit by `__date` lookups when TIME_ZONE != UTC
            # (game saved as aware UTC; __date resolves in TIME_ZONE).
            game = None
            if commence:
                window_start = commence - timedelta(hours=36)
                window_end = commence + timedelta(hours=36)
                game = (
                    Game.objects
                    .filter(home_team=home, away_team=away,
                            first_pitch__gte=window_start,
                            first_pitch__lte=window_end)
                    .order_by('first_pitch').first()
                )

            # Fallback 1: no commence time in the feed → take the nearest
            # upcoming game within 7 days for this matchup.
            if not game and not commence:
                cutoff = timezone.now() + timedelta(days=7)
                game = (
                    Game.objects
                    .filter(home_team=home, away_team=away,
                            first_pitch__gte=timezone.now(),
                            first_pitch__lte=cutoff)
                    .order_by('first_pitch').first()
                )

            # Fallback 2: widen to ±4 days and pick the nearest-by-delta game
            # in Python. Catches doubleheader ambiguity and any remaining
            # TZ/formatting edge cases. Portable across SQLite and Postgres.
            if not game and commence:
                cutoff_start = commence - timedelta(days=4)
                cutoff_end = commence + timedelta(days=4)
                candidates = list(
                    Game.objects
                    .filter(home_team=home, away_team=away,
                            first_pitch__gte=cutoff_start,
                            first_pitch__lte=cutoff_end)
                )
                if candidates:
                    candidates.sort(key=lambda g: abs((g.first_pitch - commence).total_seconds()))
                    game = candidates[0]
                    logger.info(
                        f"mlb_odds_persist_match_fallback home={home.name} away={away.name} "
                        f"commence={commence} matched_pitch={game.first_pitch}"
                    )

            if not game:
                skipped += 1
                reason = 'no_game_match'
                skip_reasons[reason] = skip_reasons.get(reason, 0) + 1
                logger.info(
                    f"mlb_odds_persist_skip reason={reason} "
                    f"home={home.name} away={away.name} commence={commence}"
                )
                continue

            home_prob = american_to_prob(item.get('moneyline_home'))
            if home_prob is None:
                skipped += 1
                reason = 'no_moneyline_home'
                skip_reasons[reason] = skip_reasons.get(reason, 0) + 1
                logger.info(
                    f"mlb_odds_persist_skip reason={reason} "
                    f"game={game.id} sportsbook={item['sportsbook']!r}"
                )
                continue

            OddsSnapshot.objects.create(
                game=game,
                captured_at=now,
                sportsbook=item['sportsbook'],
                market_home_win_prob=home_prob,
                spread=item.get('spread'),
                total=item.get('total'),
                moneyline_home=item.get('moneyline_home'),
                moneyline_away=item.get('moneyline_away'),
            )
            created += 1

        status = 'ok' if created > 0 else 'empty'
        logger.info(
            f"mlb_odds_persist_summary created={created} skipped={skipped} "
            f"skip_reasons={skip_reasons} status={status}"
        )
        return {
            'status': status,
            'created': created,
            'skipped': skipped,
            'skip_reasons': skip_reasons,
        }
