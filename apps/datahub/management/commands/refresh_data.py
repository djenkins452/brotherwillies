"""Management command to refresh all live data — designed to run as a cron job.

Runs schedule, odds, and (for supported sports) injuries + pitcher stats
ingestion for all enabled sports. Exits cleanly so Railway cron service
can restart on schedule.

Each sport is configured with:
    (key, toggle_setting, has_injuries, has_pitcher_stats)
"""

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import BaseCommand


SPORTS_CONFIG = [
    # (sport_key,            toggle_setting,                 injuries, pitcher_stats, team_records)
    ('cbb',              'LIVE_CBB_ENABLED',                 True,     False,         False),
    ('cfb',              'LIVE_CFB_ENABLED',                 True,     False,         False),
    ('golf',             'LIVE_GOLF_ENABLED',                False,    False,         False),
    ('mlb',              'LIVE_MLB_ENABLED',                 False,    True,          True),
    ('college_baseball', 'LIVE_COLLEGE_BASEBALL_ENABLED',    False,    False,         False),
]

# Sports the existing capture_snapshots / resolve_outcomes commands understand.
# Baseball sports flow through their own per-sport branches (Phase 8 analytics).
SNAPSHOT_ELIGIBLE_SPORTS = {'cfb', 'cbb', 'mlb', 'college_baseball'}


class Command(BaseCommand):
    help = 'Refresh all live data (schedule, odds, injuries, pitcher stats) for enabled sports'

    def handle(self, *args, **options):
        if not settings.LIVE_DATA_ENABLED:
            self.stdout.write('LIVE_DATA_ENABLED is false — nothing to do')
            return

        for sport, toggle, has_injuries, has_pitcher_stats, has_team_records in SPORTS_CONFIG:
            if not getattr(settings, toggle, False):
                # Explicit visibility — disabled sports print a line instead of silently skipping.
                self.stdout.write(f'  {sport}: skipped ({toggle}=false)')
                continue

            self.stdout.write(f'Refreshing {sport}...')
            try:
                call_command('ingest_schedule', sport=sport, force=True)
                call_command('ingest_odds', sport=sport, force=True)
                if has_injuries:
                    call_command('ingest_injuries', sport=sport, force=True)
                if has_pitcher_stats:
                    call_command('ingest_pitcher_stats', sport=sport, force=True)
                if has_team_records:
                    call_command('ingest_team_records', sport=sport, force=True)
                if sport in SNAPSHOT_ELIGIBLE_SPORTS:
                    call_command('capture_snapshots', sport=sport)
                    call_command('resolve_outcomes', sport=sport)
                self.stdout.write(self.style.SUCCESS(f'{sport} done'))
            except Exception as e:
                # Visible, not silent — the UI "Data temporarily unavailable" state
                # downstream reflects what happened here.
                self.stdout.write(self.style.WARNING(f'{sport} failed: {e}'))

        self.stdout.write(self.style.SUCCESS('Refresh complete'))
