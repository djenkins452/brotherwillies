"""Capture model prediction snapshots for upcoming games.

Creates ModelResultSnapshot records for games approaching kickoff/tipoff
that have odds data but no existing snapshot. Designed to run as part of
the refresh_data cron cycle.
"""

import logging
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from apps.analytics.models import ModelResultSnapshot
from apps.cbb.models import Game as CBBGame
from apps.cbb.services.model_service import (
    compute_data_confidence as cbb_confidence,
    compute_house_win_prob as cbb_house_prob,
)
from apps.cfb.models import Game as CFBGame
from apps.cfb.services.model_service import (
    compute_data_confidence as cfb_confidence,
    compute_house_win_prob as cfb_house_prob,
)

logger = logging.getLogger(__name__)

DEFAULT_WINDOW_HOURS = 24


class Command(BaseCommand):
    help = 'Capture model snapshots for upcoming games'

    def add_arguments(self, parser):
        parser.add_argument(
            '--sport',
            type=str,
            choices=['cfb', 'cbb', 'all'],
            default='all',
        )
        parser.add_argument(
            '--window-hours',
            type=int,
            default=DEFAULT_WINDOW_HOURS,
            help=f'Hours before game to capture (default: {DEFAULT_WINDOW_HOURS})',
        )

    def handle(self, *args, **options):
        sport = options['sport']
        window = options['window_hours']
        now = timezone.now()
        cutoff = now + timedelta(hours=window)

        cfb_count = 0
        cbb_count = 0

        if sport in ('cfb', 'all'):
            cfb_count = self._capture_cfb(now, cutoff)

        if sport in ('cbb', 'all'):
            cbb_count = self._capture_cbb(now, cutoff)

        self.stdout.write(
            self.style.SUCCESS(f'Captured {cfb_count} CFB + {cbb_count} CBB snapshots')
        )

    def _capture_cfb(self, now, cutoff):
        games = CFBGame.objects.filter(
            status='scheduled',
            kickoff__gte=now,
            kickoff__lte=cutoff,
        ).select_related('home_team', 'away_team').prefetch_related(
            'odds_snapshots', 'injuries', 'result_snapshots'
        )

        captured = 0
        for game in games:
            if game.result_snapshots.exists():
                continue

            latest_odds = game.odds_snapshots.first()
            if not latest_odds:
                continue

            try:
                house_prob = cfb_house_prob(game)
                confidence = cfb_confidence(game, latest_odds)

                with transaction.atomic():
                    ModelResultSnapshot.objects.create(
                        game=game,
                        market_prob=latest_odds.market_home_win_prob,
                        house_prob=house_prob,
                        house_model_version='v1',
                        data_confidence=confidence,
                    )
                captured += 1
            except Exception as e:
                logger.error(f'Failed to capture CFB snapshot for {game.id}: {e}')

        return captured

    def _capture_cbb(self, now, cutoff):
        games = CBBGame.objects.filter(
            status='scheduled',
            tipoff__gte=now,
            tipoff__lte=cutoff,
        ).select_related('home_team', 'away_team').prefetch_related(
            'odds_snapshots', 'injuries', 'result_snapshots'
        )

        captured = 0
        for game in games:
            if game.result_snapshots.exists():
                continue

            latest_odds = game.odds_snapshots.first()
            if not latest_odds:
                continue

            try:
                house_prob = cbb_house_prob(game)
                confidence = cbb_confidence(game, latest_odds)

                with transaction.atomic():
                    ModelResultSnapshot.objects.create(
                        cbb_game=game,
                        market_prob=latest_odds.market_home_win_prob,
                        house_prob=house_prob,
                        house_model_version='v1',
                        data_confidence=confidence,
                    )
                captured += 1
            except Exception as e:
                logger.error(f'Failed to capture CBB snapshot for {game.id}: {e}')

        return captured
