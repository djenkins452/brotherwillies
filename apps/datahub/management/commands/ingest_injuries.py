"""Management command to ingest injury data for a sport."""

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.datahub.providers.registry import get_provider

SPORT_TOGGLES = {
    'cbb': 'LIVE_CBB_ENABLED',
    'cfb': 'LIVE_CFB_ENABLED',
}

SUPPORTED_SPORTS = ['cbb', 'cfb']


class Command(BaseCommand):
    help = 'Ingest injury data from external APIs (CFB and CBB only)'

    def add_arguments(self, parser):
        parser.add_argument(
            '--sport',
            required=True,
            choices=SUPPORTED_SPORTS,
            help='Sport to ingest (cbb, cfb)',
        )
        parser.add_argument(
            '--force',
            action='store_true',
            help='Run even if live data toggle is disabled',
        )

    def handle(self, *args, **options):
        sport = options['sport']
        force = options['force']

        if not force:
            if not settings.LIVE_DATA_ENABLED:
                raise CommandError(
                    "LIVE_DATA_ENABLED is false. Use --force to override."
                )
            toggle = SPORT_TOGGLES.get(sport)
            if toggle and not getattr(settings, toggle, False):
                raise CommandError(
                    f"{toggle} is false. Use --force to override."
                )

        self.stdout.write(f"Ingesting {sport} injuries...")
        try:
            provider = get_provider(sport, 'injuries')
            stats = provider.run()
            self.stdout.write(self.style.SUCCESS(
                f"Done: {stats}"
            ))
        except ValueError as e:
            raise CommandError(str(e))
