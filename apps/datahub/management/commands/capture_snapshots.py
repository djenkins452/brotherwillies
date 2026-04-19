"""Capture model prediction snapshots for upcoming games.

Creates ModelResultSnapshot records for games approaching first pitch /
kickoff / tipoff that have odds data but no existing snapshot. Designed
to run as part of the refresh_data cron cycle.

All team-sports share the same snapshot shape; per-sport branching is
only used because each sport has its own Game model and FK column on
ModelResultSnapshot.
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
from apps.college_baseball.models import Game as CBaseGame
from apps.mlb.models import Game as MLBGame

logger = logging.getLogger(__name__)

DEFAULT_WINDOW_HOURS = 24

SUPPORTED_SPORTS = ['cfb', 'cbb', 'mlb', 'college_baseball', 'all']


class Command(BaseCommand):
    help = 'Capture model snapshots for upcoming games'

    def add_arguments(self, parser):
        parser.add_argument(
            '--sport',
            type=str,
            choices=SUPPORTED_SPORTS,
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

        counts = {'cfb': 0, 'cbb': 0, 'mlb': 0, 'college_baseball': 0}

        if sport in ('cfb', 'all'):
            counts['cfb'] = self._capture_cfb(now, cutoff)
        if sport in ('cbb', 'all'):
            counts['cbb'] = self._capture_cbb(now, cutoff)
        if sport in ('mlb', 'all'):
            counts['mlb'] = self._capture_mlb(now, cutoff)
        if sport in ('college_baseball', 'all'):
            counts['college_baseball'] = self._capture_college_baseball(now, cutoff)

        self.stdout.write(self.style.SUCCESS(
            f"Captured {counts['cfb']} CFB + {counts['cbb']} CBB + "
            f"{counts['mlb']} MLB + {counts['college_baseball']} CB snapshots"
        ))

    # --- CFB / CBB: unchanged behavior ---

    def _capture_cfb(self, now, cutoff):
        games = CFBGame.objects.filter(
            status='scheduled',
            kickoff__gte=now,
            kickoff__lte=cutoff,
        ).select_related('home_team', 'away_team').prefetch_related(
            'odds_snapshots', 'injuries', 'result_snapshots',
        )
        captured = 0
        for game in games:
            if game.result_snapshots.exists():
                continue
            latest_odds = game.odds_snapshots.first()
            if not latest_odds:
                continue
            try:
                with transaction.atomic():
                    ModelResultSnapshot.objects.create(
                        game=game,
                        market_prob=latest_odds.market_home_win_prob,
                        house_prob=cfb_house_prob(game),
                        house_model_version='v1',
                        data_confidence=cfb_confidence(game, latest_odds),
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
            'odds_snapshots', 'injuries', 'result_snapshots',
        )
        captured = 0
        for game in games:
            if game.result_snapshots.exists():
                continue
            latest_odds = game.odds_snapshots.first()
            if not latest_odds:
                continue
            try:
                with transaction.atomic():
                    ModelResultSnapshot.objects.create(
                        cbb_game=game,
                        market_prob=latest_odds.market_home_win_prob,
                        house_prob=cbb_house_prob(game),
                        house_model_version='v1',
                        data_confidence=cbb_confidence(game, latest_odds),
                    )
                captured += 1
            except Exception as e:
                logger.error(f'Failed to capture CBB snapshot for {game.id}: {e}')
        return captured

    # --- Baseball: use the same pattern, keyed by first_pitch ---

    def _capture_mlb(self, now, cutoff):
        # Import lazily so Phase 3 can replace the model service without churn here.
        from apps.mlb.services.model_service import (
            compute_data_confidence as mlb_confidence,
            compute_house_win_prob as mlb_house_prob,
        )
        games = MLBGame.objects.filter(
            status='scheduled',
            first_pitch__gte=now,
            first_pitch__lte=cutoff,
        ).select_related('home_team', 'away_team').prefetch_related(
            'odds_snapshots', 'injuries', 'result_snapshots',
        )
        captured = 0
        for game in games:
            if game.result_snapshots.exists():
                continue
            latest_odds = game.odds_snapshots.first()
            if not latest_odds:
                continue
            try:
                with transaction.atomic():
                    ModelResultSnapshot.objects.create(
                        mlb_game=game,
                        market_prob=latest_odds.market_home_win_prob,
                        house_prob=mlb_house_prob(game),
                        house_model_version='v1',
                        data_confidence=mlb_confidence(game, latest_odds),
                    )
                captured += 1
            except Exception as e:
                logger.error(f'Failed to capture MLB snapshot for {game.id}: {e}')
        return captured

    def _capture_college_baseball(self, now, cutoff):
        from apps.college_baseball.services.model_service import (
            compute_data_confidence as cb_confidence,
            compute_house_win_prob as cb_house_prob,
        )
        games = CBaseGame.objects.filter(
            status='scheduled',
            first_pitch__gte=now,
            first_pitch__lte=cutoff,
        ).select_related('home_team', 'away_team').prefetch_related(
            'odds_snapshots', 'injuries', 'result_snapshots',
        )
        captured = 0
        for game in games:
            if game.result_snapshots.exists():
                continue
            latest_odds = game.odds_snapshots.first()
            if not latest_odds:
                continue
            try:
                with transaction.atomic():
                    ModelResultSnapshot.objects.create(
                        college_baseball_game=game,
                        market_prob=latest_odds.market_home_win_prob,
                        house_prob=cb_house_prob(game),
                        house_model_version='v1',
                        data_confidence=cb_confidence(game, latest_odds),
                    )
                captured += 1
            except Exception as e:
                logger.error(f'Failed to capture CB snapshot for {game.id}: {e}')
        return captured
