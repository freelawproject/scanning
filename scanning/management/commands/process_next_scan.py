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
import threading
import time
from contextlib import contextmanager

from django.core.management.base import BaseCommand

logger = logging.getLogger(__name__)

# Every tick opens a fresh TCP+TLS connection (CONN_MAX_AGE=0 +
# connections.close_all()), so an occasional transient connect failure
# (SSL close_notify loss on a Postgres/pgbouncer blip) is expected. Retry
# a couple of times before giving up on the tick. See issue #116.
MAX_DB_RETRIES = 3
RETRY_BACKOFF_SECONDS = 0.5

#: The claim order, by action, then by age. The worker is serial, so a
#: person's wait goes first: an upload (a blank action means the same),
#: then a redaction compute, then the page edit apply (#224), a
#: backfill nobody watches. The opinion creation (#336) follows a
#: curator's approval, so it goes before the apply too. An unlisted
#: action ranks with the compute.
CLAIM_PRIORITY = (
    "full_pipeline",
    "compute_redactions",
    "compute_then_create_opinions",
    "create_opinions",
    "apply_page_edits",
)

#: An apply queued longer than this is claimed next. Without it, a
#: steady stream of uploads starves the apply: the five queued applies
#: (``apply.MAX_SCANS_IN_FLIGHT``) are never claimed, and the trigger
#: queues no more. The wait is measured from the trigger's write of
#: ``date_modified``.
CLAIM_LIFT_SECONDS = 15 * 60


