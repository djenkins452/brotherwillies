from django.contrib import admin
from .models import SiteConfig, BettingRecommendation


@admin.register(SiteConfig)
class SiteConfigAdmin(admin.ModelAdmin):
    list_display = ('__str__', 'ai_temperature', 'ai_max_tokens')
    fieldsets = (
        ('AI Insight Settings', {
            'fields': ('ai_temperature', 'ai_max_tokens'),
            'description': (
                'Controls for the OpenAI-powered AI Insight feature on game detail pages. '
                'Temperature 0 = deterministic (most factual), higher = more creative variation.'
            ),
        }),
    )

    def has_add_permission(self, request):
        # Only allow one row
        return not SiteConfig.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(BettingRecommendation)
class BettingRecommendationAdmin(admin.ModelAdmin):
    list_display = ('sport', 'bet_type', 'pick', 'line', 'odds_american', 'confidence_score', 'model_edge', 'model_source', 'created_at')
    list_filter = ('sport', 'bet_type', 'model_source')
    search_fields = ('pick',)
    readonly_fields = ('id', 'created_at', 'feature_contributions_display')
    exclude = ('feature_contributions',)   # shown via display method

    def feature_contributions_display(self, obj):
        """v3.1: render the feature_contributions JSON in a staff-readable
        block. Degrades gracefully — pre-v3.1 rows show 'No data captured'."""
        import json
        from django.utils.html import format_html
        fc = obj.feature_contributions or {}
        if not fc:
            return format_html(
                '<em>No data captured (pre-v3.1 row, or sport not yet wired).</em>'
            )
        # Pretty-print to keep the diagnostic readable on long-tail data.
        try:
            pretty = json.dumps(fc, indent=2, sort_keys=True, default=str)
        except (TypeError, ValueError):
            pretty = str(fc)
        return format_html('<pre style="font-size:12px;">{}</pre>', pretty)
    feature_contributions_display.short_description = 'Feature contributions (v3.1)'
