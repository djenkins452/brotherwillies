"""Backfill historical MockBet snapshot/CLV fields.

Default mode is dry-run — operators can preview the impact before any DB
writes. Pass `--commit` to actually persist.

Examples:
    python manage.py backfill_mockbets               # dry run, prints summary
    python manage.py backfill_mockbets --commit      # persists changes
    python manage.py backfill_mockbets --user dan    # scope to one user

Idempotent — running twice is safe; the second run only touches bets that
were still missing data on the first pass.
"""
from django.contrib.auth.models import User
from django.core.management.base import BaseCommand

from apps.mockbets.models import MockBet
from apps.mockbets.services.backfill import backfill_mockbet_data


class Command(BaseCommand):
    help = 'Backfill historical MockBet closing odds + CLV + recommendation snapshot fields.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--commit',
            action='store_true',
            help='Persist changes. Without this flag, runs as a dry-run.',
        )
        parser.add_argument(
            '--user',
            type=str,
            default=None,
            help='Limit to a single username (e.g. --user demo).',
        )

    def handle(self, *args, **options):
        dry_run = not options['commit']
        username = options.get('user')

        qs = MockBet.objects.all()
        if username:
            try:
                user = User.objects.get(username=username)
            except User.DoesNotExist:
                self.stderr.write(self.style.ERROR(f'User not found: {username}'))
                return
            qs = qs.filter(user=user)

        mode = 'DRY RUN' if dry_run else 'COMMIT'
        scope = f'user={username}' if username else 'all users'
        self.stdout.write(f'Backfill mode: {mode} | scope: {scope}')

        summary = backfill_mockbet_data(dry_run=dry_run, queryset=qs)

        # Headline summary line
        self.stdout.write(self.style.SUCCESS('=' * 50))
        self.stdout.write(f"Processed: {summary['processed']} bets")
        self.stdout.write(f"Closing odds filled: {summary['closing_odds_filled']}")
        self.stdout.write(f"CLV computed: {summary['clv_computed']}")
        self.stdout.write(f"Rec snapshot filled (from linked BettingRecommendation): {summary['rec_snapshot_filled']}")
        self.stdout.write(f"Skipped (no pre-game odds snapshot): {summary['skipped_no_odds']}")
        self.stdout.write(f"Skipped (no recommendation row to copy from): {summary['skipped_no_snapshot']}")
        if dry_run:
            self.stdout.write(self.style.WARNING(
                'No changes written (dry run). Re-run with --commit to persist.'
            ))
        else:
            self.stdout.write(self.style.SUCCESS('Changes committed.'))
