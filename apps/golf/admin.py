from django.contrib import admin
from .models import GolfEvent, Golfer, GolfRound


@admin.register(GolfEvent)
class GolfEventAdmin(admin.ModelAdmin):
    list_display = ['name', 'start_date', 'end_date']


@admin.register(Golfer)
class GolferAdmin(admin.ModelAdmin):
    list_display = ['name']


@admin.register(GolfRound)
class GolfRoundAdmin(admin.ModelAdmin):
    list_display = ['event', 'golfer', 'round_number', 'score']
    list_filter = ['event']
