"""v3.3 SHADOW — backfill TeamBullpenSnapshot from RelieverAppearance data.

Walks the MLB Game universe chronologically and, for each unique
(team, first_pitch) pair, computes the bullpen snapshot that would
have been known immediately BEFORE first_pitch. Writes append-only
`TeamBullpenSnapshot` rows via the deterministic
`apps.mlb.services.bullpen_builder.build_snapshot`.

DETERMINISM

  Same input (raw appearances) → identical snapshots. The builder
  and this command are the SAME code path used by daily updates, so
  historical and forward computations cannot drift.

DEPENDENCY

  This command reads `RelieverAppearance` rows and writes
  `TeamBullpenSnapshot` rows. It does NOT hit any external API.
  Run `ingest_reliever_appearances` first to populate the raw data.

USAGE

  # Backfill snapshots for every completed game in a window:
  python manage.py backfill_bullpen_snapshots --start 2026-05-01 --end 2026-08-21

  # Daily forward — snapshots for today's scheduled games (each anchored
  # on that game's first_pitch, so "state entering the game"):
  python manage.py backfill_bullpen_snapshots --today

  # Force-overwrite existing snapshots (default: skip existing):
  python manage.py backfill_bullpen_snapshots --start 2026-05-01 --refresh
"""
import logging
from datetime import date, datetime, timedelta

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone


logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = (
        'v3.3 SHADOW: build TeamBullpenSnapshot rows from ingested '
        'RelieverAppearance data. Deterministic, idempotent.'
    )

    def add_arguments(self, parser):
        parser.add_argument('--start', type=str)
        parser.add_argument('--end', type=str)
        parser.add_argument(
            '--today', action='store_true',
            help='Shortcut for start=end=today (daily forward mode).',
        )
        parser.add_argument(
            '--refresh', action='store_true',
            help='Overwrite existing snapshots for the same (team, as_of).',
        )
        parser.add_argument(
            '--quality-days', type=int, default=30,
            help='Rolling window for quality metrics (default 30).',
        )
        parser.add_argument(
            '--dry-run', action='store_true',
        )

    def handle(self, *args, **options):
        from apps.mlb.models import Game, TeamBullpenSnapshot
        from apps.mlb.services.bullpen_builder import (
            build_snapshot, persist_snapshot,
        )

        date_from, date_to = self._resolve_dates(options)

        games = list(
            Game.objects.filter(
                first_pitch__date__gte=date_from,
                first_pitch__date__lte=date_to,
            )
            .select_related('home_team', 'away_team')
            .order_by('first_pitch')
        )

        self.stdout.write(
            f'backfill_bullpen_snapshots: {len(games)} game(s) '
            f'from {date_from}..{date_to}, quality_days='
            f'{options["quality_days"]}'
        )

        created = 0
        skipped = 0
        for i, g in enumerate(games, 1):
            for team in (g.home_team, g.away_team):
                # Idempotency guard: same (team, as_of) → skip unless --refresh.
                if not options.get('refresh'):
                    if TeamBullpenSnapshot.objects.filter(
                        team=team, as_of=g.first_pitch,
                    ).exists():
                        skipped += 1
                        continue
                if options.get('dry_run'):
                    built = build_snapshot(
                        team, g.first_pitch,
                        quality_days=options['quality_days'],
                    )
                    logger.debug(
                        'DRY-RUN snapshot %s %s: era=%s whip=%s ip30=%s '
                        'apps1d=%d apps2d=%d apps3d=%d confidence=%s',
                        team.abbreviation or team.slug, g.first_pitch,
                        built.bullpen_era, built.bullpen_whip,
                        built.bullpen_ip_last30,
                        built.appearances_last_1_day,
                        built.appearances_last_2_days,
                        built.appearances_last_3_days,
                        built.data_confidence,
                    )
                    created += 1
                    continue
                persist_snapshot(
                    team, g.first_pitch,
                    quality_days=options['quality_days'],
                )
                created += 1
            if i % 50 == 0:
                self.stdout.write(
                    f'  progress: {i}/{len(games)}  '
                    f'created={created} skipped={skipped}'
                )

        self.stdout.write(self.style.SUCCESS(
            f'Done. created={created} skipped={skipped}'
        ))

    def _resolve_dates(self, options):
        if options.get('today'):
            t = timezone.localdate()
            return t, t
        raw_start = options.get('start')
        if not raw_start:
            raise CommandError('--start required unless --today given.')
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
