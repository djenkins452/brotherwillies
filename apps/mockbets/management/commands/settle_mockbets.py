"""Settle pending mock bets for finalized games.

Designed to run as part of the refresh_data cron cycle or standalone.
Idempotent — safe to run repeatedly.
"""

import logging

from django.core.management.base import BaseCommand

from apps.mockbets.services.settlement import settle_pending_bets

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Settle pending mock bets for finalized games'

    def add_arguments(self, parser):
        parser.add_argument(
            '--sport',
            type=str,
            choices=['cfb', 'cbb', 'golf', 'all'],
            default='all',
        )

    def handle(self, *args, **options):
        sport = options['sport']
        counts = settle_pending_bets(sport=sport)

        total = sum(counts.values())
        self.stdout.write(
            self.style.SUCCESS(
                f'Settled {total} mock bets '
                f'(CFB: {counts["cfb"]}, CBB: {counts["cbb"]}, Golf: {counts["golf"]})'
            )
        )
