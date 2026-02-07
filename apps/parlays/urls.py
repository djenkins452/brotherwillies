from django.urls import path
from . import views

app_name = 'parlays'

urlpatterns = [
    path('', views.parlay_hub, name='hub'),
    path('new/', views.parlay_new, name='new'),
    path('<uuid:parlay_id>/', views.parlay_detail, name='detail'),
]
