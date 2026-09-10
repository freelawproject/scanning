"""Background processing pipelines and business logic.

Functions in this module run outside the request/response cycle, in the
daemon process.  They must NOT import Django HTTP machinery
(HttpResponse, render, redirect, etc.).
"""

import contextlib
import json
import logging
import os
import re
import shutil
import time
import traceback
from collections import Counter
from pathlib import Path

import django
import fitz
from blackletter.api import (
    build_redactions as bl_build_redactions,
)
from blackletter.bl_warm import rows_are_bl_warm
from blackletter.margins import compute_margin_rects
from blackletter.models import (
    BBox,
    Label,
    Page,
)
from blackletter.models import (
    Detection as BLDetection,
)
from blackletter.models import (
    Document as BLDoc,
)
from blackletter.process import compute_redaction_rects
from blackletter.scanner import (
    _pair_opinions,
    snap_document_columns,
    snap_text_columns_to_ink,
)
from blackletter.validate import (
    _split_in_out_of_range,
    build_analysis,
    build_issues,
)
from django.conf import settings
from django.db.models import Case, F, Value, When

from scanning import boundaries
from scanning.models import (
    BUSY_STATUSES,
    DEAD_JOB_STATUSES,
    REVIEW2_CHECKS,
    REVIEW_STATUSES,
    ApplyRun,
    CheckName,
    Detection,
    ExternalJob,
    Issue,
    JobStage,
    JobStatus,
    OpinionScan,
    OpinionStatus,
    PageEdit,
    QueuedAction,
    QueueStatus,
    Scan,
    Stage,
    Status,
    Volume,
)
from scanning.utils import (
    ensure_output_dir,
    find_processing_pdf,
    has_s3_credentials,
    processing_pdf_path,
)

logger = logging.getLogger(__name__)


class _StageLog:
    """Handle yielded by :func:`_log_stage`.

    Set ``done_detail`` inside the ``with`` block to append extra
    context (e.g. an output file size) after the elapsed time on the
    stage's completion line.
    """

    def __init__(self):
        self.done_detail = ""


@contextlib.contextmanager
def _log_stage(label: str, detail: str = ""):
    """Log a processing stage's start and elapsed time at INFO level.

    Emits ``"<label> <detail>..."`` on entry and ``"<label> done
    (<n>s)"`` on successful exit, giving every heavy pipeline stage the
    same timed log line the OCR step already had. The ``done`` line is
    skipped if the wrapped block raises, matching the original OCR
    behavior (elapsed time is only reported for a stage that finished).

    :param label: Short stage name, reused verbatim in the "done" line.
    :param detail: Optional extra context for the start line (e.g. a
        page count), omitted from the "done" line to keep it terse.
    :yields: A :class:`_StageLog` whose ``done_detail`` can be set to
        append (after ", ") to the completion line, e.g. an output size.
    """
    logger.info("%s...", f"{label} {detail}".rstrip())
    t0 = time.monotonic()
    stage = _StageLog()
    yield stage
    suffix = f", {stage.done_detail}" if stage.done_detail else ""
    logger.info("%s done (%.1fs%s)", label, time.monotonic() - t0, suffix)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _volume_fully_uploaded(volume: Volume, scan_count: int) -> bool:
    """Whether a Volume has all its expected scans uploaded.

    Coverage takes precedence when ``expected_start_page`` /
    ``expected_end_page`` are set, then ``expected_parts`` as a count
    fallback. When neither expectation is recorded we cannot decide
    that a volume is "done", so we return False and leave it to a
    curator to mark via the manual dropdown.

    Trade-off in the count fallback: when only ``expected_parts`` is
    set, this returns True as soon as the scan count meets the
    expectation, without verifying that the scans actually span
    distinct page ranges. Two scans covering the same pages will look
    "fully uploaded". The accurate path is the coverage check above;
    set ``expected_start_page`` / ``expected_end_page`` when possible.

    :param volume: The Volume to check.
    :param scan_count: Pre-computed count of scans on the volume.
    :returns: True when every expected scan is present.
    :rtype: bool
    """
    if volume.expected_start_page and volume.expected_end_page:
        return volume.is_fully_covered
    if volume.expected_parts:
        return scan_count >= volume.expected_parts
    return False


def _compute_volume_queue_status(volume: Volume, scans: list[Scan]) -> str:
    """Derive the queue status a Volume should have from its scans.

    :param volume: The Volume to inspect.
    :param scans: The Volume's current scans.
    :returns: A ``QueueStatus`` value.
    :rtype: str
    """
    if not scans:
        if volume.assigned_to_id:
            return QueueStatus.ASSIGNED
        return QueueStatus.NEEDS_SCANNING
    all_approved = all(s.status == Status.APPROVED for s in scans)
    fully_uploaded = _volume_fully_uploaded(volume, len(scans))
    if all_approved and fully_uploaded:
        return QueueStatus.COMPLETE
    if fully_uploaded:
        return QueueStatus.SCANNED
    return QueueStatus.SCANNING


def refresh_volume_queue_status(volume: Volume) -> None:
    """Recompute and persist a Volume's queue_status from its scans.

    Manual ``UNAVAILABLE`` is preserved (it's a curator decision, not
    derivable from observable state). All other states are recomputed
    so callers don't have to track transitions individually.

    :param volume: The Volume to refresh in place.
    """
    if volume.queue_status == QueueStatus.UNAVAILABLE:
        return
    scans = list(volume.scans.all())
    new_status = _compute_volume_queue_status(volume, scans)
    if new_status != volume.queue_status:
        volume.queue_status = new_status
        volume.save(update_fields=["queue_status"])


def refresh_volume_queue_status_for_scan(scan: Scan) -> None:
    """Convenience: refresh the parent Volume's queue_status from a Scan.

    No-op when the scan is not attached to a Volume. Looks the volume
    up by ``volume_obj_id`` rather than going through the FK descriptor
    so the intent (and the resulting query) are explicit.

    :param scan: The scan whose parent volume should be refreshed.
    """
    if not scan.volume_obj_id:
        return
    volume = Volume.objects.get(pk=scan.volume_obj_id)
    refresh_volume_queue_status(volume)


def flag_partial_volume(scan: Scan) -> None:
    """Mark the parent volume as split when the scan carries a part label.

    A part label *is* the statement that the volume comes in more than
    one piece, so the flag is derived from it instead of from a separate
    control (issue #178). The rule matches ``import_scanlist``, the only
    other writer.

    The write is set-only. Parts arrive as separate uploads, and a later
    part with no label must not un-flag a volume that genuinely has
    parts.

    :param scan: The scan whose original PDF is now stored.
    :return: None.
    """
    if not scan.volume_obj_id or not scan.part_label:
        return
    volume = Volume.objects.get(pk=scan.volume_obj_id)
    if not volume.is_partial:
        volume.is_partial = True
        volume.save(update_fields=["is_partial"])


def apply_upload_action(scan: Scan, action: str) -> None:
    """Apply the uploader's post-upload action to a stored scan.

    Request-free core shared by the web confirm flow
    (``_finalize_uploaded_scan``) and the recovery path. Always refreshes
    the parent volume's queue status, and derives its ``is_partial``
    flag (#178) -- both are volume facts that follow from an upload
    landing, so they belong on this shared path rather than in the view.
    A recovered upload is a completed upload and must flag its volume
    exactly as a confirmed one does.

    Both actions queue the pipeline, because the stage the choice used
    to select -- page-number validation -- is disconnected (#173). What
    the pipeline does today is shard and convert, and an ``upload_only``
    volume needs both as much as any other: without them it has no
    preview and the viewer streams the multi-GB original. Only the
    message differs until validation returns (#149).

    :param scan: The scan whose original PDF is now stored.
    :param action: The chosen ``UploadAction`` value.
    :return: None.
    """
    from scanning.models import QueuedAction, Stage, Status, UploadAction

    scan.status = Status.QUEUED
    scan.stage = Stage.VALIDATE
    scan.queued_action = QueuedAction.FULL_PIPELINE
    scan.progress_message = (
        "Queued for processing..."
        if action == UploadAction.UPLOAD_VALIDATE
        else "Queued for conversion..."
    )
    scan.save()
    flag_partial_volume(scan)
    refresh_volume_queue_status_for_scan(scan)


def recover_pending_upload(pending) -> bool:
    """Recover a completed-but-unconfirmed direct-to-S3 upload.

    A ``PendingUpload`` whose object landed in S3 (the presigned POST
    finished) but was never confirmed -- ``confirm_scan_upload`` never ran
    because the container died, the tab closed, or the request 500'd. If
    the object exists and is a valid PDF, do what confirm would have done:
    attach it to the fileless scan, replay the stored action, and delete
    the pending row.

    :param pending: The ``PendingUpload`` to try to recover.
    :returns: True if recovered; False if there's nothing to recover (the
        object is missing/invalid, or the scan is already linked -- e.g. a
        re-upload whose object belongs to the confirmed original).
    :rtype: bool
    """
    from scanning import s3_sync

    scan = pending.scan
    if scan is None or scan.original_pdf.name:
        return False

    original_name = Path(pending.s3_key).name
    if not s3_sync.verify_uploaded_object(scan, original_name):
        return False

    scan.original_pdf.name = original_name
    scan.save(update_fields=["original_pdf"])
    apply_upload_action(scan, pending.action)
    pending.delete()
    logger.info(
        "Recovered unconfirmed upload for scan %s from s3 key %s",
        scan.pk,
        pending.s3_key,
    )
    return True


def _update_progress(
    scan_pk: int,
    message: str,
    current: int | None = None,
    total: int | None = None,
    **kwargs,
) -> None:
    """Update scan progress fields.

    :param scan_pk: Primary key of the scan to update.
    :param message: Human-readable progress message (truncated to 255 chars).
    :param current: Current step number, if applicable.
    :param total: Total step count, if applicable.
    :param kwargs: Additional Scan fields to update.
    """
    updates = {"progress_message": message[:255]}
    if current is not None:
        updates["progress_current"] = current
    if total is not None:
        updates["progress_total"] = total
    updates.update(kwargs)
    Scan.objects.filter(pk=scan_pk).update(**updates)


