"""v3.3 SHADOW — RelieverAppearance ingestion from MLB Stats API boxscores.

Walks `/api/v1/schedule?sportId=1&startDate=..&endDate=..` for a date
range, then `/api/v1/game/{gamePk}/boxscore` per completed game, and
writes one `RelieverAppearance` row per pitcher per game (upsert on
`(game, pitcher)` unique constraint).

Same command handles both historical backfill and daily forward
updates — no drift between historical reconstruction and forward
computation.

INVARIANTS

  * APPEND-ONLY at the semantic level. The unique constraint on
    (game, pitcher) makes re-runs UPDATE-in-place so the same
    boxscore always produces the same row — idempotent. New games
    add new rows only.

  * NO PRODUCTION SIDE EFFECTS. This command writes only to
    `RelieverAppearance`. No `Game` / `Team` / `StartingPitcher`
    fields are modified except `StartingPitcher.rating` when a
    NEW pitcher is discovered (rating defaults to 50; keeps
    downstream services happy). Existing pitchers are matched by
    `(source, external_id)` and never touched.

  * RATE LIMIT AWARE. MLB Stats API is unauthenticated and generous
    but not unlimited. This command sleeps `--sleep-ms` between
    boxscore fetches (default 250ms → 4 req/sec, well under any
    reasonable ceiling). A 30-day backfill of all MLB (~450 games)
    takes ~2 minutes.

  * RESTARTABLE. `--skip-existing` (default true) skips gamePks that
    already have appearance rows. Killing and re-running picks up
    where it left off. Combined with idempotent upsert, this is safe
    to re-run at any time.

USAGE

  # Yesterday's games only (daily cron):
  python manage.py ingest_reliever_appearances --yesterday

  # Historical backfill of a specific window:
  python manage.py ingest_reliever_appearances \
      --start 2026-05-01 --end 2026-08-21

  # Rate-limit polite backfill (default is already polite):
  python manage.py ingest_reliever_appearances \
      --start 2026-05-01 --end 2026-08-21 --sleep-ms 500

  # Full-refresh a specific gamePk (skips skip-existing check):
  python manage.py ingest_reliever_appearances --gamepk 822780 --refresh

FIELDS EXTRACTED FROM BOXSCORE

  Per pitcher in home.pitchers + away.pitchers:
    is_starter          = stats.pitching.gamesStarted == 1
    outs_recorded       = int(inningsPitched * 3)  (0.1 IP = 1 out, 0.2 = 2 outs)
    pitches             = numberOfPitches or pitchesThrown
    hits                = hits
    earned_runs         = earnedRuns
    walks               = baseOnBalls
    strikeouts          = strikeOuts
    home_runs           = homeRuns
    is_save             = saves > 0
    is_hold             = holds > 0

WHAT IS DELIBERATELY NOT DONE

  * We do NOT upsert Game rows. If the schedule contains games not
    already in our Game table (e.g. spring training, a team not in
    our slate), they are SKIPPED with a log. `ingest_schedule` is
    responsible for the Game universe.

  * We do NOT ingest starters' appearances. `is_starter=True` rows
    are still written because they are useful cross-checks (aggregate
    starters + relievers should equal team totals for verification)
    and cost nothing extra — the boxscore returns them anyway.
    The bullpen builder filters `is_starter=False`.

  * We do NOT try to identify "high-leverage" relievers at ingest
    time. That's a builder-time computation (season-to-date
    saves+holds) so it stays reactive to trades and role changes.
"""
import logging
import time
from datetime import date, datetime, timedelta

import requests
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.datahub.providers.mlb.statsapi_client import (
    StatsApiError, fetch_boxscore, fetch_schedule,
)


logger = logging.getLogger(__name__)


def _ip_to_outs(ip_str) -> int:
    """Convert MLB Stats API inningsPitched string ("3.1", "0.2", "5.0")
    to total outs. 1.0 IP = 3 outs; the fractional part is the number
    of additional outs (1 or 2), NOT decimal thirds.
    """
    if ip_str is None:
        return 0
    try:
        whole_str, _, frac_str = str(ip_str).partition('.')
        whole = int(whole_str) if whole_str else 0
        # Fractional part in MLB Stats API is 0/1/2 (outs).
        frac = int(frac_str) if frac_str else 0
    except (TypeError, ValueError):
        return 0
    return whole * 3 + frac


