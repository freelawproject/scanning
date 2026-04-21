"""Claim and process the next queued scan (a single daemon tick).

Recovers any PROCESSING rows whose ``processed_at`` is older than
``DAEMON_PROCESSING_TIMEOUT`` back to QUEUED, then atomically claims
one QUEUED scan (via ``SELECT ... FOR UPDATE SKIP LOCKED``) and
dispatches the queued action (full pipeline, validate, detect,
reprocess, or generate_files).

Examples:

    # Process one queued scan and exit (useful for local debugging).
    docker exec scanning-daemon python manage.py process_next_scan

    # Also runs automatically from run_daemon every
    # DAEMON_POLL_INTERVAL seconds (default 5s).
"""

import logging

from django.core.management.base import BaseCommand

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = (
        "Process the next queued scan: recover stale PROCESSING rows, "
        "then atomically claim one QUEUED scan and dispatch its action."
    )

    def handle(self, *args, **options):
        """Run one scheduler tick of scan processing.

        :param args: Positional arguments from the management command.
        :param options: Parsed command-line options.
        :return: None.
        """
        self._recover_stale()
        self._process_next()

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

        connections.close_all()

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
