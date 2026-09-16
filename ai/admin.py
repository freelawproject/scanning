from django.contrib import admin

from ai.models import Prompt


@admin.register(Prompt)
class PromptAdmin(admin.ModelAdmin):
    list_display = [
        "id",
        "name",
        "prompt_type",
        "is_active",
        "text_preview",
        "date_modified",
    ]
    list_filter = ["prompt_type", "is_active"]
    search_fields = ["name", "text", "notes"]
    readonly_fields = ["date_created", "date_modified"]
    ordering = ["-date_modified"]

    @admin.display(description="Text preview")
    def text_preview(self, obj):
        """First 80 chars of the prompt text, for the list view."""
        if not obj.text:
            return "—"
        flat = " ".join(obj.text.split())
        return flat[:80] + ("…" if len(flat) > 80 else "")
