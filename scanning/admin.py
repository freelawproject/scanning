from django.contrib import admin

from scanning.models import OpinionScan, Reporter, Scan


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
