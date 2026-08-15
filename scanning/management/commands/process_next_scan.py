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
        for scan_pk in self._claim_with_retry():
            if self._take(scan_pk):
                self._dispatch(scan_pk)

    def _claim_with_retry(self):
        """Recover stale rows and claim this tick's scans, retrying
        transient DB connection failures.

        A fresh TCP+TLS connection is opened each attempt; a lost handshake
        (``OperationalError``) is retried with a short backoff. Both
        ``_recover_stale`` and ``_claim_batch`` are idempotent under re-run,
        so retrying the pair is safe. A blip that survives every attempt is
        logged at WARNING (not ERROR) so a recovered, self-healing tick
        doesn't create a Sentry error event.

        :return: Primary keys of the scans claimed, possibly empty.
        :rtype: list[int]
        """
        from django.db import OperationalError, connections

        for attempt in range(MAX_DB_RETRIES):
            connections.close_all()
            try:
                self._recover_stale()
                return self._claim_batch()
            except OperationalError as exc:
                if attempt == MAX_DB_RETRIES - 1:
                    logger.warning(
                        "DB connection failed during daemon tick after "
                        "%d attempts: %s",
                        attempt + 1,
                        exc,
                    )
                    return []
                time.sleep(RETRY_BACKOFF_SECONDS * (attempt + 1))
        return []

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

    def _claim_batch(self):
        """Snapshot the scans this cycle will work, oldest first.

        A closed batch: the scans queued at the moment of the snapshot
        and no others. Anything queued while this cycle runs waits for
        the next one, which is what makes "this cycle is finished" a
        question with an answer rather than one a busy queue can keep
        postponing.

        How wide the snapshot is depends on whether steps still block.
        With ``DAEMON_BATCH_JOBS`` off, a claimed scan is worked to
        completion before the next one starts, so claiming several
        would only run them in series while holding the rest hostage;
        the batch is one scan, exactly as before. With it on, the whole
        batch's jobs go out together and the endpoint's worker cap
        decides how many run at once.

        The snapshot only *chooses*; it does not claim. Marking the
        whole batch PROCESSING here would leave the tail sitting in it
        for as long as the head takes to work -- and each scan's turn
        includes a bitonal conversion measured in minutes, so a wide
        batch would push the tail past ``DAEMON_PROCESSING_TIMEOUT``
        before it was ever dispatched. A second replica's stale sweep
        would then re-queue those rows, charge them an interruption,
        and run them alongside this one. :meth:`_take` claims each scan
        at its turn instead, so the window stays one scan wide.

        :return: Primary keys of the scans to work, oldest first.
        :rtype: list[int]
        """
        from django.conf import settings
        from django.db import transaction

        from scanning import jobs
        from scanning.models import Scan, Status

        if jobs.batch_enabled():
            size = int(settings.DAEMON_BATCH_SIZE) or None
        else:
            size = 1

        with transaction.atomic():
            queued = (
                Scan.objects.select_for_update(skip_locked=True)
                .filter(status=Status.QUEUED)
                .order_by("date_created")
                .values_list("pk", flat=True)
            )
            return list(queued[:size] if size else queued)

    def _take(self, scan_pk):
        """Claim one snapshotted scan, if it is still there to claim.

        Guarded on QUEUED, so a scan another replica took first, or one
        a user cancelled between the snapshot and now, is skipped
        rather than run twice.

        :param scan_pk: Primary key of the scan to claim.
        :return: Whether this process got it.
        :rtype: bool
        """
        from django.utils import timezone

        from scanning.models import QueuedAction, Scan, Status

        action = (
            Scan.objects.filter(pk=scan_pk)
            .values_list("queued_action", flat=True)
            .first()
        ) or QueuedAction.FULL_PIPELINE

        taken = Scan.objects.filter(pk=scan_pk, status=Status.QUEUED).update(
            status=Status.PROCESSING,
            processed_at=timezone.now(),
            progress_message=f"Starting {action}...",
            progress_current=0,
            progress_total=0,
        )
        if not taken:
            logger.info(
                "scan %s left the queue before its turn; skipping", scan_pk
            )
        return bool(taken)

    def _dispatch(self, scan_pk):
        """Run the queued action for an already-claimed scan.

        :param scan_pk: Primary key of the claimed scan (PROCESSING).
        :return: None.
        """
        from scanning.pipeline import advance_scan

        self.stdout.write(f"Processing scan {scan_pk}")
        advance_scan(scan_pk)
