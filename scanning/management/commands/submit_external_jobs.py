"""Submit one wave of pending external jobs per provider (a daemon tick).

Each provider counts its own in-flight rows against its own cap, so one
saturated endpoint cannot starve another: doctor's ceiling is its replica
count (``DOCTOR_MAX_CONCURRENCY``), and each RunPod engine's is that
engine's own serverless endpoint (``DOTS_MOCR_MAX_CONCURRENCY``).

The doctor wave blocks for as long as its slowest request (~25-45s for a
100-page bitonal shard), deliberately: holding the request open is what
keeps the provider's error code, instead of inferring failure from a
missing object later. The RunPod wave does not -- ``POST /run`` returns
as soon as the job is queued, and a poll on a later tick is what
finishes it.

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
        "Submit one wave of pending external jobs per provider, each "
        "within its own concurrency cap, and record their outcomes."
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
                "Concurrency override applied to EACH provider's wave. "
                "Defaults to each provider's own setting."
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
                summary.deferred,
                summary.unanswered,
                summary.skipped,
            )
        ):
            self.stdout.write(
                f"Submitted {summary.submitted}, retried {summary.retried}, "
                f"deferred {summary.deferred}, failed {summary.failed}, "
                f"unanswered {summary.unanswered}, skipped {summary.skipped}"
            )
