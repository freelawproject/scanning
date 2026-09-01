"""Move the deletions and the inserts onto ``PageEdit`` (#214).

One ``DELETE_PAGE`` row per ``PageDeletion``, and one ``INSERT_PAGE``
row per ``PageInsert``. Both old models are dropped in the next
migration, so this is the only pass that can read them.

The address is the part that changes. A deletion already named a
physical PDF page, so it carries over as it is. An insert named the
printed page number, which cannot place an image: front matter has
none, and two pages can print the same one. Each insert's anchor is
therefore resolved here, from the scan's stored page map -- the same
walk the viewer does when it renders a placeholder.

The image is not moved here. A data migration runs in a pod that may
not be the one holding the file, and it must not open a network
connection per row. The row keeps the old storage name, and the
``migrate_page_insert_images`` command copies the bytes to the scan's
own prefix afterwards and re-points the field.
"""

import logging

from django.db import migrations

logger = logging.getLogger(__name__)


def _anchor_for(page_map, logical_number):
    """Return the original page an inserted image follows.

    Two rules, in order. The placeholder the curator clicked is the
    exact answer, so it is tried first. When a later OCR run removed
    that placeholder, the image still belongs after the last real page
    that prints a smaller number.

    :param page_map: The scan's stored page map.
    :param logical_number: The printed number the insert was filed
        under.
    :returns: The 1-based anchor page, 0 for "before page 1", or None
        when the page map cannot place it at all.
    :rtype: int | None
    """
    anchor = 0
    for entry in page_map or []:
        if entry.get("type") == "pdf_page":
            anchor = entry.get("pdf_index", 0) + 1
            continue
        if (
            entry.get("type") == "missing"
            and entry.get("logical_number") == logical_number
        ):
            return anchor

    anchor = None
    for entry in page_map or []:
        if entry.get("type") != "pdf_page":
            continue
        number = entry.get("logical_number")
        if isinstance(number, int) and number < logical_number:
            anchor = entry.get("pdf_index", 0) + 1
    return anchor


def create_page_edits(apps, schema_editor):
    """Write one row per deletion and one per insert.

    :param apps: The historical app registry.
    :param schema_editor: The schema editor (unused).
    """
    PageDeletion = apps.get_model("scanning", "PageDeletion")
    PageInsert = apps.get_model("scanning", "PageInsert")
    PageEdit = apps.get_model("scanning", "PageEdit")

    rows = [
        PageEdit(
            scan_id=deletion.scan_id,
            kind="delete_page",
            pdf_page=deletion.pdf_page,
        )
        for deletion in PageDeletion.objects.all()
        if deletion.pdf_page
    ]

    ordinals: dict[tuple[int, int], int] = {}
    for insert in PageInsert.objects.select_related("scan").order_by(
        "scan_id", "logical_page_number"
    ):
        anchor = _anchor_for(
            insert.scan.page_map, insert.logical_page_number
        )
        if anchor is None:
            # Nothing to place it against: the volume has no page map,
            # or none of its pages print a smaller number. Say so; the
            # image is still in the old row's storage until the next
            # migration drops it.
            logger.warning(
                "Page insert %s of scan %s (printed page %s) could not "
                "be placed in the volume and was not migrated",
                insert.pk,
                insert.scan_id,
                insert.logical_page_number,
            )
            continue
        key = (insert.scan_id, anchor)
        ordinals[key] = ordinals.get(key, -1) + 1
        rows.append(
            PageEdit(
                scan_id=insert.scan_id,
                kind="insert_page",
                anchor_pdf_page=anchor,
                ordinal=ordinals[key],
                logical_page=str(insert.logical_page_number)[:32],
                image=insert.image.name,
            )
        )

    PageEdit.objects.bulk_create(rows, ignore_conflicts=True)


class Migration(migrations.Migration):
    dependencies = [
        ("scanning", "0014_alter_issue_check_name"),
    ]

    operations = [
        # No reverse, for the reason 0013 gives: a curator's decision
        # is not something a rollback may delete.
        migrations.RunPython(create_page_edits, migrations.RunPython.noop),
    ]
