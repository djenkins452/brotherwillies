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
    # Dynamic Elo rating — see apps.cfb.models.Team for documentation.
    elo_rating = models.FloatField(null=True, blank=True)
    elo_last_updated = models.DateTimeField(null=True, blank=True)

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


class TeamBullpenSnapshot(models.Model):
    """v3.3 SHADOW — Append-only per-team bullpen state timeline.

    Mirrors `OddsSnapshot`'s temporal-snapshot pattern (append-only,
    timestamped, indexed on `-as_of`) so historical reconstruction of
    a team's bullpen state before a specific first_pitch is possible.

    LEAKAGE DISCIPLINE — MANDATORY: every consumer must query with
    `as_of__lt=game.first_pitch`, NEVER `as_of__lte`. The `<` is what
    keeps a snapshot captured minutes after game start from bleeding
    into the pre-game decision. The `apps/mlb/services/bullpen.py`
    helpers enforce this by construction and a leakage test locks it.

    ANTI-PATTERN WARNING: the existing MLB `Team.wins`/`Team.losses`
    fields are OVERWRITTEN IN PLACE by `MLBTeamRecordProvider.persist`
    (apps/datahub/providers/mlb/team_record_provider.py). That pattern
    silently BREAKS historical replay because every row collapses to
    "latest". This snapshot table must NEVER be treated that way —
    every ingest run creates a NEW row with a fresh `as_of`, and
    replays walk backward from there.

    DATA STATUS (2026-08-22): the ingestion command
    `apps/datahub/management/commands/ingest_bullpen_snapshots.py` is
    scaffolded but NOT yet wired to a real data source. Until it is,
    this table is empty and `apps.mlb.services.bullpen.team_bullpen_signal`
    returns (0.0, 0.0, 'low') — the honest posture per the v3.3 brief
    ("if the available data cannot support historically correct
    reconstruction, STOP and report rather than build a misleading
    replay").
    """
    team = models.ForeignKey(
        Team, on_delete=models.CASCADE, related_name='bullpen_snapshots',
    )
    as_of = models.DateTimeField(db_index=True)

    # --- Quality signals (rolling ~30d unless the ingest source dictates
    # otherwise; the ingest command records its rolling window in `notes`).
    bullpen_era = models.FloatField(null=True, blank=True)
    bullpen_whip = models.FloatField(null=True, blank=True)
    bullpen_k_per_9 = models.FloatField(null=True, blank=True)
    bullpen_bb_per_9 = models.FloatField(null=True, blank=True)
    bullpen_ip_last30 = models.FloatField(null=True, blank=True)

    # --- Fatigue signals (v3.3-B; empty until reliever-appearance
    # ingestion ships — the design's Phase 2B in docs/v3_2_bullpen_design.md).
    appearances_last_1_day = models.IntegerField(null=True, blank=True)
    appearances_last_2_days = models.IntegerField(null=True, blank=True)
    appearances_last_3_days = models.IntegerField(null=True, blank=True)
    high_leverage_rest_days_min = models.IntegerField(null=True, blank=True)
    top_reliever_available = models.BooleanField(null=True, blank=True)

    # --- Data provenance.
    source = models.CharField(max_length=30, choices=SOURCE_CHOICES, blank=True, default='')
    data_confidence = models.CharField(
        max_length=6,
        choices=[('low', 'Low'), ('med', 'Medium'), ('high', 'High')],
        default='low',
    )
    notes = models.CharField(max_length=200, blank=True, default='')

    class Meta:
        ordering = ['-as_of']
        indexes = [
            models.Index(fields=['team', '-as_of']),
        ]

    def __str__(self):
        return f"BullpenSnapshot[{self.team.abbreviation or self.team.name}] {self.as_of.isoformat() if self.as_of else 'no-date'}"


