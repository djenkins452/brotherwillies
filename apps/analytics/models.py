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
