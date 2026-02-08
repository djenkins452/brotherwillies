"""Resolve outcomes for completed games with existing snapshots.

Populates final_outcome and closing_market_prob on ModelResultSnapshot
records for games that have finished and have scores. Designed to run
as part of the refresh_data cron cycle.
"""

import logging

from django.core.management.base import BaseCommand
from django.db import transaction

from apps.analytics.models import ModelResultSnapshot

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Resolve outcomes for completed games'

    def add_arguments(self, parser):
        parser.add_argument(
            '--sport',
            type=str,
            choices=['cfb', 'cbb', 'all'],
            default='all',
        )

    def handle(self, *args, **options):
        sport = options['sport']

        cfb_count = 0
        cbb_count = 0

        if sport in ('cfb', 'all'):
            cfb_count = self._resolve_cfb()

        if sport in ('cbb', 'all'):
            cbb_count = self._resolve_cbb()

        self.stdout.write(
            self.style.SUCCESS(f'Resolved {cfb_count} CFB + {cbb_count} CBB outcomes')
        )

    def _resolve_cfb(self):
        snapshots = ModelResultSnapshot.objects.filter(
            game__isnull=False,
            final_outcome__isnull=True,
            game__status='final',
            game__home_score__isnull=False,
            game__away_score__isnull=False,
        ).select_related('game').prefetch_related('game__odds_snapshots')

        resolved = 0
        for snap in snapshots:
            game = snap.game
            home_won = game.home_score > game.away_score

            # Closing line = last odds captured before kickoff
            closing_odds = game.odds_snapshots.filter(
                captured_at__lt=game.kickoff
            ).order_by('-captured_at').first()
            closing_prob = closing_odds.market_home_win_prob if closing_odds else None

            try:
                with transaction.atomic():
                    snap.final_outcome = home_won
                    snap.closing_market_prob = closing_prob
                    snap.save()
                resolved += 1
            except Exception as e:
                logger.error(f'Failed to resolve CFB snapshot {snap.id}: {e}')

        return resolved

    def _resolve_cbb(self):
        snapshots = ModelResultSnapshot.objects.filter(
            cbb_game__isnull=False,
            final_outcome__isnull=True,
            cbb_game__status='final',
            cbb_game__home_score__isnull=False,
            cbb_game__away_score__isnull=False,
        ).select_related('cbb_game').prefetch_related('cbb_game__odds_snapshots')

        resolved = 0
        for snap in snapshots:
            game = snap.cbb_game
            home_won = game.home_score > game.away_score

            closing_odds = game.odds_snapshots.filter(
                captured_at__lt=game.tipoff
            ).order_by('-captured_at').first()
            closing_prob = closing_odds.market_home_win_prob if closing_odds else None

            try:
                with transaction.atomic():
                    snap.final_outcome = home_won
                    snap.closing_market_prob = closing_prob
                    snap.save()
                resolved += 1
            except Exception as e:
                logger.error(f'Failed to resolve CBB snapshot {snap.id}: {e}')

        return resolved
