"""Take approved volumes back to the redaction review (#338).

A volume whose redaction review is approved (``REDACTION_REVIEW_DONE``)
is never measured again: ``yolo.queue_ready_runs`` takes review 2 and
the approval before it, and the way back from a later status is the
admin re-queue, which runs the whole pipeline. The re-read of #338
gives such a volume a new detection run with classes its rows lack, so
this command is the narrow way back: one compare-and-swap to
``READY_FOR_REDACTION_REVIEW``. The collect tick then imports the new
detections and measures again, the human rows are carried by their
address as after any second run, and a person approves the review
again, which updates the ``Opinion`` rows in place
(``opinions.create_rows``).

A volume with an approved opinion text is refused: its text review is
a person's work over the text of those opinions, and reopening the
review above it is a decision for that person, not a command.

Examples:

    # Say what would be reopened, and change nothing.
    docker exec scanning-daemon python manage.py reopen_redaction_review \\
        2561 2599 --dry-run

    docker exec scanning-daemon python manage.py reopen_redaction_review \\
        2561 2599
"""

import logging

from django.core.management.base import BaseCommand

from scanning.models import OpinionReviewStatus, Scan, Status

logger = logging.getLogger(__name__)

#: What the scan says while review 2 waits for the new measure.
REOPENED_MESSAGE = (
    "The redaction review is open again: the volume was detected again "
    "and its redactions are measured once more before the review."
)


class Command(BaseCommand):
    help = (
        "Take named volumes from an approved redaction review back to "
        "the redaction review, so a new detection run is imported."
    )

    def add_arguments(self, parser):
        """Register the CLI arguments.

        :param parser: The argparse parser to configure.
        :return: None.
        """
        parser.add_argument(
            "scan_pks", nargs="+", type=int, help="Scan numbers to reopen."
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would be reopened, and change nothing.",
        )

    def handle(self, *args, **options):
        """Reopen every named scan the rule allows.

        :param args: Unused positional arguments.
        :param options: Parsed CLI options.
        :return: None.
        """
        dry_run = options["dry_run"]
        wanted = options["scan_pks"]
        scans = {s.pk: s for s in Scan.objects.filter(pk__in=wanted)}

        reopened = refused = 0
        for pk in sorted(set(wanted)):
            scan = scans.get(pk)
            if scan is None:
                refused += 1
                self.stderr.write(f"scan {pk}: no such scan")
                continue
            if scan.status != Status.REDACTION_REVIEW_DONE:
                refused += 1
                self.stderr.write(
                    f"scan {pk}: is {scan.status}, not an approved "
                    "redaction review"
                )
                continue
            approved = scan.opinions.filter(
                status=OpinionReviewStatus.TEXT_REVIEW_DONE
            ).count()
            if approved:
                refused += 1
                self.stderr.write(
                    f"scan {pk}: {approved} opinion text(s) approved; "
                    "not reopened"
                )
                continue
            if dry_run:
                reopened += 1
                self.stdout.write(f"scan {pk}: would reopen")
                continue
            moved = Scan.objects.filter(
                pk=pk, status=Status.REDACTION_REVIEW_DONE
            ).update(
                status=Status.READY_FOR_REDACTION_REVIEW,
                progress_message=REOPENED_MESSAGE,
            )
            if not moved:
                refused += 1
                self.stderr.write(f"scan {pk}: moved by another writer")
                continue
            reopened += 1
            logger.info(
                "reopen_redaction_review: scan=%s reopened by the command",
                pk,
            )
            self.stdout.write(f"scan {pk}: reopened")

        self.stdout.write(
            self.style.SUCCESS(
                f"{reopened} reopened, {refused} refused"
                f"{' (dry run)' if dry_run else ''}"
            )
        )
