"""Claim and process the next queued scan (a single daemon tick).

Recovers any PROCESSING rows whose ``processed_at`` is older than
``DAEMON_PROCESSING_TIMEOUT`` back to QUEUED. Then, unless
``DAEMON_MAX_ACTIVE_SCANS`` scans already hold unfinished external work
(issue #218), atomically claims one QUEUED scan (via ``SELECT ... FOR
UPDATE SKIP LOCKED``) and dispatches the queued action. The cap is the
daemon's only backpressure: a scan it refuses stays QUEUED, which costs
nothing and times out nowhere. Only the full (now interim: shard and
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

# Whether the last tick found intake full, so the crossing is logged
# once in each direction instead of on every one of the 720 ticks an
# hour. Module level because ``call_command`` builds a fresh Command
# each tick; a daemon restart costs one extra line.
_intake_full = False


class Command(BaseCommand):
    help = (
        "Process the next queued scan: recover stale PROCESSING rows, "
        "then, if fewer than DAEMON_MAX_ACTIVE_SCANS scans hold external "
        "work, atomically claim one QUEUED scan and dispatch its action."
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

        The intake cap sits between the two, so a full queue skips the
        claim but never the recovery (issue #218).

        :return: ``(scan, action)`` for the claimed scan, or ``None`` if
            nothing was queued, intake is full, or the connection never
            recovered.
        :rtype: tuple | None
        """
        from django.db import OperationalError, connections

        for attempt in range(MAX_DB_RETRIES):
            connections.close_all()
            try:
                self._recover_stale()
                if self._intake_is_full():
                    return None
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

    def _intake_is_full(self):
        """Return whether the external queues are too full to admit a scan.

        The daemon's only backpressure (issue #218). Without it the tick
        claimed the oldest QUEUED scan whatever the queues held, and a
        bulk re-queue put more work behind the parked scans than the
        6-hour ``DAEMON_JOB_MAX_QUEUE_SECONDS`` ceiling allows a row to
        wait -- which fails volumes hours later for being popular.

        Three properties this placement carries:

        - It gates the **claim**, not the dispatch after it. A claimed
          scan that was then refused would transit PROCESSING and spend
          a status write for nothing.
        - A refused scan simply stays QUEUED. Nothing times out there,
          the status page already says "queued", and the scan starts as
          soon as a slot opens.
        - Stale-scan recovery runs first and unconditionally. Returning
          a scan the daemon dropped is not intake.

        There is no deadlock: the submit and collect ticks drain the
        queues whether or not anything is being claimed, and the cap is
        read fresh on the next tick.

        The staff buttons are deliberately outside this gate. They
        create rows straight from a request, one scan at a time, so a
        person can still start a re-run over an already-admitted volume.

        :return: Whether this tick must claim nothing.
        :rtype: bool
        """
        global _intake_full

        from django.conf import settings

        from scanning import jobs

        cap = int(settings.DAEMON_MAX_ACTIVE_SCANS)
        active = jobs.active_scan_count()
        full = active >= cap
        if full and not _intake_full:
            logger.warning(
                "intake paused: %d scan(s) hold external work at a cap of "
                "%d; queued scans wait for a slot (issue #218)",
                active,
                cap,
            )
        elif _intake_full and not full:
            logger.info(
                "intake resumed: %d scan(s) hold external work at a cap of %d",
                active,
                cap,
            )
        _intake_full = full
        return full

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
