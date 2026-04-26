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


def _parse_moneyline_from_details(details, home_abbr=None, away_abbr=None):
    """Parse a moneyline from ESPN's `odds[].details` string.

    ESPN has been observed serving the moneyline inside `details` as a
    string like "BAL -143" (or "+120" / "-115" without an abbreviation).
    This is distinct from a spread which uses a decimal value
    ("BAL -1.5"). Disambiguating heuristic:
      - Decimal point → spread, return (None, None).
      - |value| < 100 → spread (run line / point spread), return (None, None).
      - Integer with |value| >= 100 → treat as moneyline.

    Returns (side, value) where side is 'home', 'away', or None when the
    abbreviation can't be matched. Returns (None, None) on any parse
    failure — caller logs `mlb_odds_espn_parse_error` and moves on.
    """
    if not details or not isinstance(details, str):
        return None, None
    parts = details.strip().split()
    if not parts:
        return None, None
    # Forms: "BAL -143" (2 parts) or "-143" / "+120" (1 part).
    if len(parts) >= 2:
        abbr = parts[0]
        raw = parts[1]
    else:
        abbr = None
        raw = parts[0]
    # Spreads always carry a decimal; moneylines never do. Reject early.
    if '.' in raw:
        return None, None
    try:
        value = int(raw.lstrip('+'))
    except (ValueError, TypeError):
        return None, None
    # Reject values too small to be a moneyline (1.5-pt spread serialized
    # as "1.5" was already caught; "-99" / "5" are also rejected here).
    if abs(value) < 100:
        return None, None
    side = None
    if abbr and home_abbr and abbr.upper() == str(home_abbr).upper():
        side = 'home'
    elif abbr and away_abbr and abbr.upper() == str(away_abbr).upper():
        side = 'away'
    # If no abbreviation was present (form "+120"), caller decides which
    # side this belongs to from positional context.
    return side, value


def _coerce_to_int(value):
    """Best-effort: pull an int out of a value that might be wrapped in a dict.

    ESPN started serving moneyLine as `{"value": -150, "displayValue": "-150"}`
    in some payloads — we used to assume bare ints, which crashed downstream
    code with `TypeError: '>' not supported between instances of 'dict' and 'int'`.
    This helper unwraps the common dict shapes seen in the wild.

    Returns None when no integer can be extracted (caller treats as "side
    has no moneyline available").
    """
    if value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, dict):
        # Common keys: value (most common), american, current, openValue, close
        for key in ('value', 'american', 'current', 'openValue', 'close', 'displayValue'):
            if key in value:
                return _coerce_to_int(value[key])
        return None
    if isinstance(value, str):
        # "+120" / "-150" / "120" — strip the + and parse
        try:
            return int(value.lstrip('+'))
        except (ValueError, TypeError):
            return None
    return None


def _extract_moneyline(odds_entry, side):
    """Pull the American moneyline for 'home' or 'away' out of an ESPN odds entry.

    ESPN has used at least four shapes historically:
      A. `{"homeTeamOdds": {"moneyLine": -131}, "awayTeamOdds": {"moneyLine": 113}}`
      B. `{"moneyline": {"home": -131, "away": 113}}`
      C. flat: `{"homeMoneyLine": -131, "awayMoneyLine": 113}`
      D. dict-wrapped: `{"homeTeamOdds": {"moneyLine": {"value": -131, "displayValue": "-131"}}}`
         (newer ESPN shape — caused production outage on 2026-04-25)
    Be tolerant of all four. _coerce_to_int handles shape D + str variants;
    callers downstream get a bare int or None.
    """
    # Shape A
    team_odds = odds_entry.get(f'{side}TeamOdds') or {}
    ml = team_odds.get('moneyLine')
    if ml is not None:
        coerced = _coerce_to_int(ml)
        if coerced is not None:
            return coerced
    # Shape B
    ml_dict = odds_entry.get('moneyline') or odds_entry.get('moneyLine') or {}
    if isinstance(ml_dict, dict):
        ml = ml_dict.get(side) or ml_dict.get(f'{side}Odds') or ml_dict.get(f'{side}Value')
        if ml is not None:
            coerced = _coerce_to_int(ml)
            if coerced is not None:
                return coerced
    # Shape C
    flat = (
        odds_entry.get(f'{side}MoneyLine')
        or odds_entry.get(f'{side}Moneyline')
        or odds_entry.get(f'{side}_moneyline')
    )
    return _coerce_to_int(flat)


