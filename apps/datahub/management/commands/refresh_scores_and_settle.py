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

Every run is wrapped in a CronRunLog row so the ops command center can
report on its history.
"""
import logging

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import BaseCommand

from apps.ops.services.cron_logging import cron_run_log


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
        parser.add_argument('--trigger', choices=['cron', 'manual', 'deploy'], default='cron')
        parser.add_argument('--triggered-by-user-id', type=int, default=None)

    def handle(self, *args, **options):
        sport = options['sport']

        triggered_by = None
        if options.get('triggered_by_user_id'):
            User = get_user_model()
            triggered_by = User.objects.filter(pk=options['triggered_by_user_id']).first()

        with cron_run_log(
            'refresh_scores_and_settle',
            trigger=options.get('trigger', 'cron'),
            triggered_by_user=triggered_by,
        ) as log:
            stdout_lines = []
            failures = []

            def _emit(line):
                stdout_lines.append(line)
                self.stdout.write(line)

            # 1. Score-only update (no odds, no model recompute)
            from apps.datahub.services.scores import update_scores_only
            _emit(f'Step 1: score-only update (sport={sport})')
            try:
                score_results = update_scores_only(sport=sport)
                for key, stats in score_results.items():
                    _emit(f'  {key}: {stats}')
            except Exception as e:
                _emit(f'  score update failed: {e}')
                failures.append(('score_update', str(e)))

            # 2. Resolve outcomes (per-sport FK-based — safe to call for 'all')
            _emit('Step 2: resolve_outcomes')
            try:
                call_command('resolve_outcomes', sport='all' if sport == 'all' else sport)
            except Exception as e:
                _emit(f'  resolve_outcomes failed: {e}')
                failures.append(('resolve_outcomes', str(e)))

            # 3. Settle mock bets (idempotent — safe to run repeatedly)
            _emit('Step 3: settle_mockbets')
            try:
                call_command('settle_mockbets', sport='all' if sport == 'all' else sport)
            except Exception as e:
                _emit(f'  settle_mockbets failed: {e}')
                failures.append(('settle_mockbets', str(e)))

            _emit('refresh_scores_and_settle complete')

            log.summary = f'sport={sport} failures={len(failures)}'
            log.stdout_tail = '\n'.join(stdout_lines)
            if failures:
                detail = '; '.join(f'{name}: {err[:200]}' for name, err in failures)
                log.mark_partial(detail)
