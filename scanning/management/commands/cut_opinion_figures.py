"""Cut the pictures of one opinion's text from the original (#463).

A task of ``run_daemon``, before the PDF pass. The unit of the work is
the ``Opinion`` row: the tick finds one row whose pictures are not all
cut (``opinion_figures.owed``), cuts each picture of its ensemble text
out of the original shards, stores it and stamps the row. No status
moves but ``ERROR`` at the cap. See :mod:`scanning.opinion_figures`.

Examples:

    # Cut the pictures of one opinion and exit (local debugging).
    docker exec scanning-daemon python manage.py cut_opinion_figures

    # Also runs automatically from run_daemon every
    # DAEMON_OPINION_FIGURE_INTERVAL seconds (default 10s).
"""

import logging
import time

from django.core.management.base import BaseCommand

logger = logging.getLogger(__name__)

# Same shape as the other ticks: every tick opens a fresh TCP+TLS
# connection, so the odd transient connect failure is expected.
MAX_DB_RETRIES = 3
RETRY_BACKOFF_SECONDS = 0.5


class Command(BaseCommand):
    help = (
        "Cut the pictures of one opinion's text out of the original scan, "
        "store them and stamp the row."
    )

    def handle(self, *args, **options):
        """Run one tick.

        :param args: Positional arguments from the management command.
        :param options: Parsed command-line options.
        :return: None.
        """
        from django.db import OperationalError, connections

        from scanning import opinion_figures

        for attempt in range(MAX_DB_RETRIES):
            connections.close_all()
            try:
                done = opinion_figures.run_tick()
                break
            except OperationalError as exc:
                if attempt == MAX_DB_RETRIES - 1:
                    # WARNING, not ERROR: a self-healing blip should
                    # not create a Sentry event (issue #116).
                    logger.warning(
                        "DB connection failed during the opinion figure "
                        "tick after %d attempts: %s",
                        attempt + 1,
                        exc,
                    )
                    return
                time.sleep(RETRY_BACKOFF_SECONDS * (attempt + 1))
        else:
            return

        if done:
            self.stdout.write(f"Cut the pictures of {done} opinion(s)")
