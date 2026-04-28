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
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    sport = models.CharField(max_length=20, choices=SPORT_CHOICES, default='all')
    start_date = models.DateField(null=True, blank=True)
    end_date = models.DateField(null=True, blank=True)
    games_evaluated = models.IntegerField(default=0)
    games_skipped = models.IntegerField(default=0)
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
