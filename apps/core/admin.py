from django.contrib import admin
from .models import SiteConfig


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
