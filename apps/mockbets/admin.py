from django.contrib import admin

from .models import MockBet, MockBetSettlementLog


@admin.register(MockBet)
class MockBetAdmin(admin.ModelAdmin):
    list_display = ['user', 'sport', 'bet_type', 'selection', 'odds_american', 'stake_amount', 'result', 'placed_at']
    list_filter = ['sport', 'bet_type', 'result', 'confidence_level', 'model_source']
    search_fields = ['user__username', 'selection', 'notes']
    readonly_fields = ['id', 'placed_at']
    raw_id_fields = ['user', 'cfb_game', 'cbb_game', 'golf_event', 'golf_golfer']


@admin.register(MockBetSettlementLog)
class MockBetSettlementLogAdmin(admin.ModelAdmin):
    list_display = ['mock_bet', 'result', 'payout', 'settled_at']
    list_filter = ['result']
    readonly_fields = ['settled_at']
    raw_id_fields = ['mock_bet']
