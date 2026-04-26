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


def _find_mlb_games_without_fresh_odds(fresh_max_age_minutes: int = 180):
    """Return (upcoming_pks, gap_pks) for MLB games in the next ~36h.

    A gap game is one with NO OddsSnapshot captured within
    `fresh_max_age_minutes`. The window starts 2h ago so a game that
    just first-pitched still gets covered. Used by the per-game ESPN
    fallback to fill exactly the holes primary missed — never the games
    primary already covered.
    """
    from datetime import timedelta
    from apps.mlb.models import Game, OddsSnapshot

    now = timezone.now()
    fresh_cutoff = now - timedelta(minutes=fresh_max_age_minutes)
    window_start = now - timedelta(hours=2)
    window_end = now + timedelta(hours=36)

    upcoming = Game.objects.filter(
        first_pitch__gte=window_start,
        first_pitch__lte=window_end,
    )
    upcoming_pks = list(upcoming.values_list('pk', flat=True))

    games_with_fresh = (
        OddsSnapshot.objects
        .filter(game_id__in=upcoming_pks, captured_at__gte=fresh_cutoff)
        .values_list('game_id', flat=True)
        .distinct()
    )
    fresh_set = set(games_with_fresh)
    gap_pks = [pk for pk in upcoming_pks if pk not in fresh_set]
    return upcoming_pks, gap_pks


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

        # --- ESPN per-game gap-fill for MLB ------------------------------
        # Strictly stronger than the previous whole-slate trigger (which
        # only fired when primary created 0 rows OR today_count was 0).
        # This new trigger looks at the slate game-by-game: for every
        # upcoming MLB game in the next 36h that has NO fresh OddsSnapshot
        # in the last FRESH_ODDS_MAX_AGE_MINUTES window, ask ESPN to fill
        # the gap.
        #
        # Behavior preserved vs the old code path:
        #   - Primary returns 0 rows → ALL upcoming games are gaps → ESPN
        #     runs and persists for all of them. Same end state as before.
        #   - Primary returns rows for some games but not others → ESPN
        #     fills exactly the missing games. NEW: previously these gaps
        #     were silently left empty unless ALL of today's games were
        #     missing (the prior trigger).
        #   - Primary covers the slate completely → ESPN call is skipped.
        #     NEW: saves a wasted scoreboard fetch.
        #
        # ESPN persist receives the gap pk set so it can NOT double-write
        # for games primary already handled. This guarantees no duplicate
        # snapshots regardless of how the slate splits.
        primary_created = (stats or {}).get('created', 0)
        primary_skipped = (stats or {}).get('skipped', 0)
        api_filled_count = 0
        espn_filled_count = 0
        still_missing_count = 0
        upcoming_total = 0

        if sport == 'mlb':
            fresh_window = getattr(settings, 'FRESH_ODDS_MAX_AGE_MINUTES', 180)
            upcoming_pks, gap_pks = _find_mlb_games_without_fresh_odds(fresh_window)
            upcoming_total = len(upcoming_pks)
            api_filled_count = upcoming_total - len(gap_pks)

            if gap_pks:
                try:
                    from apps.datahub.providers.mlb.odds_espn_provider import MLBEspnOddsProvider
                    self.stdout.write(
                        f"ESPN per-game gap-fill triggered: {len(gap_pks)} of "
                        f"{upcoming_total} upcoming games have no fresh primary odds"
                    )
                    provider = MLBEspnOddsProvider()
                    raw = provider.fetch()
                    normalized = provider.normalize(raw)
                    fallback_stats = provider.persist(
                        normalized, target_game_ids=set(gap_pks),
                    )
                    self.stdout.write(self.style.SUCCESS(
                        f"ESPN gap-fill: {fallback_stats}"
                    ))
                    fallback_created = fallback_stats.get('created', 0)
                    # Combine totals so the fail-fast check below sees the full
                    # picture. We keep 'source' = primary+espn_fallback for trace.
                    stats = {
                        'status': 'ok' if (primary_created + fallback_created) else 'empty',
                        'created': primary_created + fallback_created,
                        'skipped': primary_skipped + fallback_stats.get('skipped', 0),
                        'source': 'primary+espn_fallback',
                    }
                    # Re-query gaps to compute the post-fallback summary —
                    # gives us the truth of what actually got filled vs is
                    # still missing, instead of inferring from counts.
                    _, post_gap_pks = _find_mlb_games_without_fresh_odds(fresh_window)
                    espn_filled_count = len(gap_pks) - len(post_gap_pks)
                    still_missing_count = len(post_gap_pks)
                except Exception as e:
                    logger.error(f"mlb_odds_espn_fallback_failed err={e}")
                    self.stdout.write(self.style.WARNING(f"ESPN fallback failed: {e}"))
                    still_missing_count = len(gap_pks)
            else:
                # No gaps — every upcoming game already has fresh primary
                # odds. Skip the ESPN call entirely.
                self.stdout.write(
                    f"ESPN gap-fill skipped: all {upcoming_total} upcoming "
                    f"games already have fresh primary odds"
                )

            # ---- Required debug summary ---------------------------------
            # Single line answers "did every game get odds?" — the spec
            # requirement. ERROR level when still_missing > 0 so the row
            # surfaces in the Ops Command Center recent-failures panel.
            log_fn = logger.error if still_missing_count > 0 else logger.info
            log_fn(
                'mlb_odds_ingest_summary upcoming_games=%d '
                'api_filled=%d espn_filled=%d still_missing=%d',
                upcoming_total, api_filled_count, espn_filled_count, still_missing_count,
            )

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
