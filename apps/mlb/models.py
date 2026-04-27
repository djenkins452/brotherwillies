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
    wins = models.IntegerField(null=True, blank=True)
    losses = models.IntegerField(null=True, blank=True)

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
    wins = models.IntegerField(null=True, blank=True)
    losses = models.IntegerField(null=True, blank=True)

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

# Auto-failover layer (Commit 1 of Provider Health Reliability):
# Snapshots tag which provider supplied the data and at what quality.
# odds_source is the literal source (which API answered).
# source_quality is the *interpretation* — primary means "the preferred
# provider answered fresh", fallback means "secondary provider answered",
# stale means "we didn't get fresh data, this is from cache/older snap",
# unavailable means "no data — recommendation surfaces should treat as missing."
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


class OddsSnapshot(models.Model):
    """Mirror of CBB/CFB OddsSnapshot. `spread` stores the run line.

    snapshot_type: every API pull lands as 'raw'. When the movement
      detector decides a row crosses the significance threshold the
      type is upgraded to 'significant' and movement_score/movement_class
      are populated. 'closing' and 'bet_context' rows are never pruned.
    movement_score: 0..100, computed on write only (never on read).
    movement_class: bucketed score for fast UI lookups.
    """
    game = models.ForeignKey(Game, on_delete=models.CASCADE, related_name='odds_snapshots')
    captured_at = models.DateTimeField()
    sportsbook = models.CharField(max_length=50, default='consensus')
    market_home_win_prob = models.FloatField()
    market_away_win_prob = models.FloatField(null=True, blank=True)
    spread = models.FloatField(null=True, blank=True)
    total = models.FloatField(null=True, blank=True)
    moneyline_home = models.IntegerField(null=True, blank=True)
    moneyline_away = models.IntegerField(null=True, blank=True)
    snapshot_type = models.CharField(
        max_length=20, choices=SNAPSHOT_TYPE_CHOICES, default='raw', db_index=True,
    )
    movement_score = models.FloatField(null=True, blank=True)
    movement_class = models.CharField(
        max_length=10, choices=MOVEMENT_CLASS_CHOICES, null=True, blank=True,
    )
    # Provider Health Reliability layer — see SNAPSHOT_SOURCE_CHOICES above.
    odds_source = models.CharField(
        max_length=20, choices=SNAPSHOT_SOURCE_CHOICES, default='odds_api', db_index=True,
    )
    source_quality = models.CharField(
        max_length=15, choices=SNAPSHOT_SOURCE_QUALITY_CHOICES, default='primary',
    )
    # is_derived flags snapshots whose moneyline values were not directly
    # observed but synthesized — e.g., when ESPN gave us only one side of
    # the line and we filled the other via symmetric inversion. These rows
    # are excluded from primary betting decisions: the recommendation
    # engine blocks them, the UI hides them outside staff diagnostics, and
    # bulk-bet actions never include them. Default False so any row that
    # doesn't explicitly set it (older data, primary path) is treated as
    # genuine market data.
    is_derived = models.BooleanField(default=False, db_index=True)

    class Meta:
        ordering = ['-captured_at']
        indexes = [
            models.Index(fields=['game', '-captured_at']),
            models.Index(fields=['snapshot_type', '-captured_at']),
        ]

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


# --------------------------------------------------------------------- #
# Tiered Intelligence — Phase 1 Opportunity Signals (NOT recommendations)
#
# Spread + Total signals live in their own tables, separate from the
# Moneyline BettingRecommendation pipeline. They are RULE-BASED — no
# model inference, no edge math, no tier assignment. The UI labels them
# "Opportunity Signal — informational only" and they are NEVER mixed
# into "Bet All" actions.
#
# Why separate tables instead of polymorphic columns on a shared model:
#   - Different schema (favorite/underdog only matters for spread).
#   - Different signal vocabularies (tight/large vs high/low).
#   - Independent extensibility (we'll likely add more spread signals
#     than total signals over time).
#   - Zero risk of accidentally widening the BettingRecommendation
#     surface, which the Moneyline guardrails depend on.
#
# Idempotency contract: at most one row per (game, odds_snapshot,
# signal_type) — enforced by UniqueConstraint. Running the generator
# twice on the same snapshot is a no-op. This matters because the
# post-save hook fires on every snapshot insert, including ESPN
# fallback writes that re-cover games primary already wrote for.
# --------------------------------------------------------------------- #


class SpreadOpportunity(models.Model):
    """Rule-based signal on the run-line market. NOT a recommendation."""

    SIGNAL_CHOICES = [
        ('tight_spread', 'Tight Spread'),       # |spread| <= 1.5
        ('large_favorite', 'Large Favorite'),   # |spread| >= 2.5
    ]

    game = models.ForeignKey(
        Game, on_delete=models.CASCADE, related_name='spread_opportunities',
    )
    odds_snapshot = models.ForeignKey(
        'OddsSnapshot', on_delete=models.CASCADE,
        related_name='spread_opportunities',
    )
    signal_type = models.CharField(max_length=30, choices=SIGNAL_CHOICES, db_index=True)
    # Stored from the home-team perspective, same convention as
    # OddsSnapshot.spread. UI uses the spread_display template filter
    # to render either side correctly.
    spread = models.FloatField()
    favorite_team_name = models.CharField(max_length=120, blank=True)
    underdog_team_name = models.CharField(max_length=120, blank=True)
    # Carried through from the source snapshot so the Spread tile can
    # render the same Verified/ESPN/Derived badge family without an
    # extra join. Stays in sync because the signal is regenerated only
    # via the post_save hook, which always reads the snapshot's source.
    source = models.CharField(max_length=20, default='odds_api', db_index=True)
    source_quality = models.CharField(max_length=15, default='primary')
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ['-created_at']
        constraints = [
            models.UniqueConstraint(
                fields=['game', 'odds_snapshot', 'signal_type'],
                name='uniq_spread_signal_per_snapshot',
            ),
        ]
        indexes = [
            models.Index(fields=['game', '-created_at']),
            models.Index(fields=['signal_type', '-created_at']),
        ]

    def __str__(self):
        return f"{self.get_signal_type_display()} {self.spread:+.1f} for {self.game}"


class TotalOpportunity(models.Model):
    """Rule-based signal on the runs over/under market. NOT a recommendation."""

    SIGNAL_CHOICES = [
        ('high_scoring', 'High Scoring'),  # total >= 9.5
        ('low_scoring', 'Low Scoring'),    # total <= 7.5
    ]

    game = models.ForeignKey(
        Game, on_delete=models.CASCADE, related_name='total_opportunities',
    )
    odds_snapshot = models.ForeignKey(
        'OddsSnapshot', on_delete=models.CASCADE,
        related_name='total_opportunities',
    )
    signal_type = models.CharField(max_length=30, choices=SIGNAL_CHOICES, db_index=True)
    total = models.FloatField()
    source = models.CharField(max_length=20, default='odds_api', db_index=True)
    source_quality = models.CharField(max_length=15, default='primary')
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ['-created_at']
        constraints = [
            models.UniqueConstraint(
                fields=['game', 'odds_snapshot', 'signal_type'],
                name='uniq_total_signal_per_snapshot',
            ),
        ]
        indexes = [
            models.Index(fields=['game', '-created_at']),
            models.Index(fields=['signal_type', '-created_at']),
        ]

    def __str__(self):
        return f"{self.get_signal_type_display()} O/U {self.total:.1f} for {self.game}"
