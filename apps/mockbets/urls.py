from django.urls import path

from . import views

app_name = 'mockbets'

urlpatterns = [
    path('', views.my_bets, name='my_bets'),
    path('place/', views.place_bet, name='place_bet'),
    path('<uuid:bet_id>/', views.bet_detail, name='bet_detail'),
    path('<uuid:bet_id>/review/', views.review_bet, name='review_bet'),
]
