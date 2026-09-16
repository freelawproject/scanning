"""Write the bracket readings of the volumes already computed (issue #328).

The compute stores one ``BracketReading`` row per headnote bracket the
reader found (``brackets.write_rows``), but only from this deploy on.
The volumes already in review 2 have no readings, so the new finding
would never be raised for them. This command reads the glued OCR volume
of each one and writes the rows.

It reads the database and one JSON object per volume. It renders
nothing, it pulls no PDF and it spends no GPU time. It writes no scan
status.

The findings of review 2 are derived from the rows, so the command
rebuilds them for every volume it read (``findings.rebuild``).

Examples:

    docker exec scanning-daemon python manage.py stamp_bracket_readings \\
        --dry-run
    docker exec scanning-daemon python manage.py stamp_bracket_readings
    docker exec scanning-daemon python manage.py stamp_bracket_readings 1828
"""

from django.core.management.base import BaseCommand
from django.db import transaction

from scanning import brackets, detections, findings, text_fit
from scanning.models import CheckName, Issue, Scan, Status

#: The statuses whose volumes are read by default: the two the
#: redaction review is open in, the set ``refit_text_redactions`` uses.
#: ``--all`` adds the closed one.
DEFAULT_STATUSES = (
    Status.PAGE_COMPLETENESS_REVIEW_DONE,
    Status.READY_FOR_REDACTION_REVIEW,
)


class Command(BaseCommand):
    help = (
        "Write the headnote bracket readings of every volume in the "
        "redaction review, and rebuild its findings."
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
            help="Scan numbers to read; every eligible scan when the "
            "list is empty.",
        )
        parser.add_argument(
            "--all",
            action="store_true",
            help="Include the volumes whose redaction review is closed.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what it would write and write nothing.",
        )

    def handle(self, *args, **options):
        """Read every selected volume.

        :param args: Unused.
        :param options: The parsed arguments.
        :return: None.
        """
        dry_run = options["dry_run"]
        scans = self._scans(options)
        if not scans:
            self.stdout.write("No volume to read.")
            return

        readings = findings_written = volumes = 0
        for scan in scans:
            counts = self._stamp(scan, dry_run)
            if counts is None:
                continue
            volumes += 1
            readings += counts[0]
            findings_written += counts[1]
        self.stdout.write("")
        self.stdout.write(
            f"{volumes} volume(s) read of {len(scans)}: {readings} "
            f"reading(s), {findings_written} missed bracket(s)."
        )
        if dry_run:
            self.stdout.write(self.style.WARNING("Dry run: nothing written."))

    def _scans(self, options) -> list[Scan]:
        """Return the volumes to read, in order.

        :param options: The parsed arguments.
        :returns: The scans.
        :rtype: list[Scan]
        """
        statuses: list[str] = list(DEFAULT_STATUSES)
        if options["all"]:
            statuses.append(Status.REDACTION_REVIEW_DONE)
        queryset = Scan.objects.filter(status__in=statuses)
        if options["scan_pks"]:
            queryset = Scan.objects.filter(pk__in=options["scan_pks"])
        return list(queryset.order_by("pk"))

    def _stamp(self, scan: Scan, dry_run: bool):
        """Read one volume, and say what it found.

        :param scan: The scan.
        :param dry_run: Whether to roll the writes back.
        :returns: ``(readings, findings)``, or None when the volume has
            no OCR document.
        :rtype: tuple[int, int] | None
        """
        run = detections.measured_run(scan)
        document = text_fit.load_document(scan, run)
        if document is None:
            self.stdout.write(
                self.style.WARNING(
                    f"scan {scan.pk}: no OCR volume to read; left alone"
                )
            )
            return None
        # One transaction per volume, so a dry run rolls the writes
        # back after it has counted, and a fault on one volume leaves
        # that volume whole.
        with transaction.atomic():
            written = brackets.write_rows(scan, document, run)
            findings.rebuild(scan, run=run)
            missed = Issue.objects.filter(
                scan=scan,
                check_name=CheckName.MISSING_HEADNOTE_BRACKET,
            ).count()
            if dry_run:
                transaction.set_rollback(True)
        self.stdout.write(
            f"scan {scan.pk}: {written} reading(s), {missed} missed bracket(s)"
        )
        return written, missed
