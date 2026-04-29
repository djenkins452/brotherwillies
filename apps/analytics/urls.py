from django.urls import path

from . import views


app_name = 'analytics'

urlpatterns = [
    path('backtest/', views.backtest_analytics, name='backtest'),
    path('backtest/run/', views.trigger_backtest, name='trigger_backtest'),
]
