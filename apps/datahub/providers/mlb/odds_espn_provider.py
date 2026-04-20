"""MLB Odds Provider — ESPN fallback source.

ESPN's public scoreboard endpoint embeds DraftKings odds per event. No API
key, no rate-limit quota of the kind The Odds API has. Used as a fallback
when the primary provider (The Odds API) produces zero snapshots.

Endpoint shape (per event):
    event.competitions[0].odds[] -> [
        {
            'provider': {'name': 'DraftKings', ...},
            'details': 'BAL -1.5',
            'overUnder': 8.5,
            'spread': -1.5,             # home-POV
            'homeTeamOdds': {'moneyLine': -131, 'favorite': True, ...},
            'awayTeamOdds': {'moneyLine': 113, 'favorite': False, ...},
        },
        ...
    ]

Matching: ESPN gives us teams by `competitor.team.abbreviation` and
`team.displayName`. We match through the same `_find_team` helper used by
the Odds-API path so alias coverage stays in one place.
"""
import logging
from datetime import timedelta

from django.conf import settings
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from apps.datahub.providers.base import AbstractProvider
from apps.datahub.providers.client import APIClient
from apps.datahub.providers.mlb.name_aliases import normalize_mlb_team_name
from apps.mlb.models import Game, OddsSnapshot, Team

logger = logging.getLogger(__name__)

PREFERRED_PROVIDER = 'DraftKings'
DAYS_AHEAD = 2  # scoreboard call window: today + next 2 days


def _find_team(name):
    if not name:
        return None
    canonical = normalize_mlb_team_name(name)
    return Team.objects.filter(name__iexact=canonical).first()


def american_to_prob(ml):
    if ml is None:
        return None
    if ml > 0:
        return 100.0 / (ml + 100.0)
    return abs(ml) / (abs(ml) + 100.0)


class MLBEspnOddsProvider(AbstractProvider):
    """Fallback MLB odds source via ESPN's public scoreboard JSON."""
    sport = 'mlb'
    data_type = 'odds'

    def __init__(self):
        self.client = APIClient(
            base_url=settings.ESPN_BASEBALL_BASE_URL,
            rate_limit_delay=0.5,
        )

    def fetch(self):
        """Fetch scoreboard for today + next DAYS_AHEAD days.

        Returns a list of ESPN event dicts. Uses a single request per day
        since the scoreboard is date-scoped.
        """
        events: list = []
        today = timezone.localdate()
        for i in range(DAYS_AHEAD + 1):
            date_str = (today + timedelta(days=i)).strftime('%Y%m%d')
            try:
                data = self.client.get('/mlb/scoreboard', params={'dates': date_str})
                day_events = (data or {}).get('events') or []
                logger.info(f"mlb_odds_espn_fetch date={date_str} events={len(day_events)}")
                events.extend(day_events)
            except Exception as e:
                logger.warning(f"mlb_odds_espn_fetch_failed date={date_str} err={e}")
        return events

    def normalize(self, raw):
        normalized = []
        for event in raw or []:
            competitions = event.get('competitions') or []
            if not competitions:
                continue
            comp = competitions[0]
            competitors = comp.get('competitors') or []
            if len(competitors) != 2:
                continue
            home = next((c for c in competitors if c.get('homeAway') == 'home'), None)
            away = next((c for c in competitors if c.get('homeAway') == 'away'), None)
            if home is None or away is None:
                continue

            home_team = (home.get('team') or {}).get('displayName') or ''
            away_team = (away.get('team') or {}).get('displayName') or ''
            if not home_team or not away_team:
                continue

            # Commence time — prefer event.date, fall back to competition.date.
            commence = event.get('date') or comp.get('date') or ''

            odds_list = comp.get('odds') or []
            if not odds_list:
                continue
            # Prefer DraftKings when available; otherwise first entry.
            preferred = next(
                (o for o in odds_list
                 if (o.get('provider') or {}).get('name') == PREFERRED_PROVIDER),
                odds_list[0],
            )

            spread = preferred.get('spread')
            total = preferred.get('overUnder')
            home_ml = (preferred.get('homeTeamOdds') or {}).get('moneyLine')
            away_ml = (preferred.get('awayTeamOdds') or {}).get('moneyLine')
            provider_name = (preferred.get('provider') or {}).get('name') or 'ESPN'

            normalized.append({
                'home_team': home_team,
                'away_team': away_team,
                'commence_time': commence,
                'sportsbook': provider_name,
                'moneyline_home': home_ml,
                'moneyline_away': away_ml,
                'spread': spread,
                'total': total,
            })
        logger.info(f"mlb_odds_espn_normalize events={len(raw) if raw else 0} out={len(normalized)}")
        return normalized

    def persist(self, normalized):
        created = skipped = 0
        skip_reasons: dict[str, int] = {}
        now = timezone.now()
        for item in normalized:
            home = _find_team(item['home_team'])
            away = _find_team(item['away_team'])
            if not home or not away:
                skipped += 1
                skip_reasons['no_team_match'] = skip_reasons.get('no_team_match', 0) + 1
                logger.info(
                    f"mlb_odds_espn_persist_skip reason=no_team_match "
                    f"home={item['home_team']!r} away={item['away_team']!r}"
                )
                continue

            commence = parse_datetime(item.get('commence_time') or '')
            if commence and timezone.is_naive(commence):
                commence = timezone.make_aware(commence)

            # Same generous matching as the primary provider (±36h; Python-side
            # nearest-delta fallback within ±4 days).
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

            if not game:
                skipped += 1
                skip_reasons['no_game_match'] = skip_reasons.get('no_game_match', 0) + 1
                logger.info(
                    f"mlb_odds_espn_persist_skip reason=no_game_match "
                    f"home={home.name} away={away.name} commence={commence}"
                )
                continue

            home_prob = american_to_prob(item.get('moneyline_home'))
            if home_prob is None:
                skipped += 1
                skip_reasons['no_moneyline_home'] = skip_reasons.get('no_moneyline_home', 0) + 1
                logger.info(
                    f"mlb_odds_espn_persist_skip reason=no_moneyline_home game={game.id}"
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
            f"mlb_odds_espn_persist_summary created={created} skipped={skipped} "
            f"skip_reasons={skip_reasons} status={status}"
        )
        return {
            'status': status,
            'created': created,
            'skipped': skipped,
            'skip_reasons': skip_reasons,
            'source': 'espn',
        }
