"""Submit one wave of pending external jobs (a single daemon tick).

Claims up to ``DOCTOR_MAX_CONCURRENCY`` pending ``ExternalJob`` rows,
sends them from a bounded thread pool, and records what came back.
Blocks for as long as the slowest request in the wave (~25-45s for a
100-page bitonal shard), deliberately: holding the request open is what
keeps the provider's error code, instead of inferring failure from a
missing object later.

Examples:

    # Submit one wave and exit (useful for local debugging).
    docker exec scanning-daemon python manage.py submit_external_jobs

    # Also runs automatically from run_daemon every
    # DAEMON_SUBMIT_INTERVAL seconds (default 5s).
"""

import logging
import time

from django.core.management.base import BaseCommand

logger = logging.getLogger(__name__)

# Same shape as process_next_scan: every tick opens a fresh TCP+TLS
# connection, so the odd transient connect failure is expected.
MAX_DB_RETRIES = 3
RETRY_BACKOFF_SECONDS = 0.5


class Command(BaseCommand):
    help = (
        "Submit up to DOCTOR_MAX_CONCURRENCY pending external jobs and "
        "record their outcomes."
    )

    def add_arguments(self, parser):
        """Register the command's options.

        :param parser: The argument parser.
        :return: None.
        """
        parser.add_argument(
            "--limit",
            type=int,
            default=None,
            help=(
                "Maximum jobs to submit in this wave. Defaults to "
                "DOCTOR_MAX_CONCURRENCY."
            ),
        )

    def handle(self, *args, **options):
        """Run one submit tick.

        :param args: Positional arguments from the management command.
        :param options: Parsed command-line options.
        :return: None.
        """
        from django.db import OperationalError, connections

        from scanning import jobs

        for attempt in range(MAX_DB_RETRIES):
            connections.close_all()
            try:
                summary = jobs.submit_pending(limit=options["limit"])
                break
            except OperationalError as exc:
                if attempt == MAX_DB_RETRIES - 1:
                    # WARNING, not ERROR: a self-healing blip should
                    # not create a Sentry event (issue #116).
                    logger.warning(
                        "DB connection failed during submit tick after "
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
                summary.submitted,
                summary.failed,
                summary.retried,
                summary.unanswered,
                summary.skipped,
            )
        ):
            self.stdout.write(
                f"Submitted {summary.submitted}, retried {summary.retried}, "
                f"failed {summary.failed}, unanswered {summary.unanswered}, "
                f"skipped {summary.skipped}"
            )
