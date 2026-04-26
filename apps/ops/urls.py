from django.urls import path

from . import views

app_name = 'ops'

urlpatterns = [
    path('command-center/', views.command_center, name='command_center'),
    path('trigger/refresh-data/', views.trigger_refresh_data, name='trigger_refresh_data'),
    path('trigger/refresh-scores/', views.trigger_refresh_scores, name='trigger_refresh_scores'),
    path('trigger/test-odds-api/', views.trigger_test_odds_api, name='trigger_test_odds_api'),
    path('trigger/diagnose-mlb-gaps/', views.trigger_diagnose_mlb_odds_gaps, name='trigger_diagnose_mlb_odds_gaps'),
]
