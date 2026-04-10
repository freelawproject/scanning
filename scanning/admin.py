from django.contrib import admin

from scanning.models import (
    Detection,
    Issue,
    OpinionScan,
    PageDeletion,
    PageInsert,
    Reporter,
    Scan,
    Volume,
)


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
        "uploaded_by",
        "date_created",
    ]
    list_filter = ["status", "reporter", "source"]
    search_fields = [
        "volume",
        "reporter__full_name",
        "notes",
        "uploaded_by__username",
    ]
    raw_id_fields = ["uploaded_by", "reporter"]
    readonly_fields = ["date_created", "date_modified", "processed_at"]
    date_hierarchy = "date_created"


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
