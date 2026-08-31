"""Bitonal conversion: deciding to skip it, and merging its results.

The conversion runs on doctor, one request per shard
(:mod:`scanning.doctor_client`), tracked on ``ExternalJob`` rows
(:mod:`scanning.jobs`). Both ends of that are here: whether a volume
needs converting at all, and reassembling the shards into
``bitonal.pdf``.

``bitonal.pdf`` is the stage's completion marker. It carries the
original's exact page geometry at a fraction of the size, which is what
lets everything downstream read page dimensions and detection
coordinates off it instead of the multi-GB original.

Page inserts and deletes are not this module's problem: they are
page-level edits, and the smart-edit paths merge their one converted
page into the existing ``bitonal.pdf`` rather than re-run a volume pass.
"""

from __future__ import annotations

import logging
import tempfile
import time
from pathlib import Path

import fitz
from django.db import transaction
from django.utils import timezone

from scanning import s3_sync
from scanning.models import (
    DEAD_JOB_STATUSES,
    IN_FLIGHT_JOB_STATUSES,
    ExternalJob,
    JobEngine,
    JobProvider,
    JobStage,
    JobStatus,
    Scan,
    Status,
)
from scanning.utils import ensure_output_dir

logger = logging.getLogger(__name__)

#: Message a converted scan parks with. Echoes ``run_full_pipeline``,
#: since the scan lands in the same interim state either way (#173).
CONVERTED_MESSAGE = (
    "Uploaded, sharded and converted. Page-number validation is "
    "temporarily disabled while the pipeline is rebuilt on the new OCR "
    "stack; this scan will be processed once that lands."
)

#: Message a scan parks with when conversion was not needed.
SKIPPED_MESSAGE = (
    "Uploaded and sharded. The scan is already bitonal, so no "
    "conversion was needed. Page-number validation is temporarily "
    "disabled while the pipeline is rebuilt on the new OCR stack."
)

#: Prefix of the merge's scratch directory in the system temp dir.
#: ``cleanup_processing_tmp`` sweeps leaked ones (a SIGKILL mid-merge
#: skips the ``TemporaryDirectory`` cleanup), so the name is shared
#: rather than inlined (#215).
MERGE_TMP_PREFIX = "bitonal-"


class BitonalMergeError(Exception):
    """A converted shard set could not be reassembled."""


def source_is_bitonal(pdf_path: str | Path) -> bool:
    """Return whether every page of ``pdf_path`` is already 1-bit.

    Converting a 1-bpc scan buys a few percent of size for a full
    rasterization pass, and doctor does not check -- it re-rasterizes
    whatever it is given, so a 600-dpi bitonal source sent at 200 dpi
    would *lose* resolution. ``serve_scan_pdf`` falls back to the
    original, which for such a volume is small, so skipping the stage is
    the cheapest correct answer.

    Conservative: a page with no image cannot be *shown* to be bitonal,
    so it counts as needing conversion. Reads image metadata only, so it
    costs a page-tree walk.

    :param pdf_path: The original scan PDF.
    :returns: True when every page carries at least one image and every
        image on every page is 1 bit per component.
    :rtype: bool
    """
    with fitz.open(str(pdf_path)) as doc:
        if doc.page_count < 1:
            return False
        for index in range(doc.page_count):
            # (xref, smask, width, height, bpc, colorspace, ...)
            images = doc[index].get_images(full=True)
            if not images:
                return False
            if any(image[4] != 1 for image in images):
                return False
    return True


def live_convert_jobs(scan) -> list[ExternalJob]:
    """Return a scan's current-run conversion jobs, in page order.

    The live run is the rows at ``max(run)``: a re-run keeps the
    previous run as history, and reading those as live would report work
    nobody wants any more.

    :param scan: The scan (or its pk) to look up.
    :returns: The live run's rows ordered by shard index, or an empty
        list when the scan has never been converted.
    :rtype: list[ExternalJob]
    """
    from scanning import jobs

    return jobs.live_run(scan, JobStage.CONVERT, JobEngine.BITONAL)


