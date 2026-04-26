"""Per-game diagnostic for MLB odds gap-fill failures.

Run-once tool: identifies upcoming MLB games with no fresh primary
snapshot, then probes BOTH the Odds API and ESPN scoreboard live to
report — per game — exactly which stage of the pipeline failed.

Output columns (matching the spec request):
    Game | API Present | Bookmakers | Parsed | Matched | ESPN Attempt | Final Reason

Why a separate command (vs reading the cron's logs):
  - The cron's structured logs already carry the same evidence, but
    they're per-row and require log-grepping to assemble. This command
    runs the full pipeline once on demand and produces a pre-joined,
    human-readable per-game table.
  - It does NOT persist any snapshots. It's pure diagnostic.
  - Safe to run on top of any state — won't affect data.

Trigger paths:
  - `python manage.py diagnose_mlb_odds_gaps` (local / Railway shell)
  - "Diagnose MLB Odds Gaps" button on /ops/command-center/ (UI)

Limitations (be honest):
  - Snapshot of NOW. Doesn't reconstruct historical refreshes.
  - Network-dependent. ODDS_API_KEY must be set; ESPN must be reachable.
"""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Optional

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone
from django.utils.dateparse import parse_datetime

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Per-game diagnostic record
# ---------------------------------------------------------------------------

class _GameDiag:
    """Mutable state container per game. The command fills the fields
    in pipeline order, then prints them as one row of the output table."""

    def __init__(self, game):
        self.game = game
        self.matchup = f'{game.away_team.name} @ {game.home_team.name}'
        # Game state — answers "is this even a pre-game line situation?"
        self.status = (game.status or 'unknown').upper()[:7]
        # Minutes from now to first_pitch. Negative = game has started.
        from django.utils import timezone as _tz
        delta = (game.first_pitch - _tz.now()).total_seconds() / 60.0
        self.minutes_to_pitch = int(delta)
        # DB coverage — what's actually persisted right now for this game.
        self.db_books_count = 0       # distinct sportsbooks with any snapshot in last 180m
        self.snap_age_min = None      # minutes since latest snapshot, or None
        self.snap_source = '-'        # 'odds_api' / 'espn' / '-' if no snapshot
        self.total_books_all_time = 0
        self.total_snapshots = 0
        self.primary_books_all_time = 0
        self.espn_books_all_time = 0
        self.last_primary_age_min = None
        self.last_espn_age_min = None
        self._populate_db_coverage()
        # Stage 1 — Odds API (filled later)
        self.api_present = '?'
        self.api_bookmakers = '-'
        self.api_parsed = '-'
        self.api_matched = '-'
        self.api_skip_reason = ''
        # Stage 2 — ESPN (filled later)
        self.espn_attempt = '-'
        self.espn_skip_reason = ''
        # Stage 3 — final
        self.final_reason = ''

    def _populate_db_coverage(self):
        """Read current DB state for this game so the table shows our
        coverage alongside the API's. The discrepancy is the actionable
        signal: 'API has 12 books, DB has 3 fresh' = under-covering.

        Also surfaces ALL-TIME stats per source so we can distinguish:
          - "primary worked earlier today, then game went live" (total
            books from odds_api > 1, latest_primary_age large) — normal
            live-game drop, not a bug.
          - "primary NEVER persisted for this game" (total books from
            odds_api = 0 or 1) — the actual bug we'd be hunting.
        """
        from datetime import timedelta
        from django.utils import timezone as _tz
        from apps.mlb.models import OddsSnapshot
        now = _tz.now()
        cutoff = now - timedelta(minutes=180)
        all_for_game = OddsSnapshot.objects.filter(game=self.game)
        recent = all_for_game.filter(captured_at__gte=cutoff)
        self.db_books_count = recent.values('sportsbook').distinct().count()

        # All-time totals.
        self.total_books_all_time = all_for_game.values('sportsbook').distinct().count()
        self.total_snapshots = all_for_game.count()
        self.primary_books_all_time = (
            all_for_game.filter(odds_source='odds_api')
            .values('sportsbook').distinct().count()
        )
        self.espn_books_all_time = (
            all_for_game.filter(odds_source='espn')
            .values('sportsbook').distinct().count()
        )

        # Latest snapshot overall.
        latest = all_for_game.order_by('-captured_at').first()
        if latest:
            self.snap_age_min = int((now - latest.captured_at).total_seconds() / 60)
            self.snap_source = (latest.odds_source or '?')[:8]

        # Latest primary + latest ESPN ages, so we can see the lag pattern.
        latest_primary = (
            all_for_game.filter(odds_source='odds_api')
            .order_by('-captured_at').first()
        )
        self.last_primary_age_min = (
            int((now - latest_primary.captured_at).total_seconds() / 60)
            if latest_primary else None
        )
        latest_espn = (
            all_for_game.filter(odds_source='espn')
            .order_by('-captured_at').first()
        )
        self.last_espn_age_min = (
            int((now - latest_espn.captured_at).total_seconds() / 60)
            if latest_espn else None
        )

    def as_row(self):
        # Format Δ first_pitch for human reading: "+125m" / "-30m" / "live".
        if self.status == 'LIVE':
            delta_str = 'live'
        elif self.minutes_to_pitch >= 0:
            delta_str = f'+{self.minutes_to_pitch}m'
        else:
            delta_str = f'{self.minutes_to_pitch}m'
        snap_age = f'{self.snap_age_min}m' if self.snap_age_min is not None else '-'
        return [
            self.matchup[:36],
            self.status,
            delta_str,
            f'{self.db_books_count}/{self.api_bookmakers}',  # DB books / API books
            snap_age,
            self.snap_source,
            self.api_present,
            self.api_parsed,
            self.api_matched,
            self.espn_attempt,
            self.final_reason,
        ]


def _norm_for_match(name: str) -> str:
    """Lowercase, punctuation-light key for fuzzy comparison. Mirrors the
    normalization in name_aliases._normalize_key."""
    if not name:
        return ''
    import re
    s = re.sub(r'[.\u2019\']', '', name).strip().lower()
    s = re.sub(r'\s+', ' ', s)
    return s


