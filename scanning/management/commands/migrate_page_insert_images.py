"""Move the migrated page insert images to the scan's own S3 prefix.

The #214 data migration carried each ``PageInsert`` row over to a
``PageEdit``, with the old storage name in the image field. The bytes
themselves stay where ``LocalProcessingStorage`` put them: on the disk
of whichever web pod took the upload. That is the defect the model
change removes, and this command is what finishes it.

Run it once, on the pod that holds the files, right after the deploy.
Until it runs, a migrated insert shows a broken image in the viewer,
because the field names a key the bucket does not have yet.

An image whose local file is gone -- the pod was preempted, which is
the normal end of a web pod -- cannot be recovered. The command clears
the field and names the scan, so a curator can upload the page again.

Examples:

    # Move every migrated image, reporting what it did.
    docker exec scanning-web python manage.py migrate_page_insert_images

    # Say what it would do, and change nothing.
    docker exec scanning-web python manage.py migrate_page_insert_images \\
        --dry-run
"""

from pathlib import Path

from django.core.files.base import ContentFile
from django.core.management.base import BaseCommand

from scanning.models import PageEdit
from scanning.storage import LocalProcessingStorage

#: Where the retired ``PageInsert.image`` field wrote its files.
LEGACY_PREFIX = "page_inserts/"


class Command(BaseCommand):
    help = (
        "Move the page insert images the #214 migration carried over "
        "into the scan's own storage prefix."
    )

    def add_arguments(self, parser):
        """Register the CLI arguments.

        :param parser: The argparse parser to configure.
        :return: None.
        """
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would move, and change nothing.",
        )

    def handle(self, *args, **options):
        """Copy each legacy image into the default storage.

        :param args: Unused positional arguments.
        :param options: Parsed CLI options.
        :return: None.
        """
        dry_run = options["dry_run"]
        legacy = LocalProcessingStorage()
        rows = PageEdit.objects.filter(
            image__startswith=LEGACY_PREFIX
        ).select_related("scan")

        moved = lost = 0
        for edit in rows:
            name = edit.image.name
            if not legacy.exists(name):
                lost += 1
                self.stderr.write(
                    f"scan {edit.scan_id}: the image of page edit "
                    f"{edit.pk} ({name}) is not on this pod's disk; "
                    f"the page must be uploaded again"
                )
                if not dry_run:
                    edit.image = ""
                    edit.save(update_fields=["image"])
                continue

            if dry_run:
                moved += 1
                self.stdout.write(f"scan {edit.scan_id}: would move {name}")
                continue

            with legacy.open(name, "rb") as fh:
                # save() runs the upload_to callable, so the new key is
                # the scan's own page_edits/ one, with a fresh name.
                edit.image.save(
                    Path(name).name, ContentFile(fh.read()), save=True
                )
            legacy.delete(name)
            moved += 1
            self.stdout.write(
                f"scan {edit.scan_id}: moved {name} -> {edit.image.name}"
            )

        self.stdout.write(
            self.style.SUCCESS(
                f"{moved} image(s) moved, {lost} without a local file"
            )
        )
