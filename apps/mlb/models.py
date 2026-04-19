"""MLB data model.

Mirrors apps.cbb.models structure for consistency, with baseball-specific
additions: StartingPitcher entity and nullable pitcher FKs on Game.

External IDs + source fields allow idempotent upsert from statsapi.mlb.com
and future providers without risk of duplicates.
"""
import uuid
from django.db import models


SOURCE_CHOICES = [
    ('mlb_stats_api', 'MLB Stats API'),
    ('odds_api', 'Odds API'),
    ('manual', 'Manual'),
]


class Conference(models.Model):
    """MLB League/Division (e.g., "AL East", "NL West")."""
    name = models.CharField(max_length=100)
    slug = models.SlugField(unique=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return self.name


class Team(models.Model):
    name = models.CharField(max_length=100)
    slug = models.SlugField(unique=True)
    conference = models.ForeignKey(Conference, on_delete=models.CASCADE, related_name='teams')
    rating = models.FloatField(default=50.0)
    primary_color = models.CharField(max_length=7, blank=True, default='')
    abbreviation = models.CharField(max_length=5, blank=True, default='')
    external_id = models.CharField(max_length=50, blank=True, default='')
    source = models.CharField(max_length=30, choices=SOURCE_CHOICES, blank=True, default='')

    class Meta:
        ordering = ['name']
        constraints = [
            models.UniqueConstraint(
                fields=['source', 'external_id'],
                condition=~models.Q(external_id=''),
                name='mlb_team_source_external_id_unique',
            )
        ]

    def __str__(self):
        return self.name


class StartingPitcher(models.Model):
    """A pitcher who may be the probable/starting pitcher for a game.

    Stats are nullable because early in a season (or for newly called-up
    pitchers) aggregate stats may not yet exist. Rating is derived from
    stats where possible; when stats are missing, rating stays at the
    default and the game's confidence score reflects that.
    """
    THROWS_CHOICES = [
        ('L', 'Left'),
        ('R', 'Right'),
        ('S', 'Switch'),
    ]
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    team = models.ForeignKey(Team, on_delete=models.CASCADE, related_name='pitchers')
    name = models.CharField(max_length=100)
    external_id = models.CharField(max_length=50, blank=True, default='')
    source = models.CharField(max_length=30, choices=SOURCE_CHOICES, blank=True, default='')
    throws = models.CharField(max_length=1, choices=THROWS_CHOICES, blank=True, default='')

    # Raw season stats — all nullable
    era = models.FloatField(null=True, blank=True)
    whip = models.FloatField(null=True, blank=True)
    k_per_9 = models.FloatField(null=True, blank=True)
    innings_pitched = models.FloatField(null=True, blank=True)

    # Derived
    rating = models.FloatField(default=50.0)

    stats_updated_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['team', 'name']
        constraints = [
            models.UniqueConstraint(
                fields=['source', 'external_id'],
                condition=~models.Q(external_id=''),
                name='mlb_pitcher_source_external_id_unique',
            )
        ]

    def __str__(self):
        return f"{self.name} ({self.team.abbreviation or self.team.slug})"

    @property
    def has_stats(self):
        return self.era is not None and self.whip is not None and self.k_per_9 is not None


class Game(models.Model):
    STATUS_CHOICES = [
        ('scheduled', 'Scheduled'),
        ('live', 'Live'),
        ('final', 'Final'),
        ('postponed', 'Postponed'),
        ('cancelled', 'Cancelled'),
    ]
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    home_team = models.ForeignKey(Team, on_delete=models.CASCADE, related_name='home_games')
    away_team = models.ForeignKey(Team, on_delete=models.CASCADE, related_name='away_games')
    first_pitch = models.DateTimeField()
    neutral_site = models.BooleanField(default=False)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='scheduled')
    home_score = models.IntegerField(null=True, blank=True)
    away_score = models.IntegerField(null=True, blank=True)

    # Baseball-specific
    home_pitcher = models.ForeignKey(
        StartingPitcher, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='home_games',
    )
    away_pitcher = models.ForeignKey(
        StartingPitcher, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='away_games',
    )
    pitchers_updated_at = models.DateTimeField(null=True, blank=True)

    external_id = models.CharField(max_length=50, blank=True, default='')
    source = models.CharField(max_length=30, choices=SOURCE_CHOICES, blank=True, default='')

    class Meta:
        ordering = ['first_pitch']
        constraints = [
            models.UniqueConstraint(
                fields=['source', 'external_id'],
                condition=~models.Q(external_id=''),
                name='mlb_game_source_external_id_unique',
            )
        ]

    def __str__(self):
        return f"{self.away_team.name} @ {self.home_team.name} ({self.first_pitch.strftime('%m/%d %I:%M %p')})"


class OddsSnapshot(models.Model):
    """Mirror of CBB/CFB OddsSnapshot. `spread` stores the run line."""
    game = models.ForeignKey(Game, on_delete=models.CASCADE, related_name='odds_snapshots')
    captured_at = models.DateTimeField()
    sportsbook = models.CharField(max_length=50, default='consensus')
    market_home_win_prob = models.FloatField()
    market_away_win_prob = models.FloatField(null=True, blank=True)
    spread = models.FloatField(null=True, blank=True)
    total = models.FloatField(null=True, blank=True)
    moneyline_home = models.IntegerField(null=True, blank=True)
    moneyline_away = models.IntegerField(null=True, blank=True)

    class Meta:
        ordering = ['-captured_at']

    def __str__(self):
        return f"Odds for {self.game} at {self.captured_at}"

    def save(self, *args, **kwargs):
        if self.market_away_win_prob is None:
            self.market_away_win_prob = 1.0 - self.market_home_win_prob
        super().save(*args, **kwargs)


class InjuryImpact(models.Model):
    IMPACT_CHOICES = [
        ('low', 'Low'),
        ('med', 'Medium'),
        ('high', 'High'),
    ]
    game = models.ForeignKey(Game, on_delete=models.CASCADE, related_name='injuries')
    team = models.ForeignKey(Team, on_delete=models.CASCADE)
    impact_level = models.CharField(max_length=4, choices=IMPACT_CHOICES)
    notes = models.TextField(blank=True)

    def __str__(self):
        return f"{self.team.name} - {self.impact_level} ({self.game})"
