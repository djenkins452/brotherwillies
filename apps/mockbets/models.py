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
