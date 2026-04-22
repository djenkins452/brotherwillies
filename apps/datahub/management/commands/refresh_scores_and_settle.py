"""Lightweight 15-minute pipeline: scores + outcomes + bet settlement.

Designed to run frequently without touching odds, models, or snapshots:
    1. Score-only update (status + home_score + away_score for in-window games)
    2. resolve_outcomes — flips ModelResultSnapshot.final_outcome on final games
    3. settle_mockbets — idempotent settlement of pending MockBets

The heavy `refresh_data` pipeline (every ~6h) continues to own schedule
rebuilds, odds ingest, injuries, pitcher stats, and snapshot capture. This
command only exists to keep user-visible state (game result + bet result)
current between heavy refreshes.

Railway cron: schedule this every 15 minutes:
    python manage.py refresh_scores_and_settle
"""
import logging

from django.core.management import call_command
from django.core.management.base import BaseCommand


logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Lightweight refresh: score-only update, resolve outcomes, settle mock bets'

    def add_arguments(self, parser):
        parser.add_argument(
            '--sport',
            type=str,
            choices=['mlb', 'college_baseball', 'all'],
            default='all',
            help='Limit to a single sport (default: all supported sports)',
        )

    def handle(self, *args, **options):
        sport = options['sport']

        # 1. Score-only update (no odds, no model recompute)
        from apps.datahub.services.scores import update_scores_only
        self.stdout.write(f'Step 1: score-only update (sport={sport})')
        try:
            score_results = update_scores_only(sport=sport)
            for key, stats in score_results.items():
                self.stdout.write(f'  {key}: {stats}')
        except Exception as e:
            # Never abort — step 2+3 still catch any finals already in the DB.
            self.stdout.write(self.style.WARNING(f'  score update failed: {e}'))

        # 2. Resolve outcomes (per-sport FK-based — safe to call for 'all')
        self.stdout.write('Step 2: resolve_outcomes')
        try:
            call_command('resolve_outcomes', sport='all' if sport == 'all' else sport)
        except Exception as e:
            self.stdout.write(self.style.WARNING(f'  resolve_outcomes failed: {e}'))

        # 3. Settle mock bets (idempotent — safe to run repeatedly)
        self.stdout.write('Step 3: settle_mockbets')
        try:
            call_command('settle_mockbets', sport='all' if sport == 'all' else sport)
        except Exception as e:
            self.stdout.write(self.style.WARNING(f'  settle_mockbets failed: {e}'))

        self.stdout.write(self.style.SUCCESS('refresh_scores_and_settle complete'))
