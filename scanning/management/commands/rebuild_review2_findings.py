"""Write the findings of review 2 for the volumes already in it (issue #240, PR D).

Every finding of review 2 is derived from the detection, boundary and
redaction rows (``findings.rebuild``), and the compute and the review-2
endpoints write them as they go. The volumes measured before this
shipped have rows and no findings, and no migration writes a derived
row. This command does, once, after the deploy. It reads the database
only: no GPU time, no S3 read, no status write.

By default it takes the two open statuses of review 2. ``--all`` adds
the closed one (``REDACTION_REVIEW_DONE``), whose findings are then a
report about a review a person already closed.

Examples:

    docker exec scanning-daemon python manage.py rebuild_review2_findings
    docker exec scanning-daemon python manage.py rebuild_review2_findings \\
        --dry-run
    docker exec scanning-daemon python manage.py rebuild_review2_findings \\
        1802 1874
"""

from django.core.management.base import BaseCommand

from scanning import findings
from scanning.models import REVIEW2_CHECKS, Scan, Status

#: The statuses whose volumes are given their findings by default.
DEFAULT_STATUSES = (
    Status.PAGE_COMPLETENESS_REVIEW_DONE,
    Status.READY_FOR_REDACTION_REVIEW,
)


class Command(BaseCommand):
    help = (
        "Write the review-2 findings of every volume in the redaction "
        "review from its rows."
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
            help="Scan numbers to rebuild; every eligible scan when the "
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
            help="Say what would be rebuilt, and change nothing.",
        )

    def handle(self, *args, **options):
        """Rebuild the findings of the selected scans.

        :param args: Unused positional arguments.
        :param options: The parsed CLI options.
        :return: None.
        """
        statuses: list[str] = list(DEFAULT_STATUSES)
        if options["all"]:
            statuses.append(Status.REDACTION_REVIEW_DONE)
        scans = Scan.objects.filter(status__in=statuses).order_by("pk")
        if options["scan_pks"]:
            scans = Scan.objects.filter(pk__in=options["scan_pks"]).order_by(
                "pk"
            )
        total_open = 0
        for scan in scans:
            before = scan.issues.filter(check_name__in=REVIEW2_CHECKS).count()
            if options["dry_run"]:
                self.stdout.write(
                    f"scan {scan.pk} ({scan.status}): {before} finding(s) "
                    "now; would rebuild"
                )
                continue
            open_count = findings.rebuild(scan)
            total_open += open_count
            self.stdout.write(
                f"scan {scan.pk} ({scan.status}): {before} -> "
                f"{open_count} open finding(s)"
            )
        self.stdout.write(
            self.style.SUCCESS(
                f"{scans.count()} scan(s) "
                f"{'would be ' if options['dry_run'] else ''}rebuilt; "
                f"{total_open} finding(s) open"
            )
        )
