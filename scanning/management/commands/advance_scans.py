"""Re-enter scans whose external jobs have all landed.

The other half of the claim tick. ``process_next_scan`` starts a scan;
this restarts one that was parked waiting on a GPU, once nothing it
was waiting for is outstanding. Both go through
``pipeline.advance_scan``, which runs the action from the top and
either finishes it, parks it again on the next stage, or fails it.

Runs automatically from ``run_daemon`` every
``DAEMON_ADVANCE_INTERVAL`` seconds (default 10s).

Examples:

    # Push every ready scan as far as it will go.
    docker exec scanning-daemon python manage.py advance_scans
"""

import logging
import time

from django.core.management.base import BaseCommand
from django.db import OperationalError, connections
from django.utils import timezone

logger = logging.getLogger(__name__)

MAX_DB_RETRIES = 3
RETRY_BACKOFF_SECONDS = 0.5


class Command(BaseCommand):
    help = (
        "Re-enter the pipeline for scans waiting on external jobs that "
        "have all reached a terminal state."
    )

    def handle(self, *args, **options):
        """Advance every resumable scan once.

        :param args: Positional arguments from the management command.
        :param options: Parsed command-line options.
        :return: None.
        """
        for scan_pk in self._claim_with_retry():
            self._advance(scan_pk)

    def _claim_with_retry(self):
        """Claim the resumable scans, retrying transient DB failures.

        :return: Primary keys of the scans claimed, possibly empty.
        :rtype: list[int]
        """
        for attempt in range(MAX_DB_RETRIES):
            connections.close_all()
            try:
                return self._claim()
            except OperationalError as exc:
                if attempt == MAX_DB_RETRIES - 1:
                    logger.warning(
                        "DB connection failed during advance after %d "
                        "attempts: %s",
                        attempt + 1,
                        exc,
                    )
                    return []
                time.sleep(RETRY_BACKOFF_SECONDS * (attempt + 1))
        return []

    def _claim(self):
        """Move resumable scans from AWAITING to PROCESSING.

        Claiming before running is what keeps the two statuses honest:
        while the pipeline is executing, a daemon really is holding the
        scan, so the process-death timeout applies again. The guard on
        ``AWAITING`` makes the claim atomic against a second replica.

        :return: Primary keys of the scans claimed.
        :rtype: list[int]
        """
        from scanning.models import Scan, Status
        from scanning.pipeline import resumable_scans

        claimed = []
        for scan_pk in list(resumable_scans().values_list("pk", flat=True)):
            taken = Scan.objects.filter(
                pk=scan_pk, status=Status.AWAITING
            ).update(
                status=Status.PROCESSING,
                processed_at=timezone.now(),
                progress_message="Applying results...",
            )
            if taken:
                claimed.append(scan_pk)
        return claimed

    def _advance(self, scan_pk):
        """Run one claimed scan's action as far as it will go.

        :param scan_pk: Primary key of the claimed scan.
        :return: None.
        """
        from scanning.pipeline import advance_scan

        self.stdout.write(f"Advancing scan {scan_pk}")
        advance_scan(scan_pk)
