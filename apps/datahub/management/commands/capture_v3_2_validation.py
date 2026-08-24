"""v3.2 forward-validation capture — CLI wrapper for use in cron.

Chained into `refresh_data` so it runs on every cycle. Idempotent:
one snapshot per (mlb_game, engine_version). Safe to run any
frequency — off-window games and already-captured games are no-ops.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = ('Capture one canonical decision-time snapshot per MLB game '
            'inside the T-60min ±15min pregame window (idempotent).')

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true',
                            help='Report what would be captured; write nothing.')

    def handle(self, *_, **opts):
        from apps.analytics.services.v3_2_capture import (
            capture_pending, MIN_WINDOW_MIN, MAX_WINDOW_MIN, ENGINE_VERSION,
        )
        from apps.analytics.services.v3_2_settlement import settle_pending

        result = capture_pending(dry_run=opts.get('dry_run', False))
        self.stdout.write(
            f'capture_v3_2_validation: engine={ENGINE_VERSION} '
            f'window=[T+{MIN_WINDOW_MIN}min, T+{MAX_WINDOW_MIN}min] '
            f'candidates={result["candidates_in_window"]} '
            f'created={result["captured"]} '
            f'already={result["already_captured"]} '
            f'dry_run={result["dry_run"]}'
        )

        # Attach settlement to every unsettled snapshot whose game is
        # now final. Cheap when there's nothing to do.
        if not opts.get('dry_run'):
            s = settle_pending()
            self.stdout.write(
                f'settle_v3_2_validation: attempted={s["attempted"]} '
                f'settled={s["settled"]} skipped={s["skipped"]}'
            )
