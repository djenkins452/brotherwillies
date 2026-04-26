"""Management command to ingest odds data for a sport.

Hardened with a post-ingestion sanity check: if ingestion produced zero
OddsSnapshot rows, we either raise (in DEBUG) or log a high-severity
structured error (in prod). This guarantees we never silently operate
with empty odds — the single most common user-visible failure mode.
"""
import logging

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.datahub.providers.registry import get_provider

logger = logging.getLogger(__name__)

SPORT_TOGGLES = {
    'cbb': 'LIVE_CBB_ENABLED',
    'cfb': 'LIVE_CFB_ENABLED',
    'golf': 'LIVE_GOLF_ENABLED',
    'mlb': 'LIVE_MLB_ENABLED',
    'college_baseball': 'LIVE_COLLEGE_BASEBALL_ENABLED',
}


def _current_day_snapshot_count(sport: str) -> int:
    """Return number of OddsSnapshot rows linked to games starting today.

    Used by the post-ingest sanity check. Games today are the ones users
    will actually see on the hub — if we have zero for today, odds are
    effectively broken from the user's perspective regardless of what
    the ingestion return-code claimed.
    """
    today = timezone.localdate()
    if sport == 'mlb':
        from apps.mlb.models import OddsSnapshot
        return OddsSnapshot.objects.filter(game__first_pitch__date=today).count()
    if sport == 'cbb':
        from apps.cbb.models import OddsSnapshot
        return OddsSnapshot.objects.filter(game__tipoff__date=today).count()
    if sport == 'cfb':
        from apps.cfb.models import OddsSnapshot
        return OddsSnapshot.objects.filter(game__kickoff__date=today).count()
    if sport == 'college_baseball':
        from apps.college_baseball.models import OddsSnapshot
        return OddsSnapshot.objects.filter(game__first_pitch__date=today).count()
    # Golf has a different model; skip today-window check.
    return -1


