"""Write the OCR documents of a volume's opinions again (issue #350).

The documents are derived: a cut of the corrected volume's engine
documents, with the redaction and boundary verdict of every unit. A
re-glue raises ``Opinion.glue_revision`` on every row of the scan that
a human has not approved, and the collect tick writes the rows again
under the new revision (``opinion_ocr.glue_due``). Nothing else moves:
no S3 read, no row created, no job started, no scan status.

Three reasons to run it: a new engine read arrived for a volume whose
opinions were glued without it, a threshold or a shape change in
``opinion_ocr``, and a Mistral re-glue that changed the boxes.

A ``TEXT_REVIEW_DONE`` row keeps its glues. An ``ERROR`` row gets the
new revision and a clean attempt count, but stays ``ERROR``: its way
back is the next approval, the rule of every terminal status.

Examples:

    # Say what would move, and change nothing.
    docker exec scanning-daemon python manage.py reglue_opinion_ocr \\
        2845 --dry-run

    # Glue the opinions of two volumes again.
    docker exec scanning-daemon python manage.py reglue_opinion_ocr \\
        2845 2702
"""

from django.core.management.base import BaseCommand, CommandError

from scanning import opinion_ocr
from scanning.models import Opinion, OpinionReviewStatus, Scan


class Command(BaseCommand):
    help = (
        "Raise the glue revision of every unapproved opinion of the named "
        "volumes, so the collect tick writes their OCR documents again."
    )

    def add_arguments(self, parser):
        """Register the CLI arguments.

        :param parser: The argparse parser to configure.
        :return: None.
        """
        parser.add_argument(
            "scan_pks",
            nargs="+",
            type=int,
            help="Scan numbers whose opinions are glued again.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would move, and change nothing.",
        )

    def handle(self, *args, **options):
        """Raise the revision, or report, for every named scan.

        :param args: Unused positional arguments.
        :param options: Parsed CLI options.
        :return: None.
        :raises CommandError: If a named scan does not exist.
        """
        dry_run = options["dry_run"]
        moved = 0
        for pk in options["scan_pks"]:
            scan = Scan.objects.filter(pk=pk).first()
            if scan is None:
                raise CommandError(f"scan {pk} does not exist")
            count = (
                Opinion.objects.filter(scan=scan)
                .exclude(status=OpinionReviewStatus.TEXT_REVIEW_DONE)
                .count()
            )
            if dry_run:
                self.stdout.write(
                    f"scan {pk}: would glue {count} opinion(s) again"
                )
                moved += count
                continue
            moved += opinion_ocr.reglue(scan)
            self.stdout.write(f"scan {pk}: {count} opinion(s) due again")
        self.stdout.write(
            f"{'Would move' if dry_run else 'Moved'} {moved} opinion(s)"
        )
