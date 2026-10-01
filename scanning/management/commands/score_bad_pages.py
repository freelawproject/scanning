"""Score volumes with the bad-page model and write their review-1 cards (#436).

With no argument, one tick of the daemon pass: the newest scan past
the bitonal merge that carries no score is pulled, scored and stamped
(:func:`scanning.badpage.scoring.run_tick`). ``run_daemon`` calls it
this way every ``DAEMON_BADPAGE_INTERVAL`` seconds, which is how a
new upload gets its score and how the existing corpus is backfilled,
newest first. No scan status moves.

With a scan pk, that scan is scored now, whatever it holds: the way
to rescore a volume after a retrain, or one the pass gave up on.

    # One tick, as the daemon runs it
    python manage.py score_bad_pages

    # One volume, now
    python manage.py score_bad_pages 2845

    # One volume, from a bitonal copy already on disk
    python manage.py score_bad_pages 2845 --pdf ~/scans/scan-2845-draft.pdf
"""

import logging
import time
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from scanning import apply
from scanning.badpage import scoring
from scanning.models import Scan

logger = logging.getLogger(__name__)

# Same shape as the other ticks: every tick opens a fresh TCP+TLS
# connection, so the odd transient connect failure is expected.
MAX_DB_RETRIES = 3
RETRY_BACKOFF_SECONDS = 0.5


class Command(BaseCommand):
    help = (
        "Score the newest unscored volume with the bad-page model (the"
        " daemon tick), or the one scan given."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "scan_pk",
            type=int,
            nargs="?",
            help="A scan to score now; without it, one tick of the pass.",
        )
        parser.add_argument(
            "--pdf",
            help="With a scan: score this bitonal copy instead of its own.",
        )

    def handle(self, *args, **options):
        if options["scan_pk"] is None:
            if options["pdf"]:
                raise CommandError("--pdf needs a scan.")
            self._tick()
            return
        self._one(options["scan_pk"], options["pdf"])

    def _tick(self) -> None:
        """Run one tick of the pass, retrying a dropped DB connection."""
        from django.db import OperationalError, connections

        for attempt in range(MAX_DB_RETRIES):
            connections.close_all()
            try:
                scored = scoring.run_tick()
                break
            except OperationalError as exc:
                if attempt == MAX_DB_RETRIES - 1:
                    # WARNING, not ERROR: a self-healing blip should
                    # not create a Sentry event (issue #116).
                    logger.warning(
                        "DB connection failed during the bad-page tick "
                        "after %d attempts: %s",
                        attempt + 1,
                        exc,
                    )
                    return
                time.sleep(RETRY_BACKOFF_SECONDS * (attempt + 1))
        else:
            return
        if scored:
            self.stdout.write(f"Scored {scored} volume(s)")

    def _one(self, scan_pk: int, pdf_option: str | None) -> None:
        """Score one scan now, from its bitonal copy or the file given."""
        scan = Scan.objects.filter(pk=scan_pk).first()
        if scan is None:
            raise CommandError(f"No scan {scan_pk}.")
        if pdf_option:
            pdf = Path(pdf_option).expanduser()
            if not pdf.is_file():
                raise CommandError(f"No PDF at {pdf}.")
        else:
            try:
                pdf, _pulled = scoring.bitonal_copy(scan)
            except apply.ApplyError as exc:
                raise CommandError(str(exc)) from exc
        self.stdout.write(f"Scoring {pdf} ...")
        try:
            scoring.score_scan(scan, pdf)
        except ValueError as exc:
            raise CommandError(str(exc)) from exc
        flagged = scoring.flagged(scan)
        for page, score in flagged:
            self.stdout.write(f"  page {page}: {score:.2f}")
        self.stdout.write(
            self.style.SUCCESS(
                f"{len(flagged)} page(s) at or above {scoring.THRESHOLD:g};"
                f" scores stamped on scan {scan.pk}."
            )
        )
