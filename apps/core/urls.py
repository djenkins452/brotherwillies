from django.urls import path
from . import views

app_name = 'core'

urlpatterns = [
    # `/` → Command Center homepage (2026-05-03). The legacy
    # mock-bet analytics dashboard moved to /home-analytics/ as a
    # backwards-compat fallback. The URL name 'home' is preserved so
    # any reverse('core:home') call still works and now resolves to
    # the Command Center.
    path('', views.command_center_home, name='home'),
    path('home-analytics/', views.home, name='home_analytics'),
    path('lobby/', views.value_board, name='value_board'),
    path('api/ai-insight/<str:sport>/<uuid:game_id>/', views.ai_insight_view, name='ai_insight'),
    path('backtest/', views.backtest_results, name='backtest_results'),
]
