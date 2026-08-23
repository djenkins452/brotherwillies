"""v3.4 team-offense phase 2 — ingest team-level season-to-date hitting.

Thin CLI wrapper around the async orchestration in
`apps.analytics.services.team_batting_backfill_service`. Creates a
`TeamBattingBackfillRun` row and runs the backfill INLINE (not
threaded) so cron / operator commands surface errors immediately.

USAGE
  # Daily forward refresh (yesterday only — leakage-safe):
  python manage.py ingest_team_batting --daily

  # Backfill a specific date range:
  python manage.py ingest_team_batting --date-from 2026-03-01 --date-to 2026-08-22

  # Single date (backfill hole):
  python manage.py ingest_team_batting --date 2026-06-15

CRON RECOMMENDATION
  Once daily, ~3 hours after last game finishes (so the day's boxscore
  processing has settled at MLB). --daily writes snapshots for
  yesterday's date (leakage-safe for tomorrow's games).
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone


class Command(BaseCommand):
    help = 'Ingest team-level season-to-date hitting into TeamBattingSnapshot.'

    def add_arguments(self, parser):
        parser.add_argument('--date', type=str, default='',
                            help='YYYY-MM-DD — single date to ingest')
        parser.add_argument('--date-from', type=str, default='',
                            help='YYYY-MM-DD — range start')
        parser.add_argument('--date-to', type=str, default='',
                            help='YYYY-MM-DD — range end (inclusive)')
        parser.add_argument('--daily', action='store_true',
                            help='Ingest yesterday only (cron-safe default)')
        parser.add_argument('--kind', type=str, default='historical',
                            choices=['historical', 'daily'])
        parser.add_argument('--sleep-ms', type=int, default=100,
                            help='Per-request sleep (throttle). Default 100ms.')
        parser.add_argument('--trigger', type=str, default='manual',
                            help='For cron/observability tagging.')

    def handle(self, *_, **opts):
        from apps.analytics.models import TeamBattingBackfillRun
        from apps.analytics.services.team_batting_backfill_service import (
            run_team_batting_backfill,
        )

        date_from, date_to, kind = self._resolve_window(opts)
        run = TeamBattingBackfillRun.objects.create(
            kind=kind, date_from=date_from, date_to=date_to,
        )
        self.stdout.write(
            f'Created TeamBattingBackfillRun {run.id} '
            f'{date_from}..{date_to} (kind={kind})'
        )
        # Inline execution — command surfaces errors, cron log captures them.
        run_team_batting_backfill(str(run.id), sleep_ms=opts['sleep_ms'])
        run.refresh_from_db()
        self.stdout.write(
            f'Run {run.status}: created={run.snapshots_created} '
            f'updated={run.snapshots_updated} '
            f'errors={run.fetches_errored} '
            f'empty={run.fetches_empty} '
            f'elapsed={run.elapsed_seconds}s'
        )

    def _resolve_window(self, opts):
        kind = opts['kind']
        if opts['daily']:
            y = timezone.localdate() - timedelta(days=1)
            return y, y, 'daily'
        if opts['date']:
            d = self._parse(opts['date'])
            return d, d, kind
        if opts['date_from'] or opts['date_to']:
            if not (opts['date_from'] and opts['date_to']):
                raise CommandError(
                    '--date-from and --date-to must be given together',
                )
            df = self._parse(opts['date_from'])
            dt = self._parse(opts['date_to'])
            if dt < df:
                raise CommandError('--date-to must be >= --date-from')
            return df, dt, kind
        raise CommandError(
            'Specify --daily, --date, or --date-from + --date-to',
        )

    @staticmethod
    def _parse(s: str) -> date:
        try:
            return datetime.strptime(s, '%Y-%m-%d').date()
        except ValueError as e:
            raise CommandError(f'Bad date {s!r}: {e}')
