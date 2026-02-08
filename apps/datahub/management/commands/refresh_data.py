"""Management command to refresh all live data — designed to run as a cron job.

Runs schedule, odds, and injury ingestion for all enabled sports.
Exits cleanly so Railway cron service can restart on schedule.
"""

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import BaseCommand


SPORTS_CONFIG = [
    ('cbb', 'LIVE_CBB_ENABLED', True),
    ('cfb', 'LIVE_CFB_ENABLED', True),
    ('golf', 'LIVE_GOLF_ENABLED', False),
]


class Command(BaseCommand):
    help = 'Refresh all live data (schedule, odds, injuries) for enabled sports'

    def handle(self, *args, **options):
        if not settings.LIVE_DATA_ENABLED:
            self.stdout.write('LIVE_DATA_ENABLED is false — nothing to do')
            return

        for sport, toggle, has_injuries in SPORTS_CONFIG:
            if not getattr(settings, toggle, False):
                continue

            self.stdout.write(f'Refreshing {sport}...')
            try:
                call_command('ingest_schedule', sport=sport, force=True)
                call_command('ingest_odds', sport=sport, force=True)
                if has_injuries:
                    call_command('ingest_injuries', sport=sport, force=True)
                self.stdout.write(self.style.SUCCESS(f'{sport} done'))
            except Exception as e:
                self.stdout.write(self.style.WARNING(f'{sport} failed: {e}'))

        self.stdout.write(self.style.SUCCESS('Refresh complete'))
