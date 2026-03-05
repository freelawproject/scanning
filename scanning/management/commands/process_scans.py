from django.core.management.base import BaseCommand
from django.utils import timezone

from scanning.models import Scan, Status


class Command(BaseCommand):
    help = "Process scans that have been uploaded but not yet processed."

    def handle(self, *args, **options):
        """Find unprocessed scans and run the processing pipeline.

        Queries for scans with status UPLOADED that are missing a
        redacted or compressed PDF, then moves each through the
        PROCESSING -> EXTRACTED workflow.

        :param args: Positional arguments (unused).
        :param options: Command options (unused).
        """
        scans = Scan.objects.needs_processing()

        if not scans.exists():
            self.stdout.write("No scans to process.")
            return

        for scan in scans:
            self.stdout.write(
                f"Processing {scan}"
            )
            # Change Scan status and store start time
            scan.status = Status.PROCESSING
            scan.processed_at = timezone.now()
            scan.save(update_fields=["status", "processed_at"])

            # TODO implement blackletter call and use result to update Scan object and create OpinionScan objects

            self.stdout.write(
                f"Blackletter finished for {scan}"
            )

            # Change scan status
            scan.status = Status.EXTRACTED
            scan.save(update_fields=["status"])

        self.stdout.write(f"Processed {scans.count()} scan(s).")
