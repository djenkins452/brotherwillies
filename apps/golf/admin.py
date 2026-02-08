from django.contrib import admin
from .models import GolfEvent, Golfer, GolfRound, GolfOddsSnapshot


@admin.register(GolfEvent)
class GolfEventAdmin(admin.ModelAdmin):
    list_display = ['name', 'start_date', 'end_date', 'external_id']


@admin.register(Golfer)
class GolferAdmin(admin.ModelAdmin):
    list_display = ['name', 'external_id']


@admin.register(GolfRound)
class GolfRoundAdmin(admin.ModelAdmin):
    list_display = ['event', 'golfer', 'round_number', 'score']
    list_filter = ['event']


@admin.register(GolfOddsSnapshot)
class GolfOddsSnapshotAdmin(admin.ModelAdmin):
    list_display = ['event', 'golfer', 'sportsbook', 'outright_odds', 'implied_prob', 'captured_at']
    list_filter = ['event', 'sportsbook']