def _extract_pitcher_stats(player_dict):
    """Extract the pitching-stat fields we care about from a boxscore
    pitcher block. Missing fields default to 0."""
    ps = (player_dict.get('stats') or {}).get('pitching') or {}
    return {
        'is_starter': int(ps.get('gamesStarted') or 0) >= 1,
        'outs_recorded': _ip_to_outs(ps.get('inningsPitched')),
        'pitches': (ps.get('numberOfPitches')
                    if ps.get('numberOfPitches') is not None
                    else ps.get('pitchesThrown')),
        'hits': int(ps.get('hits') or 0),
        'earned_runs': int(ps.get('earnedRuns') or 0),
        'walks': int(ps.get('baseOnBalls') or 0),
        'strikeouts': int(ps.get('strikeOuts') or 0),
        'home_runs': int(ps.get('homeRuns') or 0),
        'is_save': int(ps.get('saves') or 0) >= 1,
        'is_hold': int(ps.get('holds') or 0) >= 1,
    }


def _get_or_create_pitcher(mlb_person_id: int, name: str, team):
    """Find StartingPitcher by (source, external_id). Create with default
    rating (50) if new. Never updates an existing pitcher's fields."""
    from apps.mlb.models import StartingPitcher
    external_id = str(mlb_person_id)
    obj, created = StartingPitcher.objects.get_or_create(
        source='mlb_stats_api', external_id=external_id,
        defaults={
            'name': name or f'MLB-{mlb_person_id}',
            'team': team,
            'rating': 50.0,
        },
    )
    return obj, created


