"""Import the volume queue from pdfchecker's scanlist.csv.

Each CSV row describes a volume (or a part of one) to be scanned. This
command only populates ``Volume`` records; it does not create ``Scan``
objects, which are created later when a real PDF is uploaded.
"""

import csv
import logging

from django.core.management.base import BaseCommand

from scanning.models import (
    Priority,
    QueueStatus,
    Reporter,
    Volume,
)

logger = logging.getLogger(__name__)

STATUS_MAP = {
    "Not Started": QueueStatus.NEEDS_SCANNING,
    "In Progress": QueueStatus.SCANNING,
    "Completed": QueueStatus.COMPLETE,
    "completed": QueueStatus.COMPLETE,
    "Upload": QueueStatus.SCANNED,
    "Review/upload": QueueStatus.SCANNED,
}

PRIORITY_MAP = {
    "CRITICAL": Priority.CRITICAL,
    "HIGH": Priority.HIGH,
    "MEDIUM": Priority.MEDIUM,
    "LOW": Priority.LOW,
    "BACKLOG": Priority.BACKLOG,
}


class Command(BaseCommand):
    help = "Import the volume queue from pdfchecker's scanlist.csv."

    def add_arguments(self, parser):
        parser.add_argument("csv_file", help="Path to scanlist.csv")
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show what would be imported without saving.",
        )

    @staticmethod
    def _merge_volume_row(
        volume: Volume,
        start: int | None,
        end: int | None,
        is_partial: bool,
    ) -> None:
        """Widen a volume's page range to cover an additional CSV row.

        Used when several rows (e.g. parts A/B or advance sheets) map to
        the same volume created earlier in this run.

        :param volume: The volume created earlier in this run.
        :param start: The row's first page, if any.
        :param end: The row's last page, if any.
        :param is_partial: Whether the row carries a part label.
        """
        changed = []
        if start and (
            not volume.expected_start_page
            or start < volume.expected_start_page
        ):
            volume.expected_start_page = start
            changed.append("expected_start_page")
        if end and (
            not volume.expected_end_page or end > volume.expected_end_page
        ):
            volume.expected_end_page = end
            changed.append("expected_end_page")
        if is_partial and not volume.is_partial:
            volume.is_partial = True
            changed.append("is_partial")
        if changed:
            volume.save(update_fields=changed)

    def handle(self, *args, **options):
        csv_path = options["csv_file"]
        dry_run = options["dry_run"]

        reporters = {r.short_name: r for r in Reporter.objects.all()}
        volumes_created = existing_skipped = invalid_rows = errors = 0
        # Track volumes created during this run so multi-row volumes
        # (parts A/B, advance sheets) merge into one record instead of
        # being reported as already existing.
        created_keys: set[tuple[int, int]] = set()
        created_volumes: dict[tuple[int, int], Volume] = {}
        skipped_keys: set[tuple[int, int]] = set()

        with open(csv_path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f, delimiter="\t")
            for row in reader:
                slug = (row.get("Slug") or "").strip()
                volume_str = (row.get("Volume #") or "").strip()
                status_str = (row.get("Status") or "").strip()
                priority_str = (row.get("Priority") or "").strip().upper()
                first_page = (row.get("First Page") or "").strip()
                last_page = (row.get("Last Page") or "").strip()
                notes = (row.get("Notes") or "").strip()
                assigned = (row.get("Assigned To") or "").strip()
                source_library = (row.get("Located") or "").strip()
                is_advance = (row.get("Advance Sheet/Volume") or "").strip()
                book_num = (row.get("Book") or "").strip()

                if not slug or not volume_str:
                    invalid_rows += 1
                    continue

                reporter = reporters.get(slug)
                if not reporter:
                    logger.error("Unknown reporter slug: %s", slug)
                    errors += 1
                    continue

                # Parse volume number (strip trailing A/B/C)
                vol_digits = "".join(c for c in volume_str if c.isdigit())
                if not vol_digits:
                    logger.error("Bad volume: %s", volume_str)
                    errors += 1
                    continue
                vol_num = int(vol_digits)

                # Part label: "142A" → "A", advance sheet "3" → "3"
                part_label = volume_str[len(vol_digits) :]
                if not part_label and book_num:
                    part_label = book_num
                if (
                    not part_label
                    and is_advance
                    and is_advance.lower() == "yes"
                ):
                    # Use page range as label for advance sheets
                    if first_page:
                        part_label = first_page

                queue_status = STATUS_MAP.get(
                    status_str, QueueStatus.NEEDS_SCANNING
                )
                priority = PRIORITY_MAP.get(priority_str, Priority.MEDIUM)
                start = int(first_page) if first_page else None
                end = int(last_page) if last_page else None

                # Build notes
                parts = []
                if notes:
                    parts.append(notes)
                if assigned:
                    parts.append(f"Assigned to: {assigned}")
                combined_notes = "\n".join(parts)

                is_partial = bool(part_label)
                key = (reporter.id, vol_num)

                # A later row for a volume we already created this run
                # (e.g. part B after part A): widen its page range and
                # flag it partial, but do not report it as a duplicate.
                if key in created_keys:
                    if not dry_run:
                        self._merge_volume_row(
                            created_volumes[key], start, end, is_partial
                        )
                    continue

                # Already messaged as existing earlier this run.
                if key in skipped_keys:
                    continue

                # Skip volumes that already exist in the database.
                if Volume.objects.filter(
                    reporter=reporter, volume_number=vol_num
                ).exists():
                    skipped_keys.add(key)
                    existing_skipped += 1
                    self.stdout.write(
                        self.style.WARNING(
                            f"  Skipping existing volume: {slug} vol {vol_num}"
                        )
                    )
                    continue

                if dry_run:
                    self.stdout.write(
                        f"  {slug} vol {vol_num}"
                        f"{'/' + part_label if part_label else ''}"
                        f" [{queue_status}] {priority}"
                        f" pp.{start or '?'}-{end or '?'}"
                    )
                    created_keys.add(key)
                    volumes_created += 1
                    continue

                vol = Volume.objects.create(
                    reporter=reporter,
                    volume_number=vol_num,
                    priority=priority,
                    queue_status=queue_status,
                    source_library=source_library,
                    is_partial=is_partial,
                    expected_start_page=start,
                    expected_end_page=end,
                    notes=combined_notes,
                )
                created_keys.add(key)
                created_volumes[key] = vol
                volumes_created += 1

        action = "Would create" if dry_run else "Created"
        self.stdout.write(
            self.style.SUCCESS(
                f"{action} {volumes_created} volumes,"
                f" skipped {existing_skipped} existing,"
                f" {invalid_rows} invalid rows,"
                f" {errors} errors."
            )
        )
