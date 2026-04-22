"""Score-only refresh service — the lightweight 15-minute path.

Dispatches to each schedule provider's `update_scores_only()` method. Providers
that set `supports_score_only = True` write only status/home_score/away_score
for games inside the live window; the rest are skipped. The heavy 6-hour
`refresh_data` pipeline continues to own odds, models, pitcher stats, etc.

Extending to a new sport: set `supports_score_only = True` on that sport's
schedule provider and implement `_find_existing_game`, `_normalized_game_time`,
and `_extract_score_fields`.
"""
import logging

from django.conf import settings

from apps.datahub.providers.registry import get_provider

logger = logging.getLogger(__name__)


# Sports whose schedule providers currently implement score-only updates.
# CBB/CFB are intentionally excluded: those sports have weekly cadence and
# the 6-hour heavy path already captures their finals quickly enough.
_SPORTS = [
    ('mlb', 'LIVE_MLB_ENABLED'),
    ('college_baseball', 'LIVE_COLLEGE_BASEBALL_ENABLED'),
]


def update_scores_only(sport='all'):
    """Run the score-only refresh for one or all supported sports.

    Returns a dict keyed by sport with the provider's counts. Emits structured
    per-provider logs plus a cycle-summary log so operators can see provider
    health at a glance in the Railway deploy log.
    """
    if not settings.LIVE_DATA_ENABLED:
        logger.info('LIVE_DATA_ENABLED is false — skipping score-only refresh')
        return {}

    results = {}
    providers_run = 0
    providers_failed = 0
    providers_disabled = 0
    total_updated = 0
    total_not_found = 0

    for sport_key, toggle in _SPORTS:
        if sport not in (sport_key, 'all'):
            continue
        if not getattr(settings, toggle, False):
            results[sport_key] = {'status': 'disabled'}
            providers_disabled += 1
            continue
        try:
            provider = get_provider(sport_key, 'schedule')
            stats = provider.update_scores_only()
            results[sport_key] = stats
            providers_run += 1
            total_updated += stats.get('updated', 0)
            total_not_found += stats.get('not_found', 0)
            logger.info(f'{sport_key} score update success', extra=stats)
        except Exception as e:
            # Never raise out of the lightweight path — a single sport's
            # provider outage must not block settlement for the others.
            providers_failed += 1
            results[sport_key] = {'status': 'error', 'error': str(e)}
            logger.error(f'{sport_key} score update failed', exc_info=e)

    logger.info(
        'Score refresh cycle complete',
        extra={
            'sport_filter': sport,
            'providers_run': providers_run,
            'providers_failed': providers_failed,
            'providers_disabled': providers_disabled,
            'total_updated': total_updated,
            'total_not_found': total_not_found,
        },
    )
    return results
