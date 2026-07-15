from django.contrib import admin, messages
from django.contrib.admin.utils import quote
from django.urls import NoReverseMatch, reverse
from django.utils.html import format_html
from django.utils.text import capfirst

from scanning.models import (
    Detection,
    Issue,
    OpinionScan,
    Page,
    PageDeletion,
    PageInsert,
    PendingUpload,
    Reporter,
    Scan,
    Status,
    Volume,
)
from scanning.services import (
    refresh_volume_queue_status,
    refresh_volume_queue_status_for_scan,
)


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


class InterruptedFilter(admin.SimpleListFilter):
    """Filter scans flagged after too many daemon interruptions (issue #124)."""

    title = "interrupted too often"
    parameter_name = "interrupted"

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
            return queryset.filter(status=Status.ERROR_INTERRUPTED)
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
        "interruption_count",
        "uploaded_by",
        "date_created",
    ]
    list_filter = [
        "status",
        "reporter",
        "source",
        RetryCapFilter,
        InterruptedFilter,
    ]
    search_fields = [
        "volume",
        "reporter__full_name",
        "notes",
        "uploaded_by__username",
    ]
    raw_id_fields = ["uploaded_by", "reporter", "volume_obj"]
    readonly_fields = [
        "date_created",
        "date_modified",
        "processed_at",
        "pages_link",
    ]
    # Per-page payload fields hold megabytes on a processed scan.
    # Rendering them as editable textareas makes the change view crawl,
    # so keep them off the form (they stay in the DB and are used by the
    # pipeline).
    exclude = [
        "ocr_results",
        "opinions_json",
        "page_map",
        "missing_pages",
        "margin_rects",
        "redaction_rects",
        "process_output",
    ]
    date_hierarchy = "date_created"
    actions = [
        "reset_to_queued",
        "requeue_retry_cap_scans",
        "requeue_interrupted_scans",
    ]

    @staticmethod
    def _admin_change_link(obj):
        """Render an object as a clickable admin change-link.

        Mirrors the link format Django's own delete confirmation uses,
        so summarized rows still read as ``Verbose name: <a>obj</a>``.

        :param obj: A model instance.
        :return: ``Verbose name: <link>`` HTML, or plain text if the
            model has no admin change URL.
        :rtype: SafeString
        """
        opts = obj._meta
        label = capfirst(opts.verbose_name)
        try:
            url = reverse(
                f"admin:{opts.app_label}_{opts.model_name}_change",
                args=(quote(obj.pk),),
            )
        except NoReverseMatch:
            return format_html("{}: {}", label, str(obj))
        return format_html('{}: <a href="{}">{}</a>', label, url, str(obj))

    def get_deleted_objects(
        self, objs, request
    ) -> tuple[list[str], dict[str, int], set[str], list[str]]:
        """Summarize cascade deletions without loading every related row.

        The default admin collector instantiates every related object to
        render the confirmation tree. A processed scan cascades to tens
        of thousands of ``Detection`` rows (plus ``Page``, ``Issue``,
        etc.), so the collector exhausts memory and takes the server
        down before the user even confirms.

        We keep clickable links only for the *bounded* sets a human would
        actually click, the selected scans and any blocking
        ``OpinionScan`` rows, and reduce the *unbounded* cascade tables to
        COUNT-based summaries.

        :param objs: Scans selected for deletion.
        :param request: The admin HTTP request.
        :return: ``(deletable_objects, model_count, perms_needed,
            protected)``.
        :rtype: tuple
        """
        scan_ids = [obj.pk for obj in objs]

        # OpinionScan.scan is PROTECT: a scan with opinions cannot be
        # deleted. Surface the blocking opinions as links so the
        # confirmation page explains why, instead of letting the real
        # delete raise ProtectedError (500). Opinions are bounded per
        # volume, so instantiating them is cheap.
        protected: list[str] = [
            self._admin_change_link(opinion)
            for opinion in OpinionScan.objects.filter(scan_id__in=scan_ids)
        ]

        model_count: dict[str, int] = {}
        for model in (Scan, Page, Detection, Issue, PageInsert, PageDeletion):
            field = "pk" if model is Scan else "scan_id"
            count = model.objects.filter(**{f"{field}__in": scan_ids}).count()
            if count:
                model_count[str(model._meta.verbose_name_plural)] = count

        deletable_objects: list[str] = [
            self._admin_change_link(obj) for obj in objs
        ]
        perms_needed: set[str] = set()
        return deletable_objects, model_count, perms_needed, protected

    @admin.display(description="Pages")
    def pages_link(self, obj):
        """Render a link to the filtered Page admin for this scan.

        :param obj: The Scan instance.
        :return: HTML link or em dash if there are no pages.
        :rtype: SafeString | str
        """
        count = obj.pages.count() if obj.pk else 0
        if not count:
            return "—"
        url = reverse("admin:scanning_page_changelist")
        return format_html(
            '<a href="{}?scan__id__exact={}">View {} page(s)</a>',
            url,
            obj.pk,
            count,
        )

    def save_model(self, request, obj, form, change):
        """Save a Scan and refresh affected Volume(s) queue_status.

        Covers admin edits that the lifecycle helper would normally see
        elsewhere: flipping ``status`` (e.g. PENDING_REVIEW → APPROVED)
        and reassigning ``volume_obj`` to a different volume. In the
        reassignment case both the old and new volumes are refreshed.

        :param request: The admin HTTP request.
        :param obj: The Scan being saved.
        :param form: The admin form instance.
        :param change: True for an edit, False for a new object.
        """
        old_volume_id = None
        if change and obj.pk:
            old_volume_id = (
                Scan.objects.filter(pk=obj.pk)
                .values_list("volume_obj_id", flat=True)
                .first()
            )
        super().save_model(request, obj, form, change)
        refresh_volume_queue_status_for_scan(obj)
        if old_volume_id and old_volume_id != obj.volume_obj_id:
            old_volume = Volume.objects.filter(pk=old_volume_id).first()
            if old_volume is not None:
                refresh_volume_queue_status(old_volume)

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

    @admin.action(
        description=(
            "Re-queue selected scans flagged as interrupted too often "
            "(resets interruption_count so the daemon will try again)"
        )
    )
    def requeue_interrupted_scans(self, request, queryset):
        """Re-queue scans flagged ERROR_INTERRUPTED by the daemon guard.

        Resets ``status``, ``interruption_count``, and ``progress_message``
        so the daemon picks them up again. Use once the underlying pod churn
        (deploys, evictions, OOM) that interrupted them has settled (issue
        #124).

        :param request: The admin HTTP request.
        :param queryset: Selected Scan queryset.
        :return: None.
        """
        updated = queryset.filter(status=Status.ERROR_INTERRUPTED).update(
            status=Status.QUEUED,
            interruption_count=0,
            progress_message="Re-queued via admin (interruption cap reset)",
            progress_current=0,
            progress_total=0,
        )
        self.message_user(
            request,
            f"Re-queued {updated} scan(s) with interruption_count reset. "
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


@admin.register(PendingUpload)
class PendingUploadAdmin(admin.ModelAdmin):
    list_display = [
        "id",
        "scan",
        "created_by",
        "expected_size",
        "action",
        "date_created",
    ]
    search_fields = ["id", "s3_key", "scan__id"]
    raw_id_fields = ["scan", "created_by"]
    readonly_fields = ["id", "date_created", "date_modified"]


@admin.register(Page)
class PageAdmin(admin.ModelAdmin):
    list_display = [
        "scan",
        "page_index",
        "book_page",
        "pdf_link",
        "is_blank",
        "status",
        "extracted_by",
        "needs_review",
        "has_prompt",
        "has_xml",
        "date_modified",
    ]
    list_filter = ["status", "needs_review", "extracted_by", "is_blank"]
    search_fields = ["scan__id", "book_page", "scan__reporter__short_name"]
    raw_id_fields = ["scan", "user_prompt"]
    readonly_fields = [
        "date_created",
        "date_modified",
        "pdf_link",
    ]
    ordering = ["scan", "page_index"]
    list_select_related = ["scan", "user_prompt"]

    @admin.display(description="PDF")
    def pdf_link(self, obj):
        """Render ``pdf_path`` as a clickable link to the served file.

        The view resolves the local file first and lazily pulls from S3
        if it isn't on disk, so this works regardless of which mode the
        portal is running in.
        """
        if not obj.pdf_path or not obj.pk:
            return "—"
        url = reverse("serve_page_pdf", kwargs={"pk": obj.pk})
        return format_html(
            '<a href="{}" target="_blank" rel="noopener">{}</a>',
            url,
            obj.pdf_path,
        )

    @admin.display(boolean=True, description="Prompt")
    def has_prompt(self, obj):
        """Whether this page has a user_prompt FK set."""
        return obj.user_prompt_id is not None

    @admin.display(boolean=True, description="XML")
    def has_xml(self, obj):
        """Whether the page has extracted XML content."""
        return bool(obj.xml_content)
