"""diagnose_odds — on-demand odds-pipeline state dump.

Usage: `python manage.py diagnose_odds [--sport mlb]`

Prints counts, latest-captured timestamps, and per-day coverage so an
operator can quickly rule out "odds pipeline silently broken" without
needing a shell on the production host. Safe to run at any time.
"""
from collections import Counter
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone


class Command(BaseCommand):
    help = 'Print odds-pipeline health: counts, latest capture, per-day coverage'

    def add_arguments(self, parser):
        parser.add_argument(
            '--sport',
            choices=['cbb', 'cfb', 'mlb', 'college_baseball', 'all'],
            default='all',
        )
        parser.add_argument(
            '--days',
            type=int,
            default=3,
            help='Report per-day coverage for the next N days (default 3)',
        )

    def handle(self, *args, **options):
        sport = options['sport']
        days = options['days']
        if sport in ('all', 'mlb'):
            self._report_mlb(days)

    def _report_mlb(self, days):
        from apps.mlb.models import Game, OddsSnapshot
        now = timezone.now()
        total = OddsSnapshot.objects.count()
        latest = OddsSnapshot.objects.order_by('-captured_at').first()
        self.stdout.write(self.style.MIGRATE_HEADING(f'MLB odds diagnostic'))
        self.stdout.write(f'  total_snapshots: {total}')
        if latest:
            self.stdout.write(f'  latest_captured_at: {latest.captured_at.isoformat()}')
            self.stdout.write(f'  latest_sportsbook: {latest.sportsbook}')
            self.stdout.write(
                f'  latest_game: {latest.game.away_team.name} @ {latest.game.home_team.name} '
                f'({latest.game.first_pitch.isoformat()})'
            )
        else:
            self.stdout.write(self.style.WARNING('  NO SNAPSHOTS IN DB'))

        sportsbooks = (
            OddsSnapshot.objects.values_list('sportsbook', flat=True)[:500]
        )
        counts = Counter(sportsbooks)
        self.stdout.write(f'  sportsbooks_top5: {counts.most_common(5)}')

        self.stdout.write('  per-day coverage:')
        for i in range(days):
            day = (now + timedelta(days=i)).date()
            games = Game.objects.filter(first_pitch__date=day).count()
            with_odds = (
                Game.objects
                .filter(first_pitch__date=day, odds_snapshots__isnull=False)
                .distinct().count()
            )
            missing = games - with_odds
            line = f'    {day}: games={games} games_with_odds={with_odds} missing={missing}'
            if games and missing == games:
                self.stdout.write(self.style.WARNING(line + '  ← ZERO coverage'))
            else:
                self.stdout.write(line)
