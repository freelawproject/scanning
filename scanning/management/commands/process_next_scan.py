"""Claim and process the next queued scan (a single daemon tick).

Recovers any PROCESSING rows whose ``processed_at`` is older than
``DAEMON_PROCESSING_TIMEOUT`` back to QUEUED. Then atomically claims
one QUEUED scan (via ``SELECT ... FOR UPDATE SKIP LOCKED``) and
dispatches the queued action. Intake is not capped: a job row waits in
our own queue with no clock, so a long backlog drains in order instead
of expiring (issue #218). Only the full
(now interim: shard and
park) pipeline is dispatchable; the legacy actions (validate, detect,
reprocess, generate_files) were disconnected by issue #173 and rows
still carrying one are parked back to PENDING_REVIEW with the unified
pipeline-paused message.

Examples:

    # Process one queued scan and exit (useful for local debugging).
    docker exec scanning-daemon python manage.py process_next_scan

    # Also runs automatically from run_daemon every
    # DAEMON_POLL_INTERVAL seconds (default 5s).
"""

import logging
import time

from django.core.management.base import BaseCommand

logger = logging.getLogger(__name__)

# Every tick opens a fresh TCP+TLS connection (CONN_MAX_AGE=0 +
# connections.close_all()), so an occasional transient connect failure
# (SSL close_notify loss on a Postgres/pgbouncer blip) is expected. Retry
# a couple of times before giving up on the tick. See issue #116.
MAX_DB_RETRIES = 3
RETRY_BACKOFF_SECONDS = 0.5


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
        claimed = self._claim_next_scan_with_retry()
        if claimed is None:
            return
        scan, action = claimed
        self._dispatch(scan, action)

    def _claim_next_scan_with_retry(self):
        """Recover stale rows and claim the next queued scan, retrying
        transient DB connection failures.

        A fresh TCP+TLS connection is opened each attempt; a lost handshake
        (``OperationalError``) is retried with a short backoff. Both
        ``_recover_stale`` and ``_claim_next`` are idempotent under re-run,
        so retrying the pair is safe. A blip that survives every attempt is
        logged at WARNING (not ERROR) so a recovered, self-healing tick
        doesn't create a Sentry error event.

        :return: ``(scan, action)`` for the claimed scan, or ``None`` if
            nothing was queued or the connection never recovered.
        :rtype: tuple | None
        """
        from django.db import OperationalError, connections

        for attempt in range(MAX_DB_RETRIES):
            connections.close_all()
            try:
                self._recover_stale()
                return self._claim_next()
            except OperationalError as exc:
                if attempt == MAX_DB_RETRIES - 1:
                    logger.warning(
                        "DB connection failed during daemon tick after "
                        "%d attempts: %s",
                        attempt + 1,
                        exc,
                    )
                    return None
                time.sleep(RETRY_BACKOFF_SECONDS * (attempt + 1))
        return None

    def _recover_stale(self):
        """Reset scans stuck in PROCESSING past the timeout back to QUEUED.

        A scan is usually stale because the daemon was killed (SIGKILL/OOM,
        which never runs the SIGTERM re-queue handler) mid-pipeline, so this
        path bumps ``interruption_count`` and flags chronic offenders
        ERROR_INTERRUPTED just like the signal handler does, bounding the
        otherwise-unbounded re-queue loop (issue #124).

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
        Scan.requeue_or_flag_interrupted(
            stale,
            requeue_message="Re-queued (previous attempt timed out)",
        )

    def _claim_next(self):
        """Atomically claim the next queued scan and mark it PROCESSING.

        :return: ``(scan, action)`` for the claimed scan, or ``None`` if no
            scan is queued.
        :rtype: tuple | None
        """
        from django.db import transaction
        from django.utils import timezone

        from scanning.models import QueuedAction, Scan, Status

        with transaction.atomic():
            scan = (
                Scan.objects.select_for_update(skip_locked=True)
                .filter(status=Status.QUEUED)
                .order_by("date_created")
                .first()
            )
            if scan is None:
                return None

            action = scan.queued_action or QueuedAction.FULL_PIPELINE
            Scan.objects.filter(pk=scan.pk).update(
                status=Status.PROCESSING,
                processed_at=timezone.now(),
                progress_message=f"Starting {action}...",
                progress_current=0,
                progress_total=0,
            )

        return scan, action

    def _dispatch(self, scan, action):
        """Run the queued action for an already-claimed scan.

        :param scan: The claimed Scan instance (status PROCESSING).
        :param action: The QueuedAction to run.
        :return: None.
        """
        from scanning import services
        from scanning.models import QueuedAction, Scan, Status
        from scanning.utils import PIPELINE_PAUSED_MESSAGE

        self.stdout.write(f"Processing scan {scan.pk} ({action})")

        dispatch = {
            QueuedAction.FULL_PIPELINE: services.run_full_pipeline,
            # Issue #196. It runs here, and not on the collect tick,
            # because it renders every page of the volume three times.
            # It parks the scan itself and raises nothing, so the
            # generic ERROR arm below never sees it.
            QueuedAction.COMPUTE_REDACTIONS: services.run_compute_redactions,
            # Issue #224. Same shape: it pulls and writes whole volumes,
            # parks the scan itself and raises nothing.
            QueuedAction.APPLY_PAGE_EDITS: services.run_apply_page_edits,
        }

        # Legacy actions whose pipelines were disconnected (issue #173).
        # The views that queued them now refuse with the same message, so
        # a row can only carry one from before the cutover (or an admin
        # re-queue that kept a stale ``queued_action``). Park it back in
        # PENDING_REVIEW -- these scans all passed the old pipeline once,
        # so that is where they came from -- rather than ERROR: nothing
        # is wrong with the scan, the action just no longer exists.
        legacy = {
            QueuedAction.VALIDATE,
            QueuedAction.DETECT,
            QueuedAction.REPROCESS,
            QueuedAction.GENERATE_FILES,
        }
        if action in legacy:
            logger.warning(
                "Scan %s queued legacy action %r; parking it (issue #173)",
                scan.pk,
                action,
            )
            Scan.objects.filter(pk=scan.pk).update(
                status=Status.PENDING_REVIEW,
                progress_message=PIPELINE_PAUSED_MESSAGE,
            )
            return

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
