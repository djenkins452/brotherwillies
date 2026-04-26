"""Management command to refresh all live data — designed to run as a cron job.

Runs schedule, odds, and (for supported sports) injuries + pitcher stats
ingestion for all enabled sports. Exits cleanly so Railway cron service
can restart on schedule.

Each sport is configured with:
    (key, toggle_setting, has_injuries, has_pitcher_stats)

Every invocation writes a CronRunLog row via the cron_run_log context
manager. If LIVE_DATA_ENABLED is false we still record the run (with a
'success' status and "skipped" summary) so the dashboard reflects reality.
Per-sport failures fall through to mark_partial(), which paints the run
yellow on the dashboard rather than green.
"""

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import BaseCommand

from apps.ops.services.cron_logging import cron_run_log


SPORTS_CONFIG = [
    # (sport_key,            toggle_setting,                 injuries, pitcher_stats, team_records)
    ('cbb',              'LIVE_CBB_ENABLED',                 True,     False,         False),
    ('cfb',              'LIVE_CFB_ENABLED',                 True,     False,         False),
    ('golf',             'LIVE_GOLF_ENABLED',                False,    False,         False),
    ('mlb',              'LIVE_MLB_ENABLED',                 True,     True,          True),
    ('college_baseball', 'LIVE_COLLEGE_BASEBALL_ENABLED',    False,    False,         False),
]

# Sports the existing capture_snapshots / resolve_outcomes commands understand.
# Baseball sports flow through their own per-sport branches (Phase 8 analytics).
SNAPSHOT_ELIGIBLE_SPORTS = {'cfb', 'cbb', 'mlb', 'college_baseball'}


class Command(BaseCommand):
    help = 'Refresh all live data (schedule, odds, injuries, pitcher stats) for enabled sports'

    def add_arguments(self, parser):
        # The ops command-center spawns this via subprocess and tags the row
        # with the requesting user. Default 'cron' covers Railway-scheduled runs.
        parser.add_argument('--trigger', choices=['cron', 'manual', 'deploy'], default='cron')
        parser.add_argument('--triggered-by-user-id', type=int, default=None)

    def handle(self, *args, **options):
        triggered_by = None
        if options.get('triggered_by_user_id'):
            User = get_user_model()
            triggered_by = User.objects.filter(pk=options['triggered_by_user_id']).first()

        with cron_run_log(
            'refresh_data',
            trigger=options.get('trigger', 'cron'),
            triggered_by_user=triggered_by,
        ) as log:
            stdout_lines = []

            def _emit(line):
                stdout_lines.append(line)
                self.stdout.write(line)

            if not settings.LIVE_DATA_ENABLED:
                _emit('LIVE_DATA_ENABLED is false — nothing to do')
                log.summary = 'skipped: LIVE_DATA_ENABLED=false'
                log.stdout_tail = '\n'.join(stdout_lines)
                return

            sport_failures = []
            sport_successes = []

            for sport, toggle, has_injuries, has_pitcher_stats, has_team_records in SPORTS_CONFIG:
                if not getattr(settings, toggle, False):
                    _emit(f'  {sport}: skipped ({toggle}=false)')
                    continue

                _emit(f'Refreshing {sport}...')
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
                    _emit(f'{sport} done')
                    sport_successes.append(sport)
                except Exception as e:
                    # Visible, not silent — the UI "Data temporarily unavailable"
                    # state downstream reflects what happened here.
                    _emit(f'{sport} failed: {e}')
                    sport_failures.append((sport, str(e)))

            # Settle pending mock bets for any games that finalized this cycle.
            try:
                call_command('settle_mockbets')
            except Exception as e:
                _emit(f'settle_mockbets failed: {e}')
                sport_failures.append(('settle_mockbets', str(e)))

            _emit('Refresh complete')

            log.summary = (
                f'success={len(sport_successes)} fail={len(sport_failures)}'
                f' [{",".join(sport_successes) or "none"}]'
            )
            log.stdout_tail = '\n'.join(stdout_lines)
            if sport_failures:
                detail = '; '.join(f'{name}: {err[:200]}' for name, err in sport_failures)
                log.mark_partial(detail)
