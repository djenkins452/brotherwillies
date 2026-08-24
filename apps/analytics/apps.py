from django.apps import AppConfig


class AnalyticsConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.analytics'

    def ready(self):
        # 2026-08-24: application-owned scheduler for the V3.2
        # forward-validation dedicated capture cadence. Spawned only
        # in web/WSGI contexts (see `should_start_scheduler`); no-op
        # in tests, migrations, and one-off management commands. See
        # apps/analytics/services/dedicated_capture_scheduler.py for
        # the design rationale (why an application-owned scheduler is
        # correct here rather than Railway Cron Jobs).
        try:
            from apps.analytics.services.dedicated_capture_scheduler import (
                start_scheduler_if_appropriate,
            )
            start_scheduler_if_appropriate()
        except Exception:
            # Never let scheduler startup crash Django boot. Failures
            # here are surfaced via logging + the forward-health report's
            # cadence block.
            import logging
            logging.getLogger(__name__).exception(
                'v3_2 scheduler bootstrap raised in ready()'
            )
