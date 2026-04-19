"""Management command to refresh team W/L records.

Currently MLB-only. College Baseball team records are populated as part of
the ESPN schedule ingestion (same endpoint), so no separate command there.
"""

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.datahub.providers.registry import get_provider

SPORT_TOGGLES = {
    'mlb': 'LIVE_MLB_ENABLED',
}


class Command(BaseCommand):
    help = 'Refresh team W/L records (MLB only for now)'

    def add_arguments(self, parser):
        parser.add_argument(
            '--sport',
            required=True,
            choices=['mlb'],
            help='Sport to refresh team records for',
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

        self.stdout.write(f"Refreshing {sport} team records...")
        try:
            provider = get_provider(sport, 'team_record')
            stats = provider.run()
            self.stdout.write(self.style.SUCCESS(f"Done: {stats}"))
        except ValueError as e:
            raise CommandError(str(e))