def _handle_pipeline_exception(
    scan_pk: int, exc: Exception, context: str = "pipeline"
) -> None:
    """Classify a pipeline-level exception and update the scan status.

    - ``RunpodTransientError``: increment ``retry_count`` and re-queue
      up to ``settings.RUNPOD_MAX_TRANSIENT_RETRIES`` attempts, then
      escalate to ERROR.
    - All other exceptions: immediately mark the scan as ERROR.

    All transitions are guarded by ``status=Status.PROCESSING`` so we
    never stomp a scan that a concurrent process (stale-recovery, admin
    action, second daemon replica) has already moved out of PROCESSING.

    The two terminal outcomes (ERROR, ERROR_MAX_RETRIES) release the
    scan's local processing files: no retry will read them, so keeping
    them spends disk on a failure only an admin re-queue -- which
    re-downloads from S3 -- can revive (#215). The re-queue outcome
    keeps its files on purpose, so the retry does not pay the download
    again.

    :param scan_pk: Primary key of the scan that failed.
    :param exc: The exception that was raised.
    :param context: Short label for log messages (e.g. ``"pipeline"``,
        ``"validate"``, ``"detect"``).
    """
    from scanning import s3_sync
    from scanning.runpod_client import RunpodTransientError

    if isinstance(exc, RunpodTransientError):
        max_retries = settings.RUNPOD_MAX_TRANSIENT_RETRIES
        err_msg = f"Max retries exceeded: {str(exc)[:200]}"
        retry_msg = f"Retrying: {str(exc)[:200]}"

        # Single atomic UPDATE: increment retry_count and branch on the
        # PRE-increment value using CASE/WHEN. This avoids a TOCTOU race
        # between reading the count and deciding. The PROCESSING guard
        # ensures we never stomp a scan already moved by another process.
        #
        # Pre-increment `retry_count >= max_retries` is equivalent to
        # post-increment `retry_count > max_retries`.
        updated = Scan.objects.filter(
            pk=scan_pk, status=Status.PROCESSING
        ).update(
            retry_count=F("retry_count") + 1,
            status=Case(
                When(
                    retry_count__gte=max_retries,
                    then=Value(Status.ERROR_MAX_RETRIES),
                ),
                default=Value(Status.QUEUED),
            ),
            progress_message=Case(
                When(retry_count__gte=max_retries, then=Value(err_msg)),
                default=Value(retry_msg),
            ),
        )

        if not updated:
            logger.warning(
                "[%s] scan %s status update skipped: row no longer in PROCESSING",
                context,
                scan_pk,
            )
            return

        # Read back post-update state for logging only (non-critical).
        try:
            scan = Scan.objects.only("retry_count", "status").get(pk=scan_pk)
        except Scan.DoesNotExist:
            return
        if scan.status == Status.ERROR_MAX_RETRIES:
            # Error level (raises a Sentry event): the transient retries
            # are exhausted, so this is no longer self-healing and likely
            # signals a real RunPod capacity shortage worth investigating.
            logger.error(
                "[%s] scan %s transient RunPod failure, max retries (%d) exceeded: %s",
                context,
                scan_pk,
                max_retries,
                exc,
            )
            # Terminal: no retry reads the local files. The read-back,
            # not the update count, says which status the CASE wrote --
            # and a status another writer moved here since is terminal
            # all the same.
            s3_sync.release_local_processing(scan)
        else:
            logger.warning(
                "[%s] scan %s transient RunPod failure (%d/%d), re-queuing: %s",
                context,
                scan_pk,
                scan.retry_count,
                max_retries,
                exc,
            )
        return

    logger.exception("[%s] scan %s failed: %s", context, scan_pk, exc)
    updated = Scan.objects.filter(pk=scan_pk, status=Status.PROCESSING).update(
        status=Status.ERROR,
        progress_message=str(exc)[:255],
    )
    if not updated:
        logger.warning(
            "[%s] scan %s ERROR mark skipped: row no longer in PROCESSING",
            context,
            scan_pk,
        )
        return
    scan = Scan.objects.filter(pk=scan_pk).only("pk").first()
    if scan is not None:
        s3_sync.release_local_processing(scan)


def _ensure_shards(scan: "Scan") -> dict | None:
    """Compute (or reuse) the scan's shard set, with stage logging.

    Failures propagate to the caller's ``_handle_pipeline_exception``
    like any other pipeline stage. ``ensure_shards`` leaves no manifest
    behind on failure, so a re-queued scan retries the sharding.

    S3 and transport errors are re-raised as ``RunpodTransientError``:
    this stage is the pipeline's only bulk multi-GB upload, and a
    connection reset or a 503 partway through is exactly as retriable as
    a RunPod worker dying, so it gets the same retry-and-re-queue
    treatment instead of an immediate ERROR that needs a manual
    re-queue. Sharding's own failures (``ShardingError``) stay terminal.

    :param scan: The scan to shard.
    :return: The manifest describing the committed shard set, or None
        when sharding is disabled -- in which case there are no shards
        for an external job to read, so the caller must not create any.
    :rtype: dict | None
    :raises RunpodTransientError: On an S3/transport failure, so the
        caller re-queues the scan instead of marking it ERROR.
    """
    from boto3.exceptions import Boto3Error
    from botocore.exceptions import BotoCoreError, ClientError

    from scanning import sharding
    from scanning.runpod_client import RunpodTransientError

    with _log_stage("Sharding"):
        try:
            return sharding.ensure_shards(scan)
        except (BotoCoreError, Boto3Error, ClientError) as exc:
            raise RunpodTransientError(
                f"S3 error while sharding scan {scan.pk}: {exc}"
            ) from exc


def _snap_text_columns_to_ink(scan_pk: int, pdf_path: str) -> int:
    """Widen this scan's ``TEXT_COLUMN`` detections onto the text they clip.

    Thin wrapper over :func:`blackletter.scanner.snap_text_columns_to_ink`,
    which does the measuring. What is app-specific is the persistence: the
    corrected boxes are written back to the ``Detection`` rows, so the
    viewer overlay and the detection entries show what the geometry
    actually used, and no later step has to re-measure the ink to agree
    with it.

    Only the x-bounds move, so header and footer geometry is untouched.

    :param scan_pk: Primary key of the scan.
    :param pdf_path: The PDF the detections were measured against.
    :return: Number of detections widened.
    """
    by_page: dict[int, list] = {}
    for det in Detection.objects.filter(
        scan_id=scan_pk, active=True, label="TEXT_COLUMN"
    ).order_by("page_index", "x0"):
        by_page.setdefault(det.page_index, []).append(det)
    if not by_page:
        return 0

    changed = []
    with fitz.open(str(pdf_path)) as doc:
        for page_index, columns in sorted(by_page.items()):
            if page_index >= doc.page_count:
                continue
            fitz_page = doc[page_index]
            page = Page(
                index=page_index,
                pdf_width=fitz_page.rect.width,
                pdf_height=fitz_page.rect.height,
                img_width=columns[0].img_width or 1,
                img_height=columns[0].img_height or 1,
            )
            page.detections = [
                BLDetection(
                    bbox=BBox(x1=d.x0, y1=d.y0, x2=d.x1, y2=d.y1),
                    label=Label.TEXT_COLUMN,
                    confidence=d.confidence,
                    page_index=page_index,
                )
                for d in columns
            ]
            if not snap_text_columns_to_ink(fitz_page, page):
                continue
            # strict: the snap rewrites boxes in place and must hand back
            # one per column. A length change would mean it reordered or
            # dropped one, and pairing the survivors by position would
            # silently write a column's new bounds onto its neighbour.
            for det, snapped in zip(columns, page.detections, strict=True):
                new_x0 = round(snapped.bbox.x1, 1)
                new_x1 = round(snapped.bbox.x2, 1)
                if abs(new_x0 - det.x0) < 1 and abs(new_x1 - det.x1) < 1:
                    continue
                det.x0 = new_x0
                det.x1 = new_x1
                changed.append(det)

    if changed:
        Detection.objects.bulk_update(changed, ["x0", "x1"])
    return len(changed)


#: A printed page range as ``Scan.ocr_results`` stores it. The en dash
#: is what a reporter prints; ``views_process._page_number_value``
#: normalizes a curator's entry, and ``page_numbers`` the model's
#: reading, so a stored value carries a hyphen. Both are read here.
_PAGE_RANGE_RE = re.compile(r"^(\d{1,4})\s*[–\-]\s*(\d{1,4})$")


def printed_page_span(value, kind) -> tuple[int, int | None] | None:
    """Parse one stored printed number into ``(start, end)``.

    A range page like ``"677-685"`` gives ``(677, 685)``; a single page
    like ``"677"`` gives ``(677, None)``; a blank or unparsable value
    gives ``None``. One parser for the two readers of a stored number:
    :func:`_page_number_lookup` over ``Scan.ocr_results`` (the
    original's space) and ``apply.page_number_lookup`` over a run's
    printed-page map (the final space, #269).

    :param value: The stored number, as ``detected`` or ``printed``.
    :param kind: The stored type, ``"range"`` or anything else.
    :returns: The span, or ``None``.
    :rtype: tuple[int, int | None] | None
    """
    if not value:
        return None
    if kind == "range":
        m = _PAGE_RANGE_RE.match(str(value))
        if m:
            return (int(m.group(1)), int(m.group(2)))
        return None
    try:
        return (int(value), None)
    except (ValueError, TypeError):
        return None


def _page_number_lookup(scan: "Scan", printed: dict | None = None) -> dict:
    """Build ``{page_index: (page_number, page_number_end)}`` for a scan.

    The numbers :func:`detection_entries` puts beside each box, so they
    must be in the space the boxes are in. Since #269 the boxes of a
    volume whose redactions are measured against its standing apply run
    are final pages, so the lookup comes from the run's printed-page map
    then; every other volume -- a legacy one, a compute in progress, a
    run not yet measured -- reads ``Scan.ocr_results``, the original's
    space. Six callers write that file, so the rule resolves here and
    not in each of them.

    The resolver reads S3 once per call. Three of the callers are the
    box-edit endpoints of review 2, whose write to the database is
    already committed when they reach this, so a failed read must not
    fail the request: it is logged, and the lookup falls back to
    ``Scan.ocr_results`` for that one write, which the next write
    corrects.

    :param scan: The scan.
    :param printed: The run's printed-page map when the caller already
        loaded it (the compute does, once); resolved here otherwise.
    :return: Mapping of 0-based page index to a page span.
    """
    from scanning import apply, review_states, yolo

    if printed is None:
        run = review_states.final_run(scan)
        if run is not None and yolo.redactions_current(
            yolo.live_detect_jobs(scan), run
        ):
            try:
                printed = apply.load_printed_pages(scan, run)
            except apply.ApplyError:
                logger.exception(
                    "scan %s: the printed pages of %s did not load; "
                    "the detection entries carry the original's numbers this once",
                    scan.pk,
                    run.label,
                )
    if printed is not None:
        return apply.page_number_lookup(printed)
    lookup = {}
    for r in scan.ocr_results:
        span = printed_page_span(r.get("detected"), r.get("type"))
        if span is not None:
            lookup[r["pdf_page"] - 1] = span
    return lookup


def _push_processing_files_to_s3(scan_pk: int) -> bool:
    """Upload a scan's intermediate processing files to S3.

    Wraps ``s3_sync.upload_processing_files`` with an exception guard so
    a failed S3 call never fails the pipeline that just succeeded. Logs
    a distinct ``logger.error`` when creds are missing in prod so the
    Sentry alert makes the root cause obvious (otherwise
    ``upload_processing_files`` silently returns 0).

    :param scan_pk: Primary key of the scan whose files to upload.
    :returns: Whether the push ran without an error. Callers that want
        to delete the local files afterwards must not do so on False --
        the local tree may hold bytes S3 never received.
    :rtype: bool
    """
    from scanning import s3_sync
    from scanning.utils import has_s3_credentials

    if (
        not settings.DEVELOPMENT
        and not getattr(settings, "TESTING", False)
        and not has_s3_credentials()
    ):
        logger.error(
            "Skipping S3 push for scan %s: AWS credentials not configured",
            scan_pk,
        )
        return False
    try:
        scan = Scan.objects.get(pk=scan_pk)
        s3_sync.upload_processing_files(scan)
    except Exception:
        logger.exception(
            "Failed to push processing files to S3 for scan %s", scan_pk
        )
        return False
    return True


def _pull_processing_files_from_s3(scan_pk: int) -> None:
    """Download a scan's processing files from S3 to its local output dir.

    Used at the start of pipelines that expect existing files (reprocess,
    detect, generate_files, validate). No-op when S3 sync is disabled or
    the prefix has nothing (e.g. first-time full pipeline). Safely
    swallows exceptions so a missing/offline S3 doesn't abort the run.

    :param scan_pk: Primary key of the scan to pull files for.
    :return: None.
    """
    try:
        from scanning import s3_sync

        scan = Scan.objects.get(pk=scan_pk)
        s3_sync.download_processing_files(scan)
    except Exception:
        logger.exception(
            "Failed to pull processing files from S3 for scan %s", scan_pk
        )


