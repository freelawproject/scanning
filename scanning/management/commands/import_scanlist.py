"""Import scan queue from pdfchecker's scanlist.csv."""

import csv

from django.core.management.base import BaseCommand

from scanning.models import (
    Priority,
    QueueStatus,
    Reporter,
    Scan,
    Source,
    Status,
)

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
        parser.add_argument(
            "csv_file", help="Path to scanlist.csv"
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show what would be imported without saving.",
        )

    def handle(self, *args, **options):
        csv_path = options["csv_file"]
        dry_run = options["dry_run"]

        reporters = {r.short_name: r for r in Reporter.objects.all()}
        created = skipped = errors = 0

        with open(csv_path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f, delimiter="\t")
            for row in reader:
                slug = (row.get("Slug") or "").strip()
                volume_str = (row.get("Volume #") or "").strip()
                status_str = (row.get("Status") or "").strip()
                priority_str = (
                    row.get("Priority") or ""
                ).strip().upper()
                first_page = (
                    row.get("First Page") or ""
                ).strip()
                last_page = (
                    row.get("Last Page") or ""
                ).strip()
                notes = (row.get("Notes") or "").strip()
                assigned = (
                    row.get("Assigned To") or ""
                ).strip()
                source_library = (
                    row.get("Located") or ""
                ).strip()
                is_advance = (
                    row.get("Advance Sheet/Volume") or ""
                ).strip()

                if not slug or not volume_str:
                    skipped += 1
                    continue

                reporter = reporters.get(slug)
                if not reporter:
                    self.stderr.write(
                        f"Unknown reporter slug: {slug}"
                    )
                    errors += 1
                    continue

                # Parse volume (handle "141A", "141B" etc)
                try:
                    volume = int(volume_str)
                except ValueError:
                    # Strip trailing letter for volume number
                    vol_digits = "".join(
                        c for c in volume_str if c.isdigit()
                    )
                    if vol_digits:
                        volume = int(vol_digits)
                    else:
                        self.stderr.write(
                            f"Bad volume: {volume_str}"
                        )
                        errors += 1
                        continue

                source = (
                    Source.OPINIONS
                    if is_advance
                    and is_advance.lower() == "yes"
                    else Source.FULL
                )

                queue_status = STATUS_MAP.get(
                    status_str, QueueStatus.NEEDS_SCANNING
                )
                priority = PRIORITY_MAP.get(
                    priority_str, Priority.MEDIUM
                )

                start = int(first_page) if first_page else None
                end = int(last_page) if last_page else None

                # Build notes
                parts = []
                if notes:
                    parts.append(notes)
                if assigned:
                    parts.append(f"Assigned to: {assigned}")
                if source_library:
                    parts.append(f"Located: {source_library}")
                combined_notes = "\n".join(parts)

                # Check for existing
                exists = Scan.objects.filter(
                    reporter=reporter,
                    volume=volume,
                    source=source,
                    start_page=start,
                ).exists()
                if exists:
                    skipped += 1
                    continue

                if dry_run:
                    self.stdout.write(
                        f"  {slug} vol {volume_str}"
                        f" [{queue_status}] {priority}"
                        f" pp.{start or '?'}-{end or '?'}"
                    )
                else:
                    Scan.objects.create(
                        reporter=reporter,
                        volume=volume,
                        source=source,
                        start_page=start,
                        end_page=end,
                        queue_status=queue_status,
                        priority=priority,
                        status=Status.UPLOADED
                        if queue_status == QueueStatus.COMPLETE
                        else Status.UPLOADED,
                        notes=combined_notes,
                        source_library=source_library,
                    )
                created += 1

        action = "Would create" if dry_run else "Created"
        self.stdout.write(
            self.style.SUCCESS(
                f"{action} {created} scans,"
                f" skipped {skipped},"
                f" {errors} errors."
            )
        )
