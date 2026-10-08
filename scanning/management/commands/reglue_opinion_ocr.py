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

``--opinion`` names opinions instead of volumes (#462), by the pk of
their review page (``/opinions/<pk>/review/``): to check a change on
one opinion before a volume, or to answer a report about one opinion
without rebuilding its neighbours. Every named opinion is checked
before any moves: one that does not exist, or one a person approved,
stops the call, because an opinion named on purpose is never skipped
without a word.

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

    # Glue two opinions again, by the pk of their review page.
    docker exec scanning-daemon python manage.py reglue_opinion_ocr \\
        --opinion 1234 1235 --dry-run
"""

from django.core.management.base import BaseCommand, CommandError

from scanning import opinion_ocr, opinion_pdf
from scanning.models import Opinion, OpinionReviewStatus, Scan


class Command(BaseCommand):
    help = (
        "Raise the glue revision of every unapproved opinion of the named "
        "volumes, of every volume with --all, or of the opinions named with "
        "--opinion, so the collect tick writes their OCR documents again."
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
            "--opinion",
            nargs="+",
            action="extend",
            type=int,
            default=[],
            metavar="PK",
            help=(
                "Opinions glued again, by the pk of their review page. Takes "
                "one or more, and can be repeated."
            ),
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
        """Raise the revision, or report, for every named scan or opinion.

        :param args: Unused positional arguments.
        :param options: Parsed CLI options.
        :return: None.
        :raises CommandError: If the call names no selector or more than
            one, if a named scan or opinion does not exist, or if a named
            opinion is approved.
        """
        dry_run = options["dry_run"]
        carry_pdf = not options["recut_pdf"]
        pks = options["scan_pks"]
        # The order of the call, once each.
        opinion_pks = list(dict.fromkeys(options["opinion"]))
        if sum((bool(pks), options["all"], bool(opinion_pks))) != 1:
            raise CommandError(
                "name the scans, pass --all, or pass --opinion: one of the "
                "three"
            )
        if opinion_pks:
            self._opinions(opinion_pks, dry_run, carry_pdf)
            return
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
                rows = list(opinion_ocr.reglue_rows(scan))
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
        self._total(dry_run, moved, carried)

    def _opinions(
        self, pks: list[int], dry_run: bool, carry_pdf: bool
    ) -> None:
        """Raise the revision, or report, for the named opinions (#462).

        Every opinion is checked before any moves, so a refusal leaves
        every row as it was.

        :param pks: The opinion pks, in the order of the call.
        :param dry_run: Report and change nothing.
        :param carry_pdf: Copy a written PDF to the new revision.
        :return: None.
        :raises CommandError: If an opinion does not exist or is approved.
        """
        found = {
            row.pk: row
            for row in Opinion.objects.filter(pk__in=pks).select_related(
                "scan", "scan__reporter"
            )
        }
        missing = [pk for pk in pks if pk not in found]
        if missing:
            raise CommandError(
                f"opinion(s) {_listed(missing)} do not exist; nothing moved"
            )
        approved = [
            pk
            for pk in pks
            if found[pk].status == OpinionReviewStatus.TEXT_REVIEW_DONE
        ]
        if approved:
            raise CommandError(
                f"opinion(s) {_listed(approved)} are approved and keep their "
                "glues; nothing moved"
            )
        rows = [found[pk] for pk in pks]
        if dry_run:
            carried = 0
            for row in rows:
                carry = carry_pdf and opinion_pdf.is_written(row)
                carried += int(carry)
                self.stdout.write(
                    f"{_named(row)}: would glue again, "
                    f"{'carry its PDF' if carry else 'its PDF is owed'}"
                )
            self._total(dry_run, len(rows), carried)
            return
        summary = opinion_ocr.reglue_opinions(rows, carry_pdf=carry_pdf)
        said = {
            opinion_ocr.REGLUE_MOVED: "due again, its PDF is owed",
            opinion_ocr.REGLUE_CARRIED: "due again, its PDF carried",
            opinion_ocr.REGLUE_LOST: (
                "taken by another writer (a revision raised or an "
                "approval), not moved"
            ),
        }
        for row in rows:
            self.stdout.write(
                f"{_named(row)}: {said[summary.outcomes[row.pk]]}"
            )
        self._total(dry_run, summary.moved, summary.carried)

    def _total(self, dry_run: bool, moved: int, carried: int) -> None:
        """Write the last line of the call."""
        self.stdout.write(
            f"{'Would move' if dry_run else 'Moved'} {moved} opinion(s), "
            f"{'would carry' if dry_run else 'carried'} {carried} PDF(s)"
        )


def _listed(pks: list[int]) -> str:
    """Return the pks as one line."""
    return ", ".join(str(pk) for pk in pks)


def _named(row: Opinion) -> str:
    """Return the line head of one opinion: its pk, its key, its scan."""
    return f"opinion {row.pk} ({row}) of scan {row.scan_id}"