class TeamBattingSnapshot(models.Model):
    """v3.4 team-offense phase 2 — season-to-date team hitting counts.

    One row per (team, as_of_date). `as_of_date` is the last date
    included in the aggregate (inclusive). Values come from MLB
    Stats API's `/v1/teams/{id}/stats?stats=byDateRange&group=hitting`
    with `startDate=season_start` and `endDate=as_of_date`.

    Why season-to-date raw counts (not derived rate stats):
      * Rolling-30d OPS/OBP/SLG is derivable by SUBTRACTING two
        snapshots (STD(D-1) minus STD(D-31)). One fetch per date
        instead of two.
      * Derived stats (OBP/SLG/OPS) computed at read time from raw
        counts using standard formulas — no rounding drift, no
        formula-drift-across-callers risk.
      * Reproducible from the raw payload alone; audit friendly.

    LEAKAGE DISCIPLINE — MANDATORY: every consumer queries with
    `as_of_date < game.first_pitch.date()`. Strict `<`. The
    `team_offense_v2` service enforces this by construction.

    IDEMPOTENCY: unique on (team, as_of_date). Re-ingesting the same
    date updates in place. Ingest is safe to retry any number of
    times.

    STATUS (2026-08-23): populated by `ingest_team_batting` command
    (Railway cron + one-shot backfill). Empty until first ingestion
    run completes; consumers return zero + 'low' confidence in that
    state. Same honest posture used by the bullpen shadow before
    its historical reconstruction shipped.
    """
    team = models.ForeignKey(
        Team, on_delete=models.CASCADE, related_name='batting_snapshots',
    )
    as_of_date = models.DateField(db_index=True)
    season = models.IntegerField(db_index=True)

    # --- Raw hitting counts, season-to-date through as_of_date.
    plate_appearances = models.IntegerField(default=0)
    at_bats = models.IntegerField(default=0)
    hits = models.IntegerField(default=0)
    doubles = models.IntegerField(default=0)
    triples = models.IntegerField(default=0)
    home_runs = models.IntegerField(default=0)
    walks = models.IntegerField(default=0)
    hit_by_pitch = models.IntegerField(default=0)
    sac_flies = models.IntegerField(default=0)
    strikeouts = models.IntegerField(default=0)
    runs = models.IntegerField(default=0)
    games_played = models.IntegerField(default=0)

    # --- Rate stats as reported by the API (rounded — we recompute
    # from raw counts at read time; these are stored only for audit).
    obp_reported = models.FloatField(null=True, blank=True)
    slg_reported = models.FloatField(null=True, blank=True)
    ops_reported = models.FloatField(null=True, blank=True)

    source = models.CharField(
        max_length=30, choices=SOURCE_CHOICES, blank=True, default='',
    )
    ingested_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-as_of_date']
        indexes = [
            models.Index(fields=['team', '-as_of_date']),
            models.Index(fields=['season', '-as_of_date']),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=['team', 'as_of_date'],
                name='mlb_team_batting_snapshot_team_asof_unique',
            ),
        ]

    def __str__(self):
        return (
            f'TeamBattingSnapshot[{self.team.abbreviation or self.team.slug}] '
            f'{self.as_of_date} PA={self.plate_appearances} '
            f'OPS={self.ops_reported}'
        )


class RelieverAppearance(models.Model):
    """v3.3 SHADOW — one row per pitcher per game appearance.

    Append-only raw data source that feeds the deterministic
    `apps.mlb.services.bullpen_builder`. From this table + a target
    `reference_date` T, the builder deterministically produces the
    bullpen state that would have been known immediately BEFORE T
    (leakage-safe filter: `game__first_pitch__lt=T`).

    Populated by
    `apps/datahub/management/commands/ingest_reliever_appearances.py`
    from MLB Stats API `/api/v1/game/{gamePk}/boxscore`. Same command
    handles both historical backfill and daily forward updates — one
    code path, no drift risk between historical reconstruction and
    production computation.

    Every field derives from a single boxscore payload:
      * gamesStarted (from boxscore pitcher stats) → is_starter
      * inningsPitched → outs_recorded (0.1=1 out, 0.2=2 outs, 1.0=3)
      * numberOfPitches → pitches
      * hits / earnedRuns / baseOnBalls / strikeOuts / homeRuns
      * saves / holds → is_save / is_hold

    LEAKAGE: consumers query `game__first_pitch__lt=reference_date`.
    Strict `<`. Same L1 pattern used by OddsSnapshot and TeamBullpenSnapshot.

    IDEMPOTENCY: unique constraint on (game, pitcher). Re-ingesting a
    boxscore updates existing rows in place; no duplicates.
    """
    game = models.ForeignKey(
        Game, on_delete=models.CASCADE, related_name='pitcher_appearances',
    )
    team = models.ForeignKey(
        Team, on_delete=models.CASCADE, related_name='pitcher_appearances',
    )
    pitcher = models.ForeignKey(
        StartingPitcher, on_delete=models.CASCADE,
        related_name='appearances',
        help_text='StartingPitcher row is reused for relievers too; the '
                  'name is legacy from v3.0. is_starter distinguishes.',
    )
    is_starter = models.BooleanField(db_index=True)
    outs_recorded = models.IntegerField(
        default=0,
        help_text='Total outs. 3.1 IP = 10 outs.',
    )
    pitches = models.IntegerField(null=True, blank=True)
    hits = models.IntegerField(default=0)
    earned_runs = models.IntegerField(default=0)
    walks = models.IntegerField(default=0)
    strikeouts = models.IntegerField(default=0)
    home_runs = models.IntegerField(default=0)
    is_save = models.BooleanField(default=False)
    is_hold = models.BooleanField(default=False)
    ingested_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-game__first_pitch']
        constraints = [
            models.UniqueConstraint(
                fields=['game', 'pitcher'],
                name='mlb_reliever_appearance_game_pitcher_unique',
            ),
        ]
        indexes = [
            models.Index(fields=['team', '-game']),
            models.Index(fields=['pitcher', '-game']),
            models.Index(fields=['team', 'is_starter']),
        ]

    def __str__(self):
        role = 'S' if self.is_starter else 'R'
        return f"{role} {self.pitcher.name} {self.outs_recorded}o / {self.pitches}p"