def detection_entries(scan_pk: int, page_numbers: dict | None = None) -> list:
    """The live detections of a scan, in the shape blackletter reads.

    One dict per row, with the printed page number beside each box.
    This is what ``detections.json`` used to hold; since #240 the rows
    are the only store, and every reader (the pairing, the redaction
    geometry, step 3) takes this list in memory. Nothing writes it to
    disk or to S3.

    :param scan_pk: Primary key of the scan.
    :param page_numbers: The ``{page_index: (start, end)}`` lookup, when
        the caller holds it (the redaction compute loads the run's
        printed pages once, #269). Resolved by
        :func:`_page_number_lookup` otherwise.
    :return: The detection dicts, empty when the scan has none.
    """
    if page_numbers is None:
        # The one reader of the scan row; the geometry passes ``{}``
        # and reads none.
        page_numbers = _page_number_lookup(Scan.objects.get(pk=scan_pk))

    # ``pk`` last, so two boxes at one height list in one order on every
    # database: a reader that indexes the list must not depend on a tie.
    all_saved = (
        Detection.objects.live()
        .filter(scan_id=scan_pk)
        .order_by("page_index", "y0", "x0", "pk")
    )
    det_data = []
    for d in all_saved:
        entry = {
            # The row pk, so the compute can name the caption and the
            # key rows of each opinion exactly (#240 PR C). blackletter
            # reads the keys it knows and ignores this one.
            "id": d.pk,
            "page_index": d.page_index,
            "label": d.label,
            "label_id": d.label_id,
            "confidence": d.confidence,
            "bbox": [d.x0, d.y0, d.x1, d.y1],
            "img_width": d.img_width,
            "img_height": d.img_height,
            "model_count": d.model_count,
        }
        if d.found_by and d.model_name != Detection.ModelName.MANUAL:
            # Load-bearing, not decoration: the confidence gates are per
            # model family since blackletter #73, and
            # ``rows_are_bl_warm`` reads this provenance off the list.
            # ``blackletter.api.pair`` reads it from here, so a list
            # without it pairs a bl-warm volume on the legacy gates.
            # A hand-added detection carries none, and must not: it
            # would read as a second model family and send the whole
            # volume back to those gates. Rows written before #196 carry
            # a "manual" claim, so the row kind is the guard, not the
            # field.
            entry["found_by"] = d.found_by
        pn = page_numbers.get(d.page_index)
        if pn:
            entry["page_number"] = pn[0]
            if pn[1] is not None:
                entry["page_number_end"] = pn[1]
        det_data.append(entry)
    return det_data


def _build_document_from_detections(
    scan: "Scan", det_data: list, pdf_path: str
) -> "BLDoc":
    """Build a blackletter Document from detection data and a PDF.

    :param scan: The Scan instance for reporter/volume metadata.
    :param det_data: List of detection dicts (:func:`detection_entries`).
    :param pdf_path: Path to the PDF to read page dimensions from.
    :return: The constructed Document.
    """
    document, _ids = _build_document_with_ids(scan, det_data, pdf_path)
    return document


def _build_document_with_ids(
    scan: "Scan", det_data: list, pdf_path: str
) -> tuple["BLDoc", dict[int, int]]:
    """Build the Document, and remember which row each detection came from.

    The pairing (#240 PR C) returns blackletter's own detection objects,
    which carry no row pk, so the compute maps each object back to its
    ``Detection`` row by ``id()`` and writes the caption and the key FKs
    exactly, with no match by rounded coordinates. Only an entry that
    carries ``"id"`` (:func:`detection_entries` puts it there) is in the
    map.

    :param scan: The Scan instance for reporter/volume metadata.
    :param det_data: List of detection dicts (:func:`detection_entries`).
    :param pdf_path: Path to the PDF to read page dimensions from.
    :return: The Document and ``{id(bl_detection): Detection pk}``.
    """
    row_ids: dict[int, int] = {}
    with fitz.open(str(pdf_path)) as src_pdf:
        pages_data = {}
        for entry in det_data:
            pi = entry["page_index"]
            if pi not in pages_data:
                pages_data[pi] = {
                    "img_width": entry.get("img_width", 1),
                    "img_height": entry.get("img_height", 1),
                    "detections": [],
                }
            pages_data[pi]["detections"].append(entry)
        pages = []
        for pi in sorted(pages_data.keys()):
            pd = pages_data[pi]
            if pi < src_pdf.page_count:
                pw, ph = src_pdf[pi].rect.width, src_pdf[pi].rect.height
            else:
                pw, ph = 612.0, 792.0
            page = Page(
                index=pi,
                pdf_width=pw,
                pdf_height=ph,
                img_width=pd["img_width"],
                img_height=pd["img_height"],
            )
            for d in pd["detections"]:
                b = d.get("bbox", [0, 0, 1, 1])
                detection = BLDetection(
                    bbox=BBox(x1=b[0], y1=b[1], x2=b[2], y2=b[3]),
                    label=Label(d["label_id"]),
                    confidence=d["confidence"],
                    page_index=pi,
                )
                page.detections.append(detection)
                if d.get("id") is not None:
                    row_ids[id(detection)] = d["id"]
            pages.append(page)

    scan_obj = scan if isinstance(scan, Scan) else Scan.objects.get(pk=scan)
    document = BLDoc(
        pdf_path=str(pdf_path),
        pages=pages,
        reporter=scan_obj.reporter.short_name or "",
        volume=str(scan_obj.volume) or "",
        first_page=scan_obj.start_page or 1,
        ocr_applied=True,
        # Which confidence gates every consumer of this document reads
        # (``label_confidence(label, document.bl_warm)``). bl-warm and
        # the legacy trio score the same labels differently, so this is
        # not a label: on one volume of 1364 pages the wrong family
        # keeps 13 editorial notes bl-warm drops, and drops 8 header
        # boxes it keeps. The provenance travels on each row's
        # ``found_by``, and blackletter reads it (blackletter #73).
        bl_warm=rows_are_bl_warm(det_data),
    )
    return document, row_ids


def _snapped_document(
    scan: "Scan", pdf_path: str, page_numbers: dict | None = None
) -> tuple["BLDoc", dict[int, int], list]:
    """Build the corrected document the compute pairs and measures on.

    Every blackletter entry point corrects the column boxes before
    reading them, and the margin strips of this same scan are computed
    from corrected ones. Skipping it here is how a hand-added
    TEXT_COLUMN (which reaches the DB exactly as the reviewer drew it)
    would give the headnote rects a different column to the margins on
    the same page.

    :param scan: The scan.
    :param pdf_path: The PDF the detections were measured against.
    :param page_numbers: See :func:`detection_entries`.
    :return: The document, the ``{id(bl_detection): pk}`` map, and the
        entries it was built from (empty when the scan has none).
    """
    det_data = detection_entries(scan.pk, page_numbers=page_numbers)
    if not det_data:
        return BLDoc(pdf_path=str(pdf_path), pages=[]), {}, []
    document, row_ids = _build_document_with_ids(scan, det_data, pdf_path)
    snap_document_columns(document)
    return document, row_ids, det_data


def _measure_redaction_rects(document: "BLDoc", pairs: list | None) -> list:
    """Measure the redaction rects on the snapped document. Nothing is written.

    The compute builds the document once (``_snapped_document``), pairs
    it once (``boundaries.write_computed``) and hands both here, so the
    rects are measured from the same pairs the boundary rows were
    written from (#240 PR C). A caller with no pairs gets them paired
    here. ``redactions.write_computed`` converts the answer to points and
    writes the rows (#240 PR B).

    :param document: The snapped document.
    :param pairs: The ``(caption, key)`` pairs of that document, or None
        to pair here.
    :return: blackletter's rects, in pixels of the render, one entry per
        page; empty for a document with no pages.
    """
    if not document.pages:
        return []
    with _log_stage("Redaction rects"):
        opinions = _pair_opinions(document) if pairs is None else pairs
        # ``ocr_applied`` is set on the Document, so blackletter measures
        # this geometry from the page ink itself (see
        # ``scanner._measure_from_ink``), and finishes each headnote rect
        # against the page detections: snapped to its column box, cut at the
        # headnote boundaries inside it, then grown onto adjoining ink. The
        # app ran those three passes itself until blackletter #68 moved them
        # where every consumer gets them.
        return compute_redaction_rects(document, opinions, skip_doctr=True)


def _measure_margin_rects(pdf_path: str, document: "BLDoc") -> list:
    """Measure the margin strips against the pages of ``document``.

    The strips are pulled back off any real detection they would cover, so
    key icons, captions and other content near a page edge survive. That
    happens inside :func:`blackletter.margins.compute_margin_rects`, which
    also uses the detections to tighten the content box. Nothing is
    written.

    :param pdf_path: Path to the PDF to compute margins for.
    :param document: The snapped document the rects were measured from.
    :return: blackletter's strips, in points; empty for a document with
        no pages, because without detections the bounds would come from
        the page's marks alone, and bleed-through at a page edge would
        suppress that page's top strip: a worse answer than none.
    """
    if not document.pages:
        return []
    with _log_stage("Margin rects"):
        return compute_margin_rects(str(pdf_path), pages=document.pages)


def _build_combined_redactions(scan_pk: int) -> Path:
    """Write ``redactions.json`` from the rows, for blackletter's ``generate``.

    All coordinates in the output are in PDF points. The opinion
    filenames come from :func:`blackletter.api.build_redactions`, which
    used to convert the pixel rects too; since #240 the rects are
    ``Redaction`` rows in points (``redactions.visible_by_page``), so the
    pages of the payload are written from them and the conversion is
    gone with the blob. blackletter gets no pages: it read them only to
    scale pixel rects, and both rect lists are empty.

    :param scan_pk: Primary key of the scan.
    :return: Path to the generated redactions.json.
    """
    from scanning import redactions

    scan = Scan.objects.get(pk=scan_pk)
    output_dir = Path(scan.output_dir)

    combined = bl_build_redactions(
        [],
        [],
        [],
        boundaries.viewer_payload(scan, live_only=True),
        reporter=scan.reporter.short_name or "",
        volume=str(scan.volume) or "",
    )
    combined["pages"] = {
        str(entry["page_index"]): [
            {
                "x0": r["x0"],
                "y0": r["y0"],
                "x1": r["x1"],
                "y1": r["y1"],
                "fill": r["fill"],
                "type": r["rect_type"],
            }
            for r in entry["rects"]
        ]
        for entry in redactions.visible_by_page(scan)
    }

    out_path = output_dir / "redactions.json"
    out_path.write_text(json.dumps(combined))
    n_rects = sum(len(v) for v in combined["pages"].values())
    logger.info(
        "Combined redactions: %s pages, %s rects, %s opinions",
        len(combined["pages"]),
        n_rects,
        len(combined["opinions"]),
    )
    return out_path


# ---------------------------------------------------------------------------
# Recalculate (rebuild issues without re-running OCR)
# ---------------------------------------------------------------------------


def _expected_range(scan: "Scan") -> tuple[int | None, int | None]:
    """Return the expected first/last printed page numbers for a scan.

    Uses the scanner-entered ``start_page``/``end_page`` fields, the same
    source the validate stage uses, so a recheck sees the same range the
    original run did. Deriving it from the uploaded filename instead is
    both unreliable (the ``.original.pdf`` suffix defeats the
    reporter.volume.first.last parser) and needs a local copy of the PDF,
    which production does not keep around between requests.

    Both values are returned together or not at all: every downstream
    range check needs the pair.

    :param scan: The Scan to read the range from.
    :returns: ``(exp_start, exp_end)``, or ``(None, None)`` when the scan
        has no end page recorded.
    :rtype: tuple[int | None, int | None]
    """
    if not scan.end_page:
        return None, None
    return scan.start_page or 1, scan.end_page


#: Prefix :mod:`scanning.page_numbers` stamps on the ``zone`` of every
#: entry it reads off a dots.mocr run (``dots-header``,
#: ``dots-footer``). It is what tells a new-pipeline page number from a
#: legacy PaddleOCR one.
DOTS_ZONE_PREFIX = "dots-"


def has_legacy_ocr(scan: "Scan") -> bool:
    """Return whether a scan's page numbers came from the retired OCR.

    The legacy validate stage (PaddleOCR over a page-number crop) no
    longer runs anywhere (#173), so a recompute over its readings can
    only reproduce them. Review 1 says so instead of pretending to redo
    the work (#151).

    Two signals answer it, because neither alone is enough. A ``dots-``
    zone proves the new stage wrote the entry, but a volume dots read
    with no number on any page carries none. An ``ANALYZE`` job row
    proves the new stage ran at all, and it outlives a recompute. A
    scan with no readings at all is not legacy: it has nothing to
    recompute either way, and the caller handles that first.

    A dead row does not count: a run that failed, was cancelled, or
    expired delivered nothing, so the scan's readings are still the
    retired stage's, and "run OCR again" is the right advice for it.

    :param scan: The scan to classify.
    :returns: ``True`` when the readings are the retired stage's.
    :rtype: bool
    """
    if not scan.ocr_results:
        return False
    if any(
        (entry.get("zone") or "").startswith(DOTS_ZONE_PREFIX)
        for entry in scan.ocr_results
    ):
        return False
    return (
        not ExternalJob.objects.filter(scan=scan, stage=JobStage.ANALYZE)
        .exclude(status__in=DEAD_JOB_STATUSES)
        .exists()
    )


