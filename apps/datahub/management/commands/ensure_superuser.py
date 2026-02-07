from django.core.management.base import BaseCommand
from django.contrib.auth.models import User


class Command(BaseCommand):
    help = 'Create admin superuser if it does not already exist (idempotent)'

    def handle(self, *args, **options):
        username = 'admin'
        email = 'admin@brotherwillies.com'

        if User.objects.filter(username=username).exists():
            self.stdout.write(f'Superuser "{username}" already exists — skipping')
            return

        User.objects.create_superuser(
            username=username,
            email=email,
            password='brotherwillies',
        )
        self.stdout.write(self.style.SUCCESS(f'Superuser "{username}" created'))
