"""Start a detection run again over volumes whose run died (#250).

The daemon's sweep (``yolo.enqueue_missing_runs``) starts one detection
run per shard set and never a second: a dead run means
``YOLO_MAX_ATTEMPTS`` were spent on a shard, and a fourth attempt is a
staff decision, not a tick. This command is that decision. It is the
operator path for the two cases the sweep leaves alone on purpose: one
volume whose run died, and an endpoint outage that failed many volumes
at once.

It calls ``yolo.ensure_detect_jobs``, which replaces a run that holds a
dead row and carries every shard whose result is still in the bucket
(``jobs._reusable_results``), so a re-run pays only for the shards that
died. A live run is reused, not restarted, and says so. The merge and
the redaction computation follow on the collect tick, as for any run.

Row creation is what costs GPU money, so this is a command and not a
tick, and ``TestKnownEnqueuePaths`` names it on purpose, beside
``reread_failed_pages``.

Examples:

    # Say which volumes hold a dead run, and change nothing.
    docker exec scanning-daemon python manage.py enqueue_yolo_detect \\
        --dead-runs --dry-run

    # Start a fresh run over every volume in review whose run died,
    # at most twenty of them.
    docker exec scanning-daemon python manage.py enqueue_yolo_detect \\
        --dead-runs --limit 20

    # Two named volumes, whatever the state of their run.
    docker exec scanning-daemon python manage.py enqueue_yolo_detect \\
        2561 2599
"""

from django.core.management.base import BaseCommand, CommandError

from scanning import sharding, yolo
from scanning.models import DEAD_JOB_STATUSES, JobStatus, Scan


class Command(BaseCommand):
    help = (
        "Start a fresh detection run over named volumes, or over every "
        "volume in review whose detection run died; only the shards "
        "that died are re-paid."
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
            help="Scan numbers to detect again.",
        )
        parser.add_argument(
            "--dead-runs",
            action="store_true",
            help=(
                "Take every volume in a review status whose live "
                "detection run holds a failed, cancelled or expired row."
            ),
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=None,
            help="Start at most this many runs.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would be started, and change nothing.",
        )

    def handle(self, *args, **options):
        """Start a run for every selected scan.

        :param args: Unused positional arguments.
        :param options: Parsed CLI options.
        :return: None.
        :raises CommandError: If nothing selects a scan, or if the stage
            is switched off, since a row created now would wait in the
            queue with no clock (#218).
        """
        dry_run = options["dry_run"]
        wanted = options["scan_pks"]
        limit = options["limit"]

        if not wanted and not options["dead_runs"]:
            raise CommandError(
                "Name the scans, or pass --dead-runs for every volume in "
                "review whose run died."
            )
        if not dry_run and not yolo.enabled():
            raise CommandError(
                "YOLO detection is not enabled here; a new run would only "
                "park its rows in the queue."
            )

        scans = Scan.objects.filter(status__in=yolo.SWEEP_STATUSES)
        if wanted:
            scans = Scan.objects.filter(pk__in=wanted)
        scans = list(scans.order_by("pk"))

        started = skipped = 0
        for scan in scans:
            if limit is not None and started >= limit:
                break
            rows = yolo.live_detect_jobs(scan)
            dead = any(row.status in DEAD_JOB_STATUSES for row in rows)
            if not wanted and not dead:
                skipped += 1
                continue
            if rows and not dead:
                # A named scan with a live or finished run: the sweep
                # rule holds, and this command does not force a run
                # over paid output.
                skipped += 1
                self.stderr.write(
                    f"scan {scan.pk}: run {rows[0].run} is not dead "
                    f"({', '.join(sorted({r.status for r in rows}))}); "
                    "nothing to do"
                )
                continue
            if dry_run:
                started += 1
                self.stdout.write(
                    f"scan {scan.pk}: would start a fresh run"
                    + (f" (run {rows[0].run} died)" if rows else "")
                )
                continue

            manifest, reason = sharding.committed_manifest(scan)
            if manifest is None:
                skipped += 1
                self.stderr.write(f"scan {scan.pk}: {reason}")
                continue
            new_rows = yolo.ensure_detect_jobs(scan, manifest)
            pending = sum(1 for r in new_rows if r.status == JobStatus.PENDING)
            started += 1
            self.stdout.write(
                f"scan {scan.pk}: run {new_rows[0].run} started, "
                f"{pending} of {len(new_rows)} shard(s) to detect again"
            )

        for pk in sorted(set(wanted) - {scan.pk for scan in scans}):
            self.stderr.write(f"scan {pk}: no such scan")

        self.stdout.write(
            self.style.SUCCESS(
                f"{started} run(s) started, {skipped} skipped"
                f"{' (dry run)' if dry_run else ''}"
            )
        )