def _is_manual_read(result: dict) -> bool:
    """Return whether a per-page page number was entered by hand.

    ``assign_page`` stamps ``zone`` and ``ocr`` with ``"manual"`` when a
    curator types a page number in step 1.

    :param result: One per-page entry from ``Scan.ocr_results``.
    :returns: ``True`` when the number came from a person, not a model.
    :rtype: bool
    """
    return "manual" in (result.get("ocr"), result.get("zone"))


def _note_curator_ranges(issues: list[dict], ocr_results: list[dict]) -> None:
    """Answer a curator's own page range with a note, not a warning.

    ``build_issues`` writes one ``page_range`` card per range page:
    "Verify this is expected." That is the right question to ask of a
    machine reading. It is noise for a range the curator typed a
    minute earlier with the page in front of them (#233). The card
    stays, because the range is a fact about the volume that the next
    reader must see, and it stays at ``info``, worded as the record it
    is.

    The card names the range's first page, which is the key this
    matches on: the address space of ``page_range`` is the printed
    number, not the physical page (``models.PHYSICAL_PAGE_CHECKS``).

    :param issues: The rebuilt issue dicts, edited in place.
    :param ocr_results: The per-page entries the issues were built
        from, curator numbers already overlaid.
    :returns: None.
    """
    typed: dict[int, tuple[int, str]] = {}
    for entry in ocr_results:
        if entry.get("type") != "range" or not _is_manual_read(entry):
            continue
        match = _PAGE_RANGE_RE.match(str(entry.get("detected") or ""))
        if match:
            first, last = int(match.group(1)), int(match.group(2))
            typed[first] = (entry["pdf_page"], f"{first}-{last}")
    if not typed:
        return
    for issue in issues:
        if issue["check_name"] != CheckName.PAGE_RANGE:
            continue
        named = typed.get(issue["page_number"])
        if named is None:
            continue
        pdf_page, label = named
        issue["severity"] = Issue.Severity.INFO
        issue["message"] = (
            f"PDF page {pdf_page} carries the printed page range "
            f"{label}, entered at review 1."
        )


def _project_trailing_gap(
    result: dict, analysis: dict, exp_end: int | None
) -> None:
    """Draw one placeholder for a page range missing at the end (#256).

    ``build_issues`` collapses a run of more than 6 missing pages into
    one ``large_gap`` card and drops every page of the run from
    ``actually_missing``, which is the only source of a ``missing``
    entry in ``page_map``. So a volume that stops 41 pages before its
    recorded last page carries a card and nothing a reviewer can act
    on: no upload form, and no button to ask a scanner for the leaves
    (#249).

    The collapse is right **inside** a volume, where the pages are
    almost always in the book with a number nobody read. It is wrong at
    the end, where the expected last page says the pages should be
    there and the volume stops before them. So this appends one
    placeholder for the trailing run, and only for that one.

    **One placeholder per gap, because the gap is the address.** An
    insert and an INSERT repair request are both addressed by
    ``anchor_pdf_page``, the physical page the leaf follows
    (#214/#249), and one open row may exist per address. So a
    placeholder per missing number would put 41 buttons on one row.
    The label is the range instead, with the hyphen every reader of a
    range parses (#233).

    The threshold is deliberately not repeated here: the run qualifies
    when its first page did **not** survive into
    ``result["missing_pages"]``, which is the proof that the collapse
    took it. A retune upstream can therefore not give one page two
    placeholders.

    :param result: What ``build_issues`` returned, edited in place.
    :param analysis: What ``build_analysis`` returned. Its
        ``missing_pages`` still holds the collapsed pages.
    :param exp_end: The scan's recorded last printed page. Without one
        there is no trailing gap to find (issue #209).
    :returns: None.
    """
    missing = analysis.get("missing_pages") or []
    all_nums = analysis.get("all_nums") or []
    # The run must reach the recorded last page, or it is not the end
    # of the volume. Every number the volume shows is out of
    # ``missing``, so a run that ends there also starts above the last
    # number read.
    if not exp_end or not missing or not all_nums or missing[-1] != exp_end:
        return
    first = exp_end
    for page in reversed(missing[:-1]):
        if page != first - 1:
            break
        first = page
    if first in set(result["missing_pages"]):
        # Not collapsed: blackletter drew one placeholder per page.
        return

    result["page_map"].append(
        {
            "type": "missing",
            "logical_number": f"{first}-{exp_end}",
            "missing_range": [first, exp_end],
        }
    )

    # The card of that run, reworded: it reads "likely an OCR misread
    # rather than genuinely missing pages", which says nothing about
    # what a reviewer does next. The key does not move, so a dismissal
    # still matches the card. A card that is not there changes nothing:
    # the placeholder stands on the run alone.
    for issue in result["issues"]:
        if (
            issue["check_name"] == CheckName.LARGE_GAP
            and issue["page_number"] == first
        ):
            issue["message"] = (
                f"Pages {first}\u2013{exp_end} "
                f"({exp_end - first + 1} pages) are not in this volume. "
                f"The last page number read is {max(all_nums)}. If the "
                f"book has these pages, ask a scanner for them at the "
                f"placeholder at the end of the volume. If the pages "
                f"are there with a number nobody read, correct a page "
                f"number and recompute."
            )


def rebuild_page_map(scan: "Scan") -> None:
    """Rebuild ``page_map`` and ``missing_pages`` from current ocr_results.

    Recomputes the page-sequence projection (duplicate flags and
    missing-page placeholders) without re-running OCR, opening the PDF, or
    touching Issue records, so dismissed issues are preserved. Used after a
    manual page-number edit so the viewer reflects the change immediately;
    Issue cards are refreshed separately by a full Recheck.

    :param scan: The Scan whose page_map to rebuild.
    """
    from scanning import page_edits

    ocr_results = scan.ocr_results
    if not ocr_results:
        return
    # The curator's own numbers live on PageEdit rows (#214), and this
    # blob is the cache of the run plus those rows. Overlaying here is
    # what makes the viewer show a number the moment it is typed.
    ocr_results, _stale = page_edits.overlay_page_numbers(scan, ocr_results)
    exp_start, exp_end = _expected_range(scan)
    analysis = build_analysis(ocr_results, exp_start, exp_end)
    result = build_issues(
        analysis, scan.page_count, exp_start=exp_start, exp_end=exp_end
    )
    # Both builders of ``page_map`` must agree, or a page-number edit
    # would drop the placeholder from under the reviewer (#256).
    _project_trailing_gap(result, analysis, exp_end)
    scan.ocr_results = ocr_results
    scan.page_map = result["page_map"]
    scan.missing_pages = result["missing_pages"]
    # Only the fields this function owns: the approve button (#151) and
    # the collect tick both write the status of the same row.
    scan.save(update_fields=["ocr_results", "page_map", "missing_pages"])


def recalculate_issues(scan: "Scan") -> None:
    """Rebuild issues from scan.ocr_results without re-running OCR.

    Runs off stored data only, so it works on a web pod that never
    downloaded the scan's processing files from S3.

    :param scan: The Scan instance to recalculate issues for.
    """
    from scanning import page_edits

    ocr_results = scan.ocr_results
    if not ocr_results:
        return

    # The curator outranks the model, so the overlay comes first: every
    # step below -- the offset heuristic, the sequence analysis, the
    # issue list -- must see the numbers a person typed (#214).
    ocr_results, stale_edits = page_edits.overlay_page_numbers(
        scan, ocr_results
    )
    scan.ocr_results = ocr_results

    exp_start, exp_end = _expected_range(scan)

    out_of_range, seen_nums = _split_in_out_of_range(
        ocr_results, exp_start, exp_end
    )

    auto_corrected = []
    if out_of_range and seen_nums:
        in_range_by_page = {
            p: num for num, pages in seen_nums.items() for p in pages
        }
        in_range_sorted = sorted(in_range_by_page.items())
        offsets = {}
        for r in out_of_range:
            if _is_manual_read(r):
                # A curator typed this number, so it outranks the
                # offset heuristic. It is still reported as an
                # out-of-range reading below, just not overwritten.
                continue
            p, detected = r["pdf_page"], int(r["detected"])
            before = [(pp, n) for pp, n in in_range_sorted if pp < p]
            after = [(pp, n) for pp, n in in_range_sorted if pp > p]
            if before and after:
                pp_b, n_b = before[-1]
                pp_a, n_a = after[0]
                expected = round(
                    n_b + (n_a - n_b) / max(pp_a - pp_b, 1) * (p - pp_b)
                )
            elif before:
                pp_b, n_b = before[-1]
                expected = n_b + (p - pp_b)
            elif after:
                pp_a, n_a = after[0]
                expected = n_a - (pp_a - p)
            else:
                continue
            offsets[p] = (detected, expected, expected - detected)

        if offsets:
            offset_vals = [v[2] for v in offsets.values()]
            modal_offset, modal_count = Counter(offset_vals).most_common(1)[0]
            if modal_count >= len(offset_vals) * 0.5 and modal_offset != 0:
                to_fix = {
                    p for p, (d, e, o) in offsets.items() if o == modal_offset
                }
                new_results = []
                for r in ocr_results:
                    if r["pdf_page"] in to_fix and r.get("detected"):
                        old_val = r["detected"]
                        r = dict(r)
                        r["detected"] = str(int(old_val) + modal_offset)
                        auto_corrected.append(
                            (r["pdf_page"], old_val, r["detected"])
                        )
                    new_results.append(r)
                ocr_results = new_results
                scan.ocr_results = ocr_results

    analysis = build_analysis(ocr_results, exp_start, exp_end)

    result = build_issues(
        analysis, scan.page_count, exp_start=exp_start, exp_end=exp_end
    )

    # Before the cards scanning appends below, and before the
    # dismissal filter, so a dismissal matches the card as it reads.
    _note_curator_ranges(result["issues"], ocr_results)

    # A range missing at the end of the volume gets a placeholder, so a
    # reviewer can upload the pages or ask a scanner for them (#256).
    _project_trailing_gap(result, analysis, exp_end)

    # Every open edit this volume cannot take, not only the page
    # numbers: a delete or an insert made against another original is
    # refused by the readers that act on it, so the curator has to hear
    # about it here or the decision just disappears.
    result["issues"].extend(
        page_edits.stale_edit_issues(
            stale_edits
            + [
                edit
                for edit in page_edits.stale_edits(scan)
                if edit.kind != PageEdit.Kind.SET_NUMBER
            ]
        )
    )

    for pdf_page, old_val, new_val in auto_corrected:
        result["issues"].append(
            {
                "page_number": pdf_page,
                "check_name": "auto_corrected",
                "severity": "warning",
                "message": (
                    f"PDF page {pdf_page}: OCR read '{old_val}', "
                    f"auto-corrected to '{new_val}' "
                    f"based on surrounding page numbers."
                ),
            }
        )

    # A page the curator marked for deletion answers its own cards
    # (#255): the cards are built from ocr_results, which still holds
    # the page, so the "No page number detected" card of a page on its
    # way out came back on every press of the recompute button.
    result["issues"] = page_edits.drop_deleted_pages(scan, result["issues"])

    # A dismissal is a curator decision, so it is a PageEdit row, not
    # the absence of an Issue row: the rebuild below gives every issue
    # it writes a new primary key, and a deleted row came back on the
    # next press of the recompute button (#214).
    #
    # Last, so that it covers the whole list. The two kinds appended
    # above are exactly the ones a curator meets again and again --
    # the apply pass rewrites ocr_results from the run on every tick,
    # so the offset heuristic re-corrects and re-warns each time, and a
    # stale edit stays stale until someone acts on it.
    result["issues"] = page_edits.drop_dismissed(scan, result["issues"])

    # Refresh page_count opportunistically: the PDF has not changed since
    # validation, and in production the local copy is usually absent
    # because rechecks run on a web pod that never pulled it from S3.
    # Every failure here is survivable -- an absent copy, a partial one,
    # a file that is not a PDF at all -- and the stored count stands.
    # The recompute is a read of stored data, and it must not 500 over
    # a file it does not need (#153/#151).
    try:
        pdf_path = scan.pdf_path
    except FileNotFoundError:
        pdf_path = None
    if pdf_path:
        try:
            with fitz.open(pdf_path) as pdf_fitz:
                scan.page_count = len(pdf_fitz)
        except Exception:
            logger.warning(
                "recalculate_issues: scan %s: could not read %s for a "
                "page count; keeping the stored %s",
                scan.pk,
                pdf_path,
                scan.page_count,
            )

    scan.page_map = result["page_map"]
    scan.missing_pages = result["missing_pages"]
    scan.s3_uploaded = False
    scan.progress_message = "Done"
    # Never a full save: the approve button (#151) writes
    # PAGE_COMPLETENESS_REVIEW_DONE concurrently, and a full save off
    # this instance would silently write a stale status over it. Save
    # only the fields this function owns, and decide the status on the
    # row as it is in the DB, not on the copy in memory.
    scan.save(
        update_fields=[
            "ocr_results",
            "page_count",
            "page_map",
            "missing_pages",
            "s3_uploaded",
            "progress_message",
        ]
    )
    # A recheck must not move a scan between review states (#154/#263):
    # a scan in a review state keeps it. The recompute button of step 1
    # is reachable from a volume already in review 2, so the two #263
    # states are here for the same reason as the two #154 ones -- a
    # rebuild of the page-number issues says nothing about the
    # redactions. The write to PENDING_REVIEW stays for the legacy rows
    # that already carry it.
    Scan.objects.filter(pk=scan.pk).exclude(status__in=REVIEW_STATUSES).update(
        status=Status.PENDING_REVIEW
    )
    scan.refresh_from_db(fields=["status"])

    # The findings of review 2 (#240 PR D) are derived from the
    # detection, boundary and redaction rows, not from the page numbers,
    # so a recheck of review 1 leaves them alone: ``findings.rebuild``
    # is their only writer.
    scan.issues.exclude(check_name__in=REVIEW2_CHECKS).delete()
    Issue.objects.bulk_create(
        [Issue(scan=scan, **i) for i in result["issues"]]
    )


