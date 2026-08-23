from django.urls import path

from . import views


app_name = 'analytics'

urlpatterns = [
    path('backtest/', views.backtest_analytics, name='backtest'),
    path('backtest/run/', views.trigger_backtest, name='trigger_backtest'),
    # v3.3 SHADOW — bullpen historical backfill control (2026-08-22).
    # Staff triggers the ~11-min backfill from the browser; a background
    # thread runs the same code paths as ingest_reliever_appearances +
    # backfill_bullpen_snapshots and streams progress into a
    # BullpenBackfillRun row for live status.
    path(
        'bullpen-backfill/',
        views.bullpen_backfill, name='bullpen_backfill',
    ),
    path(
        'bullpen-backfill/trigger/',
        views.trigger_bullpen_backfill, name='trigger_bullpen_backfill',
    ),
    path(
        'bullpen-integrity/',
        views.bullpen_integrity_audit, name='bullpen_integrity',
    ),
    # v3.3 post-first-failure diagnostic (2026-08-22): read-only
    # 3-step probe of the MLB Stats API endpoints the backfill depends
    # on. Never writes data.
    path(
        'bullpen-api-check/',
        views.bullpen_api_check, name='bullpen_api_check',
    ),
    # v3.3 post-first-failure retry (2026-08-22): starts a new
    # backfill using the last failed/completed_with_errors run's
    # window. skip-existing carries forward the prior run's work.
    path(
        'bullpen-backfill/retry/',
        views.retry_bullpen_backfill, name='retry_bullpen_backfill',
    ),
    # v3.3 async experiment (2026-08-23): the sync
    # ?experiment=bullpen URL times out at production scale under
    # gunicorn's 30s worker limit. This page runs the same A/B/C
    # replay off the request thread and shows results when done.
    path(
        'bullpen-experiment/',
        views.bullpen_experiment, name='bullpen_experiment',
    ),
    path(
        'bullpen-experiment/trigger/',
        views.trigger_bullpen_experiment, name='trigger_bullpen_experiment',
    ),
    # v3.3 attribution study (2026-08-23): diagnostic breakdown of the
    # WHY behind the A/B/C degradation. Runs on the async framework
    # (same BullpenExperimentRun model, kind='attribution').
    path(
        'bullpen-experiment/attribution/',
        views.trigger_bullpen_attribution,
        name='trigger_bullpen_attribution',
    ),
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
