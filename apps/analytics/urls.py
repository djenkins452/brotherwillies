from django.urls import path

from . import views


app_name = 'analytics'

urlpatterns = [
    path('backtest/', views.backtest_analytics, name='backtest'),
    path('backtest/run/', views.trigger_backtest, name='trigger_backtest'),
    # Phase 1A staff diagnostic — Model Input Inventory.
    # Re-runs the live model + recommender for one game and shows the
    # full input → score → calibration → edge → gate trace. Read-only.
    path(
        'model-inventory/',
        views.model_inventory_index,
        name='model_inventory_index',
    ),
    path(
        'model-inventory/mlb/<uuid:game_id>/',
        views.model_inventory_detail,
        name='model_inventory_detail',
    ),
    # Phase 1B Elo shadow review — side-by-side active vs alt-mode
    # comparison on recently-emitted MLB recommendations.
    path(
        'shadow-review/',
        views.shadow_review,
        name='shadow_review',
    ),
    # Recommendation Health Score (2026-05-14) — composite 0-100 score
    # across seven dimensions. Governance / observability surface;
    # cannot influence recommendation behavior.
    path(
        'health-score/',
        views.health_score,
        name='health_score',
    ),
    # Elo Activation Monitor (2026-05-16) — pre/post-cutover diagnostic
    # for the 2-3 week observation window. Read-only.
    path(
        'elo-monitor/',
        views.elo_monitor,
        name='elo_monitor',
    ),
    # Method Replay (2026-05-22) — retrospective MLB moneyline backtest.
    # Compares candidate methodologies (varying MARKET_BLEND_WEIGHT)
    # against actual historical outcomes. No-future-leakage by design.
    path(
        'method-replay/',
        views.method_replay,
        name='method_replay',
    ),
]
