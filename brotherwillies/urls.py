from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import path, include

urlpatterns = [
    path('bw-manage/', admin.site.urls),
    path('', include('apps.core.urls')),
    path('accounts/', include('apps.accounts.urls')),
    path('cfb/', include('apps.cfb.urls')),
    path('value/', include('apps.cfb.value_urls')),
    path('cbb/', include('apps.cbb.urls')),
    path('cbb/value/', include('apps.cbb.value_urls')),
    path('golf/', include('apps.golf.urls')),
    path('parlays/', include('apps.parlays.urls')),
    path('profile/', include('apps.accounts.profile_urls')),
    path('feedback/', include('apps.feedback.urls')),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
