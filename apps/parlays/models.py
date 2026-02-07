import uuid
from django.db import models
from django.contrib.auth.models import User


class Parlay(models.Model):
    RISK_CHOICES = [
        ('low', 'Low'),
        ('med', 'Medium'),
        ('high', 'High'),
    ]
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='parlays')
    created_at = models.DateTimeField(auto_now_add=True)
    sportsbook = models.CharField(max_length=50, blank=True)
    total_odds = models.CharField(max_length=20, blank=True)
    implied_probability = models.FloatField(null=True, blank=True)
    house_probability = models.FloatField(null=True, blank=True)
    user_probability = models.FloatField(null=True, blank=True)
    correlation_risk = models.CharField(max_length=4, choices=RISK_CHOICES, default='low')
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"Parlay {self.id} by {self.user.username}"


class ParlayLeg(models.Model):
    MARKET_CHOICES = [
        ('spread', 'Spread'),
        ('moneyline', 'Moneyline'),
        ('total', 'Total'),
    ]
    parlay = models.ForeignKey(Parlay, on_delete=models.CASCADE, related_name='legs')
    game = models.ForeignKey('cfb.Game', on_delete=models.CASCADE)
    market_type = models.CharField(max_length=10, choices=MARKET_CHOICES)
    selection = models.CharField(max_length=100)
    odds = models.CharField(max_length=20, blank=True)
    market_prob = models.FloatField(null=True, blank=True)
    house_prob = models.FloatField(null=True, blank=True)
    user_prob = models.FloatField(null=True, blank=True)
    same_game_group = models.IntegerField(null=True, blank=True)

    def __str__(self):
        return f"{self.selection} ({self.market_type})"
