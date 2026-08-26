"""Confirm in-flight external jobs and finish the scans they belong to.

Three parts, in order:

1. ``jobs.retry_dead()`` -- put failed shards back in the queue while
   they are still owed an attempt. First, so a row that goes back to
   PENDING is already PENDING when part 3 judges its scan; otherwise
   that scan would be written off in the same tick that revived it.
2. ``jobs.sweep_jobs()`` -- for each job still in flight, check whether
   its result object appeared. The only path that can finish a job whose
   response we never saw: doctor converts and uploads even after we stop
   reading, so a killed daemon loses the answer, not the work.
3. ``bitonal.finish_ready_scans()`` -- merge the shards of any scan
   whose jobs are all done and move it out of ``AWAITING`` (or out of
   ``ERROR``, for a run that finished after its scan was written off).

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
        "Confirm in-flight external jobs against their result objects, "
        "then merge and park any scan whose jobs have all finished."
    )

    def handle(self, *args, **options):
        """Run one confirm tick.

        :param args: Positional arguments from the management command.
        :param options: Parsed command-line options.
        :return: None.
        """
        from django.db import OperationalError, connections

        from scanning import bitonal, jobs

        for attempt in range(MAX_DB_RETRIES):
            connections.close_all()
            try:
                retried = jobs.retry_dead()
                summary = jobs.sweep_jobs()
                finished = bitonal.finish_ready_scans()
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
                retried,
                summary.completed,
                summary.retried,
                summary.failed,
                summary.errors,
                finished,
            )
        ):
            self.stdout.write(
                f"Completed {summary.completed}, retried "
                f"{summary.retried + retried}, failed {summary.failed}, "
                f"still waiting {summary.pending}, check errors "
                f"{summary.errors}; finished {finished} scan(s)"
            )
