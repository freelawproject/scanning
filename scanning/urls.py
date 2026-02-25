from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path

from scanning.views import (
    login_view,
    logout_view,
    scan_detail,
    scan_list,
    scan_upload,
)

urlpatterns = [
    path("login/", login_view, name="login"),
    path("logout/", logout_view, name="logout"),
    path("", scan_list, name="scan_list"),
    path("upload/", scan_upload, name="scan_upload"),
    path("scans/<int:pk>/", scan_detail, name="scan_detail"),
    path("admin/", admin.site.urls),
]

if settings.DEBUG:
    urlpatterns += static(
        settings.MEDIA_URL, document_root=settings.MEDIA_ROOT
    )

if settings.DEVELOPMENT and not settings.TESTING:
    urlpatterns += [
        path("__debug__/", include("debug_toolbar.urls")),
    ]
