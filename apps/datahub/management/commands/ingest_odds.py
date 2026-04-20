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
            if sport == 'mlb':
                logger.warning(f"mlb_odds_primary_run_failed err={e}")
                self.stdout.write(self.style.WARNING(
                    f"Primary odds source errored ({e}). Will try ESPN fallback."
                ))
                stats = {'created': 0, 'skipped': 0}
            else:
                raise

        # --- ESPN fallback for MLB ---------------------------------------
        # When the primary provider (The Odds API) gets zero rows we try the
        # ESPN scoreboard feed, which embeds DraftKings odds and needs no
        # API key. Any coverage > 0 is better than silently empty.
        if sport == 'mlb' and (stats or {}).get('created', 0) == 0:
            try:
                from apps.datahub.providers.mlb.odds_espn_provider import MLBEspnOddsProvider
                self.stdout.write("Primary odds source returned zero — trying ESPN fallback...")
                fallback_stats = MLBEspnOddsProvider().run()
                self.stdout.write(self.style.SUCCESS(f"ESPN fallback: {fallback_stats}"))
                # Merge the fallback stats into `stats` so the fail-fast
                # check below uses the combined create count.
                stats = {
                    'status': fallback_stats.get('status', 'empty'),
                    'created': fallback_stats.get('created', 0),
                    'skipped': (stats or {}).get('skipped', 0) + fallback_stats.get('skipped', 0),
                    'source': 'espn_fallback',
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