class Command(BaseCommand):
    help = (
        'v3.3 SHADOW: ingest per-pitcher game appearances from MLB '
        'Stats API boxscores into RelieverAppearance. Idempotent, '
        'restartable, rate-limit aware. No production side effects.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--start', type=str,
            help='Start date YYYY-MM-DD (inclusive). Required unless '
                 '--yesterday or --gamepk is used.',
        )
        parser.add_argument(
            '--end', type=str,
            help='End date YYYY-MM-DD (inclusive). Defaults to --start.',
        )
        parser.add_argument(
            '--yesterday', action='store_true',
            help='Shortcut for start=end=yesterday (daily cron mode).',
        )
        parser.add_argument(
            '--gamepk', type=int,
            help='Ingest a specific gamePk. Overrides date range.',
        )
        parser.add_argument(
            '--sleep-ms', type=int, default=250,
            help='Milliseconds to sleep between boxscore fetches. '
                 'Default 250 (4 req/sec).',
        )
        parser.add_argument(
            '--refresh', action='store_true',
            help='Re-fetch boxscores even for gamePks that already have '
                 'appearance rows (default: skip).',
        )
        parser.add_argument(
            '--dry-run', action='store_true',
            help='Fetch + parse but do not write anything.',
        )
        parser.add_argument(
            '--max-games', type=int, default=0,
            help='Cap the number of games ingested (0 = no cap). '
                 'Useful for feasibility runs.',
        )

    def handle(self, *args, **options):
        base = settings.MLB_STATSAPI_BASE_URL

        # --- Resolve date range or gamePk ---
        if options.get('gamepk'):
            gamepks = [options['gamepk']]
            date_from = date_to = None
        else:
            date_from, date_to = self._resolve_dates(options)
            gamepks = self._list_gamepks(base, date_from, date_to)

        if options.get('max_games') and options['max_games'] > 0:
            gamepks = gamepks[:options['max_games']]

        self.stdout.write(
            f'ingest_reliever_appearances: {len(gamepks)} gamePk(s) '
            f'{"(dry-run) " if options["dry_run"] else ""}'
            f'from {date_from or "gamepk"}..{date_to or "gamepk"}'
        )

        skipped_no_game = 0
        skipped_existing = 0
        created = 0
        updated = 0
        for i, gamepk in enumerate(gamepks, 1):
            n_created, n_updated, skip_reason = self._ingest_game(
                base, gamepk, options,
            )
            if skip_reason == 'no_game':
                skipped_no_game += 1
            elif skip_reason == 'existing':
                skipped_existing += 1
            else:
                created += n_created
                updated += n_updated
            if i % 20 == 0:
                self.stdout.write(
                    f'  progress: {i}/{len(gamepks)}  '
                    f'created={created} updated={updated} '
                    f'skipped_existing={skipped_existing} '
                    f'skipped_no_game={skipped_no_game}'
                )
            if options['sleep_ms'] > 0 and i < len(gamepks):
                time.sleep(options['sleep_ms'] / 1000.0)

        self.stdout.write(self.style.SUCCESS(
            f'Done. created={created} updated={updated} '
            f'skipped_existing={skipped_existing} '
            f'skipped_no_game={skipped_no_game}'
        ))

    # -----------------------------------------------------------------
    # Helpers

    def _resolve_dates(self, options):
        if options.get('yesterday'):
            y = timezone.localdate() - timedelta(days=1)
            return y, y
        raw_start = options.get('start')
        if not raw_start:
            raise CommandError(
                '--start required unless --yesterday or --gamepk given.'
            )
        try:
            start = datetime.strptime(raw_start, '%Y-%m-%d').date()
        except ValueError:
            raise CommandError(f'--start must be YYYY-MM-DD (got {raw_start})')
        raw_end = options.get('end') or raw_start
        try:
            end = datetime.strptime(raw_end, '%Y-%m-%d').date()
        except ValueError:
            raise CommandError(f'--end must be YYYY-MM-DD (got {raw_end})')
        if end < start:
            raise CommandError('--end must be >= --start')
        return start, end

    def _list_gamepks(self, base, date_from, date_to):
        """Chunked /schedule via the canonical statsapi_client.
        Filters to FINAL games only."""
        try:
            all_games = fetch_schedule(date_from, date_to)
        except StatsApiError as e:
            raise CommandError(f'schedule fetch failed: {e.human_summary()}')
        gamepks = []
        for g in all_games:
            status = (g.get('status') or {}).get('detailedState', '')
            if status not in ('Final', 'Completed Early', 'Game Over'):
                continue
            gpk = g.get('gamePk')
            if gpk:
                gamepks.append(int(gpk))
        return gamepks

    def _ingest_game(self, base, gamepk, options):
        """Fetch one boxscore, write appearances. Returns
        (n_created, n_updated, skip_reason_or_None)."""
        from apps.mlb.models import Game, RelieverAppearance

        game = Game.objects.filter(
            source='mlb_stats_api', external_id=str(gamepk),
        ).select_related('home_team', 'away_team').first()
        if game is None:
            return 0, 0, 'no_game'

        # Skip if already ingested (unless --refresh).
        if not options.get('refresh'):
            if RelieverAppearance.objects.filter(game=game).exists():
                return 0, 0, 'existing'

        try:
            data = fetch_boxscore(gamepk)
        except StatsApiError as e:
            logger.warning('boxscore fetch failed gamepk=%s: %s', gamepk, e.human_summary())
            return 0, 0, 'no_game'

        n_created = n_updated = 0
        for side, team in (('home', game.home_team), ('away', game.away_team)):
            side_block = (data.get('teams') or {}).get(side) or {}
            pitchers_list = side_block.get('pitchers') or []
            players_map = side_block.get('players') or {}
            for pid in pitchers_list:
                player = players_map.get(f'ID{pid}') or {}
                person = player.get('person') or {}
                stats = _extract_pitcher_stats(player)
                if options.get('dry_run'):
                    n_created += 1
                    continue
                pitcher, _ = _get_or_create_pitcher(
                    pid, person.get('fullName', ''), team,
                )
                obj, created_bool = RelieverAppearance.objects.update_or_create(
                    game=game, pitcher=pitcher,
                    defaults={
                        'team': team,
                        **stats,
                    },
                )
                if created_bool:
                    n_created += 1
                else:
                    n_updated += 1
        return n_created, n_updated, None
