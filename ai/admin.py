from django.contrib import admin
from django.urls import reverse
from django.utils.html import format_html

from ai.models import Prompt


@admin.register(Prompt)
class PromptAdmin(admin.ModelAdmin):
    list_display = [
        "id",
        "name",
        "prompt_type",
        "is_active",
        "text_preview",
        "pages_link",
        "date_modified",
    ]
    list_filter = ["prompt_type", "is_active"]
    search_fields = ["name", "text", "notes"]
    readonly_fields = ["date_created", "date_modified", "pages_link"]
    ordering = ["-date_modified"]

    @admin.display(description="Text preview")
    def text_preview(self, obj):
        """First 80 chars of the prompt text, for the list view."""
        if not obj.text:
            return "—"
        flat = " ".join(obj.text.split())
        return flat[:80] + ("…" if len(flat) > 80 else "")

    @admin.display(description="Linked pages")
    def pages_link(self, obj):
        """Link to the Page admin filtered to pages currently using this prompt."""
        if not obj.pk:
            return "—"
        url = reverse("admin:scanning_page_changelist")
        return format_html(
            '<a href="{}?user_prompt__id__exact={}">View page</a>',
            url,
            obj.pk,
        )
