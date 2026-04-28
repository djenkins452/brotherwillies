"""Incrementally update Elo ratings for newly-finalized games.

Idempotent: each game is processed at most once because `process_game`
checks for an existing TeamEloHistory row before applying the update.
Designed for cron — safe to run on every refresh cycle.

Examples:
  python manage.py update_elo_ratings              # all sports
  python manage.py update_elo_ratings --sport mlb
"""
from django.core.management.base import BaseCommand
from django.db import transaction

from apps.analytics.models import TeamEloHistory
from apps.core.services.elo_service import (
    SPORT_ELO_REGISTRY,
    get_game_model,
    process_game,
)


SUPPORTED = ['all'] + list(SPORT_ELO_REGISTRY.keys())


class Command(BaseCommand):
    help = (
        'Incrementally update Elo ratings for final games not yet in '
        'TeamEloHistory. Idempotent.'
    )

    def add_arguments(self, parser):
        parser.add_argument('--sport', type=str, choices=SUPPORTED, default='all')

    def handle(self, *args, **options):
        sport_arg = options['sport']
        sports = list(SPORT_ELO_REGISTRY.keys()) if sport_arg == 'all' else [sport_arg]

        for sport in sports:
            self._update_sport(sport)

    def _update_sport(self, sport: str):
        entry = SPORT_ELO_REGISTRY[sport]
        GameModel = get_game_model(sport)

        # Pull the set of game ids already represented in TeamEloHistory.
        # Pre-filtering on the server side keeps the iteration cheap even
        # on a multi-season backlog.
        already_processed = set(
            TeamEloHistory.objects
            .filter(sport=sport)
            .exclude(**{f'{entry.history_game_fk}__isnull': True})
            .values_list(f'{entry.history_game_fk}_id', flat=True)
            .distinct()
        )

        candidate_qs = (
            GameModel.objects
            .filter(status='final', home_score__isnull=False, away_score__isnull=False)
            .exclude(id__in=already_processed)
            .select_related('home_team', 'away_team')
            .order_by(entry.time_field)
        )

        processed = 0
        skipped = 0
        with transaction.atomic():
            for game in candidate_qs.iterator():
                if process_game(sport, game):
                    processed += 1
                else:
                    # process_game returned False — score equal, missing
                    # scores, or another concurrent update beat us to it.
                    skipped += 1

        self.stdout.write(self.style.SUCCESS(
            f"[{sport}] Update complete — processed={processed}, skipped={skipped}."
        ))
