from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path

from scanning.monitoring import health_check, heartbeat, sentry_fail
from scanning.views import (
    claim_scan,
    login_view,
    logout_view,
    opinion_detail,
    opinion_list,
    opinion_upload,
    queue_detail_view,
    queue_view,
    scan_detail,
    scan_list,
    scan_upload,
    update_scan_status,
)

urlpatterns = [
    path("login/", login_view, name="login"),
    path("logout/", logout_view, name="logout"),
    path("", scan_list, name="scan_list"),
    path("upload/", scan_upload, name="scan_upload"),
    path("scans/<int:pk>/", scan_detail, name="scan_detail"),
    path("opinions/", opinion_list, name="opinion_list"),
    path("opinions/upload/", opinion_upload, name="opinion_upload"),
    path("opinions/<int:pk>/", opinion_detail, name="opinion_detail"),
    path("queue/", queue_view, name="queue"),
    path("queue/<int:pk>/", queue_detail_view, name="queue_detail"),
    path("queue/<int:pk>/claim/", claim_scan, name="claim_scan"),
    path("queue/<int:pk>/status/", update_scan_status, name="update_scan_status"),
    path("admin/", admin.site.urls),
    path("monitoring/heartbeat/", heartbeat, name="heartbeat"),
    path("monitoring/health-check/", health_check, name="health_check"),
    path("sentry/error/", sentry_fail, name="sentry_fail"),
]

if settings.DEBUG:
    urlpatterns += static(
        settings.MEDIA_URL, document_root=settings.MEDIA_ROOT
    )

if settings.DEVELOPMENT and not settings.TESTING:
    urlpatterns += [
        path("__debug__/", include("debug_toolbar.urls")),
    ]
    urlpatterns.append(
        path("__reload__/", include("django_browser_reload.urls")),
    )
