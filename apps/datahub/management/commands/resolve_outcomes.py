"""Resolve outcomes for completed games with existing snapshots.

Populates final_outcome and closing_market_prob on ModelResultSnapshot
records for games that have finished and have scores. Designed to run
as part of the refresh_data cron cycle.

Per-sport helpers exist because each sport model has its own game-time
field name (kickoff / tipoff / first_pitch). The body of each helper is
otherwise identical — kept readable over DRY-for-DRY's-sake here.
"""

import logging

from django.core.management.base import BaseCommand
from django.db import transaction

from apps.analytics.models import ModelResultSnapshot

logger = logging.getLogger(__name__)

SUPPORTED_SPORTS = ['cfb', 'cbb', 'mlb', 'college_baseball', 'all']


class Command(BaseCommand):
    help = 'Resolve outcomes for completed games'

    def add_arguments(self, parser):
        parser.add_argument(
            '--sport',
            type=str,
            choices=SUPPORTED_SPORTS,
            default='all',
        )

    def handle(self, *args, **options):
        sport = options['sport']
        counts = {'cfb': 0, 'cbb': 0, 'mlb': 0, 'college_baseball': 0}

        if sport in ('cfb', 'all'):
            counts['cfb'] = self._resolve_cfb()
        if sport in ('cbb', 'all'):
            counts['cbb'] = self._resolve_cbb()
        if sport in ('mlb', 'all'):
            counts['mlb'] = self._resolve_by_fk(
                fk='mlb_game', time_field='first_pitch',
            )
        if sport in ('college_baseball', 'all'):
            counts['college_baseball'] = self._resolve_by_fk(
                fk='college_baseball_game', time_field='first_pitch',
            )

        self.stdout.write(self.style.SUCCESS(
            f"Resolved {counts['cfb']} CFB + {counts['cbb']} CBB + "
            f"{counts['mlb']} MLB + {counts['college_baseball']} CB outcomes"
        ))

    def _resolve_cfb(self):
        return self._resolve_by_fk(fk='game', time_field='kickoff')

    def _resolve_cbb(self):
        return self._resolve_by_fk(fk='cbb_game', time_field='tipoff')

    def _resolve_by_fk(self, fk, time_field):
        """Shared resolver parameterized by FK column + time field name."""
        filter_kwargs = {
            f'{fk}__isnull': False,
            'final_outcome__isnull': True,
            f'{fk}__status': 'final',
            f'{fk}__home_score__isnull': False,
            f'{fk}__away_score__isnull': False,
        }
        snapshots = (
            ModelResultSnapshot.objects.filter(**filter_kwargs)
            .select_related(fk)
            .prefetch_related(f'{fk}__odds_snapshots')
        )

        resolved = 0
        for snap in snapshots:
            game = getattr(snap, fk)
            if game is None:
                continue
            home_won = game.home_score > game.away_score
            game_time = getattr(game, time_field)

            closing_odds = (
                game.odds_snapshots.filter(captured_at__lt=game_time)
                .order_by('-captured_at')
                .first()
            )
            closing_prob = closing_odds.market_home_win_prob if closing_odds else None

            try:
                with transaction.atomic():
                    snap.final_outcome = home_won
                    snap.closing_market_prob = closing_prob
                    snap.save()
                resolved += 1
            except Exception as e:
                logger.error(f'Failed to resolve {fk} snapshot {snap.id}: {e}')

        return resolved
