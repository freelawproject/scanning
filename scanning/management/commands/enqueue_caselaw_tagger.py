"""Start the case-law block tagger over named volumes.

The tagger reads a volume's glued dots.mocr document and its reviewed
detections, serializes one sequence per opinion, writes the input and
its map to the bucket, and creates one RunPod row per volume
(``tagger.ensure_tag_jobs``). The daemon's submit wave sends it; the
collect tick glues the answer into ``r{run}-volume.json``.

Nothing enqueues this stage on its own yet: row creation is what costs
GPU money, and this command is the one deliberate way in, the way
``enqueue_yolo_detect`` and ``reread_failed_pages`` are for their
stages. ``TestKnownEnqueuePaths`` names it on purpose. A tick pass
comes after the stage has been watched on a few volumes.

A volume is tagged after review 2 (``tagger.TAG_STATUSES``), when the
boxes and the pairing are final. ``--any-status`` skips that gate for
a test volume; do not use it on production volumes still in review.

Examples:

    # Say what would be sent for one volume, and change nothing.
    docker exec scanning-daemon python manage.py enqueue_caselaw_tagger \\
        2574 --dry-run

    # Tag two volumes that have finished review 2.
    docker exec scanning-daemon python manage.py enqueue_caselaw_tagger \\
        2574 3129

    # Tag every volume that has finished review 2 and has no run yet.
    docker exec scanning-daemon python manage.py enqueue_caselaw_tagger \\
        --pending --limit 10
"""

from django.core.management.base import BaseCommand, CommandError

from scanning import tagger
from scanning.models import JobStatus, Scan


class Command(BaseCommand):
    help = (
        "Serialize named volumes' opinions and start one tagger job each; "
        "a live run is reused, not restarted."
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
            help="Scan numbers to tag.",
        )
        parser.add_argument(
            "--pending",
            action="store_true",
            help=(
                "Take every volume that has finished review 2 and has no "
                "tagger run yet."
            ),
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=None,
            help="Start at most this many runs.",
        )
        parser.add_argument(
            "--any-status",
            action="store_true",
            help=(
                "Tag a named volume whatever its status. For a test "
                "volume; review 2 is the gate otherwise."
            ),
        )
        parser.add_argument(
            "--force-new-run",
            action="store_true",
            help="Start a new run even when a live one describes today's input.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help=(
                "Build the input and report its size, but write nothing "
                "and create no row."
            ),
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
        if not wanted and not options["pending"]:
            raise CommandError(
                "Name the scans, or pass --pending for every volume past "
                "review 2 with no run."
            )
        if not dry_run and not tagger.enabled():
            raise CommandError(
                "The tagger is not enabled here; a new run would only park "
                "its row in the queue."
            )

        if wanted:
            scans = list(Scan.objects.filter(pk__in=wanted).order_by("pk"))
            missing = sorted(set(wanted) - {scan.pk for scan in scans})
            if missing:
                raise CommandError(f"No such scan: {missing}")
            if not options["any_status"]:
                wrong = [
                    scan
                    for scan in scans
                    if scan.status not in tagger.TAG_STATUSES
                ]
                if wrong:
                    raise CommandError(
                        "Not past review 2: "
                        + ", ".join(f"{s.pk} ({s.status})" for s in wrong)
                        + ". Pass --any-status for a test volume."
                    )
        else:
            scans = [
                scan
                for scan in Scan.objects.filter(
                    status__in=tagger.TAG_STATUSES
                ).order_by("-pk")
                if not tagger.live_tag_jobs(scan)
            ]
        if limit is not None:
            scans = scans[:limit]
        if not scans:
            self.stdout.write("Nothing to tag.")
            return

        started = 0
        for scan in scans:
            if dry_run:
                try:
                    prepared = tagger.prepare_input(scan)
                except tagger.TaggerInputError as exc:
                    self.stdout.write(
                        f"scan {scan.pk}: cannot build input: {exc}"
                    )
                    continue
                self.stdout.write(
                    f"scan {scan.pk}: would send {prepared.stats['opinions']} "
                    f"opinions, {prepared.stats['chars']} chars "
                    f"(digest {prepared.digest[:16]}; footnote cells "
                    f"{prepared.stats['footnote_cells']}, redacted "
                    f"{prepared.stats['redacted_cells']}, key icons "
                    f"{prepared.stats['key_icons']}, images "
                    f"{prepared.stats['images']})"
                )
                continue
            try:
                rows = tagger.ensure_tag_jobs(
                    scan, force_new_run=options["force_new_run"]
                )
            except tagger.TaggerInputError as exc:
                self.stdout.write(f"scan {scan.pk}: not started: {exc}")
                continue
            row = rows[0]
            state = (
                "already running"
                if row.status
                in (
                    JobStatus.SUBMITTED,
                    JobStatus.IN_QUEUE,
                    JobStatus.IN_PROGRESS,
                )
                else row.status
            )
            self.stdout.write(
                f"scan {scan.pk}: run {row.run}, {row.input_manifest['sequence_count']} "
                f"opinions, row {state}"
            )
            started += 1
        if not dry_run:
            self.stdout.write(f"{started} run(s) live.")