def run_compute_issues(scan: "Scan", result_key: str) -> bool:
    """Read page numbers off a glued dots.mocr run and rebuild Issues.

    The apply step of issues #149/#204, called by
    ``dots_mocr.apply_ready_runs`` on the collect tick. Deliberately
    not daemon-queued work (#212): the scan never transits
    QUEUED/PROCESSING, so it stays in the review flow while its issues
    recompute, there is no claim for a cancel to race, and no
    scan-wide retry budget is spent. Failures raise to the caller,
    whose per-run bookkeeping bounds the retries.

    The one status write is a single compare-and-swap on the review
    edge: ``AWAITING_VALIDATION`` or the legacy ``PENDING_REVIEW``
    moves to ``READY_FOR_PAGE_COMPLETENESS_REVIEW``; a scan already
    READY is a recompute and keeps its status. A scan the edge cannot
    take (cancelled or moved between the caller's read and here) is
    left alone: its ``ocr_results`` were refreshed -- idempotent data
    -- but no Issues are rebuilt and no status moves. A legacy
    ``CANCELLED`` row is one such scan (#219 deleted the view that
    wrote it, so only historical rows hold it), and
    :func:`recalculate_issues` never full-saves: it writes its data
    fields with ``update_fields`` and decides the status with one
    conditional DB update, so it can neither revive a cancelled scan
    nor write a stale READY over a concurrent approval (#151).

    :param scan: The scan whose live run is fully glued.
    :param result_key: S3 key of the run's glued volume JSON.
    :returns: Whether the apply completed. False means the scan left
        the eligible statuses mid-apply and was left alone.
    :rtype: bool
    """
    from scanning import page_edits, page_numbers, s3_sync

    started = time.monotonic()
    document = s3_sync.download_json_object(result_key)
    results = page_numbers.ocr_results_from_volume(document)
    # Overlay before the save, not only inside recalculate_issues: a
    # scan that fails the edge below keeps this blob until its next
    # recompute, and the viewer would show the model's reading over a
    # number a curator had already typed (#214).
    results, _stale = page_edits.overlay_page_numbers(scan, results)
    scan.ocr_results = results
    scan.save(update_fields=["ocr_results"])

    if scan.status != Status.READY_FOR_PAGE_COMPLETENESS_REVIEW:
        # The review edge: the apply is the last prerequisite of
        # review 1, since the caller only sees scans whose conversion
        # already parked and whose OCR run is glued.
        edged = Scan.objects.filter(
            pk=scan.pk,
            status__in=(
                Status.AWAITING_VALIDATION,
                Status.PENDING_REVIEW,
            ),
        ).update(status=Status.READY_FOR_PAGE_COMPLETENESS_REVIEW)
        if not edged:
            logger.info(
                "compute_issues: scan %s left the eligible statuses "
                "mid-apply; its issues were not rebuilt",
                scan.pk,
            )
            return False

    scan.status = Status.READY_FOR_PAGE_COMPLETENESS_REVIEW
    recalculate_issues(scan)

    logger.info(
        "compute_issues: scan %s: %d page(s), %d without a number, "
        "%d issue(s), in %.1fs",
        scan.pk,
        len(results),
        sum(1 for entry in results if not entry["detected"]),
        scan.issues.count(),
        time.monotonic() - started,
    )
    return True


# ---------------------------------------------------------------------------
# Full pipeline (upload and walk away)
# ---------------------------------------------------------------------------


def _import_detections(
    scan_pk: int,
    detections: list,
    run: "ApplyRun | None" = None,
    detect_run: int | None = None,
) -> int:
    """Replace a scan's model detections with the ones just merged.

    The rows a curator made by hand (``model_name`` ``MANUAL``) are
    kept, and every other row goes. A hand-drawn box costs curator time
    and it addresses the same physical page of the same original, so a
    re-run of a deterministic model is no reason to throw it away.
    What the re-run does replace is every box the model itself drew.

    Each new row carries its **address** (#240): the source page the
    glued document names beside the box (``detections.source_of_entry``),
    the run whose page space ``page_index`` is in, and the detection run
    that found it. Then every standing ``DetectionDecision`` of the scan
    is resolved onto the new rows by that address
    (``detections.resolve``), which is how a curator's approvals and
    deletions survive the import that used to lose them, and every kept
    hand-drawn row is moved to its page in the new space
    (``detections.relocate_manual_rows``), or a box drawn before a
    deletion would paint one page out after it.

    ``found_by`` is copied onto each row, because the confidence gates
    are per model family (``label_confidence(label, bl_warm)``), and
    that field is where every reader looks for the family.

    :param scan_pk: Primary key of the scan.
    :param detections: The merged document's ``detections`` list, in
        the page coordinates of ``run`` (or of the original).
    :param run: The apply run whose final space the list is in; None
        for the original's space.
    :param detect_run: The ``ExternalJob.run`` of the detection run.
    :return: How many rows were created.
    """
    from scanning import detections as decisions

    scan = Scan.objects.get(pk=scan_pk)
    kept = Detection.objects.filter(
        scan_id=scan_pk, model_name=Detection.ModelName.MANUAL
    ).count()
    Detection.objects.filter(scan_id=scan_pk).exclude(
        model_name=Detection.ModelName.MANUAL
    ).delete()

    # One decision for the whole run, off blackletter's own reader,
    # rather than a guess per row: the run either came from bl-warm or
    # it did not.
    model_name = (
        Detection.ModelName.BL_WARM if rows_are_bl_warm(detections) else ""
    )
    rows = []
    for entry in detections:
        bbox = entry.get("bbox") or [0, 0, 1, 1]
        edit_id, source_page = decisions.source_of_entry(entry)
        rows.append(
            Detection(
                scan_id=scan_pk,
                page_index=entry["page_index"],
                label=entry["label"],
                label_id=entry["label_id"],
                confidence=entry["confidence"],
                x0=bbox[0],
                y0=bbox[1],
                x1=bbox[2],
                y1=bbox[3],
                img_width=entry.get("img_width", 0),
                img_height=entry.get("img_height", 0),
                model_name=model_name,
                model_count=entry.get("model_count", 1),
                found_by=entry.get("found_by") or [],
                active=True,
                source_edit_id=edit_id,
                source_page=source_page,
                source_fingerprint=scan.source_fingerprint or "",
                apply_run=run,
                detect_run=detect_run,
            )
        )
    Detection.objects.bulk_create(rows, batch_size=1000)
    landed, stale = decisions.resolve(scan)
    moved = 0
    if run is not None and kept:
        # The kept hand-drawn rows follow the new page space, by their
        # address; the model rows arrived in it.
        moved, _unplaced = decisions.relocate_manual_rows(scan, run)
    logger.info(
        "Imported %d detection(s) for scan %s (%d hand-made row(s) kept, "
        "%d moved; %d decision(s) landed, %d stale)",
        len(rows),
        scan_pk,
        kept,
        moved,
        landed,
        len(stale),
    )
    return len(rows)


_UNSET_RUN = object()


def geometry_pdf_path(scan: "Scan", run=_UNSET_RUN) -> str:
    """Return the PDF the redaction geometry of ``scan`` is measured on.

    The one rule for "which PDF the geometry reads" (#269). With a
    complete standing apply run it is the run's bitonal copy
    (``ApplyRun.bitonal_key``), pulled to its local mirror by
    ``apply.local_copy``; the final page space, where the imported
    detections live. Without one it is :func:`processing_pdf_path`, the
    review-1 copy of the original's space: a legacy volume, or a scan
    the apply has not reached. An identity run's key is the volume
    ``bitonal.pdf`` itself, so the two answers name the same file; for
    a 1-bit original it is the original, which ``processing_pdf_path``
    also falls back to.

    :param scan: The scan.
    :param run: The standing run when the caller has it (``None`` for
        none); ``review_states.final_run`` is asked otherwise.
    :returns: A local path.
    :rtype: str
    """
    from scanning import apply, review_states

    if run is _UNSET_RUN:
        run = review_states.final_run(scan)
    if run is None:
        return processing_pdf_path(scan)
    return str(apply.local_copy(scan, run.bitonal_key))


