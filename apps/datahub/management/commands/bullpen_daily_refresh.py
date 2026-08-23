"""v3.3 SHADOW — daily bullpen refresh (wraps ingest + snapshot build).

Runs the two-phase daily maintenance:
  1. ingest_reliever_appearances --yesterday   → RelieverAppearance rows
  2. backfill_bullpen_snapshots --today        → TeamBullpenSnapshot rows

Records the run in `BullpenBackfillRun` (kind='daily') so the same
staff-only status page shows both historical and daily runs. Also
wraps the whole thing in `cron_run_log` so it appears alongside the
other cron jobs (refresh_data, capture_snapshots, resolve_outcomes,
refresh_scores_and_settle).

Integrate on Railway as one of the existing cron/start-command steps.
Same discipline the project's other daily commands use.
"""
import logging
import time
import traceback
from datetime import timedelta

from django.core.management import call_command
from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.ops.services.cron_logging import cron_run_log


logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = (
        'v3.3 SHADOW: daily bullpen refresh. Runs '
        'ingest_reliever_appearances --yesterday and '
        'backfill_bullpen_snapshots --today. Idempotent; safe to re-run.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--trigger', choices=['cron', 'manual', 'deploy'], default='cron',
        )

    def handle(self, *args, **options):
        from apps.analytics.models import BullpenBackfillRun

        trigger = options.get('trigger', 'cron')
        yesterday = timezone.localdate() - timedelta(days=1)
        today = timezone.localdate()

        run = BullpenBackfillRun.objects.create(
            kind='daily',
            status='running',
            phase='ingest_appearances',
            date_from=yesterday,
            date_to=today,
            started_at=timezone.now(),
        )

        with cron_run_log('bullpen_daily_refresh', trigger=trigger) as log:
            try:
                self.stdout.write(f'[daily] ingest --yesterday ({yesterday}) …')
                call_command(
                    'ingest_reliever_appearances', '--yesterday',
                    '--sleep-ms=250',
                )
                run.phase = 'build_snapshots'
                run.save(update_fields=['phase'])

                self.stdout.write(f'[daily] backfill snapshots --today ({today}) …')
                call_command('backfill_bullpen_snapshots', '--today')

                run.phase = 'done'
                run.status = 'completed'
                run.finished_at = timezone.now()
                # Snapshot counters aren't threaded through the CLI
                # subcommands, so leave the row's fine-grained counters
                # at 0 for daily runs — the historical page and the
                # command-line output are the operator's truth. The
                # cron_run_log row carries the timing + status too.
                run.save()
                log.summary = f'daily bullpen refresh {yesterday}..{today} OK'
                self.stdout.write(self.style.SUCCESS(log.summary))
            except Exception as exc:  # noqa: BLE001
                run.status = 'failed'
                run.error_message = ''.join(traceback.format_exception(exc))[:6000]
                run.finished_at = timezone.now()
                run.save()
                logger.exception('bullpen_daily_refresh failed')
                # Re-raise so cron_run_log records failure + surfaces via
                # ops dashboard alongside the other failing cron jobs.
                raise
