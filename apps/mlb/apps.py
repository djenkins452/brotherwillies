from django.apps import AppConfig


class MlbConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.mlb'
    verbose_name = 'MLB'

    def ready(self):
        # Import signal handlers so post_save receivers are registered.
        # Side-effect import is the standard Django pattern for this.
        from apps.mlb import signals  # noqa: F401
