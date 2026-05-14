import uuid

from django.db import models
from django.contrib.auth.models import User


class UserGameInteraction(models.Model):
    ACTION_CHOICES = [
        ('viewed', 'Viewed'),
        ('evaluated', 'Evaluated'),
        ('parlay_leg_added', 'Parlay Leg Added'),
    ]
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='game_interactions')
    game = models.ForeignKey('cfb.Game', on_delete=models.CASCADE, null=True, blank=True, related_name='interactions')
    cbb_game = models.ForeignKey('cbb.Game', on_delete=models.CASCADE, null=True, blank=True, related_name='interactions')
    mlb_game = models.ForeignKey('mlb.Game', on_delete=models.CASCADE, null=True, blank=True, related_name='interactions')
    college_baseball_game = models.ForeignKey('college_baseball.Game', on_delete=models.CASCADE, null=True, blank=True, related_name='interactions')
    action = models.CharField(max_length=20, choices=ACTION_CHOICES)
    page_key = models.CharField(max_length=50, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.user.username} {self.action} {self.game}"


class ModelResultSnapshot(models.Model):
    CONFIDENCE_CHOICES = [
        ('low', 'Low'),
        ('med', 'Medium'),
        ('high', 'High'),
    ]
    game = models.ForeignKey('cfb.Game', on_delete=models.CASCADE, null=True, blank=True, related_name='result_snapshots')
    cbb_game = models.ForeignKey('cbb.Game', on_delete=models.CASCADE, null=True, blank=True, related_name='result_snapshots')
    mlb_game = models.ForeignKey('mlb.Game', on_delete=models.CASCADE, null=True, blank=True, related_name='result_snapshots')
    college_baseball_game = models.ForeignKey('college_baseball.Game', on_delete=models.CASCADE, null=True, blank=True, related_name='result_snapshots')
    captured_at = models.DateTimeField(auto_now_add=True)
    market_prob = models.FloatField()
    house_prob = models.FloatField()
    user_prob = models.FloatField(null=True, blank=True)
    house_model_version = models.CharField(max_length=20, default='v1')
    data_confidence = models.CharField(max_length=4, choices=CONFIDENCE_CHOICES, default='med')
    closing_market_prob = models.FloatField(null=True, blank=True)
    final_outcome = models.BooleanField(null=True, blank=True, help_text='True = home win')

    class Meta:
        ordering = ['-captured_at']

    def __str__(self):
        return f"Snapshot for {self.game}"


