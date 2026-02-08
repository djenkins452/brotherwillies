from django.urls import path

from . import views

app_name = 'mockbets'

urlpatterns = [
    path('', views.my_bets, name='my_bets'),
    path('analytics/', views.analytics_dashboard, name='analytics'),
    path('place/', views.place_bet, name='place_bet'),
    path('flat-bet-sim/', views.flat_bet_sim, name='flat_bet_sim'),
    path('ai-commentary/', views.ai_commentary, name='ai_commentary'),
    path('<uuid:bet_id>/', views.bet_detail, name='bet_detail'),
    path('<uuid:bet_id>/review/', views.review_bet, name='review_bet'),
]
