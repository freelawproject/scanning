"""Write one redacted opinion PDF per tick (#336, part 3).

The fifth task of ``run_daemon``, last in its schedule. The unit of the
work is the ``Opinion`` row: the tick finds one row that owes its PDF
(``opinion_pdf.due``), cuts its pages out of the corrected volume's
bitonal copy, hands them to ``blackletter.api.generate`` with the
redaction rows and the pictures, uploads the file and stamps the row.
No scan status moves. See :mod:`scanning.opinion_pdf`.

One PDF per tick (``opinion_pdf.PDFS_PER_TICK``), the rule of the
Mistral wave: the tick blocks the serial scheduler for the whole write,
and ``process_next_scan`` and the two job waves wait for it. After the
pulls that is a second or two of re-encoding; the first tick of a
volume pulls the corrected bitonal copy, and the first tick that needs
a picture pulls a shard of about 200 MB, inside the same loop. The
interval (``DAEMON_OPINION_PDF_INTERVAL``) is the throughput knob.

Examples:

    # Write one PDF and exit (useful for local debugging).
    docker exec scanning-daemon python manage.py build_opinion_pdfs

    # Also runs automatically from run_daemon every
    # DAEMON_OPINION_PDF_INTERVAL seconds (default 5s).
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
        "Write the redacted PDF of one opinion that owes it: cut its pages "
        "out of the corrected volume, paint the redaction rows, stamp the "
        "pictures from the shards, upload the file and stamp the row."
    )

    def handle(self, *args, **options):
        """Run one tick.

        :param args: Positional arguments from the management command.
        :param options: Parsed command-line options.
        :return: None.
        """
        from django.db import OperationalError, connections

        from scanning import opinion_pdf

        for attempt in range(MAX_DB_RETRIES):
            connections.close_all()
            try:
                written = opinion_pdf.run_tick()
                break
            except OperationalError as exc:
                if attempt == MAX_DB_RETRIES - 1:
                    # WARNING, not ERROR: a self-healing blip should
                    # not create a Sentry event (issue #116).
                    logger.warning(
                        "DB connection failed during the opinion PDF tick "
                        "after %d attempts: %s",
                        attempt + 1,
                        exc,
                    )
                    return
                time.sleep(RETRY_BACKOFF_SECONDS * (attempt + 1))
        else:
            return

        if written:
            self.stdout.write(f"Wrote {written} opinion PDF(s)")
