from django.contrib import admin
from .models import Conference, Team, Game, OddsSnapshot, InjuryImpact


@admin.register(Conference)
class ConferenceAdmin(admin.ModelAdmin):
    list_display = ['name', 'slug']
    prepopulated_fields = {'slug': ('name',)}


@admin.register(Team)
class TeamAdmin(admin.ModelAdmin):
    list_display = ['name', 'conference', 'rating', 'slug']
    list_filter = ['conference']
    search_fields = ['name']


@admin.register(Game)
class GameAdmin(admin.ModelAdmin):
    list_display = ['home_team', 'away_team', 'kickoff', 'status', 'neutral_site']
    list_filter = ['status', 'neutral_site']


@admin.register(OddsSnapshot)
class OddsSnapshotAdmin(admin.ModelAdmin):
    list_display = ['game', 'sportsbook', 'market_home_win_prob', 'spread', 'captured_at']
    list_filter = ['sportsbook']


@admin.register(InjuryImpact)
class InjuryImpactAdmin(admin.ModelAdmin):
    list_display = ['game', 'team', 'impact_level']
    list_filter = ['impact_level']
