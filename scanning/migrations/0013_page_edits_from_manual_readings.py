"""Move the page numbers a curator typed out of the JSON blob (#214).

One ``PageEdit`` row per ``Scan.ocr_results`` entry stamped
``"manual"``. The stamp was the only record that a person, not a
model, put the number there, and the readers stop trusting it in this
release: a rebuilt blob keeps the numbers only if a row holds them.

The rows carry no author -- the blob kept none -- and a blank
fingerprint, which reads as a legacy row and matches any original. The
entries are left in place: the blob is a cache now, rebuilt from the
run plus these rows on the next recompute.
"""

from django.db import migrations

#: ``PageEdit.value`` is 32 characters. A reading longer than that is
#: not a page number, so it is dropped rather than truncated into one.
MAX_VALUE = 32


def create_page_edits(apps, schema_editor):
    """Write one SET_NUMBER row per manual reading.

    :param apps: The historical app registry.
    :param schema_editor: The schema editor (unused).
    """
    Scan = apps.get_model("scanning", "Scan")
    PageEdit = apps.get_model("scanning", "PageEdit")

    rows = []
    for scan in Scan.objects.exclude(ocr_results=[]).iterator():
        for entry in scan.ocr_results or []:
            if "manual" not in (entry.get("ocr"), entry.get("zone")):
                continue
            page = entry.get("pdf_page")
            if not isinstance(page, int) or page < 1:
                continue
            value = str(entry.get("detected") or "")
            if len(value) > MAX_VALUE:
                continue
            rows.append(
                PageEdit(
                    scan_id=scan.pk,
                    kind="set_number",
                    pdf_page=page,
                    value=value,
                    replaced="",
                    logical_page="",
                    source_fingerprint="",
                )
            )
    # ignore_conflicts: the partial unique key already holds one open
    # decision per page, so a re-run of this migration is a no-op
    # rather than a failure.
    PageEdit.objects.bulk_create(rows, ignore_conflicts=True)


class Migration(migrations.Migration):
    dependencies = [
        ("scanning", "0012_scan_source_fingerprint_pageedit"),
    ]

    operations = [
        # No reverse: the rows cannot be told apart from the ones a
        # curator writes after the deploy, and deleting a person's
        # decision to undo a migration is worse than leaving it.
        migrations.RunPython(create_page_edits, migrations.RunPython.noop),
    ]
