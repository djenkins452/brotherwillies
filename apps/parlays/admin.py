from django.contrib import admin
from .models import Parlay, ParlayLeg


class ParlayLegInline(admin.TabularInline):
    model = ParlayLeg
    extra = 0


@admin.register(Parlay)
class ParlayAdmin(admin.ModelAdmin):
    list_display = ['id', 'user', 'correlation_risk', 'implied_probability', 'house_probability', 'created_at']
    list_filter = ['correlation_risk']
    inlines = [ParlayLegInline]