def recover_stale() -> None:
    """Reset scans stuck in PROCESSING past the timeout back to QUEUED.

    A scan is usually stale because the worker was killed (SIGKILL/OOM,
    which never runs the SIGTERM re-queue handler) mid-pipeline, so this
    path bumps ``interruption_count`` and flags chronic offenders
    ERROR_INTERRUPTED just like the signal handler does, bounding the
    otherwise-unbounded re-queue loop (issue #124).

    ``processed_at`` is the claim stamp and the heartbeat: every
    progress write of the long actions refreshes it
    (``services._update_progress``, #503), so with more than one
    worker a live two-hour apply is never swept for being old.

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
        logger.info(
            "Recovering stale scan %s (processing since %s)",
            scan.pk,
            scan.processed_at,
        )
    Scan.requeue_or_flag_interrupted(
        stale,
        requeue_message="Re-queued (previous attempt timed out)",
    )


def _claim_rank():
    """Return the claim order as an annotation: ``CLAIM_PRIORITY``,
    with an apply that has waited ``CLAIM_LIFT_SECONDS`` moved to the
    front.

    :returns: A ``Case`` expression over ``queued_action``.
    """
    from datetime import timedelta

    from django.db.models import Case, IntegerField, Value, When
    from django.utils import timezone

    from scanning.models import QueuedAction

    lifted_before = timezone.now() - timedelta(seconds=CLAIM_LIFT_SECONDS)
    return Case(
        When(
            queued_action=QueuedAction.APPLY_PAGE_EDITS,
            date_modified__lt=lifted_before,
            then=Value(0),
        ),
        When(queued_action="", then=Value(0)),
        *(
            When(queued_action=action, then=Value(index))
            for index, action in enumerate(CLAIM_PRIORITY)
        ),
        default=Value(CLAIM_PRIORITY.index(QueuedAction.COMPUTE_REDACTIONS)),
        output_field=IntegerField(),
    )


def claim_next(scan_pk: int | None = None):
    """Atomically claim the next queued scan, or the one named, and
    mark it PROCESSING.

    The claim is ``SELECT ... FOR UPDATE SKIP LOCKED`` over
    ``status=QUEUED``, so any number of workers may call it at once
    and each gets its own scan. With ``scan_pk`` the claim is the
    dispatch of a button press (``tasks.run_scan``, #503): the scan is
    claimed only if it is still QUEUED, so a tick that took it first
    costs the press nothing.

    :param scan_pk: A scan to claim, or ``None`` for the next by rank.
    :return: ``(scan, action)`` for the claimed scan, or ``None`` if no
        scan is queued.
    :rtype: tuple | None
    """
    from django.db import transaction
    from django.utils import timezone

    from scanning.models import QueuedAction, Scan, Status

    with transaction.atomic():
        queued = Scan.objects.select_for_update(skip_locked=True).filter(
            status=Status.QUEUED
        )
        if scan_pk is not None:
            scan = queued.filter(pk=scan_pk).first()
        else:
            scan = (
                queued.annotate(claim_rank=_claim_rank())
                .order_by("claim_rank", "date_created")
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


#: How often (seconds) a running action stamps ``processed_at`` (#503).
#: Well inside ``DAEMON_PROCESSING_TIMEOUT``, so a live action on any
#: worker is never swept as stale, and a dead worker's scan is swept
#: one timeout after its last beat.
HEARTBEAT_SECONDS = 60


def stamp_heartbeat(scan_pk: int) -> int:
    """Refresh the claim stamp of a scan still PROCESSING.

    :param scan_pk: The claimed scan.
    :returns: Rows written: 1, or 0 once the scan left PROCESSING.
    :rtype: int
    """
    from django.utils import timezone

    from scanning.models import Scan, Status

    return Scan.objects.filter(pk=scan_pk, status=Status.PROCESSING).update(
        processed_at=timezone.now()
    )


@contextmanager
def heartbeat(scan_pk: int, interval: float | None = None):
    """Stamp the scan every ``interval`` seconds from a thread, for the
    block.

    The thread has a database connection of its own (Django's are per
    thread) and closes it when the block ends. A failed stamp is logged
    and the next one is tried: the sweep tolerates a missed beat, and
    an action must never die of its own heartbeat.

    :param scan_pk: The claimed scan.
    :param interval: Seconds between beats; ``HEARTBEAT_SECONDS``.
    :return: A context manager.
    """
    from django.db import connection

    stop = threading.Event()
    period = HEARTBEAT_SECONDS if interval is None else interval

    def beat() -> None:
        try:
            while not stop.wait(period):
                try:
                    if not stamp_heartbeat(scan_pk):
                        return
                except Exception:
                    logger.warning(
                        "heartbeat of scan %s failed; next beat in %ss",
                        scan_pk,
                        period,
                        exc_info=True,
                    )
        finally:
            connection.close()

    thread = threading.Thread(
        target=beat, name=f"heartbeat-{scan_pk}", daemon=True
    )
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join(timeout=period + 5)


def dispatch(scan, action) -> None:
    """Run the queued action for an already-claimed scan.

    :param scan: The claimed Scan instance (status PROCESSING).
    :param action: The QueuedAction to run.
    :return: None.
    """
    from scanning import services
    from scanning.models import QueuedAction, Scan, Status
    from scanning.utils import PIPELINE_PAUSED_MESSAGE

    logger.info("Processing scan %s (%s)", scan.pk, action)

    dispatch_table = {
        QueuedAction.FULL_PIPELINE: services.run_full_pipeline,
        # Issue #196. It runs here, and not on the collect tick,
        # because it renders every page of the volume three times.
        # It parks the scan itself and raises nothing, so the
        # generic ERROR arm below never sees it.
        QueuedAction.COMPUTE_REDACTIONS: services.run_compute_redactions,
        # Issue #224. Same shape: it pulls and writes whole volumes,
        # parks the scan itself and raises nothing.
        QueuedAction.APPLY_PAGE_EDITS: services.run_apply_page_edits,
        # Issue #336. Rows and one JSON read; parks the scan itself
        # and raises nothing.
        QueuedAction.CREATE_OPINIONS: services.run_create_opinions,
        # Issue #240. The compute of a review-2 approval, which
        # queues CREATE_OPINIONS itself on success.
        QueuedAction.COMPUTE_THEN_CREATE_OPINIONS: (
            services.run_compute_then_create_opinions
        ),
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

    fn = dispatch_table.get(action)
    if fn is None:
        logger.error("Unknown queued_action %r for scan %s", action, scan.pk)
        Scan.objects.filter(pk=scan.pk).update(
            status=Status.ERROR,
            progress_message=f"Unknown action: {action}",
        )
        return

    try:
        with heartbeat(scan.pk):
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
        """Reset the stale PROCESSING scans; see :func:`recover_stale`."""
        recover_stale()

    def _claim_next(self):
        """Claim the next queued scan; see :func:`claim_next`."""
        return claim_next()

    def _dispatch(self, scan, action):
        """Run the queued action; see :func:`dispatch`."""
        self.stdout.write(f"Processing scan {scan.pk} ({action})")
        dispatch(scan, action)