def merge_convert_results(scan, convert_jobs: list[ExternalJob]) -> Path:
    """Reassemble converted shards into the scan's ``bitonal.pdf``.

    Merges in strict shard order and asserts the page arithmetic. It
    needs no access to the original, by construction rather than
    omission: ``sharding.verify_shards`` proved each shard's pages are
    the original's byte-for-byte, and doctor verified each result's page
    count and MediaBox against the shard it was given. What is left is
    assembly -- a missing, duplicated or misordered shard -- which page
    counts in shard order close.

    Idempotent: it rebuilds from the result objects every time, so a
    daemon killed between the merge and the park just merges again.

    :param scan: The scan being converted.
    :param convert_jobs: The live run's rows, ordered by shard index.
    :returns: Path to the written ``bitonal.pdf``.
    :rtype: Path
    :raises BitonalMergeError: If a result is missing, has the wrong
        page count, or the merged volume does not match the original's.
    """
    if not convert_jobs:
        raise BitonalMergeError(f"scan {scan.pk} has no conversion jobs")

    expected_total = convert_jobs[0].input_manifest.get("source_page_count")
    output_dir = Path(ensure_output_dir(scan))
    destination = output_dir / s3_sync.PIPELINE_INPUT_NAME
    started = time.monotonic()

    # A temp dir, not the output dir: the generic S3 sync sweeps up
    # everything there, and these are wire artifacts that stay out of it.
    with tempfile.TemporaryDirectory(
        prefix=f"{MERGE_TMP_PREFIX}{scan.pk}-"
    ) as tmp:
        tmp_dir = Path(tmp)
        with fitz.open() as merged:
            for index, job in enumerate(convert_jobs):
                if job.shard_index != index:
                    raise BitonalMergeError(
                        f"scan {scan.pk} shard sequence breaks at position "
                        f"{index}: job {job.pk} covers shard "
                        f"{job.shard_index}"
                    )
                if not job.result_key:
                    raise BitonalMergeError(
                        f"scan {scan.pk} shard {index} has no result key"
                    )
                local = tmp_dir / f"{index:04d}.pdf"
                s3_sync.download_object(job.result_key, local)
                with fitz.open(str(local)) as part:
                    expected = job.input_manifest.get("page_count")
                    if expected is not None and part.page_count != expected:
                        raise BitonalMergeError(
                            f"scan {scan.pk} shard {index} converted to "
                            f"{part.page_count} page(s), the shard has "
                            f"{expected}"
                        )
                    merged.insert_pdf(part)

            if expected_total is not None and (
                merged.page_count != expected_total
            ):
                raise BitonalMergeError(
                    f"scan {scan.pk} merged to {merged.page_count} page(s), "
                    f"the original has {expected_total}"
                )
            # deflate compresses object streams, not the CCITT image
            # streams, so the scan data is copied through untouched.
            merged.save(str(destination), garbage=3, deflate=True)

    size_mb = destination.stat().st_size / 1024 / 1024
    logger.info(
        "Merged %d converted shard(s) into %s for scan %s (%.1f MB) in %.1fs",
        len(convert_jobs),
        destination.name,
        scan.pk,
        size_mb,
        time.monotonic() - started,
    )
    s3_sync.upload_file_to_s3(scan, s3_sync.PIPELINE_INPUT_NAME)
    return destination


def _park(scan, status: str, message: str) -> bool:
    """Move a scan out of AWAITING, if it is still there.

    Guarded because an admin action or a cancel may have moved the scan
    while its jobs ran, and stomping that undoes a human decision.

    ERROR is deliberately not a start status here. It is terminal: a
    pass able to move a scan out of it would re-run this stage's merge
    on every tick against a failure that is not going to change, and
    the merge downloads every shard result to do it. The way back from
    ERROR is a person, through an admin re-queue.

    :param scan: The scan to move.
    :param status: Status to write.
    :param message: Progress message to write.
    :returns: Whether this writer won the row.
    :rtype: bool
    """
    updated = Scan.objects.filter(pk=scan.pk, status=Status.AWAITING).update(
        status=status,
        progress_message=message[:255],
        progress_current=0,
        progress_total=0,
    )
    if not updated:
        logger.info(
            "scan %s left AWAITING before its conversion was applied; "
            "leaving its jobs alone",
            scan.pk,
        )
    return bool(updated)


def _finish_scan(scan, convert_jobs: list[ExternalJob]) -> bool:
    """Merge one scan's results, park it, and consume its rows.

    The park precedes the CONSUMED write and both sit in one
    transaction: a row counts as applied only once its scan has actually
    moved, or a lost race leaves the results unreadable and the scan
    waiting for work nothing will redo.

    :param scan: The scan whose conversion finished.
    :param convert_jobs: The live run's rows.
    :returns: Whether this writer moved the scan.
    :rtype: bool
    """
    merge_convert_results(scan, convert_jobs)

    with transaction.atomic():
        if not _park(scan, Status.AWAITING_VALIDATION, CONVERTED_MESSAGE):
            return False
        now = timezone.now()
        _log_stage_duration(scan, convert_jobs, now)
        ExternalJob.objects.filter(
            pk__in=[job.pk for job in convert_jobs],
            status=JobStatus.COMPLETED,
        ).update(status=JobStatus.CONSUMED, consumed_at=now)

    # The merged file is in S3, so the shard results are now duplicate
    # bytes nothing reads. Best effort, and after the commit: an orphan
    # costs storage, a premature delete costs the ability to re-merge.
    s3_sync.delete_objects(
        [job.result_key for job in convert_jobs if job.result_key]
    )
    return True