class BacktestRun(models.Model):
    """One execution of the backtesting framework — settings + summary metrics.

    Designed to be written once per run and read many times. Detail-level
    breakdowns (per edge bucket, calibration curve, CLV stats, etc.) all
    live inside `summary` as a JSON blob. Rationale: the data is small,
    the consumers are admin/debug views, and a JSON column keeps the
    schema simple — no need to normalize aggregations the UI doesn't query.

    `notes` flags methodology caveats — most importantly, when reconstructions
    fall back to recomputing with current ratings (because a historical
    ModelResultSnapshot wasn't captured at the time), the run is marked
    "approximate" so consumers know not to trust it as a true OOS backtest.
    """
    SPORT_CHOICES = [
        ('all', 'All sports'),
        ('cfb', 'CFB'),
        ('cbb', 'CBB'),
        ('mlb', 'MLB'),
        ('college_baseball', 'College Baseball'),
    ]
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('running', 'Running'),
        ('completed', 'Completed'),
        ('failed', 'Failed'),
    ]
    RATING_MODE_CHOICES = [
        ('static', 'Static (team.rating)'),
        ('elo', 'Dynamic Elo'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    sport = models.CharField(max_length=20, choices=SPORT_CHOICES, default='all')
    start_date = models.DateField(null=True, blank=True)
    end_date = models.DateField(null=True, blank=True)
    games_evaluated = models.IntegerField(default=0)
    games_skipped = models.IntegerField(default=0)
    # Run lifecycle — drives the analytics control page (running →
    # completed/failed). Default 'completed' so historical rows that
    # predate the control page show up correctly without backfill.
    status = models.CharField(
        max_length=20, choices=STATUS_CHOICES, default='completed', db_index=True,
    )
    # Which rating system the run used. Auto-populated by run_backtest from
    # the active USE_DYNAMIC_RATINGS setting (or its forced override). Drives
    # the Static vs Elo comparison cards on the analytics page.
    rating_mode = models.CharField(
        max_length=10, choices=RATING_MODE_CHOICES, default='static', db_index=True,
    )
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    error_message = models.TextField(blank=True, default='')
    is_approximate = models.BooleanField(
        default=False,
        help_text='True when any games were reconstructed using current '
                  'ratings instead of stored ModelResultSnapshot.house_prob.',
    )
    summary = models.JSONField(default=dict, blank=True)
    notes = models.TextField(blank=True, default='')

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"BacktestRun({self.sport}, {self.created_at:%Y-%m-%d %H:%M})"


class TeamEloHistory(models.Model):
    """Append-only log of every Elo rating change.

    Two rows per processed game (one per team). Powers the rebuild's
    idempotence check (`is this game already processed?`) and lets us
    reconstruct what a team's rating was at any point in time — the
    backtest service will eventually use this to do true historical
    reconstruction instead of approximating with current ratings.

    Polymorphic FKs match the MockBet pattern for cross-sport tables:
    one of the four `<sport>_team` and one of the four `<sport>_game`
    fields is set per row.
    """
    SPORT_CHOICES = [
        ('cfb', 'CFB'),
        ('cbb', 'CBB'),
        ('mlb', 'MLB'),
        ('college_baseball', 'College Baseball'),
    ]
    captured_at = models.DateTimeField(auto_now_add=True)
    sport = models.CharField(max_length=20, choices=SPORT_CHOICES, db_index=True)

    # Team being updated — exactly one is non-null per row, matching `sport`.
    cfb_team = models.ForeignKey(
        'cfb.Team', on_delete=models.CASCADE, null=True, blank=True,
        related_name='elo_history',
    )
    cbb_team = models.ForeignKey(
        'cbb.Team', on_delete=models.CASCADE, null=True, blank=True,
        related_name='elo_history',
    )
    mlb_team = models.ForeignKey(
        'mlb.Team', on_delete=models.CASCADE, null=True, blank=True,
        related_name='elo_history',
    )
    college_baseball_team = models.ForeignKey(
        'college_baseball.Team', on_delete=models.CASCADE, null=True, blank=True,
        related_name='elo_history',
    )

    # Game that caused the change — exactly one is non-null per row.
    cfb_game = models.ForeignKey(
        'cfb.Game', on_delete=models.CASCADE, null=True, blank=True,
        related_name='elo_history',
    )
    cbb_game = models.ForeignKey(
        'cbb.Game', on_delete=models.CASCADE, null=True, blank=True,
        related_name='elo_history',
    )
    mlb_game = models.ForeignKey(
        'mlb.Game', on_delete=models.CASCADE, null=True, blank=True,
        related_name='elo_history',
    )
    college_baseball_game = models.ForeignKey(
        'college_baseball.Game', on_delete=models.CASCADE, null=True, blank=True,
        related_name='elo_history',
    )

    pre_rating = models.FloatField()
    post_rating = models.FloatField()
    k_factor = models.FloatField()
    is_home = models.BooleanField()
    won = models.BooleanField()
    # Margin (winner - loser), nullable because MLB and college_baseball
    # don't use margin-of-victory in their Elo updates.
    margin = models.IntegerField(null=True, blank=True)
    margin_multiplier = models.FloatField(default=1.0)

    class Meta:
        ordering = ['-captured_at']
        indexes = [
            models.Index(fields=['sport', 'cfb_game']),
            models.Index(fields=['sport', 'cbb_game']),
            models.Index(fields=['sport', 'mlb_game']),
            models.Index(fields=['sport', 'college_baseball_game']),
        ]

    def __str__(self):
        team = (
            self.cfb_team or self.cbb_team
            or self.mlb_team or self.college_baseball_team
        )
        return f"EloHistory({self.sport}, {team}, {self.pre_rating:.1f} → {self.post_rating:.1f})"


# ---------------------------------------------------------------------------
# Recommendation Health Score snapshots (2026-05-14)
#
# Persists the composite Health Score plus its component breakdown each
# time `capture_health_snapshot` runs (typically: daily cron, before
# major model changes for baseline comparison, after major changes for
# regression detection).
#
# THIS MODEL CANNOT INFLUENCE RECOMMENDATIONS. It is a read-only
# governance ledger. The recommendation engine does not query it; the
# Health Score service writes to it; analytics surfaces read from it.
#
# Decoupled from BacktestRun by design: BacktestRun is "what would have
# happened if we replayed history under this rating mode" — synthetic
# but authoritative for outcomes. RecommendationHealthSnapshot is
# "what is the live engine looking like right now" — current state,
# cheap to compute, dense over time.
#
# Schema:
#   - overall_score: 0-100, weighted composite.
#   - dimension_scores: per-dimension JSON, stable keys defined by the
#     scoring service. Easy to extend without migrations.
#   - supporting_data: the raw aggregations used to compute scores.
#     Auditable — operator can re-derive a score from this blob alone.
#   - rating_mode_active: 'static' or 'elo'. Lets pre/post-Elo
#     comparisons be done against the actual state at capture time.
#   - calibration_state: snapshot of the calibration constants at
#     capture time (sigmoid divisor, blend weight, clamp bounds,
#     MIN_EDGE, etc.). Future calibration changes can be reasoned
#     about against historical snapshots that captured the prior state.
#   - notes: operator-supplied free text. Optional. Used for tagging
#     "pre-Elo baseline" or "after Phase 2C edge compression".

class RecommendationHealthSnapshot(models.Model):
    """Captured Health Score + component breakdown at a moment in time.

    Read-only from the recommendation engine's perspective. The engine
    never queries this table; analytics surfaces do.
    """
    BAND_CHOICES = [
        ('strong', 'Strong (≥75)'),
        ('healthy', 'Healthy (50–74)'),
        ('watch', 'Watch (25–49)'),
        ('intervene', 'Intervene (<25)'),
    ]
    RATING_MODE_CHOICES = [
        ('static', 'Static (team.rating)'),
        ('elo', 'Dynamic Elo'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    captured_at = models.DateTimeField(auto_now_add=True, db_index=True)
    overall_score = models.FloatField(db_index=True)
    band = models.CharField(
        max_length=10, choices=BAND_CHOICES, db_index=True,
        help_text='Derived from overall_score at capture time.',
    )
    # JSON blob with one key per dimension:
    #   {
    #     'clv_trend': {'score': 78.5, 'value': 0.52, 'sample': 84, 'status': 'strong'},
    #     'calibration': {'score': 62.0, 'brier': 0.225, 'sample': 84, 'status': 'healthy'},
    #     ...
    #   }
    # Keys + sub-shape locked by the scoring service's tests.
    dimension_scores = models.JSONField(default=dict)
    # JSON blob with the raw aggregations the scores were computed from
    # (per-bucket counts, CLV+ rate, mean disagreement, etc.). Lets the
    # operator re-derive a score from a snapshot without re-running the
    # scoring service.
    supporting_data = models.JSONField(default=dict)
    # Active rating mode at capture time. Reads is_dynamic_active().
    rating_mode_active = models.CharField(
        max_length=10, choices=RATING_MODE_CHOICES, default='static',
    )
    # Snapshot of the calibration constants at capture time. Stable
    # shape:
    #   {
    #     'market_blend_weight': 0.40,
    #     'prob_min': 0.52, 'prob_max': 0.85,
    #     'min_edge': 6.0, 'min_probability': 0.60,
    #     'extreme_disagreement_gap': 0.12,
    #   }
    calibration_state = models.JSONField(default=dict)
    # Free-text tag. Recommended uses:
    #   - 'pre-elo baseline'
    #   - 'post-elo cutover, day 1'
    #   - 'after calibration retune 2026-XX-XX'
    notes = models.TextField(blank=True, default='')

    class Meta:
        ordering = ['-captured_at']
        indexes = [
            models.Index(fields=['-captured_at', 'rating_mode_active']),
            models.Index(fields=['band', '-captured_at']),
        ]

    def __str__(self):
        return (
            f"HealthSnapshot({self.captured_at:%Y-%m-%d %H:%M} "
            f"score={self.overall_score:.1f} band={self.band})"
        )
