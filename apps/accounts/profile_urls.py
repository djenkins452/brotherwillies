from django.urls import path
from . import views

urlpatterns = [
    path('', views.profile_view, name='profile'),
    path('preferences/', views.preferences_view, name='preferences'),
    path('my-model/', views.my_model_view, name='my_model'),
    path('presets/', views.presets_view, name='presets'),
    path('presets/<int:preset_id>/load/', views.load_preset, name='load_preset'),
    path('presets/<int:preset_id>/delete/', views.delete_preset, name='delete_preset'),
    path('my-stats/', views.my_stats_view, name='my_stats'),
    path('performance/', views.performance_view, name='performance'),
    path('user-guide/', views.user_guide_view, name='user_guide'),
    path('whats-new/', views.whats_new_view, name='whats_new'),
]
