"""Write the text of a volume's opinions again (issue #365).

The text is derived: the engines' units of the opinion documents
(#350), aligned, put in reading order and resolved by the vote. This
command runs that work here and now, for every opinion of the named
volumes whose OCR documents are written. It reads those documents, one
S3 read each, and writes the ``OpinionText`` rows, the findings and one
``ensemble.json``. No engine is asked to read again, and no page is
rendered.

**It waives the engine gate**, as the "Re run OCR ensemble" button
does. The daemon pass waits for ``OPINION_ENSEMBLE_MIN_ENGINES``
engine documents, which no volume has until the third engine reads, so
this command is how a two-engine corpus is read today.

Two reasons to run it: a change of the transform in ``ensemble`` after
a deploy, and a volume the daemon pass will not take.

A ``TEXT_REVIEW_DONE`` row is left alone: a human approved its text,
and nothing derived overwrites that. An ``ERROR`` row is read again and
comes back to life on success, because the command is the operator's
own decision and not a pass that would spin.

Examples:

    # Say what would be read, and change nothing.
    docker exec scanning-daemon python manage.py \\
        rerun_opinion_ensemble 2845 --dry-run

    # Write the text of two volumes again.
    docker exec scanning-daemon python manage.py \\
        rerun_opinion_ensemble 2845 2702
"""

from django.core.management.base import BaseCommand, CommandError

from scanning import ensemble, opinion_ocr
from scanning.models import Opinion, OpinionReviewStatus, Scan


class Command(BaseCommand):
    help = (
        "Read the OCR documents of every unapproved opinion of the named "
        "volumes and write its text, its warnings and its ensemble "
        "document again."
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
            help="Scan numbers whose opinions are read again.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would be read, and change nothing.",
        )

    def handle(self, *args, **options):
        """Read every named scan's opinions, or report them.

        :param args: Unused positional arguments.
        :param options: Parsed CLI options.
        :return: None.
        :raises CommandError: If a named scan does not exist.
        """
        dry_run = options["dry_run"]
        written = failed = 0
        for pk in options["scan_pks"]:
            scan = Scan.objects.filter(pk=pk).first()
            if scan is None:
                raise CommandError(f"scan {pk} does not exist")
            rows = [
                opinion
                for opinion in Opinion.objects.filter(scan=scan)
                .exclude(status=OpinionReviewStatus.TEXT_REVIEW_DONE)
                .select_related("scan", "apply_run")
                .order_by("first_printed_page", "index_in_page")
                if opinion_ocr.is_written(opinion)
            ]
            if dry_run:
                self.stdout.write(
                    f"scan {pk}: would read {len(rows)} opinion(s)"
                )
                written += len(rows)
                continue
            for opinion in rows:
                try:
                    document = ensemble.rerun(opinion)
                except ensemble.TransientFault as exc:
                    # The bucket, not the row: nothing is counted.
                    failed += 1
                    self.stderr.write(f"{opinion}: {exc}")
                    continue
                except ensemble.EnsembleError as exc:
                    ensemble.record_failure(opinion, str(exc))
                    failed += 1
                    self.stderr.write(f"{opinion}: {exc}")
                    continue
                written += 1
                self.stdout.write(
                    f"{opinion}: {document['counts']['groups']} block(s), "
                    f"{document['counts']['low_confidence']} word(s) with "
                    "no majority"
                )
        self.stdout.write(
            f"{'Would read' if dry_run else 'Wrote'} {written} opinion(s), "
            f"{failed} failed"
        )
