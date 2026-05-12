from django.contrib import admin, messages

from scanning.models import (
    Detection,
    Issue,
    OpinionScan,
    PageDeletion,
    PageInsert,
    Reporter,
    Scan,
    Status,
    Volume,
)
from scanning.services import refresh_volume_queue_status


class RetryCapFilter(admin.SimpleListFilter):
    """Filter scans that were stopped because they hit the transient retry cap."""

    title = "retry cap hit"
    parameter_name = "retry_cap"

    def lookups(self, request, model_admin):
        """Return the filter choices.

        :param request: The admin HTTP request.
        :param model_admin: The ModelAdmin instance.
        :returns: Iterable of (value, label) tuples.
        :rtype: list[tuple]
        """
        return [("1", "Yes")]

    def queryset(self, request, queryset):
        """Apply the filter to the queryset.

        :param request: The admin HTTP request.
        :param queryset: The base queryset.
        :returns: Filtered queryset.
        """
        if self.value() == "1":
            return queryset.filter(status=Status.ERROR_MAX_RETRIES)
        return queryset


@admin.register(Reporter)
class ReporterAdmin(admin.ModelAdmin):
    list_display = ["short_name", "full_name", "date_created"]
    search_fields = ["short_name", "full_name"]
    readonly_fields = ["date_created", "date_modified"]


@admin.register(Scan)
class ScanAdmin(admin.ModelAdmin):
    list_display = [
        "reporter",
        "volume",
        "source",
        "number_of_pages",
        "status",
        "retry_count",
        "uploaded_by",
        "date_created",
    ]
    list_filter = ["status", "reporter", "source", RetryCapFilter]
    search_fields = [
        "volume",
        "reporter__full_name",
        "notes",
        "uploaded_by__username",
    ]
    raw_id_fields = ["uploaded_by", "reporter"]
    readonly_fields = ["date_created", "date_modified", "processed_at"]
    date_hierarchy = "date_created"
    actions = ["reset_to_queued", "requeue_retry_cap_scans"]

    def delete_model(self, request, obj):
        """Delete a Scan and refresh its parent Volume's queue_status.

        Without this, the volume can drift (e.g. stay at COMPLETE after
        its only approved scan is deleted from the admin).

        :param request: The admin HTTP request.
        :param obj: The Scan to delete.
        """
        volume = obj.volume_obj
        super().delete_model(request, obj)
        if volume is not None:
            refresh_volume_queue_status(volume)

    def delete_queryset(self, request, queryset):
        """Bulk-delete Scans and refresh affected Volumes.

        :param request: The admin HTTP request.
        :param queryset: Scans selected for deletion.
        """
        volume_ids = list(
            queryset.exclude(volume_obj__isnull=True)
            .values_list("volume_obj_id", flat=True)
            .distinct()
        )
        super().delete_queryset(request, queryset)
        for volume in Volume.objects.filter(pk__in=volume_ids):
            refresh_volume_queue_status(volume)

    @admin.action(
        description=(
            "Reset selected scans to QUEUED "
            "(recover scans stuck in PROCESSING)"
        )
    )
    def reset_to_queued(self, request, queryset):
        """Flip selected scans back to QUEUED so the daemon re-runs them.

        Use this to recover a scan whose daemon process was killed (OOM,
        pod restart, SIGKILL, etc.) and which remained stuck in
        ``PROCESSING`` with a frozen progress message. Waiting for
        ``_recover_stale()`` to pick it up takes up to
        ``DAEMON_PROCESSING_TIMEOUT`` (default 3600s); this action is
        the manual shortcut.

        :param request: The admin HTTP request.
        :param queryset: Selected Scan queryset.
        :return: None.
        """
        updated = queryset.update(
            status=Status.QUEUED,
            progress_message="Re-queued via admin",
            progress_current=0,
            progress_total=0,
        )
        self.message_user(
            request,
            f"Re-queued {updated} scan(s). The daemon will pick them up "
            "on the next tick.",
            level=messages.SUCCESS,
        )

    @admin.action(
        description=(
            "Re-queue selected scans that hit the retry cap "
            "(resets retry_count so the daemon will try again)"
        )
    )
    def requeue_retry_cap_scans(self, request, queryset):
        """Re-queue scans stopped after hitting the transient retry cap.

        Resets ``status``, ``retry_count``, and ``progress_message`` so
        the daemon will pick them up and attempt processing again.

        :param request: The admin HTTP request.
        :param queryset: Selected Scan queryset.
        :return: None.
        """
        updated = queryset.filter(status=Status.ERROR_MAX_RETRIES).update(
            status=Status.QUEUED,
            retry_count=0,
            progress_message="Re-queued via admin (retry cap reset)",
            progress_current=0,
            progress_total=0,
        )
        self.message_user(
            request,
            f"Re-queued {updated} scan(s) with retry_count reset. "
            "The daemon will try them again on the next tick.",
            level=messages.SUCCESS,
        )


@admin.register(OpinionScan)
class OpinionScanAdmin(admin.ModelAdmin):
    list_display = [
        "reporter",
        "volume",
        "page_start",
        "status",
        "scan",
        "uploaded_by",
        "date_created",
    ]
    list_filter = ["status", "reporter"]
    search_fields = [
        "volume",
        "reporter__full_name",
        "notes",
        "uploaded_by__username",
    ]
    raw_id_fields = ["uploaded_by", "reporter", "scan"]
    readonly_fields = ["date_created", "date_modified"]


@admin.register(Volume)
class VolumeAdmin(admin.ModelAdmin):
    list_display = [
        "reporter",
        "volume_number",
        "queue_status",
        "priority",
        "assigned_to",
        "is_partial",
        "date_created",
    ]
    list_filter = ["queue_status", "priority", "reporter", "is_partial"]
    search_fields = ["reporter__full_name", "volume_number", "notes"]
    raw_id_fields = ["reporter", "assigned_to"]
    readonly_fields = ["date_created", "date_modified"]


@admin.register(Issue)
class IssueAdmin(admin.ModelAdmin):
    list_display = ["scan", "page_number", "check_name", "severity", "message"]
    list_filter = ["severity", "check_name"]
    search_fields = ["message", "check_name"]
    raw_id_fields = ["scan"]


@admin.register(Detection)
class DetectionAdmin(admin.ModelAdmin):
    list_display = [
        "scan",
        "page_index",
        "label",
        "confidence",
        "model_name",
        "active",
    ]
    list_filter = ["label", "active", "model_name"]
    search_fields = ["label"]
    raw_id_fields = ["scan"]


@admin.register(PageInsert)
class PageInsertAdmin(admin.ModelAdmin):
    list_display = ["scan", "logical_page_number", "date_created"]
    raw_id_fields = ["scan"]
    readonly_fields = ["date_created", "date_modified"]


@admin.register(PageDeletion)
class PageDeletionAdmin(admin.ModelAdmin):
    list_display = ["scan", "pdf_page", "date_created"]
    raw_id_fields = ["scan"]
    readonly_fields = ["date_created", "date_modified"]
