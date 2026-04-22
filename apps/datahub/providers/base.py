import logging
from abc import ABC, abstractmethod
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)


def _score_window():
    """Resolve the score-only live window from settings at call time.

    Read at call time (not import time) so tests and runtime overrides via
    `self.settings(...)` take effect immediately. Falls back to 24h/12h if
    the settings are missing — matches the default in brotherwillies/settings.py.
    """
    lookback = int(getattr(settings, 'SCORE_UPDATE_LOOKBACK_HOURS', 24))
    lookahead = int(getattr(settings, 'SCORE_UPDATE_LOOKAHEAD_HOURS', 12))
    return timedelta(hours=lookback), timedelta(hours=lookahead)


class AbstractProvider(ABC):
    """Base class for all data providers (schedule, odds, injuries)."""

    sport = None       # e.g. 'cbb', 'cfb', 'golf'
    data_type = None   # e.g. 'schedule', 'odds', 'injuries'

    # Subclasses that implement the lightweight score-only refresh path flip
    # this to True and implement `_find_existing_game`, `_normalized_game_time`,
    # and `_extract_score_fields`. Default False means `update_scores_only`
    # is a documented no-op for that provider — the 6-hour heavy path still
    # covers it via the full `run()` pipeline.
    supports_score_only = False

    @abstractmethod
    def fetch(self):
        """Call external API, return raw response data (dict or list)."""

    @abstractmethod
    def normalize(self, raw):
        """Transform raw API response into list of standardized dicts."""

    @abstractmethod
    def persist(self, normalized):
        """Upsert normalized data into Django models. Return stats dict."""

    def run(self):
        """Orchestrate fetch -> normalize -> persist with error handling."""
        label = f"{self.sport}/{self.data_type}"
        logger.info(f"[{label}] Starting ingestion")
        try:
            raw = self.fetch()
            if not raw:
                logger.warning(f"[{label}] No data returned from API")
                return {'status': 'empty', 'created': 0, 'updated': 0}

            normalized = self.normalize(raw)
            logger.info(f"[{label}] Normalized {len(normalized)} records")

            stats = self.persist(normalized)
            logger.info(f"[{label}] Done — {stats}")
            return stats
        except Exception as e:
            logger.error(f"[{label}] Ingestion failed: {e}", exc_info=True)
            raise

    # --- Score-only refresh path --------------------------------------------
    # The 15-minute cron runs this *narrow* update: reuses fetch+normalize but
    # writes ONLY status/home_score/away_score, only for games that already
    # exist, only when the value actually changed. No odds, no model recompute,
    # no row creation. Out-of-window records are skipped.

    def _find_existing_game(self, normalized_item):
        """Return the existing Game row for a normalized record, or None.
        Subclasses opting into score-only updates must override this."""
        return None

    def _normalized_game_time(self, normalized_item):
        """Return the game start time as an aware datetime, or None if unknown.
        Used by `update_scores_only` to filter to the live window."""
        return None

    def _extract_score_fields(self, normalized_item):
        """Return (status, home_score, away_score) for a normalized record.
        Defaults to reading the 'status', 'home_score', 'away_score' keys."""
        return (
            normalized_item.get('status'),
            normalized_item.get('home_score'),
            normalized_item.get('away_score'),
        )

    def update_scores_only(self):
        """Narrow refresh: update status + scores for existing games in the
        live window, writing only when values change. Reuses fetch+normalize
        from the heavy path so provider logic stays in one place.

        Returns counts dict — `updated` (dirty writes), `skipped` (no change),
        `out_of_window`, `not_found` (record for a game we haven't ingested
        yet — the 6-hour schedule cron will create it). Idempotent.
        """
        label = f"{self.sport}/{self.data_type}/score_only"
        if not self.supports_score_only:
            logger.info(f"[{label}] provider does not implement score-only updates")
            return {'status': 'not_supported', 'updated': 0, 'skipped': 0}

        raw = self.fetch()
        if not raw:
            return {'status': 'empty', 'updated': 0, 'skipped': 0}
        normalized = self.normalize(raw) or []

        before, after = _score_window()
        now = timezone.now()
        window_start = now - before
        window_end = now + after

        updated = 0
        skipped = 0
        out_of_window = 0
        not_found = 0

        for item in normalized:
            game_time = self._normalized_game_time(item)
            if game_time is None or not (window_start <= game_time <= window_end):
                out_of_window += 1
                continue

            game = self._find_existing_game(item)
            if game is None:
                # Visibility: structured warning per miss so operators can see
                # provider drift (games the API reports but we haven't ingested).
                # Keyed by sport + external identifier for easy log-aggregator
                # filtering. Low-volume in practice (~15 MLB games/day, most
                # already in DB after the first heavy cron cycle).
                not_found += 1
                logger.warning(
                    'Score update skipped — game not found in DB',
                    extra={
                        'sport': self.sport,
                        'external_id': self._normalized_external_id(item),
                    },
                )
                continue

            new_status, new_home, new_away = self._extract_score_fields(item)

            # Dirty check — skip the write entirely if nothing moved. At ~15
            # MLB games / 15 minutes, this keeps the DB quiet between pitches.
            if (
                game.status == new_status
                and game.home_score == new_home
                and game.away_score == new_away
            ):
                skipped += 1
                continue

            game.status = new_status
            game.home_score = new_home
            game.away_score = new_away
            game.save(update_fields=['status', 'home_score', 'away_score'])
            updated += 1

        stats = {
            'status': 'ok',
            'sport': self.sport,
            'updated': updated,
            'unchanged': skipped,  # explicit name for "found but no change"
            'skipped': skipped,    # legacy alias — callers/tests still use this
            'out_of_window': out_of_window,
            'not_found': not_found,
            'window_hours': {
                'lookback': int(before.total_seconds() // 3600),
                'lookahead': int(after.total_seconds() // 3600),
            },
        }
        logger.info('Score update summary', extra=stats)
        return stats

    def _normalized_external_id(self, normalized_item):
        """Best-effort external identifier for diagnostic logs. Override if the
        provider's normalized shape doesn't expose `external_id` directly."""
        return normalized_item.get('external_id') or ''
