"""Daemon that polls for queued scans and processes them one at a time."""

import logging
import signal
import time

from django.conf import settings
from django.core.management.base import BaseCommand

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Run the scanning daemon that polls for queued scans."

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.shutdown = False

    def handle(self, *args, **options):
        """Poll for queued scans and process them until shutdown.

        :param args: Positional arguments from the management command.
        :param options: Parsed command-line options.
        :return: None.
        """
        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT, self._handle_signal)

        interval = settings.DAEMON_POLL_INTERVAL
        timeout = settings.DAEMON_PROCESSING_TIMEOUT
        self.stdout.write(
            f"Daemon started, polling every {interval}s, "
            f"stale timeout {timeout}s"
        )

        while not self.shutdown:
            try:
                self._recover_stale()
                self._process_next()
            except Exception:
                logger.exception("Daemon loop error")
            time.sleep(interval)

        self.stdout.write("Daemon shutting down.")

    def _recover_stale(self):
        """Reset scans stuck in PROCESSING past the timeout back to QUEUED.

        :return: None.
        """
        from datetime import timedelta

        from django.conf import settings
        from django.utils import timezone

        from scanning.models import Scan, Status

        cutoff = timezone.now() - timedelta(
            seconds=settings.DAEMON_PROCESSING_TIMEOUT
        )
        stale = Scan.objects.filter(
            status=Status.PROCESSING,
            processed_at__lt=cutoff,
        )
        for scan in stale:
            self.stdout.write(
                f"Recovering stale scan {scan.pk} "
                f"(processing since {scan.processed_at})"
            )
            Scan.objects.filter(pk=scan.pk).update(
                status=Status.QUEUED,
                progress_message="Re-queued (previous attempt timed out)",
            )

    def _process_next(self):
        """Fetch the next queued scan and dispatch its action.

        :return: None.
        """
        from django.db import connections, transaction
        from django.utils import timezone

        from scanning import services
        from scanning.models import QueuedAction, Scan, Status

        # Close stale DB connections before each cycle
        connections.close_all()

        # Atomically claim the next queued scan. skip_locked ensures a
        # concurrent daemon picks a different row instead of blocking.
        with transaction.atomic():
            scan = (
                Scan.objects.select_for_update(skip_locked=True)
                .filter(status=Status.QUEUED)
                .order_by("date_created")
                .first()
            )
            if scan is None:
                return

            action = scan.queued_action or QueuedAction.FULL_PIPELINE
            Scan.objects.filter(pk=scan.pk).update(
                status=Status.PROCESSING,
                processed_at=timezone.now(),
                progress_message=f"Starting {action}...",
                progress_current=0,
                progress_total=0,
            )

        self.stdout.write(f"Processing scan {scan.pk} ({action})")

        dispatch = {
            QueuedAction.FULL_PIPELINE: services.run_full_pipeline,
            QueuedAction.VALIDATE: services.run_validate_with_bitonal,
            QueuedAction.DETECT: services.run_detect,
            QueuedAction.REPROCESS: services.run_reprocess,
            QueuedAction.GENERATE_FILES: services.run_generate_files,
        }

        fn = dispatch.get(action)
        if fn is None:
            logger.error(
                "Unknown queued_action %r for scan %s", action, scan.pk
            )
            Scan.objects.filter(pk=scan.pk).update(
                status=Status.ERROR,
                progress_message=f"Unknown action: {action}",
            )
            return

        try:
            fn(scan.pk)
        except Exception:
            logger.exception("Scan %s (%s) failed", scan.pk, action)
            # Only set ERROR if the pipeline didn't already handle it
            current = (
                Scan.objects.filter(pk=scan.pk)
                .values_list("status", flat=True)
                .first()
            )
            if current == Status.PROCESSING:
                Scan.objects.filter(pk=scan.pk).update(
                    status=Status.ERROR,
                    progress_message="Unexpected error (check logs)",
                )

    def _handle_signal(self, signum, frame):
        """Set the shutdown flag on receipt of a termination signal.

        :param signum: The signal number received.
        :param frame: The interrupted stack frame.
        :return: None.
        """
        self.stdout.write(f"Received signal {signum}, shutting down...")
        self.shutdown = True
