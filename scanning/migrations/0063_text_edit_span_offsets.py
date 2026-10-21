"""The span of a ``TEXT`` edit, in the offsets of the text the person wrote.

A word answer of the blocking review stored the span it judged in the
offsets of the text the person saw; the build now reads it in the
offsets of the text they wrote (``ensemble._kept_open``), so an answer
closes the spot it judged whatever word it put there. What follows the
span is the same in both texts, so the end moves by the length change.
The text the person saw is the edit this one replaced (a second answer
on a block a person wrote), else the engines' text (``base_text``).
"""

from django.db import migrations


def forwards(apps, schema_editor):
    OpinionEdit = apps.get_model("scanning", "OpinionEdit")
    rows = OpinionEdit.objects.filter(
        kind="text", span__isnull=False
    ).select_related("replaces")
    for row in rows:
        start, end = row.span
        shown = (
            row.replaces.text
            if row.replaces_id and row.replaces.kind == "text"
            else row.base_text
        )
        tail = len(shown) - end
        row.span = [
            start,
            max(start, min(len(row.text), len(row.text) - tail)),
        ]
        row.save(update_fields=["span"])


class Migration(migrations.Migration):
    dependencies = [("scanning", "0062_opinion_input_digest")]

    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
