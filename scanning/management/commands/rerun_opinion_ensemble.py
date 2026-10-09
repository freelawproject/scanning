"""Write the text of a volume's opinions again (issue #365).

The text is derived: the engines' units of the opinion documents
(#350), aligned, put in reading order and resolved by the vote. This
command runs that work here and now, for every opinion of the named
volumes whose OCR documents are written. It reads those documents, one
S3 read each, and writes the ``OpinionText`` rows, the findings and one
``ensemble.json``. No engine is asked to read again, and no page is
rendered.

**It waives the engine gate**, as the "Re run OCR ensemble" endpoint
does. The daemon pass waits for ``OPINION_ENSEMBLE_MIN_ENGINES``
engine documents, which a volume read by two engines never holds, so
this command is how such a volume is read.

Two reasons to run it: a change of the transform in ``ensemble`` after
a deploy, and a volume the daemon pass will not take. After a change
of the document itself, such as the risk levels of #419, run it over
every volume: ``--all``. That set holds the opinions read by
:data:`ALL_MIN_ENGINES` engines or more: every group of a one-engine
document is ``SINGLE`` and holds every engine of the document, so it
gets no level and no card, and its text review would open with no
warning. A named scan keeps the waiver of every engine count.

A ``TEXT_REVIEW_DONE`` row is left alone: a human approved its text,
and nothing derived overwrites that. An ``ERROR`` row is read again and
comes back to life on success, because the command is the operator's
own decision and not a pass that would spin; a row another work ended
keeps that work's reason, which this one never writes over.

``--opinion`` names opinions instead of volumes (#465), by the pk of
their review page (``/opinions/<pk>/review/``): to check a change of
the transform on a few chosen opinions before a volume, or to read a
list a reviewer reported in one call. Every named opinion is checked
before any is read: one that does not exist, one a person approved, or
one whose OCR documents are not written at its live revision stops the
call, because an opinion named on purpose is never passed over without
a word. A named opinion keeps the waiver of every engine count, as a
named scan does.

Examples:

    # Say what would be read, and change nothing.
    docker exec scanning-daemon python manage.py \\
        rerun_opinion_ensemble 2845 --dry-run

    # Write the text of two volumes again.
    docker exec scanning-daemon python manage.py \\
        rerun_opinion_ensemble 2845 2702

    # Write the text of every volume again.
    docker exec scanning-daemon python manage.py \\
        rerun_opinion_ensemble --all --dry-run

    # Write the text of two opinions again, by the pk of their review page.
    docker exec scanning-daemon python manage.py \\
        rerun_opinion_ensemble --opinion 1234 1235 --dry-run
"""

from django.core.management.base import BaseCommand, CommandError

from scanning import ensemble, opinion_ocr
from scanning.models import Opinion, OpinionReviewStatus, Scan

#: The least ``ocr_engine_count`` of an opinion ``--all`` reads (#419).
ALL_MIN_ENGINES = 2


def readable(least: int):
    """Return the filter of the opinions a run reads.

    **The one rule** of the command (#465): an opinion a person has not
    approved, read by ``least`` engines or more. A volume run applies it
    as a filter and a named opinion as a refusal, with
    :func:`opinion_ocr.is_written`, which is read off the row and is no
    filter of the database.

    :param least: The least ``ocr_engine_count``; 0 waives the gate.
    :returns: The queryset of every such opinion, of every scan.
    :rtype: QuerySet
    """
    return (
        Opinion.objects.filter(ocr_engine_count__gte=least)
        .exclude(status=OpinionReviewStatus.TEXT_REVIEW_DONE)
        .select_related("scan", "apply_run")
    )


