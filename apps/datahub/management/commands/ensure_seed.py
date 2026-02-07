from django.core.management import call_command
from django.core.management.base import BaseCommand
from apps.cfb.models import Conference


class Command(BaseCommand):
    help = 'Run seed_demo only if the database has no conferences yet (idempotent)'

    def handle(self, *args, **options):
        if Conference.objects.exists():
            self.stdout.write('Seed data already present — skipping')
            return

        self.stdout.write('No seed data found — running seed_demo...')
        call_command('seed_demo')
        self.stdout.write(self.style.SUCCESS('Seed data loaded'))
