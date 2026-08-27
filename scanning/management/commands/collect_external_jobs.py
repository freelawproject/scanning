"""Confirm in-flight external jobs and finish the scans they belong to.

Three passes, in order.

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
re-reads them. Reading page numbers out of the glued JSON is issue
#149.

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
        "finished dots.mocr run into its volume document."
    )

    def handle(self, *args, **options):
        """Run one confirm tick.

        :param args: Positional arguments from the management command.
        :param options: Parsed command-line options.
        :return: None.
        """
        from django.db import OperationalError, connections

        from scanning import bitonal, dots_mocr, jobs

        for attempt in range(MAX_DB_RETRIES):
            connections.close_all()
            try:
                summary = jobs.sweep_jobs()
                finished = bitonal.finish_ready_scans()
                glued = dots_mocr.finish_ready_runs()
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
            )
        ):
            self.stdout.write(
                f"Completed {summary.completed}, retried {summary.retried}, "
                f"failed {summary.failed}, still waiting {summary.pending}, "
                f"check errors {summary.errors}; finished {finished} "
                f"scan(s), glued {glued} OCR run(s)"
            )
