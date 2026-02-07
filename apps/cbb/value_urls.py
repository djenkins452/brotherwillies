from django.urls import path
from . import views

urlpatterns = [
    path('', views.value_board, name='cbb_value_board'),
]
