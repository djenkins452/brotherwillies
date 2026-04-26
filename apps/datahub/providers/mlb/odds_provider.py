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

            # ---- DIAGNOSTIC: aggregate per-event pipeline counters ----
            # Answers Stage 1 of the debugging spec ("is the API returning
            # usable data?"). Every counter is computed from the raw
            # response so we can attribute losses to a specific stage.
            events_no_books = 0
            events_with_h2h = 0
            events_with_spreads = 0
            events_with_totals = 0
            events_with_moneyline_pair = 0
            events_with_only_one_moneyline = 0
            bm_counts = []
            for e in data:
                books = e.get('bookmakers') or []
                bm_counts.append(len(books))
                if not books:
                    events_no_books += 1
                    continue
                # We use union-of-markets across books because The Odds API
                # can give different markets per book, and any of them
                # supports the moneyline path.
                seen_market_keys: set = set()
                ml_home_seen = ml_away_seen = False
                for book in books:
                    for m in book.get('markets') or []:
                        key = m.get('key')
                        if key:
                            seen_market_keys.add(key)
                        if key == 'h2h':
                            for o in m.get('outcomes') or []:
                                name = o.get('name', '')
                                # Compare normalized to the event's home/away.
                                if name == e.get('home_team'):
                                    ml_home_seen = True
                                elif name == e.get('away_team'):
                                    ml_away_seen = True
                if 'h2h' in seen_market_keys:
                    events_with_h2h += 1
                if 'spreads' in seen_market_keys:
                    events_with_spreads += 1
                if 'totals' in seen_market_keys:
                    events_with_totals += 1
                if ml_home_seen and ml_away_seen:
                    events_with_moneyline_pair += 1
                elif ml_home_seen or ml_away_seen:
                    events_with_only_one_moneyline += 1
            avg_books = (sum(bm_counts) / len(bm_counts)) if bm_counts else 0.0
            logger.info(
                'mlb_odds_fetch_pipeline events_returned=%d '
                'events_with_bookmakers=%d events_no_bookmakers=%d '
                'avg_bookmakers=%.1f '
                'events_with_h2h=%d events_with_spreads=%d events_with_totals=%d '
                'events_with_moneyline_pair=%d events_with_only_one_moneyline=%d',
                len(data), len(data) - events_no_books, events_no_books,
                avg_books, events_with_h2h, events_with_spreads, events_with_totals,
                events_with_moneyline_pair, events_with_only_one_moneyline,
            )

            # ---- DIAGNOSTIC: dump 1-2 full event payloads when the
            # DEBUG_ODDS_MATCHING flag is on. Sanitized to first bookmaker
            # only so the log line stays readable. The flag is meant to
            # be flipped on briefly via Railway env, harvested, then off.
            from django.conf import settings as django_settings
            if getattr(django_settings, 'DEBUG_ODDS_MATCHING', False):
                import json
                for i, e in enumerate(data[:2]):
                    sample = dict(e)
                    if sample.get('bookmakers'):
                        sample['bookmakers'] = sample['bookmakers'][:1]
                    logger.info(
                        'mlb_odds_fetch_debug_payload event_index=%d payload=%s',
                        i, json.dumps(sample),
                    )
        elif not data:
            logger.warning(f"mlb_odds_fetch_empty payload window={commence_from}..{commence_to}")
        return data

    def normalize(self, raw):
        normalized = []
        # ---- DIAGNOSTIC: per-event extraction stats (Stage 2 of the
        # debugging spec — "are we dropping valid data here?"). Currently
        # this stage has no logging; if events are silently lost between
        # fetch and persist, this is the blind spot we're filling.
        events_skipped_no_team_name = 0
        events_emitted_records = 0
        events_emitted_with_moneylines = 0
        events_emitted_no_moneylines = 0

        for event in raw or []:
            home = event.get('home_team', '')
            away = event.get('away_team', '')
            commence = event.get('commence_time', '')
            if not home or not away:
                events_skipped_no_team_name += 1
                logger.info(
                    'mlb_odds_normalize_skip reason=missing_team_name '
                    'home=%r away=%r commence=%r',
                    home, away, commence,
                )
                continue

            event_records_before = len(normalized)
            event_has_moneyline_in_any_book = False

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
                if record['moneyline_home'] is not None and record['moneyline_away'] is not None:
                    event_has_moneyline_in_any_book = True
                normalized.append(record)

            event_records_emitted = len(normalized) - event_records_before
            if event_records_emitted > 0:
                events_emitted_records += 1
                if event_has_moneyline_in_any_book:
                    events_emitted_with_moneylines += 1
                else:
                    events_emitted_no_moneylines += 1

        # ---- DIAGNOSTIC: per-event normalization rollup. Reading order:
        #   in:  events received from fetch()
        #   out: events that emitted at least one record  (events_emitted_records)
        #        of those, how many had at least one book with both
        #        moneylines populated (events_emitted_with_moneylines)
        # Mismatch between fetch's events_returned and normalize's
        # events_emitted_records is the silent-loss signal.
        logger.info(
            'mlb_odds_normalize_summary events_in=%d records_out=%d '
            'events_skipped_no_team_name=%d '
            'events_emitted_records=%d events_emitted_with_moneylines=%d '
            'events_emitted_no_moneylines=%d',
            len(raw or []), len(normalized), events_skipped_no_team_name,
            events_emitted_records, events_emitted_with_moneylines,
            events_emitted_no_moneylines,
        )
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

        # ---- DIAGNOSTIC: System state pre-flight. Stage 5 of the debugging
        # spec — "is the system even attempting the API call?". We surface
        # ProviderHealth state for both odds_api and espn so the deploy log
        # answers "circuit closed?" "fallback used?" without a separate query.
        try:
            from apps.ops.services.provider_health import state_summary
            for prov in ('odds_api', 'espn'):
                s = state_summary(prov)
                logger.info(
                    'mlb_odds_persist_preflight provider=%s state=%s '
                    'consecutive_failures=%d circuit_open=%s '
                    'last_status_code=%s last_open_reason=%r '
                    'last_success_at=%s last_failure_at=%s',
                    s['provider'], s['state'], s['consecutive_failures'],
                    s['is_circuit_open'], s['last_status_code'],
                    s['last_open_reason'], s['last_success_at'], s['last_failure_at'],
                )
        except Exception as exc:  # noqa: BLE001 — diagnostic must not break ingestion
            logger.warning('mlb_odds_persist_preflight_failed err=%s', exc)

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
                # Pick the candidate whose first_pitch is CLOSEST to the
                # API event's commence_time. Previous code used
                # .order_by('first_pitch').first() which always returned
                # the EARLIEST candidate — wrong for doubleheaders or any
                # case with multiple Game rows for the same matchup
                # within the ±36h window. With the old logic, both
                # API events for a doubleheader would route to game 1,
                # leaving game 2 with zero primary snaps.
                #
                # Closest-by-delta is what fallback 2 already does for
                # the wider ±4-day query; making the primary path
                # consistent eliminates the surprise.
                candidates = list(
                    Game.objects
                    .filter(home_team=home, away_team=away,
                            first_pitch__gte=window_start,
                            first_pitch__lte=window_end)
                )
                if candidates:
                    candidates.sort(
                        key=lambda g: abs((g.first_pitch - commence).total_seconds()),
                    )
                    game = candidates[0]

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
                # Explicit even though these match the model defaults — keeps
                # the contract obvious: this row came from The Odds API and
                # is primary-quality. The ESPN persist path tags rows
                # 'espn'/'fallback' so the UI and recommendation engine
                # can distinguish source without joining tables.
                odds_source='odds_api',
                source_quality='primary',
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
