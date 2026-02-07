from django.urls import path
from . import views

app_name = 'cbb'

urlpatterns = [
    path('', views.cbb_hub, name='hub'),
    path('conference/<slug:slug>/', views.conference_detail, name='conference'),
    path('game/<uuid:game_id>/', views.game_detail, name='game'),
]
