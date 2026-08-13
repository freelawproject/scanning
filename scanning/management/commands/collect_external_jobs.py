"""Sweep every in-flight external job once (the collect half of a tick).

Asks each open job's provider what happened and writes the answer to
the row: still running, finished, failed, or past its deadline and
cancelled. Applies no results and touches no scan -- a completed job
is left at ``COMPLETED`` with its payload on S3, for the advance phase
to consume.

Runs automatically from ``run_daemon`` every
``DAEMON_COLLECT_INTERVAL`` seconds (default 15s).

Examples:

    # Sweep once and print what moved.
    docker exec scanning-daemon python manage.py collect_external_jobs
"""

import logging
import time

from django.core.management.base import BaseCommand
from django.db import OperationalError, connections

logger = logging.getLogger(__name__)

# The daemon opens a fresh TCP+TLS connection each tick, so an
# occasional transient connect failure is expected. Mirrors
# ``process_next_scan``; see issue #116.
MAX_DB_RETRIES = 3
RETRY_BACKOFF_SECONDS = 0.5


class Command(BaseCommand):
    help = (
        "Poll every in-flight external job, record what its provider "
        "reported, and cancel any that ran past their deadline."
    )

    def handle(self, *args, **options):
        """Run one collect sweep.

        :param args: Positional arguments from the management command.
        :param options: Parsed command-line options.
        :return: None.
        """
        from scanning.jobs import collect_once

        for attempt in range(MAX_DB_RETRIES):
            connections.close_all()
            try:
                summary = collect_once()
                break
            except OperationalError as exc:
                if attempt == MAX_DB_RETRIES - 1:
                    # WARNING, not ERROR: a tick that never ran is
                    # picked up whole by the next one, so a self-healing
                    # blip should not raise a Sentry event.
                    logger.warning(
                        "DB connection failed during collect after %d "
                        "attempts: %s",
                        attempt + 1,
                        exc,
                    )
                    return
                time.sleep(RETRY_BACKOFF_SECONDS * (attempt + 1))
        else:
            return

        if summary.polled or summary.errors:
            self.stdout.write(f"Collected: {summary}")