class Command(BaseCommand):
    help = (
        "Read the OCR documents of every unapproved opinion of the named "
        "volumes, of every volume with --all, or of the opinions named "
        "with --opinion, and write its text, its warnings and its "
        "ensemble document again."
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
            help="Scan numbers whose opinions are read again.",
        )
        parser.add_argument(
            "--all",
            action="store_true",
            help="Read the opinions of every volume that has one again.",
        )
        parser.add_argument(
            "--opinion",
            nargs="+",
            action="extend",
            type=int,
            default=[],
            metavar="PK",
            help=(
                "Opinions read again, by the pk of their review page. Takes "
                "one or more, and can be repeated."
            ),
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would be read, and change nothing.",
        )

    def handle(self, *args, **options):
        """Read every named scan's or opinion's text, or report it.

        :param args: Unused positional arguments.
        :param options: Parsed CLI options.
        :return: None.
        :raises CommandError: If the call names no selector or more than
            one, if a named scan or opinion does not exist, or if a named
            opinion is approved or not glued.
        """
        dry_run = options["dry_run"]
        pks = options["scan_pks"]
        # The order of the call, once each.
        opinion_pks = list(dict.fromkeys(options["opinion"]))
        if opinion_pks and (pks or options["all"]):
            raise CommandError(
                "pass --opinion alone, without scans and without --all"
            )
        if opinion_pks:
            self._opinions(opinion_pks, dry_run)
            return
        if options["all"] and pks:
            raise CommandError("name the scans or pass --all, not both")
        if not options["all"] and not pks:
            raise CommandError("name the scans, or pass --all or --opinion")
        least = ALL_MIN_ENGINES if options["all"] else 0
        if options["all"]:
            pks = list(
                Opinion.objects.filter(ocr_engine_count__gte=least)
                .order_by("scan_id")
                .values_list("scan_id", flat=True)
                .distinct()
            )
        written = failed = moved = 0
        for pk in pks:
            scan = Scan.objects.filter(pk=pk).first()
            if scan is None:
                raise CommandError(f"scan {pk} does not exist")
            rows = [
                opinion
                for opinion in readable(least)
                .filter(scan=scan)
                .order_by("first_printed_page", "index_in_page")
                if opinion_ocr.is_written(opinion)
            ]
            if dry_run:
                engines = sorted({row.ocr_engine_count for row in rows})
                self.stdout.write(
                    f"scan {pk}: would read {len(rows)} opinion(s), "
                    f"engines {', '.join(map(str, engines)) or 'none'}"
                )
                written += len(rows)
                continue
            for opinion in rows:
                outcome = self._read(opinion)
                written += outcome == WRITTEN
                failed += outcome == FAILED
                moved += outcome == MOVED
        self._total(dry_run, written, failed, moved)

    def _opinions(self, pks: list[int], dry_run: bool) -> None:
        """Read the named opinions, or report them (#465).

        Every opinion is checked before any is read, so a refusal leaves
        every row as it was. The engine gate is waived.

        :param pks: The opinion pks, in the order of the call.
        :param dry_run: Report and change nothing.
        :return: None.
        :raises CommandError: If an opinion does not exist, is approved,
            or has no OCR documents at its live revision.
        """
        found = {
            row.pk: row
            for row in Opinion.objects.filter(pk__in=pks).select_related(
                "scan", "apply_run"
            )
        }
        missing = [pk for pk in pks if pk not in found]
        if missing:
            raise CommandError(
                f"opinion(s) {_listed(missing)} do not exist; nothing was read"
            )
        allowed = set(
            readable(0).filter(pk__in=pks).values_list("pk", flat=True)
        )
        approved = [pk for pk in pks if pk not in allowed]
        if approved:
            raise CommandError(
                f"opinion(s) {_listed(approved)} are approved and keep their "
                "text; nothing was read"
            )
        unglued = [pk for pk in pks if not opinion_ocr.is_written(found[pk])]
        if unglued:
            raise CommandError(
                f"opinion(s) {_listed(unglued)} have no OCR documents at "
                "their live revision; nothing was read"
            )
        rows = [found[pk] for pk in pks]
        if dry_run:
            for row in rows:
                self.stdout.write(
                    f"{_named(row)}: would read, "
                    f"{row.ocr_engine_count} engine(s)"
                )
            self._total(dry_run, len(rows), 0, 0)
            return
        written = failed = moved = 0
        for row in rows:
            outcome = self._read(row, _named(row))
            written += outcome == WRITTEN
            failed += outcome == FAILED
            moved += outcome == MOVED
        self._total(dry_run, written, failed, moved)

    def _read(self, opinion: Opinion, name: str | None = None) -> str:
        """Read one opinion's documents and write its text again.

        **The one read of a row**, for a volume and for a named opinion.
        A glue that wrote again during the read (``RevisionMoved``) wrote
        nothing and is not a failure: the row is due at the new revision,
        the answer of the button. A fault of the bucket counts nothing on
        the row; a fault of the row is recorded on it.

        :param opinion: The row.
        :param name: The head of its line, or the row's own name.
        :returns: :data:`WRITTEN`, :data:`FAILED` or :data:`MOVED`.
        :rtype: str
        """
        name = name or str(opinion)
        try:
            document = ensemble.rerun(opinion)
        except ensemble.RevisionMoved:
            self.stdout.write(
                f"{name}: the OCR glue wrote again during the read; "
                "nothing written"
            )
            return MOVED
        except ensemble.TransientFault as exc:
            # The bucket, not the row: nothing is counted.
            self.stderr.write(f"{name}: {exc}")
            return FAILED
        except ensemble.EnsembleError as exc:
            ensemble.record_failure(opinion, str(exc))
            self.stderr.write(f"{name}: {exc}")
            return FAILED
        self.stdout.write(
            f"{name}: {document['counts']['groups']} block(s), "
            f"{document['counts']['low_confidence']} word(s) with "
            "no majority"
        )
        return WRITTEN

    def _total(self, dry_run: bool, written: int, failed: int, moved: int):
        """Write the last line of the call."""
        line = (
            f"{'Would read' if dry_run else 'Wrote'} {written} opinion(s), "
            f"{failed} failed"
        )
        if moved:
            line += f", {moved} moved by the OCR glue"
        self.stdout.write(line)


#: What :meth:`Command._read` did with one row.
WRITTEN = "written"
FAILED = "failed"
MOVED = "moved"


def _listed(pks: list[int]) -> str:
    """Return the pks as one line."""
    return ", ".join(str(pk) for pk in pks)


def _named(row: Opinion) -> str:
    """Return the line head of one opinion: its pk, its key, its scan."""
    return f"opinion {row.pk} ({row}) of scan {row.scan_id}"