def _log_stage_duration(scan, convert_jobs: list[ExternalJob], now) -> None:
    """Log what the whole conversion cost this scan, end to end.

    Measured from when the rows were created -- the moment the stage
    began -- to the scan leaving AWAITING, so it covers queueing,
    conversion, retries and the merge. The pages-per-second figure is
    the one comparable against the retired in-process pass.

    :param scan: The scan leaving AWAITING.
    :param convert_jobs: The live run's rows.
    :param now: The time the scan was parked.
    :return: None.
    """
    started = min(
        (job.date_created for job in convert_jobs if job.date_created),
        default=None,
    )
    if started is None:
        return
    elapsed = (now - started).total_seconds()
    pages = sum(
        job.input_manifest.get("page_count") or 0 for job in convert_jobs
    )
    rate = f", {pages / elapsed:.1f} pages/s" if pages and elapsed > 0 else ""
    logger.info(
        "Bitonal stage done for scan %s: %d shard(s), %d page(s) in %.1fs%s",
        scan.pk,
        len(convert_jobs),
        pages,
        elapsed,
        rate,
    )


def finish_ready_scans() -> int:
    """Apply every finished conversion and report failures.

    Runs after the confirm pass. A scan is finished when no row of its
    live run is in flight or waiting to be submitted:

    - all rows completed -> merge, park in ``AWAITING_VALIDATION``.
    - any row dead (failed, cancelled, expired) -> ``ERROR``, naming the
      code. A row only reaches FAILED once its attempts are spent, since
      a retryable failure goes back to PENDING from the submit pass, so
      a dead row here means the shard is genuinely over. Loud rather
      than a silent degrade: bitonal is display-only, so the volume
      stays usable, but while this rolls out volume by volume a failure
      should be seen. An admin re-queue starts a fresh run.

    Only scans in AWAITING are examined. ERROR is terminal, and a pass
    that re-examined it would re-run the merge -- and its download of
    every shard result -- on every tick of a failure that is not going
    to change. A volume that lands in ERROR waits for a person: an admin
    re-queue.

    :returns: How many scans were moved out of AWAITING.
    :rtype: int
    """
    scan_ids = (
        Scan.objects.filter(
            status=Status.AWAITING,
            jobs__stage=JobStage.CONVERT,
            jobs__engine=JobEngine.BITONAL,
            jobs__provider=JobProvider.DOCTOR,
        )
        .values_list("pk", flat=True)
        .distinct()
    )
    finished = 0
    for scan in Scan.objects.filter(pk__in=list(scan_ids)).select_related(
        "reporter"
    ):
        convert_jobs = live_convert_jobs(scan)
        if not convert_jobs:
            continue
        if any(
            job.status in IN_FLIGHT_JOB_STATUSES
            or job.status == JobStatus.PENDING
            for job in convert_jobs
        ):
            continue

        if all(job.status == JobStatus.CONSUMED for job in convert_jobs):
            # Already merged, and the merge deletes the results it
            # consumed, so re-merging would 404 on all of them. Only
            # reachable if something put a converted scan back into
            # AWAITING: move it on rather than error it over objects we
            # deleted on purpose.
            logger.info(
                "scan %s was already converted; parking it without re-merging",
                scan.pk,
            )
            if _park(scan, Status.AWAITING_VALIDATION, CONVERTED_MESSAGE):
                finished += 1
                # Out of AWAITING, S3 holds everything: the local tree
                # is now a cache the daemon does not need (#215). Same
                # at every exit below; a lost park means someone else
                # owns the scan, so its files stay.
                s3_sync.release_local_processing(scan)
            continue

        dead = [job for job in convert_jobs if job.status in DEAD_JOB_STATUSES]
        if dead:
            first = dead[0]
            logger.error(
                "Conversion failed for scan %s: %d of %d shard(s) dead, "
                "first %s (%s)",
                scan.pk,
                len(dead),
                len(convert_jobs),
                first.status,
                first.error_code,
            )
            if _park(
                scan,
                Status.ERROR,
                f"Bitonal conversion failed on {len(dead)} of "
                f"{len(convert_jobs)} shard(s): "
                f"{first.error_code or first.status}. "
                f"{first.error_message}",
            ):
                finished += 1
                s3_sync.release_local_processing(scan)
            continue

        try:
            if _finish_scan(scan, convert_jobs):
                finished += 1
                s3_sync.release_local_processing(scan)
        except Exception as exc:
            # The merge is the one local step, so a failure is not the
            # provider's fault and retries no job.
            logger.exception("Merging conversion for scan %s failed", scan.pk)
            if _park(
                scan,
                Status.ERROR,
                f"Could not assemble the converted volume: {exc}",
            ):
                finished += 1
                s3_sync.release_local_processing(scan)

    return finished
