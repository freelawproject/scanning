"""Stamp every opinion with the digest of its inputs, moving no revision.

The creation (``opinions.create_rows``) rebuilds a matched opinion only
when the digest of its inputs moved (``opinions.input_digest``). A row
from before that rule carries no digest, so the first approval after
the deploy would rebuild every opinion of the volume once more. This
command writes the digest of every opinion at the rows as they stand,
and that approval then rebuilds only what moved.

It reads the rows and one JSON object per volume (the printed-page map)
and writes one column. No revision, no status, no glue, no PDF. Only a
volume in step 3 (``REDACTION_REVIEW_DONE``), named or not: there every
row is what the last creation built, because the writes of review 2
refuse while the review is closed. A volume sent back to step 2 may
already hold edits no build has seen, so a digest of its rows as they
stand would call those edits built; its next approval stamps it. A row
whose boundary is gone or that sits in another run's space is left for
that creation too.

Examples:

    docker exec scanning-daemon python manage.py stamp_opinion_digests \\
        --dry-run
    docker exec scanning-daemon python manage.py stamp_opinion_digests
    docker exec scanning-daemon python manage.py stamp_opinion_digests 1877
"""

from django.core.management.base import BaseCommand

from scanning import apply, opinions, review_states
from scanning.models import Opinion, Scan, Status

#: The one status a volume is stamped in: its rows are what the last
#: creation built. A volume opened again in step 2 is refused, named or
#: not, because its rows may hold edits no build has seen.
STATUSES = (Status.REDACTION_REVIEW_DONE,)


class Command(BaseCommand):
    help = (
        "Stamp every opinion with the digest of its inputs, so the next "
        "approval of its volume rebuilds only the opinions whose rows moved."
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
            help="Scan numbers to stamp; every volume with opinions when "
            "the list is empty.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what it would stamp and write nothing.",
        )

    def handle(self, *args, **options):
        """Stamp the digests, one volume at a time.

        :param args: Unused positional arguments.
        :param options: Parsed CLI options.
        :return: None.
        """
        dry_run = options["dry_run"]
        scans = Scan.objects.filter(status__in=STATUSES)
        if options["scan_pks"]:
            named = Scan.objects.filter(pk__in=options["scan_pks"])
            for scan in named.exclude(status__in=STATUSES):
                self.stderr.write(
                    f"scan {scan.pk}: {scan.status}, not in step 3; its "
                    "rows may hold edits no build has seen, so its next "
                    "approval stamps it"
                )
            scans = named.filter(status__in=STATUSES)
        scans = scans.order_by("pk")

        total_stamped = total_current = total_skipped = 0
        for scan in scans:
            run = review_states.final_run(scan)
            if run is None:
                self.stderr.write(f"scan {scan.pk}: no corrected volume")
                continue
            rows = Opinion.objects.filter(scan=scan).select_related("boundary")
            if not rows.exists():
                continue
            try:
                printed = apply.load_printed_pages(scan, run)
            except Exception as exc:  # noqa: BLE001 - reported, then skipped
                self.stderr.write(
                    f"scan {scan.pk}: the printed-page map did not load: {exc}"
                )
                continue
            lookup = opinions.printed_lookup(printed)
            scan_rows = opinions.read_rows(scan, run)
            stamped = current = skipped = 0
            for opinion in rows:
                if opinion.boundary is None or opinion.apply_run_id != run.pk:
                    skipped += 1
                    continue
                digest = opinions.input_digest(
                    opinion.boundary, run, scan_rows, lookup
                )
                if opinion.input_digest == digest:
                    current += 1
                    continue
                stamped += 1
                if not dry_run:
                    Opinion.objects.filter(pk=opinion.pk).update(
                        input_digest=digest
                    )
            verb = "would stamp" if dry_run else "stamped"
            self.stdout.write(
                f"scan {scan.pk}: {verb} {stamped}, {current} current, "
                f"{skipped} skipped (no boundary or another run)"
            )
            total_stamped += stamped
            total_current += current
            total_skipped += skipped

        verb = "would stamp" if dry_run else "stamped"
        self.stdout.write(
            self.style.SUCCESS(
                f"{verb} {total_stamped} opinion(s); {total_current} current, "
                f"{total_skipped} skipped"
            )
        )
