import uuid

from django.db import models
from django.core.validators import MinValueValidator, MaxValueValidator
from django.utils import timezone


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


class BettingRecommendation(models.Model):
    """Decision-layer snapshot: the single highest-edge pick the model would make on a game.

    Sport-agnostic via per-sport nullable FKs (mirrors MockBet's pattern). A row is a
    snapshot in time — the current pick can change as odds or injuries move, so we do
    not enforce uniqueness on (game, model_source). The freshest row is whichever has
    the newest `created_at`.
    """
    SPORT_CHOICES = [
        ('cfb', 'College Football'),
        ('cbb', 'College Basketball'),
        ('mlb', 'MLB'),
        ('college_baseball', 'College Baseball'),
    ]

    BET_TYPE_CHOICES = [
        ('moneyline', 'Moneyline'),
        ('spread', 'Spread'),
        ('total', 'Total'),
    ]

    MODEL_SOURCE_CHOICES = [
        ('house', 'House Model'),
        ('user', 'User Model'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    sport = models.CharField(max_length=20, choices=SPORT_CHOICES)

    cfb_game = models.ForeignKey(
        'cfb.Game', on_delete=models.CASCADE, null=True, blank=True, related_name='recommendations'
    )
    cbb_game = models.ForeignKey(
        'cbb.Game', on_delete=models.CASCADE, null=True, blank=True, related_name='recommendations'
    )
    mlb_game = models.ForeignKey(
        'mlb.Game', on_delete=models.CASCADE, null=True, blank=True, related_name='recommendations'
    )
    college_baseball_game = models.ForeignKey(
        'college_baseball.Game', on_delete=models.CASCADE, null=True, blank=True, related_name='recommendations'
    )

    STATUS_CHOICES = [
        ('recommended', 'Recommended'),
        ('not_recommended', 'Not Recommended'),
    ]
    STATUS_REASON_CHOICES = [
        ('', ''),
        ('low_edge', 'Low Edge'),
        ('high_juice', 'High Juice Risk'),
        ('marginal', 'Marginal'),
    ]

    bet_type = models.CharField(max_length=10, choices=BET_TYPE_CHOICES)
    pick = models.CharField(max_length=200)
    line = models.CharField(max_length=32, blank=True)
    odds_american = models.IntegerField()
    confidence_score = models.DecimalField(max_digits=5, decimal_places=2)
    model_edge = models.DecimalField(max_digits=6, decimal_places=2)
    model_source = models.CharField(max_length=5, choices=MODEL_SOURCE_CHOICES, default='house')
    # Decision-rule output — persisted so historical queries don't depend on
    # re-running current rules against old data. See compute_status() in
    # apps/core/services/recommendations.py for the rule set.
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='recommended')
    status_reason = models.CharField(max_length=20, choices=STATUS_REASON_CHOICES, blank=True, default='')

    # Market Movement integration (Commit 2 of Odds Intelligence) — strictly
    # additive. The model's confidence is still the source of truth. These
    # fields capture the movement signal AT RECOMMENDATION TIME so historical
    # analytics ("did the market agree?") survives future tuning of the
    # significance / scoring rules.
    MOVEMENT_CLASS_CHOICES = [
        ('', ''),
        ('moderate', 'Moderate'),
        ('strong', 'Strong'),
        ('sharp', 'Sharp Action'),
    ]
    movement_class = models.CharField(
        max_length=10, choices=MOVEMENT_CLASS_CHOICES, blank=True, default='',
    )
    movement_score = models.FloatField(null=True, blank=True)
    movement_supports_pick = models.BooleanField(default=False)
    market_warning = models.BooleanField(default=False)

    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['sport', '-created_at']),
        ]

    def __str__(self):
        return f"{self.sport} {self.bet_type}: {self.pick} ({self.line}) edge={self.model_edge}"

    @property
    def game(self):
        if self.sport == 'cfb':
            return self.cfb_game
        if self.sport == 'cbb':
            return self.cbb_game
        if self.sport == 'mlb':
            return self.mlb_game
        if self.sport == 'college_baseball':
            return self.college_baseball_game
        return None

    @property
    def tier(self):
        """Raw tier from model_edge (edge-based since the Apr 21 migration).

        Earlier this property passed confidence_score to _raw_tier, which is
        a leftover from when tier was confidence-based. _raw_tier now expects
        edge in pp. Persisted snapshots don't participate in the slate-level
        elite cap — that's a lobby-render concern only.
        """
        from apps.core.services.recommendations import _raw_tier
        if self.model_edge is None:
            return 'standard'
        return _raw_tier(float(self.model_edge))

    @property
    def tier_label(self):
        from apps.core.services.recommendations import _TIER_LABELS
        return _TIER_LABELS.get(self.tier, _TIER_LABELS['standard'])

    @property
    def explanation_rows(self):
        from apps.core.services.recommendations import _build_explanation_rows
        return _build_explanation_rows(self.confidence_score, self.odds_american, self.model_edge)

    @property
    def status_label(self):
        from apps.core.services.recommendations import status_label
        return status_label(self.status)

    @property
    def action_label(self):
        from apps.core.services.recommendations import action_label
        return action_label(self.status)

    @property
    def status_reason_label(self):
        from apps.core.services.recommendations import status_reason_label
        return status_reason_label(self.status_reason)

    @property
    def is_recommended(self):
        return self.status == 'recommended'

    @property
    def top_play_reasons(self):
        from apps.core.services.recommendations import top_play_reasons
        return top_play_reasons(self.model_edge, self.confidence_score, self.tier, self.status)

    # ----- Market movement helpers (Commit 2) ----------------------------
    # These derive UI-ready values from the persisted movement_* fields.
    # They never re-fetch snapshots — display layer must stay cheap.

    @property
    def confidence_nudge_pp(self) -> float:
        from apps.core.services.odds_movement import confidence_nudge_pp
        return confidence_nudge_pp(self.movement_class or None, self.movement_supports_pick)

    @property
    def displayed_confidence(self):
        """confidence_score + bounded movement nudge (capped at +5pp, clamped <99).

        Falls back to raw confidence_score when there's no movement signal
        — so the existing UI keeps working when movement data is absent
        (e.g., golf, or any sport before its provider hook ships).
        """
        from apps.core.services.odds_movement import displayed_confidence
        return displayed_confidence(self.confidence_score, self.movement_class or None, self.movement_supports_pick)

    @property
    def market_movement_chip(self):
        """Short label rendered as a chip on hub tiles + game detail."""
        from apps.core.services.odds_movement import chip_label_for
        return chip_label_for(
            self.movement_class or None,
            self.movement_supports_pick,
            self.market_warning,
        )
