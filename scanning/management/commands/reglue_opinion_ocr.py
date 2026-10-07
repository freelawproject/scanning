"""Write the OCR documents of a volume's opinions again (issue #350).

The documents are derived: a cut of the corrected volume's engine
documents, with the redaction and boundary verdict of every unit. A
re-glue raises ``Opinion.glue_revision`` on every row of the scan that
a human has not approved, and the collect tick writes the rows again
under the new revision (``opinion_ocr.glue_due``). Nothing else moves
but the PDF copy below: no row created, no job started, no scan status.

Three reasons to run it: a new engine read arrived for a volume whose
opinions were glued without it, a threshold or a shape change in
``opinion_ocr``, and a Mistral re-glue that changed the boxes. A change
of the rule, such as the bracket deletion of #373, is a pass over every
volume: ``--all``.

The redacted PDF of an opinion is stamped with the same revision, but
an OCR change moves none of its inputs, so a written PDF is copied to
the new revision and not cut again (#452). A PDF that is not written
yet stays owed. ``--recut-pdf`` makes the PDF pass write every moved
PDF again, one per tick: use it after a change of the redaction logic,
such as a blackletter upgrade. Run ``--dry-run`` first and count.

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

    # Glue the opinions of every volume again, after a rule change.
    docker exec scanning-daemon python manage.py reglue_opinion_ocr \\
        --all --dry-run

    # Cut the redacted PDFs again too, after a blackletter upgrade.
    docker exec scanning-daemon python manage.py reglue_opinion_ocr \\
        2845 --recut-pdf
"""

from django.core.management.base import BaseCommand, CommandError

from scanning import opinion_ocr, opinion_pdf
from scanning.models import Opinion, OpinionReviewStatus, Scan


class Command(BaseCommand):
    help = (
        "Raise the glue revision of every unapproved opinion of the named "
        "volumes, or of every volume with --all, so the collect tick writes "
        "their OCR documents again."
    )

    def add_arguments(self, parser):
        """Register the CLI arguments.

        :param parser: The argparse parser to configure.
        :return: None.
        """
        parser.add_argument(
            "scan_pks",
            nargs="*",
            type=int,
            help="Scan numbers whose opinions are glued again.",
        )
        parser.add_argument(
            "--all",
            action="store_true",
            help="Glue the opinions of every volume that has one again.",
        )
        parser.add_argument(
            "--recut-pdf",
            action="store_true",
            help=(
                "Cut the redacted PDFs again instead of carrying them, "
                "after a change of the redaction logic."
            ),
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
        :raises CommandError: If a named scan does not exist, or if the
            call names scans and passes ``--all``, or does neither.
        """
        dry_run = options["dry_run"]
        carry_pdf = not options["recut_pdf"]
        pks = options["scan_pks"]
        if options["all"] == bool(pks):
            raise CommandError("name the scans or pass --all, not both")
        if options["all"]:
            pks = list(
                Opinion.objects.order_by("scan_id")
                .values_list("scan_id", flat=True)
                .distinct()
            )
        moved = carried = 0
        for pk in pks:
            scan = Scan.objects.filter(pk=pk).first()
            if scan is None:
                raise CommandError(f"scan {pk} does not exist")
            if dry_run:
                rows = list(
                    Opinion.objects.filter(scan=scan).exclude(
                        status=OpinionReviewStatus.TEXT_REVIEW_DONE
                    )
                )
                count = len(rows)
                pdfs = (
                    sum(opinion_pdf.is_written(row) for row in rows)
                    if carry_pdf
                    else 0
                )
                self.stdout.write(
                    f"scan {pk}: would glue {count} opinion(s) again, "
                    f"carry {pdfs} PDF(s)"
                )
            else:
                summary = opinion_ocr.reglue(scan, carry_pdf=carry_pdf)
                count, pdfs = summary.moved, summary.carried
                self.stdout.write(
                    f"scan {pk}: {count} opinion(s) due again, "
                    f"{pdfs} PDF(s) carried"
                )
            moved += count
            carried += pdfs
        self.stdout.write(
            f"{'Would move' if dry_run else 'Moved'} {moved} opinion(s), "
            f"{'would carry' if dry_run else 'carried'} {carried} PDF(s)"
        )
