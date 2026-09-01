"""Read the page numbers of a glued dots.mocr run again (issue #228).

The apply pass runs once per OCR run. ``applied_at``, in
``provider_meta["apply"]`` on the run's first row, is what stops it
from looping every collect tick. So a change to how the adapter reads
a page number reaches only the volumes read after the deploy.

This command hands the volumes already read back to that pass. It
clears the stamp; the next collect tick re-reads the *stored* glued
document, rebuilds ``Scan.ocr_results`` and the Issues, and leaves the
status where it is. It starts no GPU job and it costs no RunPod time.

Two properties make it safe. The numbers a curator typed survive: they
are ``PageEdit`` rows since #214, and the apply writes them over the
machine output every time. An approved volume is not touched: this
command takes only the three statuses ``apply_ready_runs`` reads, and
``PAGE_COMPLETENESS_REVIEW_DONE`` is not one of them.

Run it once, after the deploy that changes the reading.

Examples:

    # Hand back every volume still in review 1.
    docker exec scanning-daemon python manage.py reapply_page_numbers

    # Say what it would do, and change nothing.
    docker exec scanning-daemon python manage.py reapply_page_numbers \\
        --dry-run

    # Two named volumes only.
    docker exec scanning-daemon python manage.py reapply_page_numbers \\
        1802 1874
"""

from django.core.management.base import BaseCommand

from scanning import dots_mocr
from scanning.models import (
    ExternalJob,
    JobEngine,
    JobProvider,
    JobStage,
    JobStatus,
    Scan,
    Status,
)

#: The statuses ``dots_mocr.apply_ready_runs`` acts on. A scan outside
#: them would hold a cleared stamp and nothing would read it.
ELIGIBLE_STATUSES = (
    Status.AWAITING_VALIDATION,
    Status.PENDING_REVIEW,
    Status.READY_FOR_PAGE_COMPLETENESS_REVIEW,
)


class Command(BaseCommand):
    help = (
        "Hand every glued dots.mocr run in review 1 back to the apply "
        "pass, so the page numbers are read again from the stored "
        "document."
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
            help="Scan numbers to hand back; every eligible scan when "
            "the list is empty.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would be handed back, and change nothing.",
        )

    def handle(self, *args, **options):
        """Clear the apply stamp of every eligible scan.

        :param args: Unused positional arguments.
        :param options: Parsed CLI options.
        :return: None.
        """
        dry_run = options["dry_run"]
        wanted = options["scan_pks"]

        glued = (
            ExternalJob.objects.filter(
                stage=JobStage.ANALYZE,
                engine=JobEngine.DOTS_MOCR,
                provider=JobProvider.RUNPOD,
                status=JobStatus.CONSUMED,
            )
            .values_list("scan_id", flat=True)
            .distinct()
        )
        scans = Scan.objects.filter(
            pk__in=list(glued), status__in=ELIGIBLE_STATUSES
        ).order_by("pk")
        if wanted:
            scans = scans.filter(pk__in=wanted)
        scans = list(scans)

        reopened = skipped = 0
        for scan in scans:
            if dots_mocr.reopen_apply(scan, dry_run=dry_run):
                reopened += 1
                verb = "would be handed back" if dry_run else "handed back"
                self.stdout.write(f"scan {scan.pk}: {verb}")
            else:
                skipped += 1

        for pk in sorted(set(wanted) - {scan.pk for scan in scans}):
            self.stderr.write(
                f"scan {pk}: no glued run in review 1; nothing to do"
            )

        self.stdout.write(
            self.style.SUCCESS(
                f"{reopened} scan(s) to read again, {skipped} skipped"
                f"{' (dry run)' if dry_run else ''}"
            )
        )
