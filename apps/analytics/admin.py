from django.contrib import admin
from .models import UserGameInteraction, ModelResultSnapshot


@admin.register(UserGameInteraction)
class UserGameInteractionAdmin(admin.ModelAdmin):
    list_display = ['user', 'game', 'action', 'page_key', 'created_at']
    list_filter = ['action', 'page_key']


@admin.register(ModelResultSnapshot)
class ModelResultSnapshotAdmin(admin.ModelAdmin):
    list_display = ['game', 'market_prob', 'house_prob', 'data_confidence', 'house_model_version', 'captured_at']
    list_filter = ['data_confidence', 'house_model_version']
