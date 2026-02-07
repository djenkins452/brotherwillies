from django.db import models
from django.contrib.auth.models import User
from django.db.models.signals import post_save
from django.dispatch import receiver


class UserProfile(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='profile')
    favorite_conference = models.ForeignKey(
        'cfb.Conference', on_delete=models.SET_NULL, null=True, blank=True
    )
    favorite_team = models.ForeignKey(
        'cfb.Team', on_delete=models.SET_NULL, null=True, blank=True
    )
    always_include_favorite_team = models.BooleanField(default=True)
    preference_spread_min = models.FloatField(null=True, blank=True)
    preference_spread_max = models.FloatField(null=True, blank=True)
    preference_odds_min = models.IntegerField(null=True, blank=True)
    preference_odds_max = models.IntegerField(null=True, blank=True)
    preference_min_edge = models.FloatField(default=0.0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Profile: {self.user.username}"


class UserModelConfig(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='model_configs')
    rating_weight = models.FloatField(default=1.0)
    hfa_weight = models.FloatField(default=1.0)
    injury_weight = models.FloatField(default=1.0)
    recent_form_weight = models.FloatField(default=1.0)
    conference_weight = models.FloatField(default=1.0)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-updated_at']

    def __str__(self):
        return f"Model config for {self.user.username}"

    @classmethod
    def get_or_create_for_user(cls, user):
        config = cls.objects.filter(user=user).first()
        if not config:
            config = cls.objects.create(user=user)
        return config


class ModelPreset(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='presets')
    name = models.CharField(max_length=100)
    rating_weight = models.FloatField(default=1.0)
    hfa_weight = models.FloatField(default=1.0)
    injury_weight = models.FloatField(default=1.0)
    recent_form_weight = models.FloatField(default=1.0)
    conference_weight = models.FloatField(default=1.0)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.name} ({self.user.username})"


class UserSubscription(models.Model):
    TIER_CHOICES = [
        ('free', 'Free'),
        ('pro', 'Pro'),
        ('elite', 'Elite'),
    ]
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='subscription')
    tier = models.CharField(max_length=5, choices=TIER_CHOICES, default='free')
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.user.username} - {self.tier}"


@receiver(post_save, sender=User)
def create_user_profile(sender, instance, created, **kwargs):
    if created:
        UserProfile.objects.create(user=instance)
        UserSubscription.objects.create(user=instance)
        UserModelConfig.objects.create(user=instance)


def user_has_feature(user, feature_key):
    if not user or not user.is_authenticated:
        return False
    try:
        tier = user.subscription.tier
    except UserSubscription.DoesNotExist:
        tier = 'free'
    features = {
        'full_value_board': ['free', 'pro', 'elite'],
        'parlay_scoring': ['free', 'pro', 'elite'],
        'multiple_presets': ['pro', 'elite'],
    }
    return tier in features.get(feature_key, [])
