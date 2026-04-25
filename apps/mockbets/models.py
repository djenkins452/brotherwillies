import uuid
from decimal import Decimal

from django.contrib.auth.models import User
from django.db import models
from django.utils import timezone


class MockBet(models.Model):
    SPORT_CHOICES = [
        ('cfb', 'College Football'),
        ('cbb', 'College Basketball'),
        ('golf', 'Golf'),
        ('mlb', 'MLB'),
        ('college_baseball', 'College Baseball'),
    ]

    # CFB/CBB bet types
    GAME_BET_TYPE_CHOICES = [
        ('moneyline', 'Moneyline'),
        ('spread', 'Spread'),
        ('total', 'Total'),
    ]

    # Golf-specific bet types
    GOLF_BET_TYPE_CHOICES = [
        ('outright', 'Outright Winner'),
        ('top_5', 'Top 5 Finish'),
        ('top_10', 'Top 10 Finish'),
        ('top_20', 'Top 20 Finish'),
        ('make_cut', 'Make the Cut'),
        ('matchup', 'Head-to-Head Matchup'),
    ]

    BET_TYPE_CHOICES = GAME_BET_TYPE_CHOICES + GOLF_BET_TYPE_CHOICES

    RESULT_CHOICES = [
        ('pending', 'Pending'),
        ('win', 'Win'),
        ('loss', 'Loss'),
        ('push', 'Push'),
    ]

    CONFIDENCE_CHOICES = [
        ('low', 'Low'),
        ('medium', 'Medium'),
        ('high', 'High'),
    ]

    MODEL_SOURCE_CHOICES = [
        ('house', 'House Model'),
        ('user', 'User Model'),
    ]

    REVIEW_CHOICES = [
        ('repeat', 'Would Repeat'),
        ('avoid', 'Would Avoid'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='mock_bets')
    # max_length=20 to fit the longest sport code ('college_baseball')
    sport = models.CharField(max_length=20, choices=SPORT_CHOICES)

    # Game FKs (nullable — only one will be set based on sport)
    cfb_game = models.ForeignKey(
        'cfb.Game', on_delete=models.CASCADE, null=True, blank=True, related_name='mock_bets'
    )
    cbb_game = models.ForeignKey(
        'cbb.Game', on_delete=models.CASCADE, null=True, blank=True, related_name='mock_bets'
    )
    golf_event = models.ForeignKey(
        'golf.GolfEvent', on_delete=models.CASCADE, null=True, blank=True, related_name='mock_bets'
    )
    golf_golfer = models.ForeignKey(
        'golf.Golfer', on_delete=models.CASCADE, null=True, blank=True, related_name='mock_bets'
    )
    mlb_game = models.ForeignKey(
        'mlb.Game', on_delete=models.CASCADE, null=True, blank=True, related_name='mock_bets'
    )
    college_baseball_game = models.ForeignKey(
        'college_baseball.Game', on_delete=models.CASCADE, null=True, blank=True, related_name='mock_bets'
    )

    bet_type = models.CharField(max_length=10, choices=BET_TYPE_CHOICES)
    selection = models.CharField(max_length=200)
    odds_american = models.IntegerField()
    implied_probability = models.DecimalField(max_digits=5, decimal_places=4)
    stake_amount = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('100.00'))
    simulated_payout = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    result = models.CharField(max_length=7, choices=RESULT_CHOICES, default='pending')
    confidence_level = models.CharField(max_length=6, choices=CONFIDENCE_CHOICES, default='medium')
    model_source = models.CharField(max_length=5, choices=MODEL_SOURCE_CHOICES, default='house')
    expected_edge = models.DecimalField(max_digits=6, decimal_places=4, null=True, blank=True)
    notes = models.TextField(blank=True)

    placed_at = models.DateTimeField(default=timezone.now)
    settled_at = models.DateTimeField(null=True, blank=True)

    # Decision review fields
    review_flag = models.CharField(max_length=6, choices=REVIEW_CHOICES, blank=True)
    review_notes = models.TextField(blank=True)

    # Snapshot of the decision-layer pick active when this bet was placed
    recommendation = models.ForeignKey(
        'core.BettingRecommendation',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='mock_bets',
    )

    # Denormalized snapshot of what the system KNEW at placement time. These
    # duplicate fields from the linked BettingRecommendation row intentionally
    # — they insulate historical analytics from future model/rule changes and
    # avoid a JOIN on the analytics hot path. See recommendation_performance.py.
    RECOMMENDATION_STATUS_CHOICES = [
        ('', ''),  # pre-migration bets
        ('recommended', 'Recommended'),
        ('not_recommended', 'Not Recommended'),
    ]
    RECOMMENDATION_TIER_CHOICES = [
        ('', ''),
        ('elite', 'Elite'),
        ('strong', 'Strong'),
        ('standard', 'Standard'),
    ]
    STATUS_REASON_CHOICES = [
        ('', ''),
        ('low_edge', 'Low Edge'),
        ('high_juice', 'High Juice Risk'),
        ('marginal', 'Marginal'),
    ]
    recommendation_status = models.CharField(
        max_length=20, choices=RECOMMENDATION_STATUS_CHOICES, blank=True, default=''
    )
    recommendation_tier = models.CharField(
        max_length=10, choices=RECOMMENDATION_TIER_CHOICES, blank=True, default=''
    )
    recommendation_confidence = models.DecimalField(
        max_digits=5, decimal_places=2, null=True, blank=True,
    )
    status_reason = models.CharField(
        max_length=20, choices=STATUS_REASON_CHOICES, blank=True, default='',
    )

    # Post-settlement loss analysis — populated by the settlement engine when
    # a bet resolves to 'loss'. See apps/mockbets/services/loss_analysis.py.
    LOSS_REASON_CHOICES = [
        ('', ''),
        ('variance', 'Bad Luck'),
        ('model_error', 'Model Miss'),
        ('market_movement', 'Market Misread'),
        ('bad_edge', 'Weak Edge'),
        ('unknown', 'Unknown'),
    ]
    loss_reason = models.CharField(
        max_length=20, choices=LOSS_REASON_CHOICES, blank=True, default='',
    )
    confidence_miss = models.DecimalField(
        max_digits=6, decimal_places=2, null=True, blank=True,
        help_text='Signed gap between snapshot confidence and market implied %',
    )
    edge_miss = models.DecimalField(
        max_digits=6, decimal_places=2, null=True, blank=True,
        help_text='Snapshot edge (pp) that did not pay off',
    )

    # Closing Line Value — the professional's signal. Captured at game start
    # by apps.mockbets.services.clv.capture_closing_odds. Positive value means
    # the bet's price beat the final market close.
    CLV_DIRECTION_CHOICES = [
        ('', ''),
        ('positive', 'Positive'),
        ('negative', 'Negative'),
    ]
    closing_odds_american = models.IntegerField(null=True, blank=True)
    clv_cents = models.FloatField(
        null=True, blank=True,
        help_text='Decimal-odds delta (bet_dec - close_dec). Positive = beat close.',
    )
    clv_direction = models.CharField(
        max_length=10, choices=CLV_DIRECTION_CHOICES, blank=True, default='',
    )

    class Meta:
        ordering = ['-placed_at']

    def __str__(self):
        return f"{self.user.username} - {self.selection} ({self.bet_type})"

    @property
    def game(self):
        """Return the associated game regardless of sport."""
        if self.sport == 'cfb':
            return self.cfb_game
        elif self.sport == 'cbb':
            return self.cbb_game
        elif self.sport == 'mlb':
            return self.mlb_game
        elif self.sport == 'college_baseball':
            return self.college_baseball_game
        return None

    @property
    def is_settled(self):
        return self.result != 'pending'

    def calculate_payout(self):
        """Calculate simulated payout based on result and odds."""
        if self.result == 'win':
            if self.odds_american > 0:
                return self.stake_amount * (Decimal(self.odds_american) / Decimal('100'))
            else:
                return self.stake_amount * (Decimal('100') / Decimal(abs(self.odds_american)))
        elif self.result == 'push':
            return self.stake_amount
        elif self.result == 'loss':
            return Decimal('0.00')
        return None

    @property
    def loss_reason_label(self):
        from apps.mockbets.services.loss_analysis import reason_label
        return reason_label(self.loss_reason)

    @property
    def loss_reason_details(self):
        from apps.mockbets.services.loss_analysis import reason_details
        return reason_details(self.loss_reason)

    # --- CLV display helpers -------------------------------------------------
    # Computed in the model (not the view) so every template that renders a
    # MockBet gets the same human-readable CLV surface without needing to
    # remember to decorate instances. Keeps logic out of templates per the
    # spec, and out of per-view boilerplate.
    @property
    def clv_percent_display(self):
        from apps.core.utils.odds import format_clv_percent
        return format_clv_percent(self.clv_cents)

    @property
    def clv_outcome_label(self):
        """Named 'clv_outcome_label' to avoid clashing with Django's auto-
        generated get_clv_direction_display or any future choice-derived accessor."""
        from apps.core.utils.odds import clv_label
        return clv_label(self.clv_cents)

    @property
    def line_movement_display(self):
        from apps.core.utils.odds import format_line_movement
        return format_line_movement(self.odds_american, self.closing_odds_american)

    # --- Decision Quality ---------------------------------------------------
    # Per-bet classification combining outcome with CLV: did the system make
    # a *good decision* even when the result was bad (or vice-versa)? CLV is
    # the leading-indicator signal; outcome is variance-bound. Together they
    # tell you if a bet was process-correct or process-flawed.
    #
    #   win  + +CLV → Perfect
    #   win  + -CLV → Got Lucky (process didn't earn it)
    #   loss + +CLV → Unlucky (process was right, variance hit)
    #   loss + -CLV → Bad Bet (process was wrong AND it lost)
    #   push        → Neutral
    #
    # Returns '' when the bet doesn't have CLV captured (pre-CLV bets,
    # bets with no closing snapshot) or is still pending — there's no
    # honest classification without both legs.
    DECISION_QUALITY_LABELS = {
        'perfect': 'Perfect',
        'lucky': 'Got Lucky',
        'unlucky': 'Unlucky',
        'bad': 'Bad Bet',
        'neutral': 'Neutral',
    }
    DECISION_QUALITY_CLASSES = {
        'perfect': 'dq-perfect',
        'lucky': 'dq-lucky',
        'unlucky': 'dq-unlucky',
        'bad': 'dq-bad',
        'neutral': 'dq-neutral',
    }

    @property
    def decision_quality(self) -> str:
        if self.result == 'pending':
            return ''
        if self.result == 'push':
            return 'neutral'
        if self.clv_cents is None:
            return ''
        if self.result == 'win':
            return 'perfect' if self.clv_direction == 'positive' else 'lucky'
        if self.result == 'loss':
            return 'unlucky' if self.clv_direction == 'positive' else 'bad'
        return ''

    @property
    def decision_quality_label(self) -> str:
        return self.DECISION_QUALITY_LABELS.get(self.decision_quality, '')

    @property
    def decision_quality_class(self) -> str:
        return self.DECISION_QUALITY_CLASSES.get(self.decision_quality, '')

    # --- Cancellation eligibility -------------------------------------------
    # A user can cancel (delete) a pending mock bet IF and only IF the
    # underlying game has not started yet. Once a game is live or final,
    # cancellation would distort analytics and CLV tracking.

    @property
    def can_cancel(self):
        """True when the user is still allowed to cancel this bet."""
        if self.result != 'pending':
            return False
        game = self.game
        if game is not None:
            # Team sports: live/final = locked
            if getattr(game, 'status', None) in ('live', 'final', 'postponed', 'cancelled'):
                return False
            # Time check — the cron may not have flipped status to 'live' yet
            # even if first pitch has passed. Use the time field as the
            # authoritative gate.
            from django.utils import timezone
            start = (
                getattr(game, 'first_pitch', None)
                or getattr(game, 'kickoff', None)
                or getattr(game, 'tipoff', None)
            )
            if start and start <= timezone.now():
                return False
            return True
        # Golf: cancel-ok until the event's start_date arrives
        if self.golf_event is not None:
            from django.utils import timezone
            return self.golf_event.start_date > timezone.now().date()
        # No game + no event → no way to verify → safer to allow cancel
        return True

    @property
    def net_result(self):
        """Net P/L for this bet (payout minus stake, or None if pending)."""
        if self.result == 'pending':
            return None
        if self.result == 'win':
            return self.simulated_payout
        elif self.result == 'push':
            return Decimal('0.00')
        else:
            return -self.stake_amount


class MockBetSettlementLog(models.Model):
    """Audit trail for settlement decisions."""
    mock_bet = models.ForeignKey(MockBet, on_delete=models.CASCADE, related_name='settlement_logs')
    settled_at = models.DateTimeField(auto_now_add=True)
    result = models.CharField(max_length=7)
    payout = models.DecimalField(max_digits=10, decimal_places=2)
    reason = models.TextField()

    class Meta:
        ordering = ['-settled_at']

    def __str__(self):
        return f"Settlement: {self.mock_bet} -> {self.result}"
