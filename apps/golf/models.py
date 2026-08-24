from django.db import models


class GolfEvent(models.Model):
    name = models.CharField(max_length=200)
    slug = models.SlugField(unique=True, blank=True, null=True)
    external_id = models.CharField(max_length=100, blank=True, db_index=True)
    start_date = models.DateField()
    end_date = models.DateField()

    class Meta:
        ordering = ['start_date']

    def __str__(self):
        return self.name


class Golfer(models.Model):
    """One row per real-world golfer.

    Uniqueness (2026-08-24 identity fix):
      * `external_id` — unique when non-blank (partial constraint).
        This is the stable provider ID (schedule_provider payloads
        carry it; odds_provider payloads do not).
      * `name_normalized` — unique across ALL rows. Computed on save
        as `name.strip().lower()`. Guarantees exactly one Golfer
        row per human-readable name regardless of which ingestion
        path writes it.

    Why both:
      Schedule-provider payloads know external_id; odds-provider
      payloads know only the name. Without a durable normalized-name
      key, the two paths accumulated duplicate rows for the same
      golfer (the `MultipleObjectsReturned: got >20` failure that
      broke refresh_data Golf). The normalized-name constraint
      prevents future accumulation; the reconciliation migration
      merges historical duplicates while preserving FK references.

    Do NOT bypass name_normalized with `.get(name='X')`. Use
    `get_by_name(...)` below or query by `external_id` when known.
    """
    name = models.CharField(max_length=100)
    first_name = models.CharField(max_length=50, blank=True, default='', db_index=True)
    last_name = models.CharField(max_length=50, blank=True, default='', db_index=True)
    external_id = models.CharField(max_length=100, blank=True, db_index=True)
    # Canonical name key — lowercased, trimmed. Populated by save().
    # Unique across all rows so name-based lookups can never return
    # >1 golfer.
    name_normalized = models.CharField(
        max_length=100, blank=True, default='', db_index=True,
    )

    class Meta:
        ordering = ['last_name', 'first_name']
        constraints = [
            models.UniqueConstraint(
                fields=['external_id'],
                condition=~models.Q(external_id=''),
                name='golf_golfer_external_id_unique',
            ),
            models.UniqueConstraint(
                fields=['name_normalized'],
                condition=~models.Q(name_normalized=''),
                name='golf_golfer_name_normalized_unique',
            ),
        ]

    def __str__(self):
        return self.name

    @staticmethod
    def _normalize_name(name: str) -> str:
        """Canonical name normalization — lowercase + collapsed
        whitespace. Kept as a single call so all consumers agree."""
        if not name:
            return ''
        return ' '.join(name.strip().split()).lower()

    def save(self, *args, **kwargs):
        # Auto-split name into first/last if not set
        if self.name and not self.last_name:
            parts = self.name.strip().split()
            if len(parts) >= 2:
                self.first_name = parts[0]
                self.last_name = ' '.join(parts[1:])
            elif parts:
                self.last_name = parts[0]
        # Canonical name key — always maintained, so uniqueness holds.
        self.name_normalized = self._normalize_name(self.name)
        super().save(*args, **kwargs)

    @classmethod
    def get_or_create_by_name(cls, name: str, *, defaults=None):
        """Idempotent lookup-or-create by normalized name. Returns
        (golfer, created). Replaces the raw `.get_or_create(name=...)`
        pattern that used to explode on duplicates."""
        norm = cls._normalize_name(name)
        if not norm:
            raise ValueError('cannot get_or_create_by_name with empty name')
        try:
            return cls.objects.get(name_normalized=norm), False
        except cls.DoesNotExist:
            payload = {'name': name.strip()}
            payload.update(defaults or {})
            return cls.objects.create(**payload), True


SNAPSHOT_TYPE_CHOICES = [
    ('raw', 'Raw Pull'),
    ('significant', 'Significant Move'),
    ('closing', 'Closing Line'),
    ('bet_context', 'Bet Context'),
]
MOVEMENT_CLASS_CHOICES = [
    ('noise', 'Noise'),
    ('moderate', 'Moderate'),
    ('strong', 'Strong'),
    ('sharp', 'Sharp Action'),
]
# See apps.mlb.models for the doc on these choices.
SNAPSHOT_SOURCE_CHOICES = [
    ('odds_api', 'The Odds API'),
    ('espn', 'ESPN Fallback'),
    ('manual', 'Manual Entry'),
    ('cached', 'Cached'),
]
SNAPSHOT_SOURCE_QUALITY_CHOICES = [
    ('primary', 'Primary'),
    ('fallback', 'Fallback'),
    ('stale', 'Stale'),
    ('unavailable', 'Unavailable'),
]


class GolfOddsSnapshot(models.Model):
    event = models.ForeignKey(GolfEvent, on_delete=models.CASCADE, related_name='odds_snapshots')
    golfer = models.ForeignKey(Golfer, on_delete=models.CASCADE, related_name='odds_snapshots')
    captured_at = models.DateTimeField()
    sportsbook = models.CharField(max_length=50, default='consensus')
    outright_odds = models.IntegerField(help_text='American format odds')
    implied_prob = models.FloatField()
    # See apps.mlb.models.OddsSnapshot for the full doc on these three fields.
    snapshot_type = models.CharField(
        max_length=20, choices=SNAPSHOT_TYPE_CHOICES, default='raw', db_index=True,
    )
    movement_score = models.FloatField(null=True, blank=True)
    movement_class = models.CharField(
        max_length=10, choices=MOVEMENT_CLASS_CHOICES, null=True, blank=True,
    )
    odds_source = models.CharField(
        max_length=20, choices=SNAPSHOT_SOURCE_CHOICES, default='odds_api', db_index=True,
    )
    source_quality = models.CharField(
        max_length=15, choices=SNAPSHOT_SOURCE_QUALITY_CHOICES, default='primary',
    )
    # See apps.mlb.models.OddsSnapshot for the doc on is_derived.
    is_derived = models.BooleanField(default=False, db_index=True)

    class Meta:
        ordering = ['-captured_at']
        indexes = [
            models.Index(fields=['event', 'golfer', '-captured_at']),
            models.Index(fields=['snapshot_type', '-captured_at']),
        ]

    def __str__(self):
        return f"{self.golfer} @ {self.event} ({self.outright_odds})"


class GolfRound(models.Model):
    event = models.ForeignKey(GolfEvent, on_delete=models.CASCADE, related_name='rounds')
    golfer = models.ForeignKey(Golfer, on_delete=models.CASCADE, related_name='rounds')
    round_number = models.IntegerField()
    score = models.IntegerField(null=True, blank=True)

    class Meta:
        ordering = ['event', 'round_number']
        unique_together = ['event', 'golfer', 'round_number']

    def __str__(self):
        return f"{self.golfer} R{self.round_number} ({self.event})"
