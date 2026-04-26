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
    """Mutable state container per gap game. The command fills the fields
    in pipeline order, then prints them as one row of the output table."""

    def __init__(self, game):
        self.game = game
        self.matchup = f'{game.away_team.name} @ {game.home_team.name}'
        # Stage 1 — Odds API
        self.api_present = '?'
        self.api_bookmakers = '-'
        self.api_parsed = '-'
        self.api_matched = '-'
        self.api_skip_reason = ''
        # Stage 2 — ESPN
        self.espn_attempt = '-'
        self.espn_skip_reason = ''
        # Stage 3 — final
        self.final_reason = ''

    def as_row(self):
        return [
            self.matchup[:36],
            self.api_present,
            str(self.api_bookmakers),
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

    # -----------------------------------------------------------------------
    # Stage 3 — final reason synthesis
    # -----------------------------------------------------------------------

    def _final_reason(self, d: _GameDiag) -> str:
        """Single-string explanation for THIS game's missing odds.

        Reasoning order:
          1. If API matched + parsed both moneylines → reason is something
             other than "no data" (probably a persist-time failure or a
             post-persist filter). Flag it explicitly.
          2. If API matched but didn't parse → no_moneyline_pair.
          3. If API absent → did ESPN fill or fail?
          4. If API team alias miss → tag it for alias-table extension.
        """
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
        headers = ['Game', 'API', 'Books', 'Parsed', 'Matched', 'ESPN', 'Final Reason']
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

    def _print_legend(self):
        self.stdout.write('')
        self.stdout.write('Legend:')
        self.stdout.write('  API=YES   game found in Odds API response')
        self.stdout.write('  API=NO    game NOT in Odds API response')
        self.stdout.write('  API=PART  team name appeared but alias resolution failed')
        self.stdout.write('  Parsed    at least one bookmaker had both moneylines')
        self.stdout.write('  Matched   API event matched our Game row')
        self.stdout.write('  ESPN=OK   ESPN had the game with extractable moneyline')
        self.stdout.write('  ESPN=DERIVED  ESPN single-side, opposite inverted')
        self.stdout.write('  ESPN=NO_ML    event present, no moneyline extractable')
        self.stdout.write('  ESPN=NO_MATCH event not found in ESPN scoreboard')
        self.stdout.write('  ESPN=NO_ODDS  event in ESPN but no odds block')
