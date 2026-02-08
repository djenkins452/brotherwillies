from django.urls import path
from . import views

app_name = 'feedback'

urlpatterns = [
    path('new/', views.feedback_new, name='new'),
    path('console/', views.feedback_console, name='console'),
    path('console/<uuid:pk>/', views.feedback_detail, name='detail'),
    path('console/<uuid:pk>/status/', views.feedback_quick_status, name='quick_status'),
    path('console/<uuid:pk>/update/', views.feedback_update, name='update'),
]