class ConfirmedLineup(models.Model):
    """v3.4 SHADOW — per-observation pregame lineup snapshot.

    APPEND-ONLY. Every time `ingest_lineups` observes a non-empty
    lineup for (game, team) that DIFFERS from the last stored lineup
    for the same pair, a new row is written. Timestamps preserve
    first-seen truth (never overwritten) — the change history is
    reconstructable by ordering rows by `observed_at`.

    Why per-observation rows (not a single mutable JSON blob):
      * A lineup change AFTER first confirmation is itself a signal
        (a late scratch may or may not affect the win probability
        differently than the originally-posted lineup).
      * Backwards compatibility with future replay: we can query
        "what did BW know at time T?" by taking the latest row where
        `observed_at < T`.
      * Preserves the FIRST_SEEN_AT truth even if MLB later corrects
        the lineup payload.

    LEAKAGE DISCIPLINE (locked by test in later commit):
      Consumers MUST query `observed_at < reference_date`, NEVER
      `<=`. Strict inequality prevents a lineup posted at first
      pitch from being treated as pregame knowledge.

      For historical games where NO row exists with observed_at <
      first_pitch, the game must be classified UNCOVERED — the
      shadow signal must degrade to zero, never fabricated from
      postgame boxscore batting order.
    """
    STATE_CHOICES = [
        # No non-empty lineup ever observed. Not written to DB — the
        # absence of any row for (game, team) implicitly means unknown.
        ('unknown', 'Unknown'),
        # First non-empty lineup observed BEFORE this game's first_pitch.
        # This is the pregame confirmation state we can legitimately act on.
        ('confirmed', 'Confirmed pregame'),
        # A subsequent observation showed a DIFFERENT lineup after we
        # had already recorded a confirmed one. Preserves late-scratch
        # semantics — the ORIGINAL confirmed row remains untouched;
        # this new row records the updated state and the delta.
        ('updated_after_confirmation', 'Updated after confirmation'),
        # First observation happened AT OR AFTER first_pitch. Cannot be
        # treated as legitimate pregame knowledge — usable for coverage
        # analysis, NEVER for scoring.
        ('post_first_pitch', 'Observed after first pitch'),
    ]

    game = models.ForeignKey(
        Game, on_delete=models.CASCADE, related_name='confirmed_lineups',
    )
    team = models.ForeignKey(
        Team, on_delete=models.CASCADE, related_name='confirmed_lineups',
    )
    observed_at = models.DateTimeField(
        db_index=True,
        help_text=(
            'Wall-clock timestamp of the poll that first saw THIS specific '
            'lineup for this (game, team). Never overwritten.'
        ),
    )
    # 9-element list of {order: 1..9, player_id, name, position}.
    # Position taken from the schedule payload's primaryPosition.abbreviation.
    # Additional fields (handedness etc.) may be added later without
    # migration since it's a JSONField.
    players = models.JSONField(default=list)
    lineup_state = models.CharField(
        max_length=32, choices=STATE_CHOICES, default='confirmed',
        db_index=True,
    )
    source = models.CharField(
        max_length=30, choices=SOURCE_CHOICES, default='mlb_stats_api',
    )
    data_confidence = models.CharField(
        max_length=6,
        choices=[('low', 'Low'), ('med', 'Medium'), ('high', 'High')],
        default='high',
    )
    # Raw payload subset (the `lineups.{side}Players` block) for audit;
    # bounded in size to avoid unbounded row growth.
    raw_snapshot = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ['-observed_at']
        indexes = [
            models.Index(fields=['game', 'team', 'observed_at']),
            models.Index(fields=['game', '-observed_at']),
            models.Index(fields=['team', '-observed_at']),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=['game', 'team', 'observed_at'],
                name='mlb_confirmed_lineup_unique_observation',
            ),
        ]

    def __str__(self):
        return (
            f"Lineup[{self.team.abbreviation or self.team.slug} "
            f"@ {self.observed_at.isoformat() if self.observed_at else '?'}] "
            f"({self.lineup_state})"
        )


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


OPPORTUNITY_OUTCOME_CHOICES = [
    ('win', 'Win'),     # the signal's evaluated direction won
    ('loss', 'Loss'),   # the signal's evaluated direction lost
    ('push', 'Push'),   # exact tie at the line — excluded from win-rate math
]


