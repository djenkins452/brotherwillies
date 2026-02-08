from django.urls import path
from . import views

app_name = 'core'

urlpatterns = [
    path('', views.home, name='home'),
    path('api/ai-insight/<str:sport>/<uuid:game_id>/', views.ai_insight_view, name='ai_insight'),
]