def _api_event_matches_game(event_home: str, event_away: str,
                            commence: Optional[str], game) -> bool:
    """Mirror the persist-side match logic without writing anything.

    We resolve through the same alias dict + DB-iexact + fuzzy fallback
    chain `_find_team` uses, then verify the matchup AND first_pitch
    window. Returns True when this API event would map to `game`.
    """
    from apps.datahub.providers.mlb.odds_provider import _find_team
    home_team = _find_team(event_home)
    away_team = _find_team(event_away)
    if home_team is None or away_team is None:
        return False
    if home_team.pk != game.home_team.pk or away_team.pk != game.away_team.pk:
        return False
    if commence:
        c = parse_datetime(commence)
        if c and timezone.is_naive(c):
            c = timezone.make_aware(c)
        if c:
            delta = abs((game.first_pitch - c).total_seconds())
            return delta <= 36 * 3600
    return True


class Command(BaseCommand):
    help = ('Per-game diagnostic of MLB odds gap-fill failures. '
            'Probes Odds API + ESPN live and reports the exact stage where '
            'each gap game failed. No persistence, no side effects.')

    def add_arguments(self, parser):
        parser.add_argument(
            '--all-upcoming', action='store_true',
            help=('Diagnose every upcoming game in the next 36h, not just the '
                  'gap games. Useful when you want to see why some games '
                  'succeeded too.'),
        )
        parser.add_argument(
            '--max-games', type=int, default=30,
            help='Cap on number of games examined (default 30).',
        )
        # Same shape as the other manual-triggerable commands so the Ops
        # dashboard's "Trigger" buttons can spawn this and have it show
        # up in the Recent Runs panel with stdout_tail populated.
        parser.add_argument('--trigger', choices=['cron', 'manual', 'deploy'], default='cron')
        parser.add_argument('--triggered-by-user-id', type=int, default=None)

    def handle(self, *args, **options):
        # Wrap the diagnostic body in cron_run_log so the captured stdout
        # tail flows into the Ops dashboard's Recent Runs panel — same
        # treatment refresh_data + refresh_scores_and_settle get. The
        # full table appears in the row's expandable summary.
        from django.contrib.auth import get_user_model
        from apps.ops.services.cron_logging import cron_run_log

        triggered_by = None
        if options.get('triggered_by_user_id'):
            User = get_user_model()
            triggered_by = User.objects.filter(pk=options['triggered_by_user_id']).first()

        with cron_run_log(
            'diagnose_mlb_odds_gaps',
            trigger=options.get('trigger', 'cron'),
            triggered_by_user=triggered_by,
        ) as log:
            stdout_lines: list[str] = []
            original_write = self.stdout.write

            def _capturing_write(line, *args, **kwargs):
                # `self.stdout.write` may be called with a style helper —
                # keep the call shape, but also append the rendered line
                # to stdout_lines so the cron-log row captures everything.
                try:
                    text = str(line)
                except Exception:
                    text = repr(line)
                stdout_lines.append(text)
                return original_write(line, *args, **kwargs)
            self.stdout.write = _capturing_write  # type: ignore[assignment]
            try:
                summary_text = self._handle_inner(options)
            finally:
                self.stdout.write = original_write  # type: ignore[assignment]
            log.summary = summary_text
            log.stdout_tail = '\n'.join(stdout_lines)

    def _handle_inner(self, options) -> str:
        from collections import Counter
        from apps.datahub.management.commands.ingest_odds import (
            _find_mlb_games_without_fresh_odds,
        )
        from apps.mlb.models import Game

        fresh_window = getattr(settings, 'FRESH_ODDS_MAX_AGE_MINUTES', 180)
        all_upcoming = options['all_upcoming']
        max_games = options['max_games']

        # ---- Pick target set ------------------------------------------------
        upcoming_pks, gap_pks = _find_mlb_games_without_fresh_odds(fresh_window)
        target_pks = upcoming_pks if all_upcoming else gap_pks

        if not target_pks:
            self.stdout.write(self.style.SUCCESS(
                'No matching games (everything covered or nothing scheduled). '
                f'upcoming={len(upcoming_pks)} gaps={len(gap_pks)}'
            ))
            return f'no_gaps upcoming={len(upcoming_pks)}'

        target_pks = target_pks[:max_games]
        target_games = list(
            Game.objects.filter(pk__in=target_pks)
            .select_related('home_team', 'away_team')
            .order_by('first_pitch')
        )
        diags = [_GameDiag(g) for g in target_games]

        self.stdout.write(
            f'Diagnosing {len(diags)} games '
            f'({"all upcoming" if all_upcoming else "gap"} mode). '
            f'Window: now → +36h. Fresh threshold: {fresh_window} min.'
        )

        # ---- Stage 0: Odds API Preflight ----------------------------------
        # Answers "is the API key correct, what's our coverage, how much
        # quota remains?" — the questions that matter when the symptom is
        # "the API isn't returning the events we expect," NOT
        # "the events are there but we're failing to parse them."
        self._print_odds_api_preflight()

        # ---- Stage 0b: per-gap-game search across filtered vs unfiltered.
        # The smoking-gun test: if a gap game appears in the UNFILTERED
        # API response but not the production-filtered one, the bug is
        # our time-window / markets filter. Definitive answer per game.
        self._print_per_game_api_search(diags)

        # ---- Stage 1: probe the Odds API -----------------------------------
        api_events = self._fetch_odds_api()
        if api_events is None:
            self.stdout.write(self.style.ERROR(
                'Odds API fetch FAILED — see log for details. All games will '
                'show api_present=ERR.'
            ))
            for d in diags:
                d.api_present = 'ERR'
        else:
            self.stdout.write(f'Odds API returned {len(api_events)} events.')
            for d in diags:
                self._classify_api_for_game(d, api_events)

        # ---- Stage 2: probe ESPN -------------------------------------------
        espn_events = self._fetch_espn()
        if espn_events is None:
            self.stdout.write(self.style.ERROR(
                'ESPN fetch FAILED — see log. All games will show espn_attempt=ERR.'
            ))
            for d in diags:
                d.espn_attempt = 'ERR'
        else:
            self.stdout.write(f'ESPN returned {len(espn_events)} events.')
            for d in diags:
                self._classify_espn_for_game(d, espn_events)

        # ---- Stage 3: synthesize a final reason per game -------------------
        for d in diags:
            d.final_reason = self._final_reason(d)

        # ---- Output --------------------------------------------------------
        self._print_table(diags)
        self._print_all_time_coverage(diags)
        # NEW: dry-run primary's persist for each game so we can see
        # the EXACT reason persist fails. Names team-pk mismatches,
        # duplicate Team rows, missing Game rows.
        self._print_primary_persist_dry_run(diags)
        self._print_legend()

        # Also emit each row at INFO so the deploy log captures the table.
        for d in diags:
            logger.info(
                'mlb_odds_gap_diagnostic game_id=%s matchup=%r api=%s '
                'bookmakers=%s parsed=%s matched=%s espn=%s reason=%s',
                d.game.id, d.matchup, d.api_present, d.api_bookmakers,
                d.api_parsed, d.api_matched, d.espn_attempt, d.final_reason,
            )

        # Tight one-line summary for the cron log row.
        # Bucket reasons by their first identifier word so the count fits.
        reason_short = Counter(
            (d.final_reason.split(' ', 1)[0] if ' ' in d.final_reason
             else d.final_reason).split('(', 1)[0]
            for d in diags
        )
        return (
            f'diagnosed={len(diags)} '
            f'reasons={dict(reason_short)}'
        )

    # -----------------------------------------------------------------------
    # Stage 0 — Odds API preflight
    # -----------------------------------------------------------------------

    def _print_odds_api_preflight(self):
        """Direct probe of The Odds API to answer the questions that aren't
        about per-event matching: key validity, plan coverage, region scope,
        and quota state. Bypasses our normal provider so we see exactly
        what the API itself returns.

        Output sections:
          - Key fingerprint (last 4 chars + length)
          - /v4/sports response — confirms MLB is in this key's catalog
          - /v4/sports/baseball_mlb/odds with regions=us  — event count
          - /v4/sports/baseball_mlb/odds with regions=us,us2 — event count
          - Quota: x-requests-used / x-requests-remaining
          - Unique bookmaker names across the wider response
        """
        import requests
        import os

        api_key = getattr(settings, 'ODDS_API_KEY', '') or ''
        self.stdout.write('')
        self.stdout.write('--- Odds API preflight ----------------------------------------')
        if not api_key:
            self.stdout.write(self.style.ERROR(
                '  ODDS_API_KEY is EMPTY in settings. The provider is being '
                'called without a key — this guarantees zero events.'
            ))
            return
        # Mask all but last 4 chars so the user can sanity-check it's the
        # right key without exposing the full secret in deploy logs.
        fingerprint = ('*' * max(0, len(api_key) - 4)) + api_key[-4:]
        self.stdout.write(f'  ODDS_API_KEY fingerprint: {fingerprint} (length={len(api_key)})')

        # 1. Sports catalog probe — proves the key is valid and what it covers.
        try:
            r = requests.get(
                'https://api.the-odds-api.com/v4/sports',
                params={'apiKey': api_key},
                timeout=10,
            )
            self.stdout.write(f'  /v4/sports → HTTP {r.status_code}')
            self._report_quota_headers(r.headers)
            if r.status_code == 200:
                sports = r.json() or []
                mlb_present = any(
                    (s.get('key') == 'baseball_mlb') for s in sports
                )
                self.stdout.write(
                    f'  Active sports for this key: {len(sports)} '
                    f'({"includes" if mlb_present else "MISSING"} baseball_mlb)'
                )
                if not mlb_present:
                    self.stdout.write(self.style.ERROR(
                        '  ⚠  baseball_mlb is NOT in this key\'s active sports list. '
                        'This is the smoking gun: the new plan does not include MLB.'
                    ))
            else:
                # 401 / 403 / 429 — show the body, that\'s where the
                # actionable error message usually is.
                body = (r.text or '')[:300]
                self.stdout.write(self.style.ERROR(
                    f'  /v4/sports failed. Body: {body!r}'
                ))
                return
        except Exception as exc:
            self.stdout.write(self.style.ERROR(f'  /v4/sports request errored: {exc}'))
            return

        # 2. THREE-way comparison to isolate filter behavior:
        #    a. Filtered as production calls it (regions=us, commenceTimeFrom/To)
        #    b. Same filter, regions=us,us2 (region scope check)
        #    c. UNFILTERED — no time window, no markets list — the maximal
        #       view of what the API has for this sport. If a game appears
        #       here but not in (a)/(b), the bug is on OUR side (filter).
        from datetime import timedelta as _td
        from collections import Counter
        now = timezone.now()
        commence_from = (now - _td(hours=2)).strftime('%Y-%m-%dT%H:%M:%SZ')
        commence_to = (now + _td(hours=72)).strftime('%Y-%m-%dT%H:%M:%SZ')

        # Stash the responses so we can do per-gap-game searches later.
        self._preflight_filtered_events = []
        self._preflight_unfiltered_events = []

        probes = [
            ('regions=us, time-windowed (production-equivalent)',
             {'apiKey': api_key, 'regions': 'us', 'markets': 'h2h',
              'oddsFormat': 'american', 'commenceTimeFrom': commence_from,
              'commenceTimeTo': commence_to}, 'filtered'),
            ('regions=us,us2, time-windowed',
             {'apiKey': api_key, 'regions': 'us,us2', 'markets': 'h2h',
              'oddsFormat': 'american', 'commenceTimeFrom': commence_from,
              'commenceTimeTo': commence_to}, 'us2'),
            ('regions=us,us2, NO time window (unfiltered)',
             {'apiKey': api_key, 'regions': 'us,us2', 'markets': 'h2h',
              'oddsFormat': 'american'}, 'unfiltered'),
        ]
        for label, params, slot in probes:
            try:
                r = requests.get(
                    'https://api.the-odds-api.com/v4/sports/baseball_mlb/odds/',
                    params=params, timeout=15,
                )
                if r.status_code != 200:
                    self.stdout.write(self.style.WARNING(
                        f'  {label} → HTTP {r.status_code} '
                        f'body={(r.text or "")[:200]!r}'
                    ))
                    self._report_quota_headers(r.headers)
                    continue
                events = r.json() or []
                if slot == 'filtered':
                    self._preflight_filtered_events = events
                elif slot == 'unfiltered':
                    self._preflight_unfiltered_events = events

                bookmakers = set()
                events_with_books = 0
                date_buckets = Counter()
                for e in events:
                    books = e.get('bookmakers') or []
                    if books:
                        events_with_books += 1
                    for b in books:
                        bookmakers.add(b.get('title', '?'))
                    ct = (e.get('commence_time') or '')[:10]
                    if ct:
                        date_buckets[ct] += 1
                self.stdout.write(
                    f'  {label} → {len(events)} events '
                    f'({events_with_books} with books, {len(bookmakers)} unique books seen)'
                )
                if date_buckets:
                    self.stdout.write(
                        f'    date_distribution={dict(date_buckets)}'
                    )
                if bookmakers and slot != 'unfiltered':
                    self.stdout.write(
                        f'    bookmakers={sorted(bookmakers)}'
                    )
                self._report_quota_headers(r.headers)
            except Exception as exc:
                self.stdout.write(self.style.WARNING(
                    f'  {label} errored: {exc}'
                ))

        self.stdout.write('---------------------------------------------------------------')
        self.stdout.write('')

    def _report_quota_headers(self, headers):
        """Surface The Odds API's quota counters when present."""
        used = headers.get('x-requests-used')
        remaining = headers.get('x-requests-remaining')
        last = headers.get('x-requests-last')
        if used or remaining:
            self.stdout.write(
                f'    quota: used={used} remaining={remaining} last_call_cost={last}'
            )

    # -----------------------------------------------------------------------
    # Stage 1 — Odds API
    # -----------------------------------------------------------------------

    def _fetch_odds_api(self):
        """Live single-shot fetch of the Odds API. Returns the raw event
        list, or None on failure. The provider's own diagnostic logging
        also fires (mlb_odds_fetch_pipeline etc.) so the deploy log picks
        up both perspectives."""
        try:
            from apps.datahub.providers.mlb.odds_provider import MLBOddsProvider
            provider = MLBOddsProvider()
            return provider.fetch() or []
        except Exception as exc:
            logger.error('diagnose_mlb_odds_gaps_api_fetch_failed err=%s', exc)
            return None

    def _print_per_game_api_search(self, diags):
        """For each game, search the filtered + unfiltered Odds API responses.

        Two-tier match:
          - STRICT  → BOTH our home AND our away teams appear in the same
                      event (in either home/away orientation). This is a
                      true match — same matchup as our DB.
          - LENIENT → at least one team matches. Useful for spotting
                      potential schedule mismatches (the case where our
                      DB has Cardinals@Pirates but the API only has
                      Brewers@Pirates).

        Verdicts in priority order:
          1. STRICT match in filtered response  → API has it; matcher bug.
          2. STRICT match in unfiltered only    → time/markets filter bug.
          3. LENIENT hits but no STRICT match   → schedule mismatch
                                                  (probable wrong matchup
                                                  in our DB or API really
                                                  has a different opponent
                                                  for this date).
          4. No hits at all                     → API genuinely missing.
        """
        filtered = getattr(self, '_preflight_filtered_events', None) or []
        unfiltered = getattr(self, '_preflight_unfiltered_events', None) or []
        if not filtered and not unfiltered:
            return

        self.stdout.write('')
        self.stdout.write('--- Per-game search across API responses ----------------------')

        def _team_words(name):
            """Return the team's nickname (last token, two-token for Red Sox /
            White Sox / Blue Jays). Lowercase for case-insensitive contains."""
            if not name:
                return ''
            parts = name.split()
            two_word = {'sox', 'jays'}
            if parts and parts[-1].lower() in two_word and len(parts) >= 2:
                return ' '.join(parts[-2:]).lower()
            return parts[-1].lower() if parts else ''

        def _matches_team(event_team_str, target_nick):
            return bool(target_nick) and target_nick in (event_team_str or '').lower()

        def _strict_hits(events, home_name, away_name):
            """Events where BOTH our teams appear, in either orientation."""
            h_nick = _team_words(home_name)
            a_nick = _team_words(away_name)
            hits = []
            for e in events:
                eh = e.get('home_team') or ''
                ea = e.get('away_team') or ''
                # Either orientation: (our_home,our_away) == (e_home,e_away)
                # OR (our_home,our_away) == (e_away,e_home).
                same = _matches_team(eh, h_nick) and _matches_team(ea, a_nick)
                swapped = _matches_team(eh, a_nick) and _matches_team(ea, h_nick)
                if same or swapped:
                    hits.append(e)
            return hits

        def _lenient_hits(events, home_name, away_name):
            """Events where at least ONE of our teams appears (anywhere)."""
            h_nick = _team_words(home_name)
            a_nick = _team_words(away_name)
            hits = []
            for e in events:
                eh = e.get('home_team') or ''
                ea = e.get('away_team') or ''
                if (_matches_team(eh, h_nick) or _matches_team(ea, h_nick)
                        or _matches_team(eh, a_nick) or _matches_team(ea, a_nick)):
                    hits.append(e)
            return hits

        for d in diags:
            home = d.game.home_team.name
            away = d.game.away_team.name
            f_strict = _strict_hits(filtered, home, away)
            u_strict = _strict_hits(unfiltered, home, away)
            f_lenient = _lenient_hits(filtered, home, away)
            u_lenient = _lenient_hits(unfiltered, home, away)

            self.stdout.write(f'  • {d.matchup}  (DB first_pitch UTC: {d.game.first_pitch.isoformat()})')
            # Show STRICT first — that's the true-match line.
            self.stdout.write(
                f'      strict_filtered={len(f_strict)} strict_unfiltered={len(u_strict)} '
                f'lenient_filtered={len(f_lenient)} lenient_unfiltered={len(u_lenient)}'
            )
            # Print up to 3 lenient hits with details so the user can see
            # what API events actually exist for these team names.
            if u_lenient:
                self.stdout.write('      API events involving either team:')
                for ev in u_lenient[:3]:
                    self.stdout.write(
                        f'        - {ev.get("home_team")} vs {ev.get("away_team")} @ {ev.get("commence_time")}'
                    )

            # Verdict
            if f_strict:
                self.stdout.write(self.style.WARNING(
                    f'      ↳ ⚠  STRICT match exists in filtered API response — our matcher is rejecting it. Bug in our matching code.'
                ))
            elif u_strict:
                self.stdout.write(self.style.ERROR(
                    f'      ↳ ⚠  STRICT match exists ONLY in unfiltered API response. Our time/markets filter is dropping it.'
                ))
            elif u_lenient:
                self.stdout.write(self.style.ERROR(
                    f'      ↳ ⚠  Schedule/data mismatch: API has games involving these teams but NOT this matchup. Either:\n'
                    f'         (a) our DB scheduled the wrong opponent for this date (schedule provider bug), or\n'
                    f'         (b) the API hasn\'t added this matchup yet (e.g. tomorrow\'s game not listed).'
                ))
            else:
                self.stdout.write(
                    f'      ↳ Game absent from API responses entirely. Neither team appears.'
                )
        self.stdout.write('---------------------------------------------------------------')

    def _classify_api_for_game(self, d: _GameDiag, api_events: list):
        """Find the API event matching this game and fill its row in."""
        from apps.datahub.providers.mlb.odds_provider import _find_team
        match = None
        for e in api_events:
            home = e.get('home_team', '')
            away = e.get('away_team', '')
            commence = e.get('commence_time', '')
            if _api_event_matches_game(home, away, commence, d.game):
                match = e
                break

        if match is None:
            # Maybe team alias miss? Re-scan to detect.
            alias_misses = []
            for e in api_events:
                if (_norm_for_match(e.get('home_team', '')) ==
                    _norm_for_match(d.game.home_team.name)
                    or _norm_for_match(e.get('away_team', '')) ==
                    _norm_for_match(d.game.away_team.name)):
                    home_team = _find_team(e.get('home_team', ''))
                    away_team = _find_team(e.get('away_team', ''))
                    if home_team is None or away_team is None:
                        alias_misses.append(e)
            if alias_misses:
                d.api_present = 'PART'
                d.api_skip_reason = 'team_alias_miss'
                d.api_matched = 'NO'
            else:
                d.api_present = 'NO'
                d.api_matched = 'NO'
            return

        d.api_present = 'YES'
        books = match.get('bookmakers') or []
        d.api_bookmakers = len(books)
        # "Parsed" = at least one bookmaker has BOTH home and away
        # moneylines (the persist gate's pre-condition).
        ml_pair_books = 0
        for b in books:
            ml_h = ml_a = None
            for m in b.get('markets') or []:
                if m.get('key') == 'h2h':
                    for o in m.get('outcomes') or []:
                        name = o.get('name', '')
                        if name == match.get('home_team'):
                            ml_h = o.get('price')
                        elif name == match.get('away_team'):
                            ml_a = o.get('price')
            if ml_h is not None and ml_a is not None:
                ml_pair_books += 1
        d.api_parsed = 'YES' if ml_pair_books > 0 else 'NO'
        d.api_matched = 'YES'  # we matched the event back to our Game
        if ml_pair_books == 0:
            d.api_skip_reason = 'no_moneyline_pair'
        else:
            d.api_skip_reason = ''

    # -----------------------------------------------------------------------
    # Stage 2 — ESPN
    # -----------------------------------------------------------------------

    def _fetch_espn(self):
        try:
            from apps.datahub.providers.mlb.odds_espn_provider import (
                MLBEspnOddsProvider,
            )
            return MLBEspnOddsProvider().fetch() or []
        except Exception as exc:
            logger.error('diagnose_mlb_odds_gaps_espn_fetch_failed err=%s', exc)
            return None

    def _classify_espn_for_game(self, d: _GameDiag, espn_events: list):
        """Run the ESPN matcher against this game and record the outcome."""
        from apps.datahub.providers.mlb.odds_espn_provider import _find_team
        from apps.datahub.providers.mlb.odds_espn_provider import (
            _pick_best_odds_entry,
        )

        match = None
        for e in espn_events:
            comps = e.get('competitions') or []
            if not comps:
                continue
            comp = comps[0]
            competitors = comp.get('competitors') or []
            if len(competitors) != 2:
                continue
            home = next((c for c in competitors if c.get('homeAway') == 'home'), None)
            away = next((c for c in competitors if c.get('homeAway') == 'away'), None)
            if home is None or away is None:
                continue
            home_name = (home.get('team') or {}).get('displayName') or ''
            away_name = (away.get('team') or {}).get('displayName') or ''
            commence = e.get('date') or comp.get('date') or ''
            if _api_event_matches_game(home_name, away_name, commence, d.game):
                match = e
                break

        if match is None:
            d.espn_attempt = 'NO_MATCH'
            d.espn_skip_reason = 'event_not_in_espn_or_alias_miss'
            return

        # ESPN found the event. Try the same moneyline extraction path
        # the persist function would.
        comp = (match.get('competitions') or [{}])[0]
        competitors = comp.get('competitors') or []
        home = next((c for c in competitors if c.get('homeAway') == 'home'), {})
        away = next((c for c in competitors if c.get('homeAway') == 'away'), {})
        home_abbr = (home.get('team') or {}).get('abbreviation') or ''
        away_abbr = (away.get('team') or {}).get('abbreviation') or ''
        odds_list = comp.get('odds') or []
        if not odds_list:
            d.espn_attempt = 'NO_ODDS'
            d.espn_skip_reason = 'event_present_no_odds_block'
            # Probe ESPN's per-event odds endpoint as a secondary
            # source. The scoreboard's odds[] sometimes ships empty even
            # though the core-API per-event endpoint has the bookmaker
            # data. If this probe returns items we know the fix is to
            # add this endpoint as a fallback within the ESPN provider.
            self._probe_espn_per_event_odds(d, match)
            return

        _, ml_h, ml_a, is_derived = _pick_best_odds_entry(
            odds_list, home_abbr=home_abbr, away_abbr=away_abbr,
        )
        if ml_h is None and ml_a is None:
            d.espn_attempt = 'NO_ML'
            d.espn_skip_reason = 'odds_block_no_extractable_ml'
        else:
            # Both fields present (with possible inversion). is_derived
            # is the relevant trust signal.
            d.espn_attempt = 'DERIVED' if is_derived else 'OK'
            if is_derived:
                d.espn_skip_reason = 'derived_blocks_recommendation'
            else:
                d.espn_skip_reason = ''

    def _probe_espn_per_event_odds(self, d: _GameDiag, scoreboard_event):
        """Probe ESPN's per-event odds endpoint as a secondary source.

        URL pattern (sports.core.api.espn.com — different host from
        scoreboard's site.api.espn.com):
            /v2/sports/baseball/leagues/mlb/events/{event_id}/competitions/{competition_id}/odds

        Used purely as a diagnostic — we record whether the endpoint
        carries odds data when scoreboard's odds[] is empty. If this
        consistently returns items, the fix is to add this endpoint as
        a fallback within MLBEspnOddsProvider.fetch().
        """
        event_id = scoreboard_event.get('id')
        comp = (scoreboard_event.get('competitions') or [{}])[0]
        competition_id = comp.get('id') or event_id
        if not event_id:
            d.espn_skip_reason += ' (no event_id to probe)'
            return
        try:
            from apps.datahub.providers.client import APIClient
            core_client = APIClient(
                base_url='https://sports.core.api.espn.com/v2/sports/baseball/leagues/mlb',
                rate_limit_delay=0.3,
            )
            # ?limit=20 to get all bookmaker entries; default page size
            # of 1 sometimes hides additional providers.
            resp = core_client.get(
                f'/events/{event_id}/competitions/{competition_id}/odds',
                params={'limit': 20},
            )
            items = (resp or {}).get('items') or []
            count = len(items)
            d.espn_skip_reason += f' [per_event_endpoint items={count}]'
            if count > 0:
                # Stash a small sample of the first item for log review.
                import json as _json
                try:
                    first = items[0]
                    sample = _json.dumps(first, default=str)[:500]
                except Exception:
                    sample = str(items[0])[:500]
                logger.info(
                    'mlb_odds_espn_per_event_probe game_id=%s event_id=%s '
                    'items=%d sample=%s',
                    d.game.id, event_id, count, sample,
                )
                # Also surface in the table cell so the user sees it
                # without having to dig into deploy logs.
                d.espn_attempt = f'PER_EVENT={count}'
        except Exception as exc:
            logger.warning(
                'mlb_odds_espn_per_event_probe_failed game_id=%s err=%s',
                d.game.id, exc,
            )
            d.espn_skip_reason += ' [per_event_probe_failed]'

    # -----------------------------------------------------------------------
    # Stage 3 — final reason synthesis
    # -----------------------------------------------------------------------

    def _final_reason(self, d: _GameDiag) -> str:
        """Single-string explanation for THIS game's missing odds.

        Reasoning order:
          1. If status=LIVE or ΔFP < -10min → game is live; pre-game
             endpoints (Odds API + ESPN scoreboard) drop these. The
             cause is contractual, not a bug. Tagged distinctly.
          2. If API matched + parsed both moneylines → reason is something
             other than "no data" (probably a persist-time failure or a
             post-persist filter). Flag it explicitly.
          3. If API matched but didn't parse → no_moneyline_pair.
          4. If API absent → did ESPN fill or fail?
          5. If API team alias miss → tag it for alias-table extension.
        """
        # Top-priority signal: game is live or already underway. The
        # pre-game endpoints we query simply do not return live games;
        # the moneyline moves to a live endpoint that this codebase
        # doesn't currently consume.
        if d.status == 'LIVE' or d.minutes_to_pitch < -10:
            base = 'game_live_or_started_pre_game_endpoints_drop_it'
            # Carry the per-event probe result along if we got one — if
            # ESPN's per-event endpoint had data, that's still a possible
            # recovery path even for live games.
            if d.espn_attempt.startswith('PER_EVENT='):
                return f'{base} (but per-event endpoint had data — see logs)'
            return base
        if d.api_present == 'YES' and d.api_parsed == 'YES':
            # Curious — primary should have persisted this game. Either
            # the gap detector saw it BEFORE the persist completed, or
            # the persist landed a row with is_derived=True (which
            # wouldn't pass freshness gate? — it's still primary so it
            # WOULD count). Most likely: the persist actually succeeded
            # but its captured_at is older than fresh_window — i.e., the
            # row exists but is stale.
            return 'api_persisted_but_stale_or_filtered (re-check fresh window)'
        if d.api_present == 'YES' and d.api_parsed == 'NO':
            return 'api_present_no_moneyline_pair'
        if d.api_present == 'PART':
            return 'api_team_alias_miss (extend MLB_TEAM_ALIASES)'
        if d.api_present == 'NO':
            # Primary doesn't have it. Look at ESPN.
            if d.espn_attempt == 'OK':
                return 'espn_should_fill_check_persist_path'
            if d.espn_attempt == 'DERIVED':
                return 'espn_filled_but_derived (blocked from recs)'
            if d.espn_attempt == 'NO_ML':
                return 'espn_present_but_no_extractable_moneyline'
            if d.espn_attempt == 'NO_ODDS':
                return 'espn_present_but_no_odds_block'
            if d.espn_attempt.startswith('PER_EVENT='):
                # Scoreboard's odds[] was empty BUT the per-event core-API
                # endpoint had bookmaker data. This is the smoking-gun
                # signal that adding the per-event endpoint as a
                # fallback inside MLBEspnOddsProvider would close the gap.
                return 'espn_scoreboard_empty_but_per_event_endpoint_has_data'
            if d.espn_attempt == 'NO_MATCH':
                return 'absent_from_both_apis'
            if d.espn_attempt == 'ERR':
                return 'api_absent_espn_fetch_errored'
        if d.api_present == 'ERR':
            return 'api_fetch_errored'
        return 'unknown — see structured logs'

    # -----------------------------------------------------------------------
    # Output
    # -----------------------------------------------------------------------

    def _print_table(self, diags):
        # Header order matches _GameDiag.as_row().
        # DB/API column shows: distinct-books-in-DB / books-in-API for this matchup.
        headers = [
            'Game', 'Status', 'ΔFP', 'DB/API', 'Snap', 'Source',
            'API', 'Parsed', 'Matched', 'ESPN', 'Final Reason',
        ]
        rows = [d.as_row() for d in diags]
        widths = [max(len(str(c)) for c in [h] + [r[i] for r in rows])
                  for i, h in enumerate(headers)]
        # Cap matchup width
        widths[0] = min(widths[0], 36)

        def _fmt(row):
            return '  '.join(str(c).ljust(w) for c, w in zip(row, widths))

        sep = '-' * (sum(widths) + 2 * (len(widths) - 1))
        self.stdout.write('')
        self.stdout.write(_fmt(headers))
        self.stdout.write(sep)
        for r in rows:
            self.stdout.write(_fmt(r))
        self.stdout.write(sep)
        self.stdout.write(f'Total: {len(rows)} games examined.')

    def _print_all_time_coverage(self, diags):
        """All-time DB coverage per game so we can distinguish 'primary
        worked earlier and we lost it as the game went live' from
        'primary NEVER persisted for this game.'

        The main table only shows snapshots in the last 180 minutes.
        These all-time stats reveal whether the under-coverage signal
        is a refresh cadence issue (rows exist but stale) vs a primary-
        path bug (rows never existed at all).
        """
        self.stdout.write('')
        self.stdout.write('--- All-time per-game DB coverage ----------------------------')
        self.stdout.write(
            '  Format: PRIMARY books (latest age) | ESPN books (latest age) | total snapshots'
        )
        self.stdout.write(
            '  ↳ if PRIMARY books = 0, primary has NEVER persisted for this game today'
        )
        self.stdout.write('')
        for d in diags:
            primary_age = (
                f'{d.last_primary_age_min}m'
                if d.last_primary_age_min is not None else 'NEVER'
            )
            espn_age = (
                f'{d.last_espn_age_min}m'
                if d.last_espn_age_min is not None else 'NEVER'
            )
            primary_marker = ' ⚠ PRIMARY-NEVER-RAN' if d.primary_books_all_time == 0 else ''
            self.stdout.write(
                f'  • {d.matchup[:40]:<40}  '
                f'PRIMARY {d.primary_books_all_time:>2} books (latest {primary_age:>6}) | '
                f'ESPN {d.espn_books_all_time:>2} books (latest {espn_age:>6}) | '
                f'total {d.total_snapshots:>3} snaps'
                f'{primary_marker}'
            )
        self.stdout.write('--------------------------------------------------------------')

    def _print_primary_persist_dry_run(self, diags):
        """For each game, simulate exactly what MLBOddsProvider.persist
        would do. Surfaces the EXACT reason primary fails to write
        for each PRIMARY-NEVER-RAN game.

        Specifically reproduces:
          1. _find_team(api_home_name) → which Team row is returned?
          2. Compare returned team.pk to game.home_team.pk
          3. Run the persist's Game.objects.filter(...) query
          4. Show what it returns
          5. Detect duplicate Team rows + duplicate Game rows for this matchup

        Output names the actual cause per game: 'team-mismatch',
        'duplicate-game-match-wrong-pk', 'no-game-found-in-window',
        'team-not-resolved'.
        """
        filtered = getattr(self, '_preflight_filtered_events', None) or []
        if not filtered:
            return

        from apps.datahub.providers.mlb.odds_provider import _find_team
        from apps.mlb.models import Team, Game
        from datetime import timedelta

        self.stdout.write('')
        self.stdout.write('--- Primary persist DRY-RUN per game ----------------------------')
        self.stdout.write('  Reproduces what MLBOddsProvider.persist() does, without writing.')
        self.stdout.write('')

        # Pre-compute duplicate Team and Game maps so we can flag them.
        dup_teams_by_name = {}
        for t in Team.objects.values('name', 'pk'):
            dup_teams_by_name.setdefault(t['name'], []).append(t['pk'])
        dup_team_names = {n for n, pks in dup_teams_by_name.items() if len(pks) > 1}

        for d in diags:
            # Find the API event whose teams match this game's teams (strict).
            home_db = d.game.home_team
            away_db = d.game.away_team
            api_event = None
            for e in filtered:
                eh = e.get('home_team', '')
                ea = e.get('away_team', '')
                # Match by resolved Team identity, both orientations.
                if (_find_team(eh) and _find_team(ea)
                        and ((_find_team(eh).pk == home_db.pk and _find_team(ea).pk == away_db.pk)
                             or (_find_team(eh).pk == away_db.pk and _find_team(ea).pk == home_db.pk))):
                    api_event = e
                    break

            self.stdout.write(f'  • {d.matchup}  (Game pk={d.game.pk})')

            if api_event is None:
                self.stdout.write(
                    f'      ↳ No API event resolves to this Game\'s exact team pks. '
                    f'(API may have the matchup but resolve to different Team rows.)'
                )
                # Surface duplicate-team scenario explicitly:
                if home_db.name in dup_team_names:
                    self.stdout.write(self.style.ERROR(
                        f'      ↳ ⚠  DUPLICATE TEAM rows for {home_db.name!r}: pks={dup_teams_by_name[home_db.name]}'
                    ))
                if away_db.name in dup_team_names:
                    self.stdout.write(self.style.ERROR(
                        f'      ↳ ⚠  DUPLICATE TEAM rows for {away_db.name!r}: pks={dup_teams_by_name[away_db.name]}'
                    ))
                continue

            # Now mimic persist's flow exactly.
            api_home = api_event.get('home_team', '')
            api_away = api_event.get('away_team', '')
            api_commence = api_event.get('commence_time', '')

            resolved_home = _find_team(api_home)
            resolved_away = _find_team(api_away)
            self.stdout.write(
                f'      api_home={api_home!r} → resolved Team pk={resolved_home.pk if resolved_home else None} '
                f'name={resolved_home.name if resolved_home else None!r}'
            )
            self.stdout.write(
                f'      api_away={api_away!r} → resolved Team pk={resolved_away.pk if resolved_away else None} '
                f'name={resolved_away.name if resolved_away else None!r}'
            )
            self.stdout.write(
                f'      DB game.home_team.pk={home_db.pk} ({home_db.name!r}) '
                f'game.away_team.pk={away_db.pk} ({away_db.name!r})'
            )
            # Verdict: do the resolved pks match the Game's teams?
            home_match = resolved_home and resolved_home.pk == home_db.pk
            away_match = resolved_away and resolved_away.pk == away_db.pk
            home_match_swap = resolved_home and resolved_home.pk == away_db.pk
            away_match_swap = resolved_away and resolved_away.pk == home_db.pk

            if not ((home_match and away_match) or (home_match_swap and away_match_swap)):
                self.stdout.write(self.style.ERROR(
                    f'      ↳ ⚠  TEAM-PK MISMATCH: _find_team returns different '
                    f'Team rows than the Game points at. Persist will skip with '
                    f'no_game_match. Likely duplicate Team rows.'
                ))
                continue

            # Teams resolved correctly. Now run the persist's Game query.
            from django.utils.dateparse import parse_datetime
            commence_dt = parse_datetime(api_commence) if api_commence else None
            if commence_dt and timezone.is_naive(commence_dt):
                commence_dt = timezone.make_aware(commence_dt)

            if commence_dt:
                window_start = commence_dt - timedelta(hours=36)
                window_end = commence_dt + timedelta(hours=36)
                # Use whichever orientation matched.
                if home_match and away_match:
                    home_for_filter, away_for_filter = resolved_home, resolved_away
                else:
                    home_for_filter, away_for_filter = resolved_away, resolved_home
                candidate_qs = Game.objects.filter(
                    home_team=home_for_filter, away_team=away_for_filter,
                    first_pitch__gte=window_start, first_pitch__lte=window_end,
                )
                candidate_pks = list(candidate_qs.values_list('pk', flat=True))
                first = candidate_qs.order_by('first_pitch').first()
                first_pk = first.pk if first else None

                self.stdout.write(
                    f'      ±36h-window Games matching this team pair: pks={candidate_pks}'
                )
                if not candidate_pks:
                    self.stdout.write(self.style.ERROR(
                        f'      ↳ ⚠  ZERO Games match the team pair within ±36h '
                        f'of API commence_time {api_commence!r}. '
                        f'first_pitch likely outside window or wrong direction.'
                    ))
                elif len(candidate_pks) > 1:
                    self.stdout.write(self.style.ERROR(
                        f'      ↳ ⚠  DOUBLEHEADER / DUPLICATE Game rows. Persist '
                        f'picks pk={first_pk} via .order_by("first_pitch").first(). '
                        f'This Game (pk={d.game.pk}) ' +
                        ("IS the picked one — primary should write here." if first_pk == d.game.pk else
                         f"is NOT picked — primary writes to pk={first_pk} instead.")
                    ))
                elif first_pk == d.game.pk:
                    self.stdout.write(
                        f'      ↳ ✅ Primary should write to THIS game pk={first_pk}. '
                        f'If snaps are missing, the bug is downstream of game match '
                        f'(moneyline extraction or DB write).'
                    )
                    # Walk the API event's bookmakers and surface what
                    # markets each one offers. If h2h is missing across
                    # all books, persist skips with no_moneyline_home and
                    # no snapshot is written — even though everything
                    # upstream looks fine. This is the most likely cause
                    # for live games where books drop moneyline.
                    books = api_event.get('bookmakers') or []
                    book_market_summary = []
                    h2h_books = 0
                    h2h_with_pair = 0
                    for b in books:
                        title = b.get('title', '?')
                        keys = sorted({m.get('key') for m in (b.get('markets') or []) if m.get('key')})
                        has_h2h = 'h2h' in keys
                        if has_h2h:
                            h2h_books += 1
                            # Check if h2h has both home + away outcomes.
                            for m in b.get('markets') or []:
                                if m.get('key') == 'h2h':
                                    outcomes = m.get('outcomes') or []
                                    home_seen = away_seen = False
                                    for o in outcomes:
                                        n = o.get('name', '')
                                        if n == api_event.get('home_team'):
                                            home_seen = True
                                        elif n == api_event.get('away_team'):
                                            away_seen = True
                                    if home_seen and away_seen:
                                        h2h_with_pair += 1
                        book_market_summary.append(f'{title}({"+".join(keys) or "no-markets"})')
                    self.stdout.write(
                        f'      bookmakers and their markets: {book_market_summary}'
                    )
                    self.stdout.write(
                        f'      bookmakers offering h2h: {h2h_books} of {len(books)}; '
                        f'bookmakers with BOTH home+away h2h outcomes: {h2h_with_pair}'
                    )
                    if h2h_with_pair == 0:
                        self.stdout.write(self.style.ERROR(
                            f'      ↳ ⚠  NO bookmaker has a usable h2h moneyline pair. '
                            f'Persist will skip every record with reason=no_moneyline_home '
                            f'and write zero snapshots. THIS IS LIKELY YOUR BUG for live games.'
                        ))
                else:
                    self.stdout.write(self.style.ERROR(
                        f'      ↳ ⚠  Game-pk mismatch: persist picks pk={first_pk} '
                        f'but diagnostic\'s game is pk={d.game.pk}.'
                    ))
        self.stdout.write('-----------------------------------------------------------------')

    def _print_legend(self):
        self.stdout.write('')
        self.stdout.write('Legend:')
        self.stdout.write('  Status    SCHEDULED / LIVE / FINAL — game state')
        self.stdout.write('  ΔFP       Minutes from now to first_pitch (negative = past)')
        self.stdout.write('  DB/API    distinct books in DB (last 180min) / books in API for this matchup')
        self.stdout.write('            ↳ if DB much less than API, we are under-covering this game')
        self.stdout.write('  Snap      Age of most recent snapshot (minutes); "-" = no snapshot')
        self.stdout.write('  Source    Source of most recent snapshot: odds_api / espn / -')
        self.stdout.write('  ↳ if Status=LIVE or ΔFP is negative, the Odds API has likely')
        self.stdout.write('    moved this event to its live-odds endpoint and our pre-game')
        self.stdout.write('    fetch will not return it. Same applies to ESPN scoreboard.')
        self.stdout.write('  API=YES   game found in Odds API response')
        self.stdout.write('  API=NO    game NOT in Odds API response')
        self.stdout.write('  API=PART  team name appeared but alias resolution failed')
        self.stdout.write('  Parsed    at least one bookmaker had both moneylines')
        self.stdout.write('  Matched   API event matched our Game row')
        self.stdout.write('  ESPN=OK   ESPN had the game with extractable moneyline')
        self.stdout.write('  ESPN=DERIVED  ESPN single-side, opposite inverted')
        self.stdout.write('  ESPN=NO_ML    event present, no moneyline extractable')
        self.stdout.write('  ESPN=NO_MATCH event not found in ESPN scoreboard')
        self.stdout.write('  ESPN=NO_ODDS  scoreboard has event but odds[] empty')
        self.stdout.write('  ESPN=PER_EVENT=N  scoreboard empty BUT per-event endpoint had N items')
        self.stdout.write('                    (smoking gun — fallback fix would recover these)')