class Command(BaseCommand):
    help = 'Ingest odds data from external APIs'

    def add_arguments(self, parser):
        parser.add_argument(
            '--sport',
            required=True,
            choices=['cbb', 'cfb', 'golf', 'mlb', 'college_baseball'],
            help='Sport to ingest',
        )
        parser.add_argument(
            '--force',
            action='store_true',
            help='Run even if live data toggle is disabled',
        )

    def handle(self, *args, **options):
        sport = options['sport']
        force = options['force']

        if not force:
            if not settings.LIVE_DATA_ENABLED:
                raise CommandError(
                    "LIVE_DATA_ENABLED is false. Use --force to override."
                )
            toggle = SPORT_TOGGLES.get(sport)
            if toggle and not getattr(settings, toggle, False):
                raise CommandError(
                    f"{toggle} is false. Use --force to override."
                )

        self.stdout.write(f"Ingesting {sport} odds...")
        stats: dict = {}
        try:
            provider = get_provider(sport, 'odds')
            stats = provider.run()
            self.stdout.write(self.style.SUCCESS(f"Done: {stats}"))
        except ValueError as e:
            # For MLB we'll try the ESPN fallback below before failing.
            if sport == 'mlb':
                logger.warning(f"mlb_odds_primary_init_failed err={e}")
                self.stdout.write(self.style.WARNING(
                    f"Primary odds source unavailable ({e}). Will try ESPN fallback."
                ))
                stats = {'created': 0, 'skipped': 0}
            else:
                raise CommandError(str(e))
        except Exception as e:
            # Explicit detection of common Odds API failure modes — the bare
            # exception message buries them in a stack trace, but a 401 here
            # almost always means "ODDS_API_KEY is invalid or quota exhausted",
            # which is the single most common production-stopping issue.
            err_str = str(e)
            if '401' in err_str:
                logger.error(
                    f"odds_api_unauthorized sport={sport} — "
                    "ODDS_API_KEY is invalid or quota is exhausted. "
                    "Rotate the key in Railway env vars."
                )
                self.stdout.write(self.style.ERROR(
                    f"⚠ ODDS API 401 UNAUTHORIZED for {sport}: the ODDS_API_KEY env var "
                    "is invalid, expired, or quota-exhausted. The deploy log shows the "
                    "rejected URL. Update the ODDS_API_KEY in Railway → Variables and "
                    "redeploy. (No quota warning will appear because the request was "
                    "rejected at auth, not at quota.)"
                ))
            elif '429' in err_str:
                logger.error(f"odds_api_rate_limited sport={sport}")
                self.stdout.write(self.style.ERROR(
                    f"⚠ ODDS API 429 RATE LIMIT for {sport}: too many requests. "
                    "Consider widening the cron interval or upgrading the API tier."
                ))
            if sport == 'mlb':
                logger.warning(f"mlb_odds_primary_run_failed err={e}")
                self.stdout.write(self.style.WARNING(
                    f"Primary odds source errored ({e}). Will try ESPN fallback."
                ))
                stats = {'created': 0, 'skipped': 0}
            else:
                # Don't crash the cron for other sports — log it loudly and move
                # on so the rest of the refresh cycle still runs.
                logger.error(f"{sport}_odds_primary_run_failed err={e}")
                self.stdout.write(self.style.WARNING(
                    f"Primary odds source errored for {sport}: {e}. Skipping."
                ))
                stats = {'created': 0, 'skipped': 0, 'status': 'error'}

        # --- ESPN fallback for MLB ---------------------------------------
        # Triggered on either of two conditions:
        #   1. Primary returned 0 rows (original intent).
        #   2. Primary created rows but TODAY's games specifically still have
        #      zero odds — this happens when The Odds API covers a week-ahead
        #      window but not today's already-started matchups. ESPN's
        #      scoreboard is today-focused and often fills that gap.
        today_count_after_primary = _current_day_snapshot_count(sport) if sport == 'mlb' else None
        primary_created = (stats or {}).get('created', 0)
        should_fall_back = sport == 'mlb' and (
            primary_created == 0
            or (today_count_after_primary is not None and today_count_after_primary == 0)
        )
        if should_fall_back:
            try:
                from apps.datahub.providers.mlb.odds_espn_provider import MLBEspnOddsProvider
                reason = (
                    'primary returned zero rows' if primary_created == 0
                    else f'primary created {primary_created} rows but today has 0 snapshots'
                )
                self.stdout.write(f"ESPN fallback triggered: {reason}")
                fallback_stats = MLBEspnOddsProvider().run()
                self.stdout.write(self.style.SUCCESS(f"ESPN fallback: {fallback_stats}"))
                # Combine with primary so the fail-fast check sees the full picture.
                # Additive on created/skipped; keep 'source' as espn_fallback for tracing.
                fallback_created = fallback_stats.get('created', 0)
                stats = {
                    'status': 'ok' if (primary_created + fallback_created) else 'empty',
                    'created': primary_created + fallback_created,
                    'skipped': (stats or {}).get('skipped', 0) + fallback_stats.get('skipped', 0),
                    'source': 'primary+espn_fallback',
                }
            except Exception as e:
                logger.error(f"mlb_odds_espn_fallback_failed err={e}")
                self.stdout.write(self.style.WARNING(f"ESPN fallback failed: {e}"))

        # --- fail-fast integrity checks ----------------------------------
        created = (stats or {}).get('created', 0)
        skipped = (stats or {}).get('skipped', 0)
        if created == 0:
            msg = (
                f"{sport}_odds_ingest_zero_created "
                f"skipped={skipped} stats={stats!r}"
            )
            if settings.DEBUG:
                raise RuntimeError(f"Odds ingestion produced zero records: {msg}")
            logger.error(msg)  # HIGH-SEVERITY in prod — triggers alerting

        # Post-ingest sanity check: even if `created > 0`, the new rows may
        # have landed on games outside today's window. Users on /mlb/ today
        # care about today's games — flag empty today-windows explicitly.
        today_count = _current_day_snapshot_count(sport)
        if today_count == 0:
            msg = f"{sport}_odds_empty_after_ingest today_count=0"
            if settings.DEBUG:
                raise RuntimeError(f"No OddsSnapshot for today's games: {msg}")
            logger.error(msg)
        elif today_count > 0:
            logger.info(f"{sport}_odds_sanity_ok today_count={today_count}")
