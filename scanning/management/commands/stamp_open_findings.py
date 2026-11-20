"""Write the stored count of every opinion's open findings again.

``Opinion.open_findings`` is the number of the Warnings column of the
opinions list, stored on the row so the list sorts by it off an index.
Every writer of a finding row stamps it (``opinion_findings.stamp_open``),
so this command is for the rows from before the column, **once after
the deploy**, and for a count that drifted.

It reads the finding rows and writes one column of the opinions whose
count moved, one volume at a time, under the row locks of that volume's
opinions, so it is safe beside live pods. No finding, no status, no
revision. A second run writes nothing.

Examples:

    docker exec scanning-daemon python manage.py stamp_open_findings \\
        --all --dry-run
    docker exec scanning-daemon python manage.py stamp_open_findings --all
    docker exec scanning-daemon python manage.py stamp_open_findings 1877
"""

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from scanning import opinion_findings
from scanning.models import Opinion


class Command(BaseCommand):
    help = (
        "Write the stored count of open findings of every opinion again, "
        "from the finding rows."
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
            help="Scan numbers whose opinions to count.",
        )
        parser.add_argument(
            "--all",
            action="store_true",
            help="Every volume with opinions.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what it would write and write nothing.",
        )

    def handle(self, *args, **options):
        """Stamp the counts, one volume at a time.

        :param args: Unused positional arguments.
        :param options: Parsed CLI options.
        :return: None.
        :raises CommandError: With no volume named and no ``--all``, or
            with both.
        """
        named = options["scan_pks"]
        if bool(named) == options["all"]:
            raise CommandError("Name the scans, or pass --all.")
        scan_ids = Opinion.objects.order_by().values_list("scan_id", flat=True)
        if named:
            scan_ids = scan_ids.filter(scan_id__in=named)
        dry_run = options["dry_run"]
        total = 0
        for scan_id in sorted(set(scan_ids)):
            # A dry run is the write, rolled back: one rule for the
            # count, and the number it reports is the number it writes.
            with transaction.atomic():
                rows = Opinion.objects.filter(scan_id=scan_id)
                # The row lock of the dismissal and of the ensemble,
                # taken before the count is read, in pk order: the
                # command runs beside live pods, and a count read
                # before one of their stamps committed would be
                # written over it.
                list(
                    rows.select_for_update()
                    .order_by("pk")
                    .values_list("pk", flat=True)
                )
                written = opinion_findings.stamp_open(rows)
                if dry_run:
                    transaction.set_rollback(True)
            if written:
                verb = "would write" if dry_run else "wrote"
                self.stdout.write(f"scan {scan_id}: {verb} {written}")
            total += written
        verb = "would write" if dry_run else "wrote"
        self.stdout.write(
            self.style.SUCCESS(f"{verb} the count of {total} opinion(s)")
        )