def run_compute_redactions(scan_pk: int) -> None:
    """Turn a merged detection run into the geometry review 2 reads.

    The apply step of issue #196, dispatched by ``process_next_scan``
    from ``QueuedAction.COMPUTE_REDACTIONS``, which
    ``yolo.queue_ready_runs`` writes on the collect tick.

    **Queued work, unlike the page-number apply of #204.** Three of its
    steps read the page ink, so they render every page of the volume:
    83 seconds for 1364 pages, measured, plus the pull of the bitonal
    copy. The collect tick runs every 15 seconds on a serial scheduler
    (#156), so this belongs where the other long stages already run.

    **The model read the original; the geometry reads the corrected
    bitonal copy.** bl-warm collapses on 1-bit pages, so detection fans
    out over the original shards (#167/#194), while the rects are
    stamped on the bitonal copy and must be measured against its ink.
    Since #269 both are in the page space of the standing apply run
    (#224): the detections come from the run's glued document
    (``ApplyRun.detections_key``), which the apply moved through its
    page map, and the copy is the run's ``bitonal_key``. A volume with
    no page edit has an identity run, whose keys alias the review-1
    artifacts, so nothing changes for it.

    **The detections are imported once per run**, and the run is the
    apply run. Rows measured against the standing run are a
    *recompute*, which a curator asks for after they add or delete a
    box: it keeps every row in the database and measures again from
    those. Importing again there would throw the curator's edits away,
    which is the whole reason they pressed the button. Rows measured
    against a run this one supersedes (a reopen and a second approval)
    describe pages the volume no longer shows, so they are imported
    again; the carry of a curator's edits across runs is #241.

    The scan goes back to a review whatever happens, and this function
    raises nothing. On success that review is review 2
    (``READY_FOR_REDACTION_REVIEW``, #263), because this pass is
    usually the last of its three conditions; on every failure it is
    the review-1 approval, because a curator must not be sent to judge
    geometry that was not measured. An ``ERROR`` status on
    an approved volume would need an admin re-queue, and that re-queue
    runs the whole pipeline again. A failure is counted on the run
    instead (``yolo.record_apply_failure``), which bounds the retries.

    :param scan_pk: Primary key of the scan to compute redactions for.
    :return: None.
    """
    from scanning import (
        apply,
        findings,
        redactions,
        review_states,
        s3_sync,
        yolo,
    )
    from scanning import detections as decisions

    django.db.connections.close_all()
    scan = Scan.objects.get(pk=scan_pk)
    rows = yolo.live_detect_jobs(scan)
    merged = bool(rows) and all(
        row.status == JobStatus.CONSUMED for row in rows
    )
    has_rows = Detection.objects.filter(scan_id=scan_pk, active=True).exists()
    # A volume with detections but no detection *run* is a legacy one,
    # and its step 2 lives in PENDING_REVIEW, since the #154 and #263
    # states describe a review it never had. Every other volume came
    # from review 1, dead run or not.
    legacy = has_rows and not rows

    def park() -> str:
        """Return where the scan belongs, read at the moment of the park.

        Derived at each exit rather than chosen once, so that one rule
        answers for every one of them (#263). A first apply that fails
        writes no ``applied_at``, so the rule gives review 1 back --
        nobody may be sent to judge geometry that was never measured. A
        *recompute* that fails under the same apply run keeps the stamp
        of the run that worked, so the rule gives review 2 back, and the
        failure message stands where the curator can read it. A park
        chosen up front sent that second case to review 1, and
        ``promote_ready_scans`` wrote its own message over the failure
        one tick later. A compute that fails under a *new* apply run has
        no stamp for it (#269), so the rule gives review 1 back: the old
        geometry describes pages the volume no longer shows.

        :returns: The status to park the scan in.
        :rtype: str
        """
        if legacy:
            return Status.PENDING_REVIEW
        if review_states.redaction_review_ready(scan, rows):
            return Status.READY_FOR_REDACTION_REVIEW
        return Status.PAGE_COMPLETENESS_REVIEW_DONE

    if not merged and not has_rows:
        # Nothing to measure: no merged run, and no detections from an
        # earlier one. Park the scan back rather than fail it -- the
        # queue claim is the only thing that was wrong.
        logger.warning(
            "compute_redactions: scan %s has neither a merged detection "
            "run nor detections",
            scan_pk,
        )
        _park_after_redactions(
            scan_pk,
            "No detections to work from. The detection run has not "
            "reached this volume yet.",
            park(),
        )
        return

    if merged:
        yolo.record_apply_start(rows)
    # After the claim is dropped, never before: a lost claim must not
    # keep ``queued_at``. A merged run reads the corrected volume of the
    # standing apply run (#269). The queue gate checked that it exists;
    # this is the backstop for an admin supersede between the queue and
    # the claim, and it spends no attempt, like a closed gate does in
    # the apply. The rule parks the scan in review 1, and the next
    # complete run queues it again.
    run = review_states.final_run(scan) if merged else None
    if merged and run is None:
        logger.warning(
            "compute_redactions: scan %s has no complete apply run; the "
            "corrected volume is not built",
            scan_pk,
        )
        _park_after_redactions(
            scan_pk,
            "The corrected volume is not built yet. The redactions are "
            "computed when it is.",
            park(),
        )
        return

    # A merged run whose rows are not measured against the standing
    # apply run brings its detections in. Any other case measures what
    # the database already holds: a recompute after a curator's edit, or
    # a legacy volume whose rows the old pipeline wrote.
    importing = merged and not yolo.redactions_current(rows, run)
    started = time.monotonic()
    try:
        detections = []
        page_numbers = None
        if merged:
            _update_progress(scan_pk, "Reading the corrected volume...")
            page_numbers = _page_number_lookup(
                scan, apply.load_printed_pages(scan, run)
            )
        if importing:
            _update_progress(
                scan_pk, "Reading the detections of this volume..."
            )
            document = apply.load_detections_document(scan, run)
            detections = document.get("detections") or []
            if not detections:
                raise RuntimeError(
                    f"scan {scan_pk}: the detection run holds no "
                    f"detections in the corrected volume"
                )

        if merged:
            # One key, not the prefix: the whole-prefix pull lands the
            # multi-GB original, which nothing here reads.
            scan.refresh_from_db()
            ensure_output_dir(scan)
            pdf_path = geometry_pdf_path(scan, run)
        else:
            _pull_processing_files_from_s3(scan_pk)
            scan.refresh_from_db()
            ensure_output_dir(scan)
            pdf_path = geometry_pdf_path(scan, None)

        if importing:
            with _log_stage("Import detections"):
                _import_detections(
                    scan_pk, detections, run=run, detect_run=rows[0].run
                )

            # Only after an import: the correction converges, so it is
            # a no-op once it is stored, and it renders every page.
            _update_progress(scan_pk, "Measuring the text columns...")
            with _log_stage("Column correction"):
                _snap_text_columns_to_ink(scan_pk, pdf_path)

        # One corrected document for the pairing and the geometry, and
        # one pairing (#240 PR C): the boundaries are rows written from
        # blackletter's own pairs, with the caption and the key rows
        # named exactly, and the rects are measured from the same pairs.
        document, row_ids, det_data = _snapped_document(
            scan, pdf_path, page_numbers
        )
        _update_progress(scan_pk, "Pairing the opinions...")
        with _log_stage("Opinion pairing"):
            opinions = boundaries.write_computed(
                scan,
                document,
                row_ids,
                run,
                rows[0].run if merged else None,
            )

        _update_progress(scan_pk, "Computing the redactions...")
        rects = _measure_redaction_rects(document, opinions)

        _update_progress(scan_pk, "Measuring the page margins...")
        margins = _measure_margin_rects(pdf_path, document)

        # The rows are the store (#240 PR B): the computed rows are
        # written again, the standing dismissals land on them, and the
        # human rows follow the page space the geometry was measured in.
        _update_progress(scan_pk, "Writing the redactions...")
        with _log_stage("Redaction rows"):
            written = redactions.write_computed(
                scan,
                run,
                rows[0].run if merged else None,
                rects,
                margins,
                document.pages,
            )
            landed, stale = redactions.resolve(scan)
            if run is not None:
                decisions.relocate_rows(
                    redactions.human_rows(scan),
                    scan,
                    run,
                    "human redaction(s)",
                )
        logger.info(
            "compute_redactions: scan %s: %d redaction row(s) written, "
            "%d dismissal(s) landed, %d stale",
            scan_pk,
            written,
            landed,
            len(stale),
        )
        # The findings of review 2 are rows too (#240 PR D), derived
        # from the rows just written. The run is passed, not read: the
        # ledger stamp that ``detections.measured_run`` reads is written
        # after the park, below.
        _update_progress(scan_pk, "Writing the findings...")
        with _log_stage("Review-2 findings"):
            open_findings = findings.rebuild(scan, run=run if merged else None)
        logger.info(
            "compute_redactions: scan %s: %d review-2 finding(s) open",
            scan_pk,
            open_findings,
        )
        # Nothing to push: every output of this pass is a row (#240),
        # and the files under ``output_dir`` are the copies it pulled.
    except Exception as exc:
        logger.exception(
            "compute_redactions: scan %s failed after %.1fs",
            scan_pk,
            time.monotonic() - started,
        )
        gave_up = merged and yolo.record_apply_failure(scan, rows, exc)
        # The message must match what happens next: the trigger retries
        # a counted failure on its next tick, and skips a run that spent
        # its attempts. A promise of a retry the last failure does not
        # get would have the curator waiting for nothing.
        if gave_up:
            message = (
                "The redaction computation failed "
                f"{yolo.APPLY_MAX_ATTEMPTS} times and stopped. The "
                "detections are safe. Ask a staff member to look at it."
            )
        elif merged:
            message = (
                "The redaction computation failed. The detections are "
                "safe, and it runs again by itself."
            )
        else:
            message = (
                "The redaction computation failed. The detections are "
                "safe; ask a staff member to look at it."
            )
        _park_after_redactions(scan_pk, message, park())
        return

    if merged:
        # Before the park, never after: the review-2 edge in ``park``
        # reads this very stamp to decide that the redactions are
        # computed (#263), so the other order would park a finished
        # volume one tick short of its own review. The apply is usually
        # the last of the three conditions, and it takes the scan over
        # the edge itself rather than leaving it to
        # ``review_states.promote_ready_scans``: the viewer reloads the
        # page the moment the scan parks, and a park in the approved
        # status would show the curator a step 2 whose approve button
        # appears a tick later, from nothing they did.
        yolo.record_apply_success(rows, run)
    _park_after_redactions(
        scan_pk, "Detection review is ready: check the redactions.", park()
    )
    logger.info(
        "compute_redactions: scan %s: %d detection(s), %d opinion(s), "
        "%d page(s) with rects, %d page(s) with margins, in %.1fs",
        scan_pk,
        len(det_data or []),
        len(opinions),
        len(rects),
        len(margins),
        time.monotonic() - started,
    )
    if s3_sync.s3_active():
        s3_sync.release_local_processing(scan)


#: The statuses a redaction computation may be queued from: the
#: approved volume of the new flow, the volume already in review 2
#: (#263) -- a recompute starts from the review the curator is looking
#: at -- and the legacy ``PENDING_REVIEW`` rows, which reached review 2
#: before the #154 statuses existed. A busy scan is refused: it holds a
#: claim already. ``REDACTION_REVIEW_DONE`` is deliberately absent: a
#: closed review is not recomputed under the person who closed it, and
#: the way back is the admin re-queue.
REDACTION_COMPUTE_STATUSES = (
    Status.PAGE_COMPLETENESS_REVIEW_DONE,
    Status.READY_FOR_REDACTION_REVIEW,
    Status.PENDING_REVIEW,
)


def queue_redaction_compute(scan: "Scan") -> tuple[bool, str]:
    """Ask the daemon to compute this scan's redaction geometry.

    The request path never does this work itself. It renders every page
    of the volume, which is 83 seconds for 1364 pages and beyond what
    an ingress gives a request. So the two review-2 buttons write a
    status here and return, and the viewer's progress poll reloads the
    page when the daemon is done -- the same route every other long
    stage takes.

    The compare-and-swap is what keeps a second press from stacking:
    the scan leaves the eligible statuses on the first one.

    :param scan: The scan to compute for.
    :returns: Whether it was queued, and a message for the curator.
    :rtype: tuple[bool, str]
    """
    queued = Scan.objects.filter(
        pk=scan.pk, status__in=REDACTION_COMPUTE_STATUSES
    ).update(
        status=Status.QUEUED,
        queued_action=QueuedAction.COMPUTE_REDACTIONS,
        progress_message="The redactions are queued for computation.",
        progress_current=0,
        progress_total=0,
    )
    if queued:
        return True, (
            "Queued. The redactions are computed on the server, and this "
            "page reloads when they are ready."
        )
    if scan.status in BUSY_STATUSES:
        return False, "This volume is busy. Wait for the current work."
    return False, (
        "This volume is not in a state that can compute redactions."
    )


def _park_after_redactions(
    scan_pk: int, message: str, status: str = None
) -> None:
    """Return a scan to the review it was taken from, whatever happened.

    The compute is the only queued action that runs *after* a review,
    so it must give the scan back where it took it. It is guarded on
    the busy statuses alone, so an admin who moved the scan while it
    computed keeps their decision.

    :param scan_pk: Primary key of the scan.
    :param message: What to show under the progress bar.
    :param status: Where to park it. Defaults to review 1's finished
        state; a successful run passes ``READY_FOR_REDACTION_REVIEW``
        (#263), and a legacy volume goes back to ``PENDING_REVIEW``,
        which is where its own step 2 lives, because the #154 and #263
        states describe a review it never had.
    :return: None.
    """
    Scan.objects.filter(
        pk=scan_pk, status__in=(Status.PROCESSING, Status.QUEUED)
    ).update(
        status=status or Status.PAGE_COMPLETENESS_REVIEW_DONE,
        progress_message=message,
    )


