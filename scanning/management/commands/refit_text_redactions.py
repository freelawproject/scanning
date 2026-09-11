"""Fit the standing text redaction boxes to the read text (issue #279).

The compute fits every text box to the dots.mocr cells under it
(``text_fit``), but only from this deploy on. The volumes already in
review 2 carry boxes blackletter measured from the fallback column
split, so each one overruns its text column. Nothing recomputes them:
``REPAIR_ON_REQUEST_ENABLED`` is off, and the admin re-queue runs the
whole pipeline again. This command fits them instead.

It reads the database and one JSON object per volume. It renders
nothing, it pulls no PDF and it spends no GPU time. It writes no scan
status.

A computed row that a standing dismiss points at is left alone, so
every decision keeps its box: ``redactions.resolve`` lands a dismiss by
an IoU of at least 0.5 against a copy of the box, and a box that loses
half its width falls under that. A dismissed box is not painted either,
so a narrower one is worth nothing.

The findings of review 2 are derived from the rows, so the command
rebuilds them for every volume it changed (``findings.rebuild``).

Examples:

    docker exec scanning-daemon python manage.py refit_text_redactions \\
        --dry-run
    docker exec scanning-daemon python manage.py refit_text_redactions
    docker exec scanning-daemon python manage.py refit_text_redactions 1802
"""

from statistics import median

from django.core.management.base import BaseCommand
from django.db import transaction

from scanning import detections, findings, redactions, text_fit
from scanning.models import Scan, Status

#: The statuses whose volumes are fitted by default: the two the
#: redaction review is open in. ``--all`` adds the closed one, whose
#: boxes a person already judged.
DEFAULT_STATUSES = (
    Status.PAGE_COMPLETENESS_REVIEW_DONE,
    Status.READY_FOR_REDACTION_REVIEW,
)


class Command(BaseCommand):
    help = (
        "Fit the standing text redaction boxes of every volume in the "
        "redaction review to the text the reader found under them."
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
            help="Scan numbers to fit; every eligible scan when the "
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
            help="Report what the fit would do and write nothing.",
        )

    def handle(self, *args, **options):
        """Fit the boxes of every selected volume.

        :param args: Unused.
        :param options: The parsed arguments.
        :return: None.
        """
        dry_run = options["dry_run"]
        scans = self._scans(options)
        if not scans:
            self.stdout.write("No volume to fit.")
            return

        total = text_fit.FitCounts()
        changed = 0
        for scan in scans:
            counts = self._fit(scan, dry_run)
            if counts is None:
                continue
            total.add(counts)
            if counts.fitted:
                changed += 1
        self._report(total, changed, len(scans), dry_run)

    def _scans(self, options) -> list[Scan]:
        """Return the volumes to fit, in order.

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

    def _fit(self, scan: Scan, dry_run: bool):
        """Fit one volume, and say what it did.

        :param scan: The scan.
        :param dry_run: Whether to roll the writes back.
        :returns: The counts, or None when the volume has no cells.
        :rtype: text_fit.FitCounts | None
        """
        run = detections.measured_run(scan)
        cells = text_fit.load_cells(scan, run)
        if not cells:
            self.stdout.write(
                self.style.WARNING(
                    f"scan {scan.pk}: no OCR volume to read; left alone"
                )
            )
            return None
        # One transaction per volume, so a dry run rolls the fit back
        # after it has counted, and a fault on one volume leaves that
        # volume whole.
        with transaction.atomic():
            counts = text_fit.fit_rows(scan, cells)
            if counts.fitted:
                # The rows moved, so the decisions must land again and
                # the findings are derived from what stands now.
                redactions.resolve(scan)
                findings.rebuild(scan, run=run)
            if dry_run:
                transaction.set_rollback(True)
        self.stdout.write(
            f"scan {scan.pk}: {counts.read} text box(es), "
            f"{counts.fitted} fitted, {counts.unreached} reached by no "
            f"cell, {counts.refused} refused"
        )
        return counts

    def _report(
        self,
        total: text_fit.FitCounts,
        changed: int,
        scans: int,
        dry_run: bool,
    ) -> None:
        """Write the corpus report.

        :param total: The counts of every volume.
        :param changed: How many volumes had a box fitted.
        :param scans: How many volumes were read.
        :param dry_run: Whether anything was written.
        :return: None.
        """
        self.stdout.write("")
        self.stdout.write(
            f"{scans} volume(s) read, {changed} with a fitted box."
        )
        self.stdout.write(
            f"{total.read} text box(es): {total.fitted} fitted, "
            f"{total.unreached} reached by no cell, {total.refused} "
            f"refused by a guard."
        )
        if total.removed:
            self.stdout.write(
                f"Width removed, in points: median "
                f"{median(total.removed):.1f}, maximum "
                f"{max(total.removed):.1f}."
            )
        if dry_run:
            self.stdout.write(self.style.WARNING("Dry run: nothing written."))
