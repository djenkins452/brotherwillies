"""v3.3 SHADOW — TeamBullpenSnapshot ingestion (SCAFFOLDED, not wired).

Populates `apps.mlb.models.TeamBullpenSnapshot` — the append-only
per-team-per-day bullpen state timeline consumed by
`apps.mlb.services.bullpen`.

CURRENT STATUS

  SCAFFOLDED. This command's `run()` is a no-op that logs its intent
  and exits cleanly. It does NOT hit any external API. To make this
  command produce real data, wire the two clearly marked TODO blocks
  below to whichever data source is chosen (options in the "Data
  source options" section).

  The intentionally-empty behavior is honest per the v3.3 brief:
  we scaffold the ingestion so downstream shadow computation, replay
  experiments, and tests are complete and green — but we do NOT invent
  a data source that hasn't been evaluated. The design doc's
  assumption of an MLB Stats API `/v1/teams/stats?group=pitching&
  situation=relief` endpoint has NOT been verified against the current
  MLB Stats API surface used elsewhere in `apps/datahub/providers/mlb/`.

DESIGN CONSTRAINTS (must hold when wired)

  1. Append-only. Every run creates NEW TeamBullpenSnapshot rows with
     a fresh `as_of = timezone.now()`. NEVER update in place. The
     `MLBTeamRecordProvider.persist` pattern (which overwrites
     `Team.wins/losses` in place) BREAKS historical replay and must
     not be repeated here.

  2. `as_of` = the wall-clock timestamp of the ingest run, NOT the
     stat's own reporting date. The bullpen service filters
     `as_of__lt=game.first_pitch` — this is what keeps a snapshot
     captured seconds after first pitch from bleeding into the
     pre-game decision.

  3. Snapshots with unavailable/thin data must set
     `data_confidence='low'` explicitly. The bullpen service treats
     'low'-confidence snapshots as data-available (so downstream can
     see the row exists) but the caller can choose to weight the
     contribution zero if confidence is too low to trust.

  4. Fatigue fields (`appearances_last_*`, `top_reliever_available`)
     may be left NULL by this v3.3-A command. They are populated by
     the (separate, not-yet-scaffolded) v3.3-B reliever-appearance
     ingestion; the bullpen service treats NULL as "no fatigue signal"
     and returns zero fatigue delta.

DATA SOURCE OPTIONS (evaluate before wiring)

  Option 1 — MLB Stats API team pitching splits.
    - Base: settings.MLB_STATSAPI_BASE_URL.
    - Endpoint that the codebase does NOT currently call:
        /v1/teams/{teamId}/stats?group=pitching&stats=season&hydrate=...
      Availability of a bullpen-only split (Roles=RELIEVER) must be
      confirmed empirically — the endpoint may return only aggregate
      pitching. If it doesn't, this option can't support Phase 2A.
    - Rate: MLB Stats API is public, unauthenticated, generous but
      not unlimited. Budget: 30 teams * 1 request = 30 req/day. Safe.
    - Backfill: MLB Stats API returns SEASON-TO-DATE at the moment of
      call. There is no historical daily-value endpoint. Backfill is
      NOT possible — this ingest can only produce forward accumulation.

  Option 2 — MLB Stats API team-roster + per-pitcher game logs.
    - Aggregate reliever stats ourselves by iterating the roster's
      relief pitchers and summing their season/split stats.
    - Higher request count (~15-20 pitchers/team * 30 teams = ~500 req/day).
    - Same forward-only limitation.

  Option 3 — External provider (FanGraphs / Baseball-Reference).
    - Historically-backfillable data available.
    - Requires TOS review, credentials, and probably paid API access.
    - Highest cost, highest quality.

  Option 4 — Manual/CSV bootstrap.
    - Load a one-time historical seed from a manually-curated CSV.
    - Zero external dependency; useful for kickstarting the replay
      denominator before forward accumulation catches up.
    - Recommend as a stopgap alongside Option 1.

TESTS

  Currently a no-op run must not crash. See
  `apps.mlb.test_bullpen.IngestScaffoldTests`.
"""
import logging

from django.core.management.base import BaseCommand
from django.utils import timezone


logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = (
        'v3.3 SHADOW: ingest per-team bullpen aggregates into '
        'TeamBullpenSnapshot. CURRENTLY SCAFFOLDED — no-op until wired '
        'to a data source (see module docstring for options).'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--verbose', action='store_true',
            help='Log the intended per-team activity.',
        )
        parser.add_argument(
            '--force-dummy', action='store_true',
            help=(
                'STAFF DEBUG ONLY: write one zero-value snapshot per team '
                'with data_confidence=low so shadow-path tests can see '
                'a real row exists. NEVER use in production — a real '
                'bullpen ingestion must replace this before the data '
                'has any signal value.'
            ),
        )

    def handle(self, *args, **options):
        # --- TODO(v3.3-A): fetch team-level relief pitching splits ------
        # Replace the block below with real ingestion. Options + design
        # constraints are documented in this file's module docstring.
        # Whichever source you pick, the CONSTRAINTS ARE INVARIANT:
        #   * Every row is a NEW insert. Never update in place.
        #   * `as_of = timezone.now()`. Never derive from the stat's
        #     own reporting date.
        #   * `data_confidence` explicit. 'low' when the pitcher pool
        #     is thin (few relief IP), 'high' otherwise.
        # ----------------------------------------------------------------
        from apps.mlb.models import Team, TeamBullpenSnapshot

        if options.get('force_dummy'):
            self.stdout.write(self.style.WARNING(
                '⚠ --force-dummy: writing zero-value snapshots per team '
                '(data_confidence=low). This is NOT ingestion — it is a '
                'test hook for the shadow pipe.'
            ))
            now = timezone.now()
            created = 0
            for team in Team.objects.all():
                TeamBullpenSnapshot.objects.create(
                    team=team,
                    as_of=now,
                    bullpen_era=None,
                    bullpen_whip=None,
                    bullpen_k_per_9=None,
                    bullpen_bb_per_9=None,
                    bullpen_ip_last30=None,
                    source='manual',
                    data_confidence='low',
                    notes='force-dummy scaffolding row',
                )
                created += 1
            self.stdout.write(f'Wrote {created} dummy snapshots.')
            return

        logger.info(
            'ingest_bullpen_snapshots: SCAFFOLDED — no external fetch. '
            'To activate v3.3-A data flow, wire this command to a real '
            'data source (see module docstring for evaluated options). '
            'Skipping cleanly.'
        )
        self.stdout.write(
            'ingest_bullpen_snapshots is scaffolded but not yet wired '
            'to a data source. No rows written. See module docstring '
            'in apps/datahub/management/commands/ingest_bullpen_snapshots.py '
            'for the four evaluated options and the invariants any '
            'implementation must hold.'
        )