def run_apply_page_edits(scan_pk: int) -> None:
    """Build the corrected volume from the page edits, or glue it.

    The worker behind ``QueuedAction.APPLY_PAGE_EDITS`` (issue #224),
    which ``apply.queue_ready_scans`` writes on the collect tick.
    Queued work, like the redaction compute (#196): the build pulls the
    original and the glue pulls the volume bitonal copy, minutes on a
    large volume, and the tick's scheduler is serial.

    The scan goes back to ``PAGE_COMPLETENESS_REVIEW_DONE`` whatever
    happens, and this raises nothing: a failure is counted on the
    ``ApplyRun`` row, which bounds the retries. The park is guarded on
    PROCESSING alone. A lost claim -- the daemon's own shutdown
    re-queued the scan, or an admin moved it -- supersedes the run, so
    the rows it created are cancelled and the next claim builds the
    next number from the same shards.

    The local tree goes at the end, as it does on every other terminal
    path (#215). The build pulls the original and the glue pulls the
    volume bitonal copy, and the first ticks after a deploy apply the
    whole approved corpus. Without this the daemon pod would hold every
    one of those volumes until ``cleanup_processing_tmp`` reached its
    cutoff.

    :param scan_pk: Primary key of the scan to apply.
    :return: None.
    """
    from scanning import apply, s3_sync

    django.db.connections.close_all()
    scan = Scan.objects.get(pk=scan_pk)
    try:
        message = apply.run_due_phases(scan)
    except Exception as exc:  # pragma: no cover - run_due_phases catches
        logger.exception("apply: scan %s: unexpected failure", scan_pk)
        message = f"Building the corrected volume failed: {exc}"
    parked = Scan.objects.filter(pk=scan_pk, status=Status.PROCESSING).update(
        status=Status.PAGE_COMPLETENESS_REVIEW_DONE,
        progress_message=message[:255],
        progress_current=0,
        progress_total=0,
    )
    if not parked:
        apply.supersede_runs(
            scan, "the daemon lost its claim during the apply"
        )
    if s3_sync.s3_active():
        s3_sync.release_local_processing(scan)


def run_full_pipeline(scan_pk: int) -> None:
    """Run the upload pipeline: shard the original, then hand it to doctor.

    Designed to run in the daemon process.

    This runs the sharding (#164) and *starts* the two external stages
    that fan out over the shards: the bitonal conversion (#176), which
    parks the scan in ``Status.AWAITING``, and the dots.mocr read
    (#190/#207), which writes no scan status at all. Each stage is one
    ``ExternalJob`` row per shard. It does not wait -- submitting,
    confirming, merging, gluing and applying belong to the
    ``submit_external_jobs`` / ``collect_external_jobs`` ticks, which
    read those rows. Nothing about what runs next may live in a call
    stack, or a killed daemon loses it.

    A volume that cannot or need not be converted parks straight in
    ``Status.AWAITING_VALIDATION``, #173's interim state, and
    ``serve_scan_pdf`` serves its original -- exactly what every
    post-#173 upload already does. See :func:`_can_convert` and
    ``bitonal.source_is_bitonal``.

    Resumable across a daemon restart: ``ensure_shards`` is idempotent
    through its manifest fingerprint and ``ensure_convert_jobs`` through
    the shard identity its rows carry, so a re-queued scan verifies both
    and moves on rather than redoing them.

    :param scan_pk: Primary key of the scan to process.
    """
    from scanning import bitonal, dots_mocr, jobs

    django.db.connections.close_all()
    _pull_processing_files_from_s3(scan_pk)

    try:
        scan = Scan.objects.get(pk=scan_pk)
    except Exception:
        traceback.print_exc()
        return

    try:
        ensure_output_dir(scan)

        # Shard for external execution (#164): the bitonal jobs below
        # fan out over the shards, and dots.mocr will too.
        _update_progress(
            scan_pk,
            "Cutting the PDF into parts (shards), so servers can work "
            "on them in parallel...",
        )
        manifest = _ensure_shards(scan)

        # Read off the original, not the conversion output: the merge
        # deliberately does not write it, so this stays its only writer.
        with fitz.open(scan.pdf_path) as pdf:
            page_count = pdf.page_count

        # Start the OCR read (#190/#207) over the same shard set. The
        # stage is independent of the bitonal branch below: it reads
        # the *original* shards, writes no scan status while it runs,
        # and the apply pass (#149/#204) defers a scan that is still
        # AWAITING. Created before the status writes, like the convert
        # rows, so a lost guard hands them back below. Idempotent
        # through the run's shard identity: a re-queue finds the live
        # run -- CONSUMED included -- instead of paying for it again.
        analyze_created: list = []
        if _can_analyze(scan_pk, manifest):
            analyze_created = dots_mocr.ensure_analyze_jobs(scan, manifest)

        if not _can_convert(scan_pk, manifest):
            still_ours = _park_unconverted(
                scan_pk,
                page_count,
                "Uploaded and sharded. Page-number validation is "
                "temporarily disabled while the pipeline is rebuilt on "
                "the new OCR stack; this scan will be processed once "
                "that lands.",
            )
        elif bitonal.source_is_bitonal(scan.pdf_path):
            logger.info(
                "Scan %s is already bitonal; skipping conversion", scan_pk
            )
            still_ours = _park_unconverted(
                scan_pk, page_count, bitonal.SKIPPED_MESSAGE
            )
        else:
            created = jobs.ensure_convert_jobs(scan, manifest)
            if all(job.status == JobStatus.CONSUMED for job in created):
                # Converted and merged on an earlier run of a shard set
                # that has not changed. A re-queue must neither convert
                # it again nor re-merge: the results are deleted once
                # merged, so there is nothing left to read.
                logger.info(
                    "Scan %s is already converted; skipping the stage",
                    scan_pk,
                )
                still_ours = _park_unconverted(
                    scan_pk, page_count, bitonal.CONVERTED_MESSAGE
                )
            else:
                still_ours = _advance_scan(
                    scan_pk,
                    Status.AWAITING,
                    page_count,
                    f"Converting {len(created)} part(s) to a small "
                    "black-and-white (bitonal) preview...",
                    progress_total=len(created),
                )
                if not still_ours:
                    # The scan left PROCESSING while we sharded (a
                    # cancel, an admin action). Its rows exist but
                    # nothing watches them now, so hand them back
                    # rather than convert a volume somebody stopped.
                    jobs.abandon_open(
                        scan,
                        "Scan left PROCESSING before its conversion started",
                        stage=JobStage.CONVERT,
                    )

        if not still_ours and analyze_created:
            # The claim was lost -- any writer that moved the scan off
            # PROCESSING, most often the daemon's own shutdown: the
            # SIGTERM handler re-queues mid-flight scans and *returns*,
            # so this very pipeline continues on a scan it no longer
            # holds. That is a retry, not an end, so hand back only the
            # unstarted work: a PENDING row of a stopped scan would
            # still be submitted and paid (submit_pending does not read
            # scan status), but a COMPLETED row is a paid result the
            # carry re-reads on the retry -- cancelling it would re-pay
            # whole volumes on every deploy that catches a pipeline
            # mid-shard.
            from scanning.models import IN_FLIGHT_JOB_STATUSES

            jobs.abandon_open(
                scan,
                "Scan left PROCESSING before its OCR read started",
                stage=JobStage.ANALYZE,
                statuses=frozenset({JobStatus.PENDING})
                | IN_FLIGHT_JOB_STATUSES,
            )

        pushed = _push_processing_files_to_s3(scan_pk)

        # After a clean push S3 holds every byte the daemon wrote,
        # whichever branch parked the scan, so the local tree is a
        # cache it no longer needs. A failed push keeps the files, and
        # so does the failure path below: a re-queued retry reads them
        # instead of downloading again.
        if pushed:
            from scanning import s3_sync

            s3_sync.release_local_processing(scan)

    except Exception as exc:
        _handle_pipeline_exception(scan_pk, exc, context="pipeline")


def _can_convert(scan_pk: int, manifest: dict | None) -> bool:
    """Return whether this environment can hand shards to doctor.

    Checked before any row exists, because a row created where it
    cannot be submitted does not merely fail: it parks its scan in
    AWAITING until the queue deadline expires hours later. Parking
    unconverted instead is what every post-#173 upload already did.

    - a committed shard set, or there is nothing for a job to read;
    - doctor configured, since no in-process converter is left;
    - S3 active, because doctor fetches the shard through a presigned
      GET. Under ``TESTING`` and in dev without credentials the shards
      never left local disk, so every request would 404 on its input.

    :param scan_pk: Primary key of the scan, for the log line.
    :param manifest: The shard manifest, or None when sharding is off.
    :returns: Whether to create conversion jobs for this scan.
    :rtype: bool
    """
    from scanning import doctor_client, s3_sync

    if manifest is None:
        logger.info("Scan %s has no shard set; skipping conversion", scan_pk)
        return False
    if not doctor_client.enabled():
        logger.info(
            "Scan %s: doctor is not configured; skipping conversion", scan_pk
        )
        return False
    if not s3_sync.s3_active():
        logger.info(
            "Scan %s: S3 is inactive, so its shards are not readable by "
            "doctor; skipping conversion",
            scan_pk,
        )
        return False
    return True


def convert_stage_open() -> bool:
    """Return whether doctor can be handed a shard in this environment.

    The two checks of :func:`_can_convert` that need no shard set:
    doctor configured, and S3 active for the presigned GET. The page
    edit apply (#224) asks this before it queues a build, so a closed
    stage costs no attempt and no upload.

    :returns: Whether the conversion stage is open.
    :rtype: bool
    """
    from scanning import doctor_client, s3_sync

    return doctor_client.enabled() and s3_sync.s3_active()


def analyze_stage_open() -> bool:
    """Return whether dots.mocr can be handed a shard in this environment.

    The mirror of :func:`convert_stage_open` for :func:`_can_analyze`.

    :returns: Whether the OCR stage is open.
    :rtype: bool
    """
    from scanning import dots_mocr, s3_sync

    return dots_mocr.enabled() and s3_sync.s3_active()


def _can_analyze(scan_pk: int, manifest: dict | None) -> bool:
    """Return whether this environment can hand shards to dots.mocr.

    The mirror of :func:`_can_convert`, for the same reason: a row
    created where it cannot be submitted sits PENDING until its queue
    deadline expires hours later, and its failure is noise about a
    volume that did nothing wrong. An environment that fails a check
    parks as before, and the staff button stays as the manual way in.

    - a committed shard set, or there is nothing for a job to read;
    - ``dots_mocr.enabled()``, the operator switch plus the account
      credentials and the engine's endpoint id;
    - S3 active, because the worker fetches the shard through a
      presigned GET.

    :param scan_pk: Primary key of the scan, for the log line.
    :param manifest: The shard manifest, or None when sharding is off.
    :returns: Whether to create OCR jobs for this scan.
    :rtype: bool
    """
    from scanning import dots_mocr, s3_sync

    if manifest is None:
        logger.info("Scan %s has no shard set; skipping OCR", scan_pk)
        return False
    if not dots_mocr.enabled():
        logger.info(
            "Scan %s: dots.mocr is not configured; skipping OCR", scan_pk
        )
        return False
    if not s3_sync.s3_active():
        logger.info(
            "Scan %s: S3 is inactive, so its shards are not readable by "
            "the OCR worker; skipping OCR",
            scan_pk,
        )
        return False
    return True


def _park_unconverted(scan_pk: int, page_count: int, message: str) -> bool:
    """Park a scan that will not be converted in the interim state.

    :param scan_pk: Primary key of the scan.
    :param page_count: Page count read off the original.
    :param message: Progress message explaining why.
    :returns: Whether the scan was still ours to move.
    :rtype: bool
    """
    return _advance_scan(
        scan_pk, Status.AWAITING_VALIDATION, page_count, message
    )


