"""Rebuild Elo ratings from scratch by chronologically replaying all final games.

Idempotent in the strong sense: running it twice produces identical final
state. Each run wipes the sport's existing TeamEloHistory and resets every
team's elo_rating before replaying — so any amount of mid-rebuild failures
or re-runs converge to the same answer as long as the underlying game
data is unchanged.

When to use:
  - First time bringing Elo online for a sport.
  - After changing K-factors, HFA, or margin formula.
  - After a data correction that affects historical scores.

Examples:
  python manage.py rebuild_elo_ratings                # all sports
  python manage.py rebuild_elo_ratings --sport mlb
"""
from django.core.management.base import BaseCommand
from django.db import transaction

from apps.core.services.elo_service import (
    SPORT_ELO_REGISTRY,
    get_game_model,
    process_game,
    reset_sport,
)


SUPPORTED = ['all'] + list(SPORT_ELO_REGISTRY.keys())


class Command(BaseCommand):
    help = (
        'Rebuild Elo ratings for one or all sports by chronologically '
        'replaying final games. Wipes existing TeamEloHistory + Elo state '
        'first; idempotent.'
    )

    def add_arguments(self, parser):
        parser.add_argument('--sport', type=str, choices=SUPPORTED, default='all')

    def handle(self, *args, **options):
        sport_arg = options['sport']
        sports = list(SPORT_ELO_REGISTRY.keys()) if sport_arg == 'all' else [sport_arg]

        for sport in sports:
            self._rebuild_sport(sport)

    def _rebuild_sport(self, sport: str):
        entry = SPORT_ELO_REGISTRY[sport]
        GameModel = get_game_model(sport)
        time_field = entry.time_field

        with transaction.atomic():
            cleared = reset_sport(sport)
            self.stdout.write(
                f"[{sport}] Reset {cleared} teams; cleared TeamEloHistory rows."
            )

            qs = (
                GameModel.objects
                .filter(status='final', home_score__isnull=False, away_score__isnull=False)
                .select_related('home_team', 'away_team')
                .order_by(time_field)
            )

            processed = 0
            skipped = 0
            for game in qs.iterator():
                if process_game(sport, game):
                    processed += 1
                else:
                    # Skipped from process_game — typically a tie or
                    # missing scores. Already-processed isn't possible
                    # here because we reset above, but the guard is cheap.
                    skipped += 1

            self.stdout.write(self.style.SUCCESS(
                f"[{sport}] Rebuild complete — processed={processed}, skipped={skipped}."
            ))
