"""Recover completed-but-unconfirmed direct-to-S3 uploads.

When a presigned upload's POST finished (the object is in S3) but the
browser never called ``confirm_scan_upload`` -- the container died mid
request, the tab closed, or confirm 500'd -- the scan is left fileless
with a lingering ``PendingUpload``. This command finds those, verifies the
object exists and is a valid PDF, attaches it to the scan, and replays the
stored upload action (exactly what confirm would have done).

Runs automatically as part of ``cleanup_processing_tmp`` (which recovers
before it deletes), but is exposed standalone for prompt recovery after a
known incident instead of waiting for the TTL sweep.

Examples:

    # Recover everything at least 5 minutes old (the default grace).
    docker exec scanning-daemon python manage.py recover_pending_uploads

    # Recover everything, including very recent uploads.
    docker exec scanning-daemon python manage.py recover_pending_uploads \\
        --min-age-minutes 0
"""

import logging

from django.core.management.base import BaseCommand

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = (
        "Attach completed-but-unconfirmed direct-to-S3 uploads to their "
        "scans (replays the upload action). Safe to run repeatedly."
    )

    def add_arguments(self, parser):
        """Register CLI flags.

        :param parser: The argparse parser to configure.
        :return: None.
        """
        parser.add_argument(
            "--min-age-minutes",
            type=float,
            default=5.0,
            help=(
                "Only consider pending uploads older than this, so an "
                "in-flight upload that is about to confirm isn't recovered "
                "out from under the browser. Default 5."
            ),
        )

    def handle(self, *args, **options):
        """Recover eligible pending uploads.

        :param args: Unused positional args.
        :param options: Parsed command options.
        :return: None.
        """
        from datetime import timedelta

        from django.utils import timezone

        from scanning import services
        from scanning.models import PendingUpload

        cutoff = timezone.now() - timedelta(minutes=options["min_age_minutes"])
        pending = PendingUpload.objects.filter(
            date_created__lt=cutoff
        ).select_related("scan")

        recovered = 0
        for row in pending:
            if services.recover_pending_upload(row):
                recovered += 1

        self.stdout.write(f"Recovered {recovered} unconfirmed upload(s).")
