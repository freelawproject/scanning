from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path

from scanning.monitoring import health_check, heartbeat, sentry_fail
from scanning.views import (
    add_page_insert,
    add_single_detection,
    delete_detection,
    apply_rect_to_opinion,
    approve_scan,
    serve_original_crop,
    assign_page,
    bake_redactions,
    cancel_processing,
    claim_scan,
    compute_redactions_api,
    delete_page,
    dismiss_issue,
    # dismiss_issues,
    export_pdf,
    flag_issue,
    generate_files,
    login_view,
    logout_view,
    opinion_detail,
    opinion_list,
    opinion_upload,
    pair_opinions_api,
    progress_api,
    queue_detail_view,
    queue_upload,
    queue_view,
    recalculate,
    remove_flag,
    # repare_opinions,
    reprocess,
    # reprocess_section_view,
    scan_detail,
    scan_list,
    scan_process_view,
    scan_upload,
    save_margin_rect,
    save_redaction_rect,
    serve_detections,
    serve_margin_rects,
    serve_ocr_results,
    serve_opinion_pdf,
    serve_opinions,
    serve_redacted_pdf,
    serve_redaction_rects,
    serve_scan_pdf,
    serve_masked_opinion_pdf,
    serve_unredacted_opinion_pdf,
    start_detect,
    start_validate,
    update_scan_status,
)

urlpatterns = [
    path("login/", login_view, name="login"),
    path("logout/", logout_view, name="logout"),
    path("", scan_list, name="scan_list"),
    # Upload is now done from queue detail page
    # path("upload/", scan_upload, name="scan_upload"),
    path("scans/<int:pk>/", scan_detail, name="scan_detail"),
    path("scans/<int:pk>/process/", scan_process_view, name="scan_process"),
    path("scans/<int:pk>/progress/", progress_api, name="progress_api"),
    path("scans/<int:pk>/pdf/", serve_scan_pdf, name="serve_scan_pdf"),
    path(
        "scans/<int:pk>/original-crop/",
        serve_original_crop,
        name="serve_original_crop",
    ),
    path(
        "scans/<int:pk>/start-validate/", start_validate, name="start_validate"
    ),
    path("scans/<int:pk>/start-detect/", start_detect, name="start_detect"),
    path(
        "scans/<int:pk>/cancel/", cancel_processing, name="cancel_processing"
    ),
    path("scans/<int:pk>/recalculate/", recalculate, name="recalculate"),
    path("scans/<int:pk>/reprocess/", reprocess, name="reprocess"),
    path("scans/<int:pk>/assign-page/", assign_page, name="assign_page"),
    path("scans/<int:pk>/delete-page/", delete_page, name="delete_page"),
    path("scans/<int:pk>/insert/", add_page_insert, name="add_page_insert"),
    path("scans/<int:pk>/dismiss-issue/", dismiss_issue, name="dismiss_issue"),
    # path("scans/<int:pk>/dismiss/", dismiss_issues, name="dismiss_issues"),
    path(
        "scans/<int:pk>/detections/", serve_detections, name="serve_detections"
    ),
    path(
        "scans/<int:pk>/opinions-json/", serve_opinions, name="serve_opinions"
    ),
    path(
        "scans/<int:pk>/margin-rects/",
        serve_margin_rects,
        name="serve_margin_rects",
    ),
    path(
        "scans/<int:pk>/redaction-rects/",
        serve_redaction_rects,
        name="serve_redaction_rects",
    ),
    path(
        "scans/<int:pk>/save-redaction-rect/",
        save_redaction_rect,
        name="save_redaction_rect",
    ),
    path(
        "scans/<int:pk>/save-margin-rect/",
        save_margin_rect,
        name="save_margin_rect",
    ),
    path(
        "scans/<int:pk>/pair-opinions/",
        pair_opinions_api,
        name="pair_opinions_api",
    ),
    path(
        "scans/<int:pk>/compute-redactions/",
        compute_redactions_api,
        name="compute_redactions_api",
    ),
    path(
        "scans/<int:pk>/generate-files/", generate_files, name="generate_files"
    ),
    path("scans/<int:pk>/approve/", approve_scan, name="approve_scan"),
    path(
        "scans/<int:pk>/redacted-pdf/",
        serve_redacted_pdf,
        name="serve_redacted_pdf",
    ),
    path(
        "scans/<int:pk>/opinion/<str:filename>/",
        serve_opinion_pdf,
        name="serve_opinion_pdf",
    ),
    path(
        "scans/<int:pk>/unredacted/<str:filename>/",
        serve_unredacted_opinion_pdf,
        name="serve_unredacted_opinion_pdf",
    ),
    path(
        "scans/<int:pk>/masked/<str:filename>/",
        serve_masked_opinion_pdf,
        name="serve_masked_opinion_pdf",
    ),
    path(
        "scans/<int:pk>/opinion-edit/<int:opinion_pk>/apply-rect/",
        apply_rect_to_opinion,
        name="apply_rect_to_opinion",
    ),
    path(
        "scans/<int:pk>/ocr-results/",
        serve_ocr_results,
        name="serve_ocr_results",
    ),
    path("scans/<int:pk>/flag/", flag_issue, name="flag_issue"),
    path(
        "scans/<int:pk>/flag/<int:flag_id>/delete/",
        remove_flag,
        name="remove_flag",
    ),
    path(
        "scans/<int:pk>/add-single-detection/",
        add_single_detection,
        name="add_single_detection",
    ),
    path(
        "scans/<int:pk>/delete-detection/",
        delete_detection,
        name="delete_detection",
    ),
    # path("scans/<int:pk>/repare-opinions/", repare_opinions, name="repare_opinions"),
    # path("scans/<int:pk>/reprocess-section/", reprocess_section_view, name="reprocess_section"),
    path(
        "scans/<int:pk>/bake-redactions/",
        bake_redactions,
        name="bake_redactions",
    ),
    path("scans/<int:pk>/export/", export_pdf, name="export_pdf"),
    path("opinions/", opinion_list, name="opinion_list"),
    path("opinions/upload/", opinion_upload, name="opinion_upload"),
    path("opinions/<int:pk>/", opinion_detail, name="opinion_detail"),
    path("queue/", queue_view, name="queue"),
    path(
        "queue/<str:reporter_slug>/<int:vol>/",
        queue_detail_view,
        name="queue_detail",
    ),
    path(
        "queue/<str:reporter_slug>/<int:vol>/upload/",
        queue_upload,
        name="queue_upload",
    ),
    path(
        "queue/<str:reporter_slug>/<int:vol>/claim/",
        claim_scan,
        name="claim_scan",
    ),
    path(
        "queue/<str:reporter_slug>/<int:vol>/status/",
        update_scan_status,
        name="update_scan_status",
    ),
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
