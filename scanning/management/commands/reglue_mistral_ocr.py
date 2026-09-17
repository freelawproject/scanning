"""Glue every read Mistral run again, volume and corrected volume.

Issue #245. The glue is the one transform of a Mistral result: the
harvest stores what Mistral wrote, line for line, and
``mistral_ocr.parse_payload`` is what turns it into pages. So a better
transform -- a new block field, a cleaner text rule, a fault nobody had
seen -- must reach the volumes already read, and this command is how.
It costs one small download per shard and no API payment, because the
per-shard results are kept for good.

It writes the same two objects the collect tick writes: the volume
document of every glued run, and, for a scan whose corrected volume
(#224) stands, that run's own document. Nothing else moves. No scan
status is written, no review state is read, no row is created and no
job is started, so the command is safe on a corpus in any state.

A run still open is skipped: the tick glues it when its rows answer.
A run whose glue has spent its attempts is glued again here, because a
person asked for it; the ledger on the row is for the tick.

Examples:

    # Say what would be written, and change nothing.
    docker exec scanning-daemon python manage.py reglue_mistral_ocr \\
        --dry-run

    # Glue every read volume again.
    docker exec scanning-daemon python manage.py reglue_mistral_ocr

    # Two named volumes only.
    docker exec scanning-daemon python manage.py reglue_mistral_ocr \\
        2726 2702
"""

from django.core.management.base import BaseCommand, CommandError

from scanning import mistral_ocr, s3_sync
from scanning.models import (
    ApplyRun,
    ExternalJob,
    JobEngine,
    JobStage,
    JobStatus,
    Scan,
)


class Command(BaseCommand):
    help = (
        "Glue every read Mistral run again, so a changed transform "
        "reaches the volumes already read. Writes the volume document "
        "and, where a corrected volume stands, its document too."
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
            help="Scan numbers to glue again; every read scan when the "
            "list is empty.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would be written, and change nothing.",
        )

    def handle(self, *args, **options):
        """Glue again, or report, every scan with a read Mistral run.

        :param args: Unused positional arguments.
        :param options: Parsed CLI options.
        :return: None.
        :raises CommandError: If S3 is off, since every result lives in
            the bucket and there is nothing to read without it.
        """
        dry_run = options["dry_run"]
        wanted = options["scan_pks"]

        if not s3_sync.s3_active():
            raise CommandError(
                "S3 is not active here; the Mistral results live in the "
                "bucket and nothing can be read without it."
            )

        volumes = applies = skipped = failed = 0
        for scan in self._candidates(wanted):
            rows = mistral_ocr.live_extract_jobs(scan)
            if not rows or any(
                row.status not in (JobStatus.COMPLETED, JobStatus.CONSUMED)
                for row in rows
            ):
                skipped += 1
                continue
            run = ApplyRun.objects.filter(
                scan=scan, superseded_at__isnull=True, built_at__isnull=False
            ).first()
            if dry_run:
                self.stdout.write(
                    f"scan {scan.pk}: would glue run {rows[0].run} "
                    f"({len(rows)} shard(s))"
                    + (f" and apply run {run.label}" if run else "")
                )
                volumes += 1
                applies += 1 if run else 0
                continue
            try:
                key = mistral_ocr.merge_extract_results(scan, rows)
            except Exception as exc:  # noqa: BLE001 - reported per scan
                self.stderr.write(f"scan {scan.pk}: {exc}")
                failed += 1
                continue
            volumes += 1
            self.stdout.write(f"scan {scan.pk}: wrote {key}")
            if run is None:
                continue
            try:
                applies += 1 if self._reglue_apply(scan, run, rows) else 0
            except Exception as exc:  # noqa: BLE001 - reported per scan
                self.stderr.write(f"scan {scan.pk} apply {run.label}: {exc}")
                failed += 1

        self.stdout.write(
            f"{'Would glue' if dry_run else 'Glued'} {volumes} volume(s) "
            f"and {applies} corrected volume(s); skipped {skipped} open "
            f"run(s), {failed} failure(s)"
        )

    def _reglue_apply(self, scan, run, volume_rows) -> bool:
        """Write one corrected volume's document again.

        The rows of the apply run are read, never created: creating a
        row is paid work, and this command starts none. So a run whose
        edited pages have not been read is left exactly as it is, and
        the tick creates its rows on its own terms. Writing it here
        would mark every edited page unread **and** stamp a key that
        says the run is done, after which nothing would ever read those
        pages -- the one way this command could lose a volume's text.

        The test is ``mistral_ocr.apply_glue_due``, the same one the
        tick makes, with only "a document for this volume run already
        stands" waived: writing that document again is what a person
        runs this command for.

        :param scan: The scan.
        :param run: The standing, built apply run.
        :param volume_rows: The volume run's rows.
        :returns: Whether a document was written.
        :rtype: bool
        """
        rows = mistral_ocr.apply_jobs(scan, run)
        volume_run = volume_rows[0].run
        if not mistral_ocr.apply_glue_due(run, rows, volume_run, force=True):
            self.stdout.write(
                f"scan {scan.pk} apply {run.label}: the edited pages are "
                f"not read yet; left as it is"
            )
            return False
        key = mistral_ocr.glue_apply_run(scan, run, rows, volume_run)
        ApplyRun.objects.filter(pk=run.pk).update(
            extract_key=key, extract_run=volume_run
        )
        self.stdout.write(f"scan {scan.pk} apply {run.label}: wrote {key}")
        return True

    def _candidates(self, wanted: list[int]):
        """Return the scans with a Mistral volume run, oldest first.

        :param wanted: The scan numbers asked for, or an empty list.
        :returns: The scans to consider.
        :rtype: QuerySet
        :raises CommandError: If a named scan has no Mistral run.
        """
        scan_ids = set(
            ExternalJob.objects.filter(
                stage=JobStage.EXTRACT,
                engine=JobEngine.MISTRAL_OCR,
                apply_run__isnull=True,
            )
            .values_list("scan_id", flat=True)
            .distinct()
        )
        if wanted:
            missing = sorted(set(wanted) - scan_ids)
            if missing:
                raise CommandError(
                    f"scan(s) {missing} have no Mistral run to glue."
                )
            scan_ids &= set(wanted)
        return Scan.objects.filter(pk__in=sorted(scan_ids)).order_by("pk")
