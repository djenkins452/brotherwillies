"""Idempotent MLB Elo backfill — Railway-safe deployment hook.

The Phase 2A Task 1 production hook. Detects whether MLB Elo state is
already populated; runs `rebuild_elo_ratings --sport mlb` only when it
isn't. Designed to be called from `ensure_seed` on every deploy. After
the first successful run the guard short-circuits and the command is a
cheap no-op.

WHY A SEPARATE COMMAND
----------------------
- `rebuild_elo_ratings` is the correct primitive but semantically
  "wipe + replay". We don't want to wipe every deploy.
- `update_elo_ratings` is the incremental updater (already wired into
  the cron refresh cycle in Phase 1B). Runs every cycle on top of
  whatever state already exists.
- `ensure_elo_backfilled` fills the gap between those two: bring state
  to "ready" exactly once, then defer to the incremental updater.

DETECTION
---------
A sport is "backfilled" when BOTH:
  - at least MIN_BACKFILLED_TEAMS teams have a non-null elo_rating,
  - at least one TeamEloHistory row exists for the sport.

The history check matters because a single `process_game` call from a
test or admin shell would set elo_rating without a true full rebuild
having happened. Both conditions together mean a real rebuild has run.

FAILURE HANDLING
----------------
Wraps the inner `rebuild_elo_ratings` call in try/except. On failure,
logs the error and exits cleanly. The Railway deploy continues — the
live system runs on static ratings (USE_DYNAMIC_RATINGS=False) anyway,
so a one-off Elo backfill failure is not deploy-blocking. Subsequent
deploys retry. Operators can also run the command manually via shell
once shell access is available (it is not, on Railway).

RUNTIME
-------
Local with SQLite + ~2,000 final MLB games: completes in 1-3 seconds.
Railway with PostgreSQL: expect 5-15 seconds. The inner command wraps
the work in `transaction.atomic`, so a partial failure rolls back.

ROLLBACK
--------
To force a re-backfill on the next deploy:
    from apps.core.services.elo_service import reset_sport
    reset_sport('mlb')

Or — for a controlled rebuild without redeploying — call the inner
command directly:
    call_command('rebuild_elo_ratings', sport='mlb')

REVERSIBILITY
-------------
The backfill writes only to `Team.elo_rating` and `TeamEloHistory`.
While `USE_DYNAMIC_RATINGS=False` (Phase 2A pre-cutover state),
neither field is read by the live recommendation engine —
`team_rating_for_model` returns `team.rating` unchanged. So even a
"bad" backfill cannot affect live behavior. The blast radius is
strictly contained to the shadow-mode comparison data on new
recommendations, which is itself diagnostic-only.
"""
from django.core.management import call_command
from django.core.management.base import BaseCommand


# Lower bound on "backfilled-looking" team count for MLB. MLB has 30
# teams; the threshold is 20 to allow for a partial backfill (e.g.,
# some teams with no final games yet at season start) while still
# rejecting the "single test team has elo_rating" false positive.
MIN_BACKFILLED_TEAMS = 20


class Command(BaseCommand):
    help = (
        'Idempotent MLB Elo backfill. Runs rebuild_elo_ratings --sport mlb '
        'exactly once (detected via existing elo_rating + TeamEloHistory '
        'state); subsequent invocations are no-ops. Safe to call on every '
        'Railway deploy.'
    )

    def add_arguments(self, parser):
        # Operators can force a rebuild even if state looks backfilled —
        # useful after a K_FACTORS / HFA_ELO change or a data correction.
        parser.add_argument(
            '--force',
            action='store_true',
            default=False,
            help='Run rebuild_elo_ratings even if state looks backfilled.',
        )

    def handle(self, *args, **options):
        from apps.analytics.models import TeamEloHistory
        from apps.mlb.models import Team as MLBTeam

        teams_with_elo = MLBTeam.objects.filter(elo_rating__isnull=False).count()
        history_rows = TeamEloHistory.objects.filter(sport='mlb').count()

        already_backfilled = (
            teams_with_elo >= MIN_BACKFILLED_TEAMS
            and history_rows > 0
        )

        if already_backfilled and not options['force']:
            self.stdout.write(
                f'MLB Elo already backfilled '
                f'(teams_with_elo={teams_with_elo}, history_rows={history_rows}) '
                f'— skipping. Use --force to rebuild anyway.'
            )
            return

        if already_backfilled and options['force']:
            self.stdout.write(
                f'MLB Elo state looks backfilled '
                f'(teams_with_elo={teams_with_elo}, history_rows={history_rows}) '
                f'but --force passed; rebuilding.'
            )
        else:
            self.stdout.write(
                f'MLB Elo not yet backfilled '
                f'(teams_with_elo={teams_with_elo}, history_rows={history_rows}, '
                f'threshold={MIN_BACKFILLED_TEAMS}); running rebuild...'
            )

        try:
            call_command('rebuild_elo_ratings', sport='mlb', stdout=self.stdout)
        except Exception as e:
            # Deploy-safe: do not raise. The live recommendation engine
            # is unaffected (USE_DYNAMIC_RATINGS=False until cutover).
            # Operator sees the failure in the deploy log; next deploy
            # retries automatically.
            self.stdout.write(self.style.WARNING(
                f'ensure_elo_backfilled: rebuild failed: {e} — continuing. '
                f'Re-deploy will retry. Live behavior unaffected '
                f'(USE_DYNAMIC_RATINGS controls live read path).'
            ))
            return

        teams_with_elo_after = MLBTeam.objects.filter(elo_rating__isnull=False).count()
        history_rows_after = TeamEloHistory.objects.filter(sport='mlb').count()
        self.stdout.write(self.style.SUCCESS(
            f'MLB Elo backfill complete: '
            f'teams_with_elo={teams_with_elo_after}, '
            f'history_rows={history_rows_after}.'
        ))
