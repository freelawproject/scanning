"""Import scan queue from pdfchecker's scanlist.csv."""

import csv
import logging

from django.core.management.base import BaseCommand

from scanning.models import (
    Priority,
    QueueStatus,
    Reporter,
    Scan,
    Source,
    Status,
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
    help = "Import scan queue from pdfchecker's scanlist.csv."

    def add_arguments(self, parser):
        parser.add_argument("csv_file", help="Path to scanlist.csv")
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show what would be imported without saving.",
        )

    def handle(self, *args, **options):
        csv_path = options["csv_file"]
        dry_run = options["dry_run"]

        reporters = {r.short_name: r for r in Reporter.objects.all()}
        volumes_created = scans_created = skipped = errors = 0

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
                    skipped += 1
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

                source = (
                    Source.OPINIONS
                    if is_advance and is_advance.lower() == "yes"
                    else Source.FULL
                )
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

                if dry_run:
                    self.stdout.write(
                        f"  {slug} vol {vol_num}"
                        f"{'/' + part_label if part_label else ''}"
                        f" [{queue_status}] {priority}"
                        f" pp.{start or '?'}-{end or '?'}"
                    )
                    scans_created += 1
                    continue

                # Get or create Volume
                vol, vol_created = Volume.objects.get_or_create(
                    reporter=reporter,
                    volume_number=vol_num,
                    defaults={
                        "priority": priority,
                        "queue_status": queue_status,
                        "source_library": source_library,
                        "is_partial": is_partial,
                        "notes": "",
                    },
                )
                if vol_created:
                    volumes_created += 1
                elif is_partial and not vol.is_partial:
                    vol.is_partial = True
                    vol.save(update_fields=["is_partial"])

                # Update volume page range to cover all scans
                if start and (
                    not vol.expected_start_page
                    or start < vol.expected_start_page
                ):
                    vol.expected_start_page = start
                if end and (
                    not vol.expected_end_page or end > vol.expected_end_page
                ):
                    vol.expected_end_page = end
                vol.save()

                # Create Scan
                exists = Scan.objects.filter(
                    reporter=reporter,
                    volume=vol_num,
                    source=source,
                    start_page=start,
                ).exists()
                if exists:
                    skipped += 1
                    continue

                Scan.objects.create(
                    volume_obj=vol,
                    reporter=reporter,
                    volume=vol_num,
                    part_label=part_label,
                    source=source,
                    start_page=start,
                    end_page=end,
                    notes=combined_notes,
                    source_library=source_library,
                    status=Status.UPLOADED,
                )
                scans_created += 1

        action = "Would create" if dry_run else "Created"
        self.stdout.write(
            self.style.SUCCESS(
                f"{action} {volumes_created} volumes,"
                f" {scans_created} scans,"
                f" skipped {skipped},"
                f" {errors} errors."
            )
        )
