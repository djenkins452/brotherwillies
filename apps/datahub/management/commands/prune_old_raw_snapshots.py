"""Prune raw OddsSnapshot rows older than the retention window.

Keeps:
  - snapshot_type='raw' rows newer than --days (default 14)
  - ALL snapshot_type='significant' rows (movement signal — never pruned)
  - ALL snapshot_type='closing' rows (CLV reference — never pruned)
  - ALL snapshot_type='bet_context' rows (anchored to a user mock bet)

Why: every refresh pull stores a snapshot per (game, sportsbook), so the
raw table grows ~hundreds of rows per refresh. Without pruning the table
would be many millions of rows within a season. Significant/closing/
bet_context rows are the ones we actually use for analytics, and they're
a tiny fraction of the volume.

Idempotent. Safe to run on every refresh_data invocation. Logs counts
per sport.

Usage:
    python manage.py prune_old_raw_snapshots
    python manage.py prune_old_raw_snapshots --days 7
    python manage.py prune_old_raw_snapshots --dry-run
"""
import logging
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.cbb.models import OddsSnapshot as CBBOddsSnapshot
from apps.cfb.models import OddsSnapshot as CFBOddsSnapshot
from apps.college_baseball.models import OddsSnapshot as CBOddsSnapshot
from apps.golf.models import GolfOddsSnapshot
from apps.mlb.models import OddsSnapshot as MLBOddsSnapshot

logger = logging.getLogger(__name__)

# (label, model_class) — extend here when adding sports.
SNAPSHOT_MODELS = [
    ('mlb', MLBOddsSnapshot),
    ('cfb', CFBOddsSnapshot),
    ('cbb', CBBOddsSnapshot),
    ('college_baseball', CBOddsSnapshot),
    ('golf', GolfOddsSnapshot),
]


class Command(BaseCommand):
    help = 'Prune raw OddsSnapshot rows older than --days (default 14).'

    def add_arguments(self, parser):
        parser.add_argument(
            '--days', type=int, default=14,
            help='Retention window in days for raw snapshots (default 14).',
        )
        parser.add_argument(
            '--dry-run', action='store_true',
            help='Report what would be deleted without deleting.',
        )

    def handle(self, *args, **options):
        days = options['days']
        dry_run = options['dry_run']
        cutoff = timezone.now() - timedelta(days=days)

        total_deleted = 0
        for label, model in SNAPSHOT_MODELS:
            qs = model.objects.filter(snapshot_type='raw', captured_at__lt=cutoff)
            count = qs.count()
            if dry_run:
                self.stdout.write(f'  {label}: would delete {count} raw rows older than {days}d')
            else:
                deleted, _ = qs.delete()
                self.stdout.write(f'  {label}: deleted {deleted} raw rows older than {days}d')
                total_deleted += deleted

        verb = 'would delete' if dry_run else 'deleted'
        self.stdout.write(self.style.SUCCESS(
            f'prune_old_raw_snapshots {verb} {total_deleted} rows total (cutoff {cutoff:%Y-%m-%d %H:%M})'
        ))
