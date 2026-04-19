from django.urls import path
from . import views

app_name = 'mlb'

urlpatterns = [
    path('', views.mlb_hub, name='hub'),
    path('game/<uuid:game_id>/', views.game_detail, name='game'),
]
