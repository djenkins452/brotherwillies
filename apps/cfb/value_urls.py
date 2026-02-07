from django.urls import path
from . import views

urlpatterns = [
    path('', views.value_board, name='value_board'),
]