def _pick_best_odds_entry(odds_list, home_abbr=None, away_abbr=None):
    """Return the first odds entry with both home + away moneylines set.

    ESPN sometimes returns multiple entries per event (e.g. a spread-only
    book first, then a moneyline book). Picking the first DraftKings entry
    misses the moneyline when that entry is spread-only. Scan all entries,
    prefer the preferred provider, and return the tuple (entry, home_ml, away_ml).

    Pass-by-pass strategy:
      1. Preferred provider with both moneylines via standard shapes.
      2. Any provider with both moneylines via standard shapes.
      3. Combined `details` parsing across ALL entries — ESPN was observed
         emitting the moneyline inside `details` strings like "BAL -143"
         instead of `homeTeamOdds.moneyLine`. We accumulate side-by-side.
      4. Fall back to the first entry's spread/total (no moneyline).
    """
    # Pass 1 — preferred provider with both moneylines present.
    for o in odds_list or []:
        if (o.get('provider') or {}).get('name') != PREFERRED_PROVIDER:
            continue
        h = _extract_moneyline(o, 'home')
        a = _extract_moneyline(o, 'away')
        if h is not None and a is not None:
            return o, h, a
    # Pass 2 — any provider with both moneylines via standard shapes.
    for o in odds_list or []:
        h = _extract_moneyline(o, 'home')
        a = _extract_moneyline(o, 'away')
        if h is not None and a is not None:
            return o, h, a
    # Pass 3 — combined details-string parsing. Each entry's details
    # might carry one side's moneyline (e.g. "BAL -143" for home,
    # "BOS +130" for away in a separate entry, OR just one side as
    # "NYY -136" with the opposite side never explicitly published).
    if odds_list and (home_abbr or away_abbr):
        details_home = details_away = None
        for o in odds_list:
            details = o.get('details')
            if not details:
                continue
            side, value = _parse_moneyline_from_details(details, home_abbr, away_abbr)
            if side == 'home' and details_home is None:
                details_home = value
            elif side == 'away' and details_away is None:
                details_away = value
            elif side is None and value is not None:
                # The string parsed as a moneyline (|v| >= 100, integer)
                # but the abbreviation matched neither home nor away —
                # most often a third-team abbreviation we don't know.
                # Surface so we can extend the alias map if needed.
                logger.warning(
                    'mlb_odds_espn_no_match_for_details '
                    'details=%r home_abbr=%r away_abbr=%r value=%s',
                    details, home_abbr, away_abbr, value,
                )

        # v1 derivation — when ESPN gives only one side's moneyline
        # ("NYY -136" but no entry for the opponent), fill the missing
        # side via symmetric inversion. Real markets carry vig (the true
        # opposite of -136 is roughly +120, not +136), so this is
        # acknowledged as approximate. Acceptable in v1 because:
        #   - The model still owns the true probability; ESPN's number
        #     only feeds into market_home_win_prob downstream.
        #   - "Approximate moneyline" is strictly better than "no
        #     moneyline" — the alternative was skipping the game entirely
        #     and showing the user no odds.
        if details_home is not None and details_away is None:
            details_away = -details_home
        elif details_away is not None and details_home is None:
            details_home = -details_away

        if details_home is not None or details_away is not None:
            # Prefer the preferred-provider entry as the carrier so
            # downstream spread/total still come from that source.
            carrier = next(
                (o for o in odds_list
                 if (o.get('provider') or {}).get('name') == PREFERRED_PROVIDER),
                odds_list[0],
            )
            # Emit the per-spec log line so deploy logs show exactly
            # what details-derivation produced for each event.
            details_seen = next(
                (o.get('details') for o in odds_list if o.get('details')),
                '',
            )
            logger.info(
                'mlb_odds_espn_parsed_details details=%r '
                'home_ml=%s away_ml=%s',
                details_seen, details_home, details_away,
            )
            return carrier, details_home, details_away
    # Pass 4 — fall back to the first entry so we still emit spread/total.
    if odds_list:
        first = odds_list[0]
        return first, _extract_moneyline(first, 'home'), _extract_moneyline(first, 'away')
    return None, None, None


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
            # Team abbreviations let _pick_best_odds_entry parse the
            # moneyline out of "BAL -143" style `details` strings.
            home_abbr = (home.get('team') or {}).get('abbreviation') or ''
            away_abbr = (away.get('team') or {}).get('abbreviation') or ''

            # Commence time — prefer event.date, fall back to competition.date.
            commence = event.get('date') or comp.get('date') or ''

            odds_list = comp.get('odds') or []
            if not odds_list:
                continue
            # Scan ALL odds entries for one with both home+away moneylines, not
            # just the first DraftKings entry — ESPN sometimes emits a spread-
            # only entry before the moneyline entry under the same provider.
            preferred, home_ml, away_ml = _pick_best_odds_entry(
                odds_list, home_abbr=home_abbr, away_abbr=away_abbr,
            )
            if preferred is None:
                continue
            # If neither standard shapes nor `details` parsing produced a
            # moneyline, AND there were details strings present, emit a
            # parse-error log so we can see the raw shape that defeated us.
            if home_ml is None and away_ml is None:
                for o in odds_list:
                    details = o.get('details')
                    if details:
                        logger.warning(
                            'mlb_odds_espn_parse_error '
                            'home_abbr=%r away_abbr=%r raw_details=%r',
                            home_abbr, away_abbr, details,
                        )
                        break

            spread = preferred.get('spread')
            total = preferred.get('overUnder')
            provider_name = (preferred.get('provider') or {}).get('name') or 'ESPN'

            # If we still have no moneyline after scanning, log the raw shape of
            # the chosen entry once per fetch so operators can see exactly what
            # ESPN sent — otherwise this failure is invisible in the log.
            if home_ml is None:
                import json as _json
                sample = {k: v for k, v in preferred.items() if k != 'links'}
                try:
                    sample_str = _json.dumps(sample, default=str)[:800]
                except Exception:
                    sample_str = str(sample)[:800]
                logger.warning(
                    f"mlb_odds_espn_no_moneyline home={home_team} away={away_team} "
                    f"entries={len(odds_list)} sample={sample_str}"
                )

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
        """Persist normalized ESPN odds rows.

        Matching strategy (deterministic, no caller-provided ID set):
          1. Normalize team names → DB Team.
          2. Restrict candidate Games to upcoming-or-just-started games
             (first_pitch in [now-2h, now+72h]). This prevents matching
             ESPN events to YESTERDAY's already-played game with the same
             matchup — a real bug in the prior implementation that caused
             the matched game.pk to fall outside the gap set.
          3. Pick the candidate whose first_pitch is closest to ESPN's
             commence_time (Python-side, portable across SQLite + PG).
          4. Per-row freshness check: if the matched game already has a
             fresh primary OddsSnapshot in the FRESH_ODDS_MAX_AGE_MINUTES
             window, skip — primary already covered it, ESPN is fallback
             only. This replaces the old `target_game_ids` filter and
             requires no caller cooperation.
          5. Persist with odds_source='espn', source_quality='fallback'.

        Logs (per spec):
          mlb_odds_espn_match_success  — per matched game
          mlb_odds_espn_no_match       — when team or game lookup fails
          mlb_odds_espn_persist_skip   — reason=has_fresh_primary etc.
          mlb_odds_espn_persist_summary — final created/skipped counts
        """
        from django.conf import settings as django_settings

        fresh_window = getattr(django_settings, 'FRESH_ODDS_MAX_AGE_MINUTES', 180)
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
                    'mlb_odds_espn_no_match stage=team api_home=%r api_away=%r '
                    'home_matched=%s away_matched=%s',
                    item['home_team'], item['away_team'], bool(home), bool(away),
                )
                continue

            commence = parse_datetime(item.get('commence_time') or '')
            if commence and timezone.is_naive(commence):
                commence = timezone.make_aware(commence)

            # Restrict to upcoming or just-started games. Without this,
            # the matchup query could return yesterday's already-played
            # game (same teams, same ±36h window) and we'd persist an
            # ESPN snapshot against a finished game.
            window_start = now - timedelta(hours=2)
            window_end = now + timedelta(hours=72)
            candidates = list(
                Game.objects
                .filter(home_team=home, away_team=away,
                        first_pitch__gte=window_start,
                        first_pitch__lte=window_end)
            )
            game = None
            if candidates:
                if commence:
                    candidates.sort(
                        key=lambda g: abs((g.first_pitch - commence).total_seconds()),
                    )
                else:
                    candidates.sort(key=lambda g: g.first_pitch)
                game = candidates[0]

            if not game:
                skipped += 1
                skip_reasons['no_game_match'] = skip_reasons.get('no_game_match', 0) + 1
                logger.info(
                    'mlb_odds_espn_no_match stage=game api_home=%r api_away=%r '
                    'matched_home=%r matched_away=%r commence=%s',
                    item['home_team'], item['away_team'],
                    home.name, away.name, commence,
                )
                continue

            # Per-row freshness: skip if THIS game already has a fresh
            # primary snapshot. Replaces the old target_game_ids filter.
            # Idempotent and self-contained — no caller cooperation needed,
            # so a manually-invoked `MLBEspnOddsProvider().run()` can no
            # longer accidentally double-write.
            fresh_cutoff = now - timedelta(minutes=fresh_window)
            already_covered = OddsSnapshot.objects.filter(
                game=game,
                captured_at__gte=fresh_cutoff,
                odds_source='odds_api',
            ).exists()
            if already_covered:
                skipped += 1
                skip_reasons['has_fresh_primary'] = skip_reasons.get('has_fresh_primary', 0) + 1
                logger.info(
                    'mlb_odds_espn_persist_skip reason=has_fresh_primary '
                    'game=%s home=%s away=%s',
                    game.id, home.name, away.name,
                )
                continue

            home_prob = american_to_prob(item.get('moneyline_home'))
            if home_prob is None:
                skipped += 1
                skip_reasons['no_moneyline_home'] = skip_reasons.get('no_moneyline_home', 0) + 1
                logger.info(
                    'mlb_odds_espn_persist_skip reason=no_moneyline_home '
                    'game=%s home=%s away=%s',
                    game.id, home.name, away.name,
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
                # Tag the source so the UI and recommendation engine can
                # tell ESPN-fallback rows apart from primary ones.
                odds_source='espn',
                source_quality='fallback',
            )
            logger.info(
                'mlb_odds_espn_match_success game_id=%s home=%s away=%s '
                'ml_home=%s ml_away=%s sportsbook=%s',
                game.id, home.name, away.name,
                item.get('moneyline_home'), item.get('moneyline_away'),
                item.get('sportsbook'),
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
