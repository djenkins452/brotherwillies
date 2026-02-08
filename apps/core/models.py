from django.db import models
from django.core.validators import MinValueValidator, MaxValueValidator


class SiteConfig(models.Model):
    """
    Singleton site configuration — editable from Django admin.
    Only one row should ever exist; use SiteConfig.get() to access it.
    """
    # AI settings
    ai_temperature = models.FloatField(
        default=0.0,
        validators=[MinValueValidator(0.0), MaxValueValidator(2.0)],
        help_text='OpenAI temperature (0 = deterministic/factual, 0.3 = slight variation, 1.0+ = creative). Default: 0'
    )
    ai_max_tokens = models.IntegerField(
        default=800,
        validators=[MinValueValidator(100), MaxValueValidator(4000)],
        help_text='Maximum tokens in AI response. Default: 800'
    )

    class Meta:
        verbose_name = 'Site Configuration'
        verbose_name_plural = 'Site Configuration'

    def __str__(self):
        return 'Site Configuration'

    def save(self, *args, **kwargs):
        # Enforce singleton: always use pk=1
        self.pk = 1
        super().save(*args, **kwargs)

    @classmethod
    def get(cls):
        """Return the singleton config, creating with defaults if needed."""
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj
