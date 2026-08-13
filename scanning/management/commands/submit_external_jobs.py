"""Hand every pending external job to its provider (the submit half).

The batch submit. Each job is handed over and the loop moves on
without waiting, so ten queued volumes reach the endpoint in about the
time one used to take, and the endpoint's own worker cap decides how
many actually run at once. Capping the pool is what saves money;
submitting the batch is what saves wall clock (issue #156).

Runs automatically from ``run_daemon`` every
``DAEMON_SUBMIT_INTERVAL`` seconds (default 5s).

Examples:

    # Submit whatever is waiting and print what went out.
    docker exec scanning-daemon python manage.py submit_external_jobs

    # Send at most five, e.g. when feeling out a new endpoint.
    docker exec scanning-daemon python manage.py submit_external_jobs --limit 5
"""

import logging
import time

from django.core.management.base import BaseCommand
from django.db import OperationalError, connections

logger = logging.getLogger(__name__)

MAX_DB_RETRIES = 3
RETRY_BACKOFF_SECONDS = 0.5


class Command(BaseCommand):
    help = (
        "Submit every pending external job to its provider without "
        "waiting for any of them to finish."
    )

    def add_arguments(self, parser):
        """Register the command's options.

        :param parser: The argument parser to extend.
        :return: None.
        """
        parser.add_argument(
            "--limit",
            type=int,
            default=None,
            help=(
                "Most jobs to submit this run. Omit to send everything "
                "pending; the endpoint queues whatever it cannot start."
            ),
        )

    def handle(self, *args, **options):
        """Run one submit sweep.

        :param args: Positional arguments from the management command.
        :param options: Parsed command-line options.
        :return: None.
        """
        from scanning.jobs import submit_pending

        for attempt in range(MAX_DB_RETRIES):
            connections.close_all()
            try:
                summary = submit_pending(limit=options.get("limit"))
                break
            except OperationalError as exc:
                if attempt == MAX_DB_RETRIES - 1:
                    logger.warning(
                        "DB connection failed during submit after %d "
                        "attempts: %s",
                        attempt + 1,
                        exc,
                    )
                    return
                time.sleep(RETRY_BACKOFF_SECONDS * (attempt + 1))
        else:
            return

        if summary.submitted or summary.deferred or summary.failed:
            self.stdout.write(f"Submitted: {summary}")
