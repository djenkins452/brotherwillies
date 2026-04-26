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
    """Three-stage match: alias lookup → DB iexact + slug → fuzzy fallback.

    Returns the matched Team or None. The fuzzy stage runs only when the
    earlier ones miss, and emits a structured log line on a successful
    fallback so we can mine the deploy logs to keep the alias dict
    growing toward zero fuzzy hits.
    """
    if not name:
        return None
    # Stage 1: alias dictionary
    canonical = normalize_mlb_team_name(name)
    team = (
        Team.objects.filter(name__iexact=canonical).first()
        or Team.objects.filter(slug=slugify(canonical)).first()
    )
    if team:
        return team

    # Stage 2: fuzzy fallback against the canonical list
    from apps.datahub.providers.mlb.name_aliases import fuzzy_match_to_canonical
    fuzzy_canonical = fuzzy_match_to_canonical(name)
    if fuzzy_canonical and fuzzy_canonical != canonical:
        team = (
            Team.objects.filter(name__iexact=fuzzy_canonical).first()
            or Team.objects.filter(slug=slugify(fuzzy_canonical)).first()
        )
        if team:
            logger.info(
                'mlb_team_match_fuzzy_recovered api_name=%r canonical_attempt=%r '
                'fuzzy_canonical=%r matched_team=%r',
                name, canonical, fuzzy_canonical, team.name,
            )
            return team

    return None


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
        # Explicit commence_time window. Without this, The Odds API's default
        # behavior can omit games that are close to first pitch (moneyline
        # often drops to their live-endpoint once games are near-start), which
        # silently leaves today's slate without odds. Asking explicitly for
        # [now-2h, now+72h] forces near-term games back into the response.
        now = timezone.now()
        commence_from = (now - timedelta(hours=2)).strftime('%Y-%m-%dT%H:%M:%SZ')
        commence_to = (now + timedelta(hours=72)).strftime('%Y-%m-%dT%H:%M:%SZ')
        data = self.client.get(
            f'/v4/sports/{MLB_SPORT_KEY}/odds/',
            params={
                'apiKey': self.api_key,
                'regions': 'us',
                'markets': 'h2h,spreads,totals',
                'oddsFormat': 'american',
                'commenceTimeFrom': commence_from,
                'commenceTimeTo': commence_to,
            },
        )
        # Log a sample so deploy logs can diagnose unexpected shapes, plus
        # the commence_time distribution so we can see on sight whether
        # today's games are even being returned.
        if data and isinstance(data, list):
            first = data[0] if data else {}
            bm_count = len((first or {}).get('bookmakers') or [])
            # Count events per UTC date in the response — if today's date
            # shows zero, it's upstream, not us.
            date_buckets: dict[str, int] = {}
            for e in data:
                ct = (e.get('commence_time') or '')[:10]  # YYYY-MM-DD
                if ct:
                    date_buckets[ct] = date_buckets.get(ct, 0) + 1
            logger.info(
                f"mlb_odds_fetch_sample events={len(data)} "
                f"first_home={first.get('home_team')!r} first_away={first.get('away_team')!r} "
                f"first_commence={first.get('commence_time')!r} first_bookmaker_count={bm_count} "
                f"date_distribution={date_buckets} "
                f"window={commence_from}..{commence_to}"
            )
        elif not data:
            logger.warning(f"mlb_odds_fetch_empty payload window={commence_from}..{commence_to}")
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

        Logging strategy (designed so the deploy log answers "why did N
        games not get odds?" without needing to attach a debugger):

          - Per-skip: structured log line including the API names AND the
            normalized canonical names we attempted to match against.
            Identifies whether the failure was alias-table coverage or
            something downstream.
          - Summary: one log line at end with created / skipped /
            skip_reasons dict.
          - Coverage ERROR: if created rows are < 50% of distinct (home,away)
            matchups in the input, the summary is logged at ERROR level so
            it surfaces in the Ops Command Center recent-failures panel
            without us having to dig through INFO traffic.
          - Debug mode: when settings.DEBUG_ODDS_MATCHING is True, every
            API team name we see is echoed at INFO. Designed to be flipped
            on briefly, harvest the names, then flipped off.
        """
        from django.conf import settings as django_settings
        debug_matching = getattr(django_settings, 'DEBUG_ODDS_MATCHING', False)

        created = skipped = 0
        skip_reasons: dict[str, int] = {}
        # Track distinct matchups seen so we can compute coverage at the end.
        # An "expected matchup" is each unique (home_team, away_team) pair
        # the API gave us. Multiple bookmakers per game count once.
        seen_matchups: set = set()
        matched_matchups: set = set()
        now = timezone.now()
        for item in normalized:
            api_home = item.get('home_team', '')
            api_away = item.get('away_team', '')
            seen_matchups.add((api_home, api_away))

            if debug_matching:
                logger.info(
                    'mlb_odds_persist_debug api_home=%r api_away=%r '
                    'sportsbook=%r commence=%r',
                    api_home, api_away, item.get('sportsbook'),
                    item.get('commence_time'),
                )

            home = _find_team(api_home)
            away = _find_team(api_away)
            if not home or not away:
                skipped += 1
                reason = 'no_team_match'
                skip_reasons[reason] = skip_reasons.get(reason, 0) + 1
                # Include the normalized canonical attempt so a log reader
                # can see whether the alias table missed the variant or the
                # DB simply doesn't have the team yet.
                logger.info(
                    'mlb_odds_persist_skip reason=%s '
                    'api_home=%r api_away=%r '
                    'normalized_home=%r normalized_away=%r '
                    'home_matched=%s away_matched=%s',
                    reason, api_home, api_away,
                    normalize_mlb_team_name(api_home),
                    normalize_mlb_team_name(api_away),
                    bool(home), bool(away),
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
            matched_matchups.add((api_home, api_away))
            # Movement intelligence — silently no-ops on the first snapshot
            # for a (game, sportsbook). When a follow-up pull crosses the
            # significance threshold, this upgrades snapshot_type to
            # "significant" and persists movement_score + movement_class.
            # Exception-safe: telemetry can't break ingestion.
            from apps.core.services.odds_movement import apply_movement_intelligence
            apply_movement_intelligence(OddsSnapshot, snapshot)
            created += 1

        status = 'ok' if created > 0 else 'empty'

        # Coverage check: how many distinct matchups did we successfully
        # persist at least one bookmaker row for? Anything under 50% is
        # almost certainly a regression — alias coverage dropped, schedule
        # dates drifted, or persist logic regressed. Log ERROR so the row
        # surfaces in the Ops Command Center recent-failures panel.
        n_seen = len(seen_matchups)
        n_matched = len(matched_matchups)
        coverage_pct = (n_matched / n_seen * 100.0) if n_seen else 0.0
        log_fn = logger.info
        if n_seen >= 4 and coverage_pct < 50.0:
            # 4-game minimum so a small slate (early season) doesn't
            # spuriously trigger the alarm.
            log_fn = logger.error
        log_fn(
            'mlb_odds_persist_summary created=%d skipped=%d '
            'skip_reasons=%s status=%s '
            'matchups_seen=%d matchups_matched=%d coverage_pct=%.1f',
            created, skipped, skip_reasons, status,
            n_seen, n_matched, coverage_pct,
        )

        return {
            'status': status,
            'created': created,
            'skipped': skipped,
            'skip_reasons': skip_reasons,
            'matchups_seen': n_seen,
            'matchups_matched': n_matched,
            'coverage_pct': round(coverage_pct, 1),
        }
