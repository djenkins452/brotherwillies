from django.urls import path
from . import views

app_name = 'golf'

urlpatterns = [
    path('', views.golf_hub, name='hub'),
    path('api/golfer-search/', views.golfer_search, name='golfer_search'),
]
