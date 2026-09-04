"""Confirm in-flight external jobs and finish the scans they belong to.

Seven passes, in order.

**1. ``jobs.sweep_jobs()`` asks after every job still in flight.** How
it asks depends on the provider:

- A RunPod job is polled at ``GET /status``. This is the only way such a
  job ever finishes, because submitting it merely put it in a queue.
- A doctor job has no status endpoint, so instead we check whether its
  result object has appeared in S3. Doctor answers on the submit call, so
  this pass matters only when that answer was lost -- a killed daemon, a
  redeployed pod, an abandoned read. Doctor converts and uploads whether
  or not we are still listening, so a lost answer costs the answer, not
  the work.

**2. ``bitonal.finish_ready_scans()`` applies finished conversions.** It
merges the shards of any scan whose conversion jobs are all done and
moves it out of ``AWAITING``.

**3. ``dots_mocr.finish_ready_runs()`` glues finished OCR runs.** It
joins the per-shard payloads of any scan whose dots.mocr rows are all
``COMPLETED`` into one volume JSON on S3 and flips the rows to
``CONSUMED`` (issue #202). It writes no scan status, and it keeps the
per-shard results: a future smart glue over page inserts and deletes
re-reads them.

**4. ``dots_mocr.apply_ready_runs()`` applies glued runs (#149/#204).**
It reads each glued volume JSON, rebuilds ``Scan.ocr_results`` and the
Issues, and takes the scan over the review edge to
``READY_FOR_PAGE_COMPLETENESS_REVIEW`` with one compare-and-swap.
Deliberately not queued work (#212): the scan never transits
QUEUED/PROCESSING. A scan still ``AWAITING`` its conversion is
deferred and picked up on the tick after the bitonal park.

**5. ``yolo.finish_ready_runs()`` merges finished detection runs.** It
joins the per-shard payloads of any scan whose detection rows are all
``COMPLETED`` into one volume JSON on S3 and flips the rows to
``CONSUMED`` (issue #196). Like the dots.mocr glue it writes no scan
status and keeps the per-shard results: a page insert recomputes the
merge from them.

**6. ``yolo.queue_ready_runs()`` queues the redaction computation.**
It is a trigger, not the work: it takes a scan in
``PAGE_COMPLETENESS_REVIEW_DONE`` to ``QUEUED`` with
``COMPUTE_REDACTIONS``, and ``process_next_scan`` runs it. Since the
daemon starts detection right after the upload (#250), the run is
usually merged by pass 5 while the scan is still in review 1, and it
waits there at no cost until the approval; this pass takes it on the
tick after. The
computation renders every page of the volume three times (83s for 1364
pages, measured), and this tick's scheduler is serial (#156), so it
must not run here.

**7. ``review_states.promote_ready_scans()`` opens review 2 (#263).**
It takes an approved scan whose redactions are computed, and whose
corrected volume is built (#224), to
``READY_FOR_REDACTION_REVIEW`` with one compare-and-swap. It is the
safety net rather than the usual writer: pass 6's computation parks a
scan it just finished in that status itself, so what is left here is
the volume whose corrected build lands after its geometry, and every
volume that was already approved and measured when #263 shipped.

Examples:

    # Run one confirm tick and exit.
    docker exec scanning-daemon python manage.py collect_external_jobs

    # Also runs automatically from run_daemon every
    # DAEMON_COLLECT_INTERVAL seconds (default 15s).
"""

import logging
import time

from django.core.management.base import BaseCommand

logger = logging.getLogger(__name__)

MAX_DB_RETRIES = 3
RETRY_BACKOFF_SECONDS = 0.5


class Command(BaseCommand):
    help = (
        "Ask after every in-flight external job, then merge and park any "
        "scan whose conversion jobs have all finished, then glue any "
        "finished dots.mocr run into its volume document, then apply "
        "every glued run (page numbers and Issues), then merge every "
        "finished detection run and queue its redaction computation, "
        "then open the redaction review of every scan that is ready "
        "for it."
    )

    def handle(self, *args, **options):
        """Run one confirm tick.

        :param args: Positional arguments from the management command.
        :param options: Parsed command-line options.
        :return: None.
        """
        from django.db import OperationalError, connections

        from scanning import (
            bitonal,
            dots_mocr,
            jobs,
            review_states,
            yolo,
        )

        for attempt in range(MAX_DB_RETRIES):
            connections.close_all()
            try:
                summary = jobs.sweep_jobs()
                finished = bitonal.finish_ready_scans()
                glued = dots_mocr.finish_ready_runs()
                applied = dots_mocr.apply_ready_runs()
                detected = yolo.finish_ready_runs()
                queued = yolo.queue_ready_runs()
                promoted = review_states.promote_ready_scans()
                break
            except OperationalError as exc:
                if attempt == MAX_DB_RETRIES - 1:
                    logger.warning(
                        "DB connection failed during collect tick after "
                        "%d attempts: %s",
                        attempt + 1,
                        exc,
                    )
                    return
                time.sleep(RETRY_BACKOFF_SECONDS * (attempt + 1))
        else:
            return

        if any(
            (
                summary.completed,
                summary.retried,
                summary.failed,
                summary.errors,
                finished,
                glued,
                applied,
                detected,
                queued,
                promoted,
            )
        ):
            self.stdout.write(
                f"Completed {summary.completed}, retried {summary.retried}, "
                f"failed {summary.failed}, still waiting {summary.pending}, "
                f"check errors {summary.errors}; finished {finished} "
                f"scan(s), glued {glued} OCR run(s), applied {applied}, "
                f"merged {detected} detection run(s), queued {queued} "
                f"redaction computation(s), opened {promoted} redaction "
                f"review(s)"
            )
