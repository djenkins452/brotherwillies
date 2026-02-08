from django.conf import settings
from django.core.management import call_command
from django.core.management.base import BaseCommand
from apps.cfb.models import Conference as CFBConference
from apps.cbb.models import Conference as CBBConference


class Command(BaseCommand):
    help = 'Run seed_demo if DB is empty, then run live data ingestion if enabled'

    def handle(self, *args, **options):
        if CFBConference.objects.exists() and CBBConference.objects.exists():
            self.stdout.write('Seed data already present — skipping')
        else:
            self.stdout.write('No seed data found — running seed_demo...')
            call_command('seed_demo')
            self.stdout.write(self.style.SUCCESS('Seed data loaded'))

        # Ensure feedback components exist
        call_command('seed_feedback')

        # Run live data ingestion if enabled
        if not settings.LIVE_DATA_ENABLED:
            self.stdout.write('Live data disabled — skipping ingestion')
            return

        sports_config = [
            ('cbb', 'LIVE_CBB_ENABLED', True),
            ('cfb', 'LIVE_CFB_ENABLED', True),
            ('golf', 'LIVE_GOLF_ENABLED', False),
        ]

        for sport, toggle, has_injuries in sports_config:
            if not getattr(settings, toggle, False):
                self.stdout.write(f'{toggle} disabled — skipping {sport}')
                continue

            self.stdout.write(f'Ingesting {sport} live data...')
            try:
                call_command('ingest_schedule', sport=sport, force=True)
                call_command('ingest_odds', sport=sport, force=True)
                if has_injuries:
                    call_command('ingest_injuries', sport=sport, force=True)
                self.stdout.write(self.style.SUCCESS(f'{sport} ingestion complete'))
            except Exception as e:
                self.stdout.write(self.style.WARNING(
                    f'{sport} ingestion failed: {e} — continuing'
                ))
