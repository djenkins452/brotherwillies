from django.contrib import admin
from .models import Conference, Team, StartingPitcher, Game, OddsSnapshot, InjuryImpact


@admin.register(Conference)
class ConferenceAdmin(admin.ModelAdmin):
    list_display = ('name', 'slug')
    prepopulated_fields = {'slug': ('name',)}


@admin.register(Team)
class TeamAdmin(admin.ModelAdmin):
    list_display = ('name', 'abbreviation', 'conference', 'rating', 'source')
    list_filter = ('conference', 'source')
    search_fields = ('name', 'abbreviation', 'slug')


@admin.register(StartingPitcher)
class StartingPitcherAdmin(admin.ModelAdmin):
    list_display = ('name', 'team', 'throws', 'era', 'whip', 'k_per_9', 'rating', 'stats_updated_at')
    list_filter = ('team', 'throws', 'source')
    search_fields = ('name',)
    readonly_fields = ('id', 'created_at', 'updated_at')


@admin.register(Game)
class GameAdmin(admin.ModelAdmin):
    list_display = ('home_team', 'away_team', 'first_pitch', 'status', 'home_pitcher', 'away_pitcher')
    list_filter = ('status',)
    raw_id_fields = ('home_pitcher', 'away_pitcher')
    readonly_fields = ('pitchers_updated_at',)


@admin.register(OddsSnapshot)
class OddsSnapshotAdmin(admin.ModelAdmin):
    list_display = ('game', 'sportsbook', 'market_home_win_prob', 'spread', 'total', 'captured_at')
    list_filter = ('sportsbook',)


@admin.register(InjuryImpact)
class InjuryImpactAdmin(admin.ModelAdmin):
    list_display = ('game', 'team', 'impact_level')
    list_filter = ('impact_level',)
