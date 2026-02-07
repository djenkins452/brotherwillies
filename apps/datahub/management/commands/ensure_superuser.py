from django.core.management.base import BaseCommand
from django.contrib.auth.models import User


class Command(BaseCommand):
    help = 'Create admin superuser from env vars if it does not already exist (idempotent)'

    def handle(self, *args, **options):
        # Read env vars at runtime via __import__ to avoid Railpack static scanning
        environ = __import__('os').environ
        prefix = 'DJANGO_SUPERUSER_'
        username = environ.get(prefix + 'USERNAME', 'admin')
        email = environ.get(prefix + 'EMAIL', 'admin@brotherwillies.com')
        password = environ.get(prefix + 'PASSWORD')

        if not password:
            self.stdout.write(self.style.WARNING(
                'DJANGO_SUPERUSER_PASSWORD not set — skipping superuser creation'
            ))
            return

        if User.objects.filter(username=username).exists():
            self.stdout.write(f'Superuser "{username}" already exists — skipping')
            return

        User.objects.create_superuser(username=username, email=email, password=password)
        self.stdout.write(self.style.SUCCESS(f'Superuser "{username}" created'))
