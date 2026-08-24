"""v3.2 forward-validation capture — CLI wrapper for use in cron.

2026-08-24 update: wrapped in `cron_run_log('capture_v3_2_validation')`
so standalone Railway cron entries produce their own CronRunLog rows
(previously only chained inside refresh_data — those runs' log rows
were owned by 'refresh_data', not 'capture_v3_2_validation').

Idempotent: one snapshot per (mlb_game, engine_version). Safe to run
at any frequency — off-window games and already-captured games are
no-ops. Repeated ticks NEVER duplicate.

Preferred Railway cadence: every 10-15 minutes (comfortably smaller
than the 30-minute canonical window). Chained call inside refresh_data
remains as harmless defense-in-depth.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = ('Capture one canonical decision-time snapshot per MLB game '
            'inside the T-60min ±15min pregame window (idempotent).')

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true',
                            help='Report what would be captured; write nothing.')
        parser.add_argument('--trigger', type=str, default='cron',
                            choices=['cron', 'manual', 'deploy'])

    def handle(self, *_, **opts):
        from apps.analytics.services.v3_2_capture import (
            capture_pending, MIN_WINDOW_MIN, MAX_WINDOW_MIN, ENGINE_VERSION,
        )
        from apps.analytics.services.v3_2_settlement import settle_pending
        from apps.ops.services.cron_logging import cron_run_log

        # Wrap in cron_run_log so this row is discoverable independently
        # of refresh_data. The dedicated Railway cron entry runs the
        # bare command; its logs become the source of truth for capture
        # cadence auditing.
        with cron_run_log(
            'capture_v3_2_validation',
            trigger=opts.get('trigger', 'cron'),
        ) as log:
            result = capture_pending(dry_run=opts.get('dry_run', False))
            capture_summary = (
                f'engine={ENGINE_VERSION} '
                f'window=[T+{MIN_WINDOW_MIN}min, T+{MAX_WINDOW_MIN}min] '
                f'candidates={result["candidates_in_window"]} '
                f'created={result["captured"]} '
                f'already={result["already_captured"]} '
                f'dry_run={result["dry_run"]}'
            )
            self.stdout.write(f'capture_v3_2_validation: {capture_summary}')

            settle_summary = ''
            if not opts.get('dry_run'):
                s = settle_pending()
                settle_summary = (
                    f'attempted={s["attempted"]} '
                    f'settled={s["settled"]} skipped={s["skipped"]}'
                )
                self.stdout.write(f'settle_v3_2_validation: {settle_summary}')

            log.summary = (f'capture: {capture_summary}'
                           + (f' | settle: {settle_summary}'
                              if settle_summary else ''))
            log.stdout_tail = f'{capture_summary}\n{settle_summary}'
