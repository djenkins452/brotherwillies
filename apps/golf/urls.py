from django.urls import path
from . import views

app_name = 'golf'

urlpatterns = [
    path('', views.golf_hub, name='hub'),
]
