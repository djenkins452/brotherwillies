"""Management command to refresh starting-pitcher season stats.

Currently MLB-only. Runs on its own cadence (daily recommended) so that
the schedule refresh cycle doesn't pay for extra API calls per tick.
"""

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.datahub.providers.registry import get_provider

SPORT_TOGGLES = {
    'mlb': 'LIVE_MLB_ENABLED',
}


class Command(BaseCommand):
    help = 'Refresh starting pitcher season stats (MLB only for now)'

    def add_arguments(self, parser):
        parser.add_argument(
            '--sport',
            required=True,
            choices=['mlb'],
            help='Sport to refresh pitcher stats for',
        )
        parser.add_argument('--force', action='store_true')

    def handle(self, *args, **options):
        sport = options['sport']
        force = options['force']

        if not force:
            if not settings.LIVE_DATA_ENABLED:
                raise CommandError("LIVE_DATA_ENABLED is false. Use --force to override.")
            toggle = SPORT_TOGGLES.get(sport)
            if toggle and not getattr(settings, toggle, False):
                raise CommandError(f"{toggle} is false. Use --force to override.")

        self.stdout.write(f"Refreshing {sport} pitcher stats...")
        try:
            provider = get_provider(sport, 'pitcher_stats')
            stats = provider.run()
            self.stdout.write(self.style.SUCCESS(f"Done: {stats}"))
        except ValueError as e:
            raise CommandError(str(e))
