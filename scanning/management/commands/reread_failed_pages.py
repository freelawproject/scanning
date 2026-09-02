"""Read again the shards whose dots.mocr worker left pages unread (#238).

Over 30 days every unread page was a repetition loop on a mostly blank
page whose verso showed through. The worker now retries such a page
once, on a thresholded render. That reaches no volume already read: the
shard completed, the glue kept the slot with its ``error``, and the
apply read the page as ``detected=None``.

This command starts a new dots.mocr run over every such volume, and the
new run re-pays only the shards with a hole: ``jobs._reusable_results``
carries a clean shard's result forward and refuses one that reports
``failed_pages`` or ``filtered_pages`` (``jobs.has_unread_pages``).
The glue and the apply follow on the collect tick, as
for any run. The apply on a volume already in
``READY_FOR_PAGE_COMPLETENESS_REVIEW`` is a recompute and keeps the
status; the numbers a curator typed survive it (#214).

Row creation is what costs GPU money, so this is a command and not a
tick, and ``TestKnownEnqueuePaths`` names it on purpose. Run it after
the worker image with the retry ladder is live -- before that, the
re-read repeats the failure.

**A second run does not pay twice for the same answer.** The worker
decodes greedily on a fixed render, so a shard read again gives the
same output. A hole the previous run already re-read and got again is
therefore stable (``jobs.hole_is_stable``): the command skips a volume
whose every hole is stable, and the carry keeps such a shard instead of
re-paying it. So the command may be run again after a new worker image,
and reads only what that image has not yet tried. Without a new image
a second run buys nothing, and says so per named scan. If the fix for
a filtered page turns out to be a JSON repair in the worker (#242), no
re-read helps until that image ships.

An approved volume is not touched: the command takes only the
statuses ``apply_ready_runs`` reads, and
``PAGE_COMPLETENESS_REVIEW_DONE`` is not one of them.

Examples:

    # Say what would be read again, and change nothing.
    docker exec scanning-daemon python manage.py reread_failed_pages \\
        --dry-run

    # Every eligible volume.
    docker exec scanning-daemon python manage.py reread_failed_pages

    # Two named volumes only.
    docker exec scanning-daemon python manage.py reread_failed_pages \\
        2561 2599
"""

from django.core.management.base import BaseCommand, CommandError

from scanning import dots_mocr, sharding
from scanning.models import (
    ExternalJob,
    JobEngine,
    JobProvider,
    JobStage,
    JobStatus,
    Scan,
)


class Command(BaseCommand):
    help = (
        "Start a new dots.mocr run over every volume in review 1 whose "
        "run left pages unread; only the shards with a hole are re-paid."
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
            help="Scan numbers to read again; every eligible scan when "
            "the list is empty.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would be read again, and change nothing.",
        )

    def handle(self, *args, **options):
        """Start a forced new run for every eligible scan.

        :param args: Unused positional arguments.
        :param options: Parsed CLI options.
        :return: None.
        :raises CommandError: If the stage is switched off, since a row
            created now would wait in the queue with no clock (#218).
        """
        dry_run = options["dry_run"]
        wanted = options["scan_pks"]

        if not dry_run and not dots_mocr.enabled():
            raise CommandError(
                "dots.mocr is not enabled here; a new run would only "
                "park its rows in the queue."
            )

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
            pk__in=list(glued), status__in=dots_mocr.APPLY_STATUSES
        ).order_by("pk")
        if wanted:
            scans = scans.filter(pk__in=wanted)
        scans = list(scans)

        started = skipped = 0

        def skip(scan, reason: str) -> None:
            # A scan the operator named deserves the reason; the
            # unnamed rest is a count.
            nonlocal skipped
            skipped += 1
            if scan.pk in wanted:
                self.stderr.write(f"scan {scan.pk}: {reason}")

        for scan in scans:
            rows = dots_mocr.live_analyze_jobs(scan)
            if not rows or any(
                row.status != JobStatus.CONSUMED for row in rows
            ):
                skip(scan, "the live dots.mocr run is not glued yet")
                continue
            holes = dots_mocr.shards_with_holes(rows)
            if not holes:
                skip(scan, "no shard of the live run reports unread pages")
                continue
            if not dots_mocr.shards_worth_rereading(rows):
                skip(
                    scan,
                    f"run {rows[0].run} already read the unread pages again "
                    "and got the same answer; another run buys nothing "
                    "without a new worker image",
                )
                continue
            shards = ", ".join(str(row.shard_index + 1) for row in holes)
            if dry_run:
                started += 1
                self.stdout.write(
                    f"scan {scan.pk}: would read shard(s) {shards} of "
                    f"{rows[0].shard_count} again"
                )
                continue

            manifest, reason = sharding.committed_manifest(scan)
            if manifest is None:
                # Named or not: a stale shard set is worth a line.
                skipped += 1
                self.stderr.write(f"scan {scan.pk}: {reason}")
                continue
            new_rows = dots_mocr.ensure_analyze_jobs(
                scan, manifest, force_new_run=True
            )
            pending = sum(
                1 for row in new_rows if row.status == JobStatus.PENDING
            )
            started += 1
            self.stdout.write(
                f"scan {scan.pk}: run {new_rows[0].run} started, "
                f"{pending} of {len(new_rows)} shard(s) to read again "
                f"(shard(s) {shards} had unread pages)"
            )

        for pk in sorted(set(wanted) - {scan.pk for scan in scans}):
            self.stderr.write(
                f"scan {pk}: no glued run in review 1; nothing to do"
            )

        self.stdout.write(
            self.style.SUCCESS(
                f"{started} scan(s) to read again, {skipped} skipped"
                f"{' (dry run)' if dry_run else ''}"
            )
        )