def _advance_scan(
    scan_pk: int,
    status: str,
    page_count: int,
    message: str,
    progress_total: int = 0,
) -> bool:
    """Move a scan out of PROCESSING at the end of the pipeline.

    Guarded on PROCESSING like every other status write here: the daemon
    claims a scan by moving it there, so anything else means somebody
    took it away (a cancel, an admin action, a second replica) and their
    decision outranks ours. That matters more than it used to -- the
    AWAITING transition starts external work, so a resurrected scan
    would spend real capacity on a volume that was stopped.

    :param scan_pk: Primary key of the scan.
    :param status: Status to write.
    :param page_count: Page count read off the original.
    :param message: Progress message.
    :param progress_total: Steps the new state is waiting on, if any.
    :returns: Whether this writer moved the scan.
    :rtype: bool
    """
    updated = Scan.objects.filter(pk=scan_pk, status=Status.PROCESSING).update(
        status=status,
        page_count=page_count,
        progress_message=message[:255],
        progress_current=0,
        progress_total=progress_total,
    )
    if not updated:
        logger.warning(
            "Scan %s left PROCESSING during the pipeline; not writing %s",
            scan_pk,
            status,
        )
    return bool(updated)


# ---------------------------------------------------------------------------
# Generate (split into redacted/unredacted opinions)
# ---------------------------------------------------------------------------


def _stamp_original_images(scan: "Scan", base_pdf_path: str) -> str:
    """Overlay original-quality image regions onto a *copy* of the base PDF.

    For each active IMAGE detection, renders the bounding box from the
    original scan PDF and inserts it into a copy of the processing PDF
    (normally ``bitonal.pdf``) at the same position. This preserves
    full-quality photographs/illustrations that would otherwise be
    degraded by bitonal conversion.

    :param scan: The Scan instance with the original PDF path.
    :param base_pdf_path: Path to the processing PDF to stamp onto.
    :return: Path to the stamped copy. Always a copy, so the processing
        PDF is never modified by downstream steps.
    """

    stamped_path = os.path.join(os.path.dirname(base_pdf_path), "stamped.pdf")

    image_dets = list(
        Detection.objects.filter(scan=scan, label="IMAGE", active=True)
        .order_by("page_index")
        .values(
            "page_index", "x0", "y0", "x1", "y1", "img_width", "img_height"
        )
    )
    if not image_dets:
        # Always copy so the processing PDF is never modified downstream
        shutil.copy2(base_pdf_path, stamped_path)
        return stamped_path

    # Save extracted images to images/ directory
    images_dir = Path(os.path.dirname(base_pdf_path)) / "images"
    images_dir.mkdir(exist_ok=True)
    page_numbers = _page_number_lookup(scan)
    img_count_by_page: dict[int, int] = {}

    with (
        fitz.open(scan.pdf_path) as original_doc,
        fitz.open(base_pdf_path) as base_doc,
    ):
        for det in image_dets:
            page_idx = det["page_index"]
            if (
                page_idx >= original_doc.page_count
                or page_idx >= base_doc.page_count
            ):
                continue

            orig_page = original_doc[page_idx]
            base_page = base_doc[page_idx]

            # Convert image-pixel bbox to PDF points
            page_rect = orig_page.rect
            img_w = det["img_width"] or 1
            img_h = det["img_height"] or 1
            sx = page_rect.width / img_w
            sy = page_rect.height / img_h

            pdf_rect = fitz.Rect(
                det["x0"] * sx,
                det["y0"] * sy,
                det["x1"] * sx,
                det["y1"] * sy,
            )

            # Render the region from the original (non-bitonal) PDF
            pix = orig_page.get_pixmap(clip=pdf_rect, dpi=150)
            png_bytes = pix.tobytes("png")

            # Stamp onto the processing PDF copy
            base_page.insert_image(pdf_rect, stream=png_bytes)

            # Save image to images/ directory
            pn = page_numbers.get(page_idx)
            page_num = pn[0] if pn else page_idx + (scan.start_page or 1)
            img_count_by_page[page_idx] = (
                img_count_by_page.get(page_idx, 0) + 1
            )
            img_name = f"{page_num}-{img_count_by_page[page_idx]:03d}.png"
            (images_dir / img_name).write_bytes(png_bytes)

        base_doc.save(stamped_path, garbage=3, deflate=True)
    return stamped_path


def run_generate_files(scan_pk: int) -> None:
    """Generate redacted/split opinion files from existing detections.

    Designed to run in the daemon process. Nothing queues it since #173;
    #206 brings it back over the redacted volume, and it is left as it
    was until then (#269 moved review 2 and the redaction compute to
    the corrected volume, not this).

    :param scan_pk: Primary key of the scan to generate files for.
    """
    django.db.connections.close_all()
    _pull_processing_files_from_s3(scan_pk)

    scan = Scan.objects.get(pk=scan_pk)

    try:
        Scan.objects.filter(pk=scan_pk).update(
            progress_message="Generating files...", progress_log=""
        )

        output = Path(scan.output_dir)
        base_pdf = find_processing_pdf(str(output))
        if not base_pdf:
            raise ValueError(
                "No processing PDF (bitonal.pdf) found in output directory"
            )

        # Stamp original-quality images into a copy, leaves base PDF untouched
        gen_pdf = _stamp_original_images(scan, str(base_pdf))

        # Correct the TEXT_COLUMN boxes against the page ink before anything
        # reads them. The upload path used to do this so that step 2 showed
        # corrected boxes, but review 1 has no detection overlay to show them
        # in, so it was a full-volume render nobody was waiting on. Here it
        # runs before the detections are written out, which is what the
        # geometry below and the pairing are measured from.
        _update_progress(scan_pk, "Correcting column boxes...")
        _snap_text_columns_to_ink(scan_pk, str(base_pdf))

        # The live detections, with the page numbers beside each box.
        det_data = detection_entries(scan_pk)
        Scan.objects.filter(pk=scan_pk).update(
            progress_message=f"Generating files ({len(det_data or [])} detections)..."
        )

        # The redaction rows are what the compute wrote and the curator
        # edited (#240, PR B); nothing is measured here.
        # Build combined redactions.json (margins + redaction rects + opinions)
        Scan.objects.filter(pk=scan_pk).update(
            progress_message="Building combined redactions...",
        )
        redactions_path = _build_combined_redactions(scan_pk)

        Scan.objects.filter(pk=scan_pk).update(
            progress_message="Generating files...",
        )

        from blackletter.api import generate as bl_generate

        result = bl_generate(
            pdf_path=str(gen_pdf),
            redactions=str(redactions_path),
            output_dir=output,
            reporter=scan.reporter.short_name or "",
            volume=str(scan.volume) or "",
            unredacted=True,
            llm=True,
        )

        opinion_count = result.get("opinion_count", 0)
        full_redacted = result.get("full_redacted", "")
        redacted_dir = Path(result.get("redacted_dir", output / "redacted"))

        unredacted_dir = output / "unredacted"

        redacted_files = (
            sorted(redacted_dir.glob("*.pdf")) if redacted_dir.is_dir() else []
        )

        scan.refresh_from_db()
        # The boundaries are rows since #240 PR C; the file names land
        # on the dicts in reading order, as ``build_redactions`` names
        # them.
        existing_opinions = boundaries.viewer_payload(scan, live_only=True)

        if existing_opinions:
            for i, op in enumerate(existing_opinions):
                if i < len(redacted_files):
                    op["filename"] = redacted_files[i].name
        else:
            for f in redacted_files:
                existing_opinions.append(
                    {"filename": f.name, "first_page": 0, "last_page": 0}
                )

        scan.redacted_pdf_path = str(full_redacted) if full_redacted else ""
        scan.progress_message = "Saving opinion records..."
        scan.save()

        OpinionScan.objects.filter(scan=scan).delete()
        for i, op in enumerate(existing_opinions):
            page_start = op.get("first_page_number", 1)
            page_end = op.get("last_page_number", page_start)
            fname = op.get("filename", "")
            opinion = OpinionScan.objects.create(
                scan=scan,
                reporter=scan.reporter,
                volume=scan.volume,
                opinion_order=i,
                page_start=page_start or 1,
                page_end=page_end or page_start or 1,
                caption_page_index=op.get("caption_page"),
                key_page_index=op.get("key_page"),
                has_image=op.get("has_image", False),
                boundary_id=op.get("id"),
                status=OpinionStatus.OK,
                uploaded_by=scan.uploaded_by,
            )
            if fname:
                media_root = Path(settings.MEDIA_ROOT).resolve()
                scan_output = Path(scan.output_dir).resolve()

                def _field_name(path: Path) -> str:
                    """Prefer a MEDIA_ROOT-relative name so Django storage
                    can resolve the file in DEV. When the file lives
                    outside MEDIA_ROOT (prod /tmp/ case), fall back to a
                    path relative to the scan's output_dir, which
                    ``serve_opinionscan_pdf`` resolves at request time.
                    """
                    resolved = path.resolve()
                    try:
                        return str(resolved.relative_to(media_root))
                    except ValueError:
                        return str(resolved.relative_to(scan_output))

                rp = redacted_dir / fname
                if rp.exists():
                    opinion.redacted_pdf.name = _field_name(rp)
                up = (
                    unredacted_dir / fname if unredacted_dir.exists() else None
                )
                if up and up.exists():
                    opinion.original_pdf.name = _field_name(up)
                opinion.save()

        _update_progress(scan_pk, "Finalizing files...")
        _push_processing_files_to_s3(scan_pk)

        # Flip status only after OpinionScan rows and S3 push are done,
        # so the frontend's poll-and-reload lands on a fully-ready step 3
        # (avoids a 404 window where rows or files aren't yet available).
        scan.refresh_from_db()
        scan.stage = Stage.APPROVED
        scan.status = Status.PENDING_REVIEW
        scan.s3_uploaded = False
        scan.progress_message = f"Generated {opinion_count} opinions"
        scan.progress_log = ""
        scan.save()
        refresh_volume_queue_status_for_scan(scan)

    except Exception as exc:
        _handle_pipeline_exception(scan_pk, exc, context="generate_files")


# ---------------------------------------------------------------------------
# S3 upload of approved files
# ---------------------------------------------------------------------------


def upload_approved_files(scan_pk: int) -> str:
    """Copy approved deliverables from processing/ to approved/ on S3.

    The generate-files step already pushed every file under the scan's
    output dir to ``processing/{pk}/...`` on S3, so this function issues
    a server-side ``copy_object`` for each deliverable (redacted opinion
    PDFs, original and redacted full PDFs) rather than re-uploading from
    local disk.

    Skips the copy (with a message) if the scan was already approved or
    if no AWS credentials are configured.

    :param scan_pk: Primary key of the scan to approve.
    :return: A user-facing message describing the result.
    :rtype: str
    """
    from scanning import s3_sync

    scan = Scan.objects.get(pk=scan_pk)

    if scan.s3_uploaded and scan.s3_path:
        return f"Files were already uploaded to S3 ({scan.s3_path})."

    if scan.stage != Stage.APPROVED:
        return "Before approving you need to generate the files."

    s3_prefix = s3_sync.approved_prefix(scan)

    if not has_s3_credentials():
        Scan.objects.filter(pk=scan_pk).update(s3_path=s3_prefix)
        return (
            "No AWS credentials configured, skipping S3 upload. "
            "Path would be: " + s3_prefix
        )

    _, count = s3_sync.copy_processing_to_approved(scan)

    Scan.objects.filter(pk=scan_pk).update(
        s3_uploaded=True,
        s3_path=s3_prefix,
    )

    msg = f"Files copied on S3 from processing/ to approved/ ({count} files)."
    if settings.DEVELOPMENT:
        msg += (
            " (DEVELOPMENT=True: no real S3 calls are made; set AWS "
            "credentials and DEVELOPMENT=False to exercise the flow.)"
        )
    return msg
