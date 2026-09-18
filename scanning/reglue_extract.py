"""The body both ``EXTRACT`` re-glue commands share (#245, #368).

``reglue_mistral_ocr`` and ``reglue_surya_ocr`` do one thing each, and
they do it the same way: the glue is the one transform of a stored
result, so a better transform must reach the volumes already read, and
the command is how. It costs one small download per shard and no
payment to the provider, because the per-shard results are kept for
good.

So the walk lives here once, and each command is its docstring plus one
:class:`EngineReglue` entry. What differs between the two engines is
five callables and the name of the fields on ``ApplyRun``; what must
not differ is the rule about what may be written:

**The rows of an apply run are read, never created.** Creating a row is
paid work, and these commands start none. A run whose edited pages have
not been read is left exactly as it is, and the collect tick creates
its rows on its own terms. A document written without them would mark
every edited page unread **and** stamp a key that says the run is done,
after which nothing would ever read those pages -- the one way a
command here could lose a volume's text.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from django.core.management.base import BaseCommand, CommandError

from scanning import s3_sync
from scanning.models import (
    ApplyRun,
    ExternalJob,
    JobStage,
    JobStatus,
    Scan,
)


@dataclass(frozen=True)
class EngineReglue:
    """One engine's re-glue, for :class:`ReglueExtractCommand`.

    :ivar label: The engine's name in every message ("Mistral").
    :ivar engine: A :class:`~scanning.models.JobEngine` value.
    :ivar key_field: The ``ApplyRun`` field that names the corrected
        volume's document.
    :ivar run_field: The ``ApplyRun`` field that holds the volume run
        it was glued from.
    :ivar live_rows: The engine's live-run reader.
    :ivar merge: Writes the volume document and returns its key.
    :ivar apply_rows: The engine's reader of one apply run's rows.
    :ivar apply_due: The engine's one rule for the corrected volume's
        document, which takes ``force``.
    :ivar glue_apply: Writes the corrected volume's document and
        returns its key.
    """

    label: str
    engine: str
    key_field: str
    run_field: str
    live_rows: Callable
    merge: Callable
    apply_rows: Callable
    apply_due: Callable
    glue_apply: Callable


class ReglueExtractCommand(BaseCommand):
    """Glue every read run of one engine again, volume and corrected.

    A subclass sets :attr:`spec` and its own docstring. Nothing else:
    no scan status is written, no review state is read, no row is
    created and no job is started, so the command is safe on a corpus
    in any state.

    A run still open is skipped: the tick glues it when its rows
    answer. A run whose glue has spent its attempts is glued again
    here, because a person asked for it; the ledger on the row is for
    the tick.
    """

    #: The engine this command re-glues. A subclass sets it.
    spec: EngineReglue

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
        """Glue again, or report, every scan with a read run.

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
                f"S3 is not active here; the {self.spec.label} results "
                f"live in the bucket and nothing can be read without it."
            )

        volumes = applies = skipped = failed = 0
        for scan in self._candidates(wanted):
            rows = self.spec.live_rows(scan)
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
                key = self.spec.merge(scan, rows)
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

        The test is the engine's own ``apply_glue_due``, the same one
        the tick makes, with only "a document for this volume run
        already stands" waived: writing that document again is what a
        person runs this command for.

        :param scan: The scan.
        :param run: The standing, built apply run.
        :param volume_rows: The volume run's rows.
        :returns: Whether a document was written.
        :rtype: bool
        """
        rows = self.spec.apply_rows(scan, run)
        volume_run = volume_rows[0].run
        if not self.spec.apply_due(run, rows, volume_run, force=True):
            self.stdout.write(
                f"scan {scan.pk} apply {run.label}: the edited pages are "
                f"not read yet; left as it is"
            )
            return False
        key = self.spec.glue_apply(scan, run, rows, volume_run)
        ApplyRun.objects.filter(pk=run.pk).update(
            **{
                self.spec.key_field: key,
                self.spec.run_field: volume_run,
            }
        )
        self.stdout.write(f"scan {scan.pk} apply {run.label}: wrote {key}")
        return True

    def _candidates(self, wanted: list[int]):
        """Return the scans with a volume run of this engine, oldest first.

        :param wanted: The scan numbers asked for, or an empty list.
        :returns: The scans to consider.
        :rtype: QuerySet
        :raises CommandError: If a named scan has no run of this engine.
        """
        scan_ids = set(
            ExternalJob.objects.filter(
                stage=JobStage.EXTRACT,
                engine=self.spec.engine,
                apply_run__isnull=True,
            )
            .values_list("scan_id", flat=True)
            .distinct()
        )
        if wanted:
            missing = sorted(set(wanted) - scan_ids)
            if missing:
                raise CommandError(
                    f"scan(s) {missing} have no {self.spec.label} run to glue."
                )
            scan_ids &= set(wanted)
        return Scan.objects.filter(pk__in=sorted(scan_ids)).order_by("pk")
