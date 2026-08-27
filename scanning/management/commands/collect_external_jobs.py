"""Confirm in-flight external jobs and finish the scans they belong to.

Two halves, in order:

1. ``jobs.sweep_jobs()`` -- ask after every job still in flight. For a
   RunPod job that is a status poll, and it is the normal path:
   submitting only queued the work. For a doctor job it is a check for
   the result object, and it is the only path that can finish a job whose
   response we never saw -- doctor converts and uploads even after we
   stop reading, so a killed daemon loses the answer, not the work.
2. ``bitonal.finish_ready_scans()`` -- merge the shards of any scan
   whose conversion jobs are all done and move it out of ``AWAITING``.
   The dots.mocr stage has no equivalent yet: issue #190 ends when every
   shard answers, and the merge follows in #149.

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
        "scan whose conversion jobs have all finished."
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
                summary.completed,
                summary.retried,
                summary.failed,
                summary.errors,
                finished,
            )
        ):
            self.stdout.write(
                f"Completed {summary.completed}, retried {summary.retried}, "
                f"failed {summary.failed}, still waiting {summary.pending}, "
                f"check errors {summary.errors}; finished {finished} scan(s)"
            )
