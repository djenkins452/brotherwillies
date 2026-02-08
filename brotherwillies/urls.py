from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import path, include
from apps.core.views import cbb_value_redirect

urlpatterns = [
    path('bw-manage/', admin.site.urls),
    path('', include('apps.core.urls')),
    path('accounts/', include('apps.accounts.urls')),
    path('cfb/', include('apps.cfb.urls')),
    path('cbb/', include('apps.cbb.urls')),
    path('cbb/value/', cbb_value_redirect, name='cbb_value_redirect'),
    path('golf/', include('apps.golf.urls')),
    path('parlays/', include('apps.parlays.urls')),
    path('profile/', include('apps.accounts.profile_urls')),
    path('feedback/', include('apps.feedback.urls')),
    path('mockbets/', include('apps.mockbets.urls')),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
