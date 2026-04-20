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

        # Ensure golfer list is populated
        call_command('seed_golfers')

        # Ensure golf events with fields and odds exist
        call_command('seed_golf_events')

        # Run live data ingestion if enabled
        if not settings.LIVE_DATA_ENABLED:
            self.stdout.write('Live data disabled — skipping ingestion')
            return

        sports_config = [
            # (sport, toggle, has_injuries, has_pitcher_stats, has_team_records)
            ('cbb',              'LIVE_CBB_ENABLED',              True,  False, False),
            ('cfb',              'LIVE_CFB_ENABLED',              True,  False, False),
            ('golf',             'LIVE_GOLF_ENABLED',             False, False, False),
            ('mlb',              'LIVE_MLB_ENABLED',              True,  True,  True),
            ('college_baseball', 'LIVE_COLLEGE_BASEBALL_ENABLED', False, False, False),
        ]

        for sport, toggle, has_injuries, has_pitcher_stats, has_team_records in sports_config:
            if not getattr(settings, toggle, False):
                self.stdout.write(f'{toggle} disabled — skipping {sport}')
                continue

            self.stdout.write(f'Ingesting {sport} live data...')
            try:
                call_command('ingest_schedule', sport=sport, force=True)
                call_command('ingest_odds', sport=sport, force=True)
                if has_injuries:
                    call_command('ingest_injuries', sport=sport, force=True)
                if has_pitcher_stats:
                    call_command('ingest_pitcher_stats', sport=sport, force=True)
                if has_team_records:
                    call_command('ingest_team_records', sport=sport, force=True)
                self.stdout.write(self.style.SUCCESS(f'{sport} ingestion complete'))
            except Exception as e:
                self.stdout.write(self.style.WARNING(
                    f'{sport} ingestion failed: {e} — continuing'
                ))

        # --- post-run diagnostic block ---------------------------------------
        # Prints a summary of odds coverage for each sport in the deploy log
        # so operators can see at a glance whether ingestion worked. Without
        # this, silent zero-odds states were invisible until users complained.
        self._print_odds_diagnostic()

    def _print_odds_diagnostic(self):
        """Emit a per-sport odds health summary to the deploy log."""
        from django.utils import timezone
        today = timezone.localdate()
        self.stdout.write('--- odds health summary ---')
        try:
            from apps.mlb.models import OddsSnapshot as MLBOdds, Game as MLBGame
            total = MLBOdds.objects.count()
            today_count = MLBOdds.objects.filter(game__first_pitch__date=today).count()
            games_today = MLBGame.objects.filter(first_pitch__date=today).count()
            games_today_with_odds = MLBGame.objects.filter(
                first_pitch__date=today, odds_snapshots__isnull=False
            ).distinct().count()
            self.stdout.write(
                f'MLB odds: total={total} today_snapshots={today_count} '
                f'today_games={games_today} today_games_with_odds={games_today_with_odds}'
            )
        except Exception as e:
            self.stdout.write(f'MLB odds diagnostic failed: {e}')
        try:
            from apps.cbb.models import OddsSnapshot as CBBOdds
            self.stdout.write(f'CBB odds: total={CBBOdds.objects.count()}')
        except Exception as e:
            self.stdout.write(f'CBB odds diagnostic failed: {e}')
        try:
            from apps.cfb.models import OddsSnapshot as CFBOdds
            self.stdout.write(f'CFB odds: total={CFBOdds.objects.count()}')
        except Exception as e:
            self.stdout.write(f'CFB odds diagnostic failed: {e}')
