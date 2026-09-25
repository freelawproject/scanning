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

With ``--stale-labels`` it is the re-read of #338: every volume whose
finished run was read before ``yolo.LABEL_SET`` gets a new run, and
nothing is carried from the old one, because its results lack the
classes the new run exists for. The pages a curator changed get a new
read of their own when their rows are stale too. A run that finished
after the worker image carried the class set is adopted with
``--read-since`` (its rows are stamped, and nothing is paid). The
approved volumes are in the selection; ``reopen_redaction_review``
takes them back to review 2 so the new detections are imported.

    # Say which volumes a re-read would take, leaving partner scans out.
    docker exec scanning-daemon python manage.py enqueue_yolo_detect \\
        --stale-labels --read-since 2026-09-16T18:00Z \\
        --exclude 2561 2599 --dry-run
"""

from datetime import datetime

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from scanning import apply, jobs, sharding, yolo
from scanning.models import (
    DEAD_JOB_STATUSES,
    IN_FLIGHT_JOB_STATUSES,
    ExternalJob,
    JobEngine,
    JobStage,
    JobStatus,
    Scan,
    Status,
)

#: The statuses ``--stale-labels`` reads (#338): the sweep's, plus an
#: approved redaction review, whose volumes need the classes too and
#: go back to review 2 by ``reopen_redaction_review``. QUEUED and
#: PROCESSING belong to the daemon, and ERROR to the admin re-queue.
RELABEL_STATUSES = yolo.SWEEP_STATUSES | {Status.REDACTION_REVIEW_DONE}

#: A row in these is still owed work or a merge, so a run holding one
#: is not replaced: its rows would still be submitted, and paid twice.
OPEN_STATUSES = {JobStatus.PENDING, JobStatus.COMPLETED} | set(
    IN_FLIGHT_JOB_STATUSES
)


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
            "--stale-labels",
            action="store_true",
            help=(
                "Take every volume whose finished run was read before "
                "the current class set (#338), and read it again "
                "without carrying the old results."
            ),
        )
        parser.add_argument(
            "--read-since",
            type=datetime.fromisoformat,
            default=None,
            help=(
                "With --stale-labels: a run whose every row was "
                "submitted after this time was read with the current "
                "class set; stamp it instead of reading it again."
            ),
        )
        parser.add_argument(
            "--exclude",
            nargs="+",
            type=int,
            default=[],
            help="Scan numbers to leave out, such as the partner scans.",
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

        if options["stale_labels"] and options["dead_runs"]:
            raise CommandError("Pass --stale-labels or --dead-runs, not both.")
        if options["read_since"] and not options["stale_labels"]:
            raise CommandError("--read-since goes with --stale-labels.")
        if (
            not wanted
            and not options["dead_runs"]
            and not options["stale_labels"]
        ):
            raise CommandError(
                "Name the scans, or pass --dead-runs for every volume in "
                "review whose run died, or --stale-labels for every "
                "volume read before the current class set."
            )
        if not dry_run and not yolo.enabled():
            raise CommandError(
                "YOLO detection is not enabled here; a new run would only "
                "park its rows in the queue."
            )

        if options["stale_labels"]:
            self._relabel(
                wanted,
                options["exclude"],
                options["read_since"],
                limit,
                dry_run,
            )
            return

        scans = Scan.objects.filter(status__in=yolo.SWEEP_STATUSES)
        if wanted:
            scans = Scan.objects.filter(pk__in=wanted)
        scans = scans.exclude(pk__in=options["exclude"])
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

    def _relabel(self, wanted, excluded, read_since, limit, dry_run):
        """Read again every selected volume read before the class set.

        :param wanted: Scan numbers to narrow the selection to, or empty
            for every volume in :data:`RELABEL_STATUSES`.
        :param excluded: Scan numbers to leave out.
        :param read_since: The time from which a submitted row was read
            with the current class set, or None.
        :param limit: The most volumes to start, or None.
        :param dry_run: Report and change nothing.
        :return: None.
        """
        if read_since is not None and timezone.is_naive(read_since):
            read_since = timezone.make_aware(read_since)
        scans = Scan.objects.filter(status__in=RELABEL_STATUSES)
        if wanted:
            scans = scans.filter(pk__in=wanted)
        scans = list(scans.exclude(pk__in=excluded).order_by("pk"))

        started = adopted = current = skipped = 0
        for scan in scans:
            if limit is not None and started >= limit:
                break
            rows = yolo.live_detect_jobs(scan)
            if not rows:
                skipped += 1
                self.stderr.write(
                    f"scan {scan.pk}: no detection run; the sweep starts one"
                )
                continue
            run = apply.current_run(scan)
            if run is not None and not run.is_built:
                run = None
            edit_rows = (
                jobs.live_run(
                    scan,
                    JobStage.DETECT,
                    JobEngine.BLACKLETTER,
                    apply_run=run,
                )
                if run is not None
                else []
            )
            open_rows = [
                r for r in rows + edit_rows if r.status in OPEN_STATUSES
            ]
            if open_rows:
                skipped += 1
                self.stderr.write(
                    f"scan {scan.pk}: detection run {open_rows[0].run} is "
                    "still working; run this again when it ends"
                )
                continue

            stale = [
                group
                for group in (rows, edit_rows)
                if group and not yolo.labels_current(group)
            ]
            took = [
                g for g in stale if read_since and _read_after(g, read_since)
            ]
            for group in took:
                stale.remove(group)
                if not dry_run:
                    _stamp(group)
                adopted += 1
                self.stdout.write(
                    f"scan {scan.pk}: {'would adopt' if dry_run else 'adopted'} "
                    f"detection run {group[0].run} "
                    f"({'edited pages' if group is edit_rows else 'volume'})"
                )
            if not stale:
                if not took:
                    current += 1
                continue

            if dry_run:
                started += 1
                self.stdout.write(
                    f"scan {scan.pk}: would read again "
                    f"({', '.join(_names(stale, edit_rows))})"
                )
                continue

            manifest, reason = sharding.committed_manifest(scan)
            if manifest is None:
                skipped += 1
                self.stderr.write(f"scan {scan.pk}: {reason}")
                continue
            if any(group is edit_rows for group in stale):
                yolo.ensure_detect_jobs(
                    scan,
                    apply.stored_shard_manifest(scan, run),
                    apply_run=run,
                    force_new_run=True,
                )
            # Always a volume run, even when only the edited pages are
            # stale: its merge is what writes the corrected volume's
            # detections again (``apply.refresh_detections``), and a
            # volume read with the class set is carried whole, at no
            # cost.
            new_rows = yolo.ensure_detect_jobs(
                scan, manifest, force_new_run=True
            )
            pending = sum(1 for r in new_rows if r.status == JobStatus.PENDING)
            started += 1
            self.stdout.write(
                f"scan {scan.pk}: run {new_rows[0].run} started, "
                f"{pending} of {len(new_rows)} shard(s) to read again"
                f" ({', '.join(_names(stale, edit_rows))})"
            )

        for pk in sorted(set(wanted) - {scan.pk for scan in scans}):
            self.stderr.write(
                f"scan {pk}: not a volume this re-read takes (no such "
                "scan, excluded, or its status is not one it reads)"
            )
        self.stdout.write(
            self.style.SUCCESS(
                f"{started} run(s) started, {adopted} adopted, {current} "
                f"current, {skipped} skipped"
                f"{' (dry run)' if dry_run else ''}"
            )
        )


def _names(stale, edit_rows) -> list[str]:
    """Name the stale groups of a volume for the report.

    :param stale: The stale row groups.
    :param edit_rows: The edited pages' group, to tell it apart.
    :returns: One name per group.
    :rtype: list[str]
    """
    return [
        "edited pages" if group is edit_rows else "volume" for group in stale
    ]


def _read_after(rows, since) -> bool:
    """Return whether every row of a run was submitted after ``since``.

    A carried row was never submitted in its own run, so its result is
    as old as the run it came from and it does not qualify.

    :param rows: One run's rows.
    :param since: An aware datetime.
    :returns: Whether the whole run was read after it.
    :rtype: bool
    """
    return all(row.submitted_at and row.submitted_at >= since for row in rows)


def _stamp(rows) -> None:
    """Stamp the current class set on a run read with it.

    :param rows: One run's rows.
    :return: None.
    """
    for row in rows:
        manifest = dict(row.input_manifest or {})
        manifest["label_set"] = yolo.LABEL_SET
        ExternalJob.objects.filter(pk=row.pk).update(input_manifest=manifest)
