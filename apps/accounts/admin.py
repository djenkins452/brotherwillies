from django.contrib import admin
from .models import UserProfile, UserModelConfig, ModelPreset, UserSubscription


@admin.register(UserProfile)
class UserProfileAdmin(admin.ModelAdmin):
    list_display = ['user', 'favorite_team', 'favorite_conference', 'preference_min_edge']


@admin.register(UserModelConfig)
class UserModelConfigAdmin(admin.ModelAdmin):
    list_display = ['user', 'rating_weight', 'hfa_weight', 'injury_weight', 'recent_form_weight', 'conference_weight']


@admin.register(ModelPreset)
class ModelPresetAdmin(admin.ModelAdmin):
    list_display = ['name', 'user', 'created_at']


@admin.register(UserSubscription)
class UserSubscriptionAdmin(admin.ModelAdmin):
    list_display = ['user', 'tier', 'created_at']
    list_filter = ['tier']