class SpreadOpportunity(models.Model):
    """Rule-based signal on the run-line market. NOT a recommendation.

    Phase 1 stored the signal + source. Phase 2 adds:
      * Settlement: outcome / settled_at populated when the underlying
        game finalizes. Settlement convention is FIXED per signal_type
        (see `EVALUATED_DIRECTION` below) so the win-rate math is
        deterministic and can be back-tested.
      * Lean classification: at signal-creation time, the generator
        looks up historical win rate for this signal_type and stamps
        `is_lean=True` only when the data clears the threshold.
        These columns are SNAPSHOTTED, not computed on read — so the
        UI can show "56% win rate, 42 games" cheaply, and a future
        threshold change wouldn't retroactively re-label old rows.
    """

    SIGNAL_CHOICES = [
        ('tight_spread', 'Tight Spread'),       # |spread| <= 1.5
        ('large_favorite', 'Large Favorite'),   # |spread| >= 2.5
    ]

    # Per-signal convention for "what does a win mean?". Documented at
    # the model layer because the answer is a product decision, not a
    # math property — and it's load-bearing for every downstream metric.
    #
    #   tight_spread     → 'underdog' covers (small dogs in MLB
    #                      historically perform well against the run line)
    #   large_favorite   → 'favorite' covers (testing the conventional
    #                      wisdom that heavy favorites cover often
    #                      enough to be exploitable)
    #
    # If the data doesn't support a direction, is_lean stays False —
    # the convention only determines which side is being TESTED, not
    # the conclusion.
    EVALUATED_DIRECTION = {
        'tight_spread': 'underdog',
        'large_favorite': 'favorite',
    }

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

    # ---- Phase 2: Lean classification ----
    # Snapshotted at creation time so threshold-tweak / data-drift can
    # never retroactively flip an old row's lean status.
    is_lean = models.BooleanField(default=False, db_index=True)
    historical_win_rate = models.FloatField(null=True, blank=True)  # 0.0..1.0
    sample_size = models.IntegerField(null=True, blank=True)

    # ---- Phase 3: Promoted recommendation ----
    # Stricter bar than is_lean. When True, this signal is treated as a
    # full recommendation (rendered in its own section, eligible for
    # bulk-bet placement). Source safety: ESPN-source rows and
    # is_derived rows are NEVER promoted, regardless of stats — the
    # promotion classifier short-circuits on those.
    is_recommended = models.BooleanField(default=False, db_index=True)
    # ROI estimate (decimal, 0..1) at -110 standard pricing, snapshotted
    # at create time. Stored separately from historical_win_rate because
    # the UI renders both ("57.2% vs 52.4%, +3.1% ROI").
    roi = models.FloatField(null=True, blank=True)

    # ---- Phase 2: Settlement ----
    outcome = models.CharField(
        max_length=10, choices=OPPORTUNITY_OUTCOME_CHOICES, blank=True, default='',
        db_index=True,
    )
    settled_at = models.DateTimeField(null=True, blank=True)

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
            models.Index(fields=['outcome']),
            models.Index(fields=['is_lean']),
            models.Index(fields=['is_recommended']),
        ]

    def __str__(self):
        return f"{self.get_signal_type_display()} {self.spread:+.1f} for {self.game}"


class TotalOpportunity(models.Model):
    """Rule-based signal on the runs over/under market. NOT a recommendation.

    See SpreadOpportunity docstring — same Phase 2 additions
    (settlement + lean classification).
    """

    SIGNAL_CHOICES = [
        ('high_scoring', 'High Scoring'),  # total >= 9.5
        ('low_scoring', 'Low Scoring'),    # total <= 7.5
    ]

    # 'over' means betting the OVER hits (combined score > total).
    # 'under' means betting the UNDER hits.
    EVALUATED_DIRECTION = {
        'high_scoring': 'over',
        'low_scoring': 'under',
    }

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

    # ---- Phase 2: Lean classification ----
    is_lean = models.BooleanField(default=False, db_index=True)
    historical_win_rate = models.FloatField(null=True, blank=True)
    sample_size = models.IntegerField(null=True, blank=True)

    # ---- Phase 3: Promoted recommendation (see SpreadOpportunity) ----
    is_recommended = models.BooleanField(default=False, db_index=True)
    roi = models.FloatField(null=True, blank=True)

    # ---- Phase 2: Settlement ----
    outcome = models.CharField(
        max_length=10, choices=OPPORTUNITY_OUTCOME_CHOICES, blank=True, default='',
        db_index=True,
    )
    settled_at = models.DateTimeField(null=True, blank=True)

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
            models.Index(fields=['outcome']),
            models.Index(fields=['is_lean']),
            models.Index(fields=['is_recommended']),
        ]

    def __str__(self):
        return f"{self.get_signal_type_display()} O/U {self.total:.1f} for {self.game}"
