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
from blackletter.process import compute_redaction_rects, page_body_covered
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

from scanning.models import (
    DEAD_JOB_STATUSES,
    CheckName,
    Detection,
    ExternalJob,
    Issue,
    JobStage,
    JobStatus,
    OpinionScan,
    OpinionStatus,
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
    viewer overlay and ``detections.json`` show what the geometry actually
    used, and no later step has to re-measure the ink to agree with it.

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


def _page_number_lookup(scan: "Scan") -> dict:
    """Build {page_index: (page_number, page_number_end)} from ocr_results.

    For range pages like "677-685", returns (677, 685).
    For single pages like "677", returns (677, None).

    :param scan: The Scan instance whose ocr_results to parse.
    :return: Mapping of page index to (start, end) page number tuple.
    """
    ocr_results = scan.ocr_results
    lookup = {}
    range_re = re.compile(r"^(\d{1,4})\s*[–\-]\s*(\d{1,4})$")
    for r in ocr_results:
        pdf_idx = r["pdf_page"] - 1
        detected = r.get("detected")
        if not detected:
            continue
        if r.get("type") == "range":
            m = range_re.match(str(detected).replace("\u2013", "-"))
            if m:
                lookup[pdf_idx] = (int(m.group(1)), int(m.group(2)))
            continue
        try:
            lookup[pdf_idx] = (int(detected), None)
        except (ValueError, TypeError):
            pass
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


def _sync_detections_to_disk(scan_pk: int, upload: bool = True) -> list | None:
    """Write current DB detections to detections.json on disk.

    :param scan_pk: Primary key of the scan whose detections to sync.
    :param upload: If ``True`` (default), also push detections.json to S3.
        Pass ``False`` when a subsequent ``_push_processing_files_to_s3``
        call will cover the upload, to avoid redundant round-trips.
    :return: The detection data written, or None if no output_dir.
    """
    scan = Scan.objects.get(pk=scan_pk)
    output_dir = Path(scan.output_dir)
    if not output_dir.is_dir():
        return

    # Build page_number lookup from ocr_results
    page_numbers = _page_number_lookup(scan)

    all_saved = Detection.objects.filter(
        scan_id=scan_pk, active=True
    ).order_by("page_index", "y0")
    det_data = []
    for d in all_saved:
        entry = {
            "page_index": d.page_index,
            "label": d.label,
            "label_id": d.label_id,
            "confidence": d.confidence,
            "bbox": [d.x0, d.y0, d.x1, d.y1],
            "img_width": d.img_width,
            "img_height": d.img_height,
            "model_count": d.model_count,
        }
        pn = page_numbers.get(d.page_index)
        if pn:
            entry["page_number"] = pn[0]
            if pn[1] is not None:
                entry["page_number_end"] = pn[1]
        det_data.append(entry)
    (output_dir / "detections.json").write_text(json.dumps(det_data))
    if upload:
        try:
            from scanning import s3_sync

            s3_sync.upload_file_to_s3(scan, "detections.json")
        except Exception:
            logger.exception(
                "Failed to push detections.json to S3 for scan %s", scan_pk
            )
    return det_data


def _build_document_from_detections(
    scan: "Scan", det_data: list, pdf_path: str
) -> "BLDoc":
    """Build a blackletter Document from detection data and a PDF.

    :param scan: The Scan instance for reporter/volume metadata.
    :param det_data: List of detection dicts (from detections.json).
    :param pdf_path: Path to the PDF to read page dimensions from.
    :return: The constructed Document.
    """
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
                page.detections.append(
                    BLDetection(
                        bbox=BBox(x1=b[0], y1=b[1], x2=b[2], y2=b[3]),
                        label=Label(d["label_id"]),
                        confidence=d["confidence"],
                        page_index=pi,
                    )
                )
            pages.append(page)

    scan_obj = scan if isinstance(scan, Scan) else Scan.objects.get(pk=scan)
    document = BLDoc(
        pdf_path=str(pdf_path),
        pages=pages,
        reporter=scan_obj.reporter.short_name or "",
        volume=str(scan_obj.volume) or "",
        first_page=scan_obj.start_page or 1,
        ocr_applied=True,
    )
    return document


def _compute_and_save_redaction_rects(scan_pk: int, pdf_path: str) -> list:
    """Compute redaction rects and save to the Scan model.

    :param scan_pk: Primary key of the scan to compute rects for.
    :param pdf_path: Path to the PDF used for page dimensions.
    :return: The computed rects list.
    """
    scan = Scan.objects.get(pk=scan_pk)

    det_data = _sync_detections_to_disk(scan_pk, upload=False)
    if not det_data:
        return []

    with _log_stage("Redaction rects"):
        document = _build_document_from_detections(scan, det_data, pdf_path)
        # Every blackletter entry point corrects the column boxes before
        # reading them, and the margin strips of this same scan are computed
        # from corrected ones. Skipping it here is how a hand-added
        # TEXT_COLUMN (which reaches the DB exactly as the reviewer drew it)
        # would give the headnote rects a different column to the margins on
        # the same page.
        snap_document_columns(document)
        opinions = _pair_opinions(document)
        # ``ocr_applied`` is set on the Document, so blackletter measures
        # this geometry from the page ink itself (see
        # ``scanner._measure_from_ink``), and finishes each headnote rect
        # against the page detections: snapped to its column box, cut at the
        # headnote boundaries inside it, then grown onto adjoining ink. The
        # app ran those three passes itself until blackletter #68 moved them
        # where every consumer gets them.
        rects = compute_redaction_rects(document, opinions, skip_doctr=True)

    Scan.objects.filter(pk=scan_pk).update(
        redaction_rects=rects,
    )
    return rects


def _load_detections(output_dir: str | Path) -> list:
    """Read ``detections.json`` from a scan's output dir.

    :param output_dir: The scan's output directory.
    :return: The detection list, or an empty list when absent/unreadable.
    """
    det_path = Path(output_dir) / "detections.json"
    if not det_path.exists():
        return []
    try:
        return json.loads(det_path.read_text())
    except (OSError, ValueError):
        logger.exception("Unreadable detections.json in %s", output_dir)
        return []


def _detections_for_geometry(scan_pk: int, output_dir: str | Path) -> list:
    """Detection dicts for the geometry helpers, DB first.

    ``detections.json`` is written by whichever process ran the pipeline, so
    another one may not have it yet: ``/tmp`` is per-container in dev and
    per-pod in production. Reading the DB avoids depending on that, and the
    DB is the source of truth anyway once a reviewer starts editing
    detections. The file is only a fallback, for a scan whose rows have not
    been imported.

    :param scan_pk: Primary key of the scan.
    :param output_dir: The scan's output directory, for the fallback.
    :return: Detection dicts shaped as ``detections.json`` stores them.
    """
    rows = Detection.objects.filter(scan_id=scan_pk, active=True).order_by(
        "page_index", "y0"
    )
    dets = [
        {
            "page_index": d.page_index,
            "label": d.label,
            "label_id": d.label_id,
            "confidence": d.confidence,
            "bbox": [d.x0, d.y0, d.x1, d.y1],
            "img_width": d.img_width,
            "img_height": d.img_height,
        }
        for d in rows
    ]
    return dets or _load_detections(output_dir)


def _pages_for_geometry(
    scan: "Scan", pdf_path: str, output_dir: str | Path, snap: bool = True
) -> list:
    """The detected pages blackletter's geometry should be measured against.

    Wraps the detection lookup and the column correction that every
    geometry consumer needs, so the rects and the margin strips of one scan
    cannot be computed from differently-corrected boxes.

    :param scan: The scan being processed.
    :param pdf_path: The PDF the detections were measured against.
    :param output_dir: The scan's output directory, for the JSON fallback.
    :param snap: Correct the ``TEXT_COLUMN`` boxes against the page ink.
        Boxes reach the DB uncorrected: nothing on the upload path snaps
        them (that would be a full-volume render review 1 does not need),
        and a hand-added column detection is stored exactly as drawn. The
        correction converges, so this is a no-op once ``run_generate_files``
        has persisted it, but it is not free before then: it renders every
        page at 100 dpi, so pass ``False`` where the column boxes are not
        read (see :func:`_build_combined_redactions`).
    :return: ``Page`` objects, empty when the scan has no detections yet.
    """
    det_data = _detections_for_geometry(scan.pk, output_dir)
    if not det_data:
        return []
    document = _build_document_from_detections(scan, det_data, pdf_path)
    if snap:
        snap_document_columns(document)
    return document.pages


def _compute_and_save_margin_rects(
    scan_pk: int, pdf_path: str, output_dir: str
) -> list:
    """Compute margin rects and save them to the Scan model.

    The strips are pulled back off any real detection they would cover, so
    key icons, captions and other content near a page edge survive. That
    happens inside :func:`blackletter.margins.compute_margin_rects`, which
    also uses the detections to tighten the content box.

    :param scan_pk: Primary key of the scan.
    :param pdf_path: Path to the PDF to compute margins for.
    :param output_dir: Directory (used for detection lookup only).
    :return: The computed margin rects list.
    """
    scan = Scan.objects.get(pk=scan_pk)
    if scan.margin_rects:
        return scan.margin_rects
    pages = _pages_for_geometry(scan, pdf_path, Path(output_dir))
    if not pages:
        # Without detections the bounds would come from the page's marks
        # alone, so bleed-through at a page edge suppresses that page's top
        # strip: a worse answer than none, and one that must not be cached or
        # it never gets recomputed once the detections land. Refuse before
        # measuring rather than after. The viewer asks for these on a sync
        # request and does not cache the reply, so computing them here would
        # render the whole volume at 100 dpi on every poll.
        logger.warning(
            "No margin rects for scan %s: no detections yet", scan_pk
        )
        return []
    margin_rects = compute_margin_rects(str(pdf_path), pages=pages)
    Scan.objects.filter(pk=scan_pk).update(
        margin_rects=margin_rects,
    )
    return margin_rects


def _add_llm_page_text_layer(scan_pk: int, llm_dir: Path) -> int:
    """Optionally make the per-page LLM PDFs searchable.

    ``ai.user_prompt`` crops three text snippets off each page (the caption's
    first line, a column-top continuation, the footnote band) and layers them
    on top of the structural roadmap it builds from the detections. Those
    crops need a text layer, and since scanning #145 nothing in the pipeline
    produces one, so they come back empty and the roadmap ships without them.

    Rather than reinstate the pass for the whole pipeline, this adds it here
    only, over pages that have already been redacted and masked, and only
    when ``LLM_PAGE_TEXT_LAYER`` is set. It is off by default because the
    model reads the page image anyway and the pass costs an OCR run over
    every page of the volume.

    :param scan_pk: Primary key of the scan, for progress reporting.
    :param llm_dir: The ``llm/`` directory of per-page PDFs.
    :return: Number of files given a text layer.
    """
    if not settings.LLM_PAGE_TEXT_LAYER or not llm_dir.is_dir():
        return 0
    from blackletter.api import add_text_layer

    _update_progress(scan_pk, "Adding a text layer to the LLM pages...")
    added = add_text_layer(llm_dir)
    logger.info("Text layer added to %s LLM pages", len(added))
    return len(added)


def _build_combined_redactions(scan_pk: int) -> Path:
    """Combine margin_rects, redaction_rects, and opinions into redactions.json.

    All coordinates in the output are in PDF points. This file is passed
    to blackletter's ``generate`` API as the single source of redaction data.

    The merging, the pixel-to-point conversion and the opinion filenames all
    come from :func:`blackletter.api.build_redactions`, so the payload
    ``generate`` reads is built by the same library that consumes it.

    :param scan_pk: Primary key of the scan.
    :return: Path to the generated redactions.json.
    """
    scan = Scan.objects.get(pk=scan_pk)
    output_dir = Path(scan.output_dir)
    pdf_path = processing_pdf_path(scan)

    # ``snap=False``: this step reads only each page's dimensions and scale,
    # never its column boxes, so correcting them would render the whole
    # volume at 100 dpi to change nothing.
    pages = _pages_for_geometry(scan, pdf_path, output_dir, snap=False)

    try:
        combined = bl_build_redactions(
            pages,
            scan.redaction_rects,
            scan.margin_rects,
            scan.opinions_json,
            reporter=scan.reporter.short_name or "",
            volume=str(scan.volume) or "",
        )
    except KeyError as exc:
        # The rects are a saved snapshot and the pages come from the live
        # detections, so a page with rects but no detections left cannot be
        # scaled. Deleting the last detection prunes that page's rects (see
        # ``views_api._drop_orphaned_redaction_rects``), so reaching this
        # means something else desynchronised them. Refusing is right --
        # guessing the scale would put a blackout in the wrong place -- but
        # name recovery a reviewer can actually carry out.
        raise RuntimeError(
            f"scan {scan_pk}: saved redaction rects reference a page with no "
            f"detections left, so their pixel coordinates cannot be converted "
            f"to points. Re-add a detection on that page, or delete the "
            f"leftover rect from the redaction overlay, then generate again. "
            f"({exc})"
        ) from exc

    out_path = output_dir / "redactions.json"
    out_path.write_text(json.dumps(combined))
    pages = combined["pages"]
    n_rects = sum(len(v) for v in pages.values())
    logger.info(
        "Combined redactions: %s pages, %s rects, %s opinions",
        len(pages),
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


def rebuild_page_map(scan: "Scan") -> None:
    """Rebuild ``page_map`` and ``missing_pages`` from current ocr_results.

    Recomputes the page-sequence projection (duplicate flags and
    missing-page placeholders) without re-running OCR, opening the PDF, or
    touching Issue records, so dismissed issues are preserved. Used after a
    manual page-number edit so the viewer reflects the change immediately;
    Issue cards are refreshed separately by a full Recheck.

    :param scan: The Scan whose page_map to rebuild.
    """
    ocr_results = scan.ocr_results
    if not ocr_results:
        return
    exp_start, exp_end = _expected_range(scan)
    analysis = build_analysis(ocr_results, exp_start, exp_end)
    result = build_issues(
        analysis, scan.page_count, exp_start=exp_start, exp_end=exp_end
    )
    scan.page_map = result["page_map"]
    scan.missing_pages = result["missing_pages"]
    scan.save()


def recalculate_issues(scan: "Scan") -> None:
    """Rebuild issues from scan.ocr_results without re-running OCR.

    Runs off stored data only, so it works on a web pod that never
    downloaded the scan's processing files from S3.

    :param scan: The Scan instance to recalculate issues for.
    """

    ocr_results = scan.ocr_results
    if not ocr_results:
        return

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
    # A recheck must not move a scan between review states (#154): a
    # scan in a page-completeness review state keeps it. The write to
    # PENDING_REVIEW stays for the legacy rows that already carry it.
    Scan.objects.filter(pk=scan.pk).exclude(
        status__in=(
            Status.READY_FOR_PAGE_COMPLETENESS_REVIEW,
            Status.PAGE_COMPLETENESS_REVIEW_DONE,
        )
    ).update(status=Status.PENDING_REVIEW)
    scan.refresh_from_db(fields=["status"])

    # Suppression flags are curator decisions stored as Issue rows, not
    # derived from the page numbers, so a recheck keeps them (same
    # exclusion the daemon rebuild uses).
    scan.issues.exclude(check_name=CheckName.SUPPRESS_DETECTION).delete()
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
    -- but no Issues are rebuilt and no status moves. Once the scan is
    READY, ``cancel_processing`` no longer matches it, and
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
    from scanning import page_numbers, s3_sync

    started = time.monotonic()
    document = s3_sync.download_json_object(result_key)
    results = page_numbers.ocr_results_from_volume(document, scan.ocr_results)
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


def run_full_pipeline(scan_pk: int) -> None:
    """Run the upload pipeline: shard the original, then hand it to doctor.

    Designed to run in the daemon process.

    Two stages of the rebuilt pipeline exist so far. This runs the first
    (sharding, #164) and *starts* the second (conversion, #176): it
    creates one ``ExternalJob`` row per shard and parks the scan in
    ``Status.AWAITING``. It does not wait -- submitting, confirming and
    merging belong to the ``submit_external_jobs`` /
    ``collect_external_jobs`` ticks, which read those rows. Nothing
    about what runs next may live in a call stack, or a killed daemon
    loses it.

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
    from scanning import bitonal, jobs

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

        if not _can_convert(scan_pk, manifest):
            _park_unconverted(
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
            _park_unconverted(scan_pk, page_count, bitonal.SKIPPED_MESSAGE)
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
                _park_unconverted(
                    scan_pk, page_count, bitonal.CONVERTED_MESSAGE
                )
            else:
                handed_over = _advance_scan(
                    scan_pk,
                    Status.AWAITING,
                    page_count,
                    f"Converting {len(created)} part(s) to a small "
                    "black-and-white (bitonal) preview...",
                    progress_total=len(created),
                )
                if not handed_over:
                    # The scan left PROCESSING while we sharded (a
                    # cancel, an admin action). Its rows exist but
                    # nothing watches them now, so hand them back
                    # rather than convert a volume somebody stopped.
                    jobs.abandon_open(
                        scan,
                        "Scan left PROCESSING before its conversion started",
                        stage=JobStage.CONVERT,
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

    Designed to run in the daemon process.

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

        # Write current DB detections -> detections.json (includes page numbers)
        det_data = _sync_detections_to_disk(scan_pk)
        Scan.objects.filter(pk=scan_pk).update(
            progress_message=f"Generating files ({len(det_data or [])} detections)..."
        )

        # Margin rects are otherwise only computed on demand, by the viewer
        # asking for them or by a reprocess. A scan taken straight from
        # review to Generate without the margins overlay ever being switched
        # on therefore shipped with no whiteouts at all: the platen bands,
        # fold shadows and corner bleed stayed in the deliverable. Computing
        # them here makes the output independent of what the reviewer
        # happened to look at; it no-ops when they already exist.
        _compute_and_save_margin_rects(scan_pk, str(base_pdf), str(output))

        # Redaction rects, same story: off the upload path, computed here
        # unless something already produced them. That "something" is either
        # the step 2 overlay asking for them or a reprocess, and in the step 2
        # case a reviewer may since have moved or deleted individual rects
        # through ``save_redaction_rect``. Recomputing would discard those
        # edits, so the stored set wins whenever there is one.
        scan.refresh_from_db()
        if not scan.redaction_rects:
            _update_progress(scan_pk, "Computing redaction rects...")
            _compute_and_save_redaction_rects(scan_pk, str(base_pdf))

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

        _add_llm_page_text_layer(scan_pk, output / "llm")

        opinion_count = result.get("opinion_count", 0)
        full_redacted = result.get("full_redacted", "")
        redacted_dir = Path(result.get("redacted_dir", output / "redacted"))

        unredacted_dir = output / "unredacted"

        redacted_files = (
            sorted(redacted_dir.glob("*.pdf")) if redacted_dir.is_dir() else []
        )

        scan.refresh_from_db()
        existing_opinions = scan.opinions_json

        if existing_opinions and "caption_page" in existing_opinions[0]:
            for i, op in enumerate(existing_opinions):
                if i < len(redacted_files):
                    op["filename"] = redacted_files[i].name
        else:
            existing_opinions = []
            for f in redacted_files:
                existing_opinions.append(
                    {"filename": f.name, "first_page": 0, "last_page": 0}
                )

        scan.redacted_pdf_path = str(full_redacted) if full_redacted else ""
        scan.opinions_json = existing_opinions
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

        _sync_pages_for_scan(scan_pk)
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


def _page_has_headnote(redactions_pages: dict, page_index: int) -> bool:
    """Return True if any rect on this page has ``type=headnote``.

    Cheap probe used by the sandwich rule to detect whether a
    multi-page headnote block is currently flowing.

    :param redactions_pages: ``redactions["pages"]`` — a dict keyed by
        stringified page_index, each value a list of rect dicts.
    :param page_index: 0-based PDF page index.
    :return: True iff at least one rect on that page is a headnote
        redaction.
    """
    return any(
        r.get("type") == "headnote"
        for r in redactions_pages.get(str(page_index), [])
    )


def _page_body_covered(
    redactions_pages: dict,
    page_index: int,
    pdf_path: Path,
) -> bool:
    """Return True when rects cover essentially all of a page's body.

    Boundary-case confirmation for the sandwich rule: when only one
    neighbor of a page has headnotes (the page is the leading or trailing
    edge of a multi-page block), we check that the redaction geometry
    actually wipes the whole body before calling the page blank.

    The measurement is :func:`blackletter.process.page_body_covered`; what
    is app-specific is looking the page's rects up in ``redactions.json``
    and reading the page size off the generated PDF.

    Returns False on any exception so an unreadable PDF cannot mis-flag
    a page as blank.

    :param redactions_pages: ``redactions["pages"]`` -- dict keyed by
        stringified page_index, each value a list of rect dicts with
        ``x0``/``y0``/``x1``/``y1`` in PDF points.
    :param page_index: 0-based PDF page index.
    :param pdf_path: Filesystem path to the per-page PDF, used only to
        read the page size.
    :return: True if essentially all of the body is covered.
    """
    rects = redactions_pages.get(str(page_index), [])
    if not rects:
        return False
    try:
        with fitz.open(str(pdf_path)) as doc:
            page_rect = doc[0].rect
        return page_body_covered(rects, page_rect.width, page_rect.height)
    except Exception:
        return False


def _blank_page_xml(book_page: str, page_index: int) -> str:
    """Return the canned ``<page><pagenumber/></page>`` XML.

    Stamped directly onto ``Page.xml_content`` for fully-redacted /
    blank pages so we skip the LLM call entirely. Uses ``book_page``
    (the printed page number from OCR) when available, otherwise
    falls back to the 1-based PDF position.

    :param book_page: The printed page number on this page, or empty
        string if OCR hasn't populated it.
    :param page_index: 0-based PDF page index, used as the fallback.
    :return: A two-line XML stub with the page number filled in both
        the ``page`` attribute and the element text.
    """
    pn = book_page or str(page_index + 1)
    return f'<page>\n    <pagenumber page="{pn}">{pn}</pagenumber>\n</page>'


def _is_blank_via_sandwich(
    page_index: int,
    opinions: list[dict],
    redactions_pages: dict,
    page_detections: list[dict],
    pdf_path: "Path | None" = None,
) -> bool:
    """Decide whether a page's body is entirely covered by headnotes.

    Page must be interior of a 3+ page opinion AND its neighbors must
    have ``headnote`` redactions, with no ``FOOTNOTES`` detected on
    this page (the footnote band is readable content, so not blank).

    Two firing conditions:

      * **Strict sandwich** — both prev and next pages have headnote
        rects. The page sits inside a multi-page headnote block;
        fires directly.
      * **Boundary sandwich** — only one neighbor has headnotes
        (this page is the leading or trailing edge of the block).
        Confirm by measuring how much of the body the page's rects
        cover, since a body made entirely of redaction rects has
        nothing left to extract.

    :param page_index: 0-based PDF page index.
    :param opinions: ``scan.opinions_json`` — the curated opinion
        list, each entry with ``caption_page`` / ``key_page`` /
        ``page_count``.
    :param redactions_pages: ``redactions["pages"]`` — per-page rect
        list keyed by stringified page_index.
    :param page_detections: All YOLO detections on this page (used
        only to check for ``FOOTNOTES``).
    :param pdf_path: Path to the per-page PDF, read for its page size
        by the boundary coverage check. If omitted, the boundary case
        can't fire and only strict sandwich pages are flagged.
    :return: True iff the page's body is effectively blank under the
        rule above.
    """
    if any(d.get("label") == "FOOTNOTES" for d in page_detections):
        return False
    if not _page_has_headnote(redactions_pages, page_index):
        return False
    for op in opinions:
        cap = op.get("caption_page")
        key = op.get("key_page")
        if cap is None or key is None:
            continue
        if not (cap < page_index < key and op.get("page_count", 0) > 2):
            continue
        prev_hn = _page_has_headnote(redactions_pages, page_index - 1)
        next_hn = _page_has_headnote(redactions_pages, page_index + 1)
        if prev_hn and next_hn:
            return True
        # Boundary case: only one side has headnotes. Confirm from the
        # rect geometry — if the whole body is redacted away, it really
        # is a blank page even though sandwich doesn't fire.
        if (prev_hn or next_hn) and pdf_path is not None:
            if _page_body_covered(redactions_pages, page_index, pdf_path):
                return True
    return False


def _sync_pages_for_scan(scan_pk: int) -> int:
    """Create / refresh ``Page`` rows for each ``llm/page_NNNN.pdf``.

    Called at the end of ``run_generate_files`` once blackletter has
    produced the per-page PDFs. Loads ``detections.json`` once, slices
    the per-page detection list onto each ``Page`` row, computes
    ``is_blank``, and stamps the canned blank-
    page XML for pages where the body is entirely redacted so we skip
    the LLM call.

    The per-page user prompt is then built via
    ``ai.user_prompt.build_user_prompt(page)`` and persisted as an
    ``ai.Prompt`` row that ``Page.user_prompt`` points at.

    Idempotent. ``Page`` is keyed on ``(scan, page_index)``. On re-run:

    - If the regenerated prompt matches the page's current
      ``Prompt.text``, the FK is left alone (no new Prompt row).
    - If it differs, a new ``Prompt`` row is created and the FK is
      repointed; the old Prompt stays as queryable history.
    - If a previously-not-blank page is now blank, the auto-blank
      stub overwrites any empty ``xml_content``. If the page was
      already extracted by Gemini, the existing result is preserved.
    - If a previously-blank page is no longer blank, the auto-blank
      stub is cleared so the LLM can pick it up on the next run.

    :param scan_pk: Primary key of the scan to sync.
    :return: Number of ``Page`` rows touched.
    """
    from ai.models import Prompt, PromptTypes
    from ai.user_prompt import build_user_prompt
    from scanning.models import ExtractedBy, ExtractionStatus, Page

    scan = Scan.objects.get(pk=scan_pk)
    output_dir = Path(scan.output_dir)
    llm_dir = output_dir / "llm"
    if not llm_dir.is_dir():
        return 0

    det_path = output_dir / "detections.json"
    redact_path = output_dir / "redactions.json"
    all_detections: list[dict] = (
        json.loads(det_path.read_text()) if det_path.is_file() else []
    )
    redactions: dict = (
        json.loads(redact_path.read_text()) if redact_path.is_file() else {}
    )
    redactions_pages: dict = redactions.get("pages", {}) or {}
    opinions: list[dict] = redactions.get("opinions", []) or []
    page_numbers = _page_number_lookup(scan)

    # Pre-index detections by page_index so we slice per-page in O(1).
    detections_by_page: dict[int, list[dict]] = {}
    for d in all_detections:
        pi = d.get("page_index")
        if pi is not None:
            detections_by_page.setdefault(pi, []).append(d)

    # Tally how many opinions start / end on each page_index, directly
    # from ``scan.opinions_json`` (caption_page / key_page are already
    # 0-based PDF indices — no reporter-page detour).
    starts_by_idx: dict[int, int] = {}
    ends_by_idx: dict[int, int] = {}
    for op in scan.opinions_json or []:
        cap = op.get("caption_page")
        key = op.get("key_page")
        if isinstance(cap, int):
            starts_by_idx[cap] = starts_by_idx.get(cap, 0) + 1
        if isinstance(key, int):
            ends_by_idx[key] = ends_by_idx.get(key, 0) + 1

    n = 0
    for pdf in sorted(llm_dir.glob("page_*.pdf")):
        try:
            # "page_0001.pdf" -> 0  (filenames are 1-based; we store 0-based)
            page_index = int(pdf.stem.split("_", 1)[1]) - 1
        except (IndexError, ValueError):
            continue

        pn = page_numbers.get(page_index)
        book_page = ""
        if pn:
            book_page = str(pn[0]) if pn[1] is None else f"{pn[0]}-{pn[1]}"

        page_detections = detections_by_page.get(page_index, [])
        is_blank = _is_blank_via_sandwich(
            page_index, opinions, redactions_pages, page_detections, pdf
        )

        page, _created = Page.objects.update_or_create(
            scan=scan,
            page_index=page_index,
            defaults={
                "pdf_path": f"llm/{pdf.name}",
                "book_page": book_page,
                "expected_opinion_starts": starts_by_idx.get(page_index, 0),
                "expected_opinion_ends": ends_by_idx.get(page_index, 0),
                "detections": page_detections,
                "is_blank": is_blank,
            },
        )

        # Auto-stamp the canned blank-page XML so we don't burn an API
        # call on a page with nothing to extract. Preserves any earlier
        # Gemini result if a previously-not-blank page is now blank.
        if is_blank and not page.xml_content:
            page.xml_content = _blank_page_xml(book_page, page_index)
            page.extracted_by = ExtractedBy.BLANK_AUTO
            page.status = ExtractionStatus.EXTRACTED
            page.save(update_fields=["xml_content", "extracted_by", "status"])
        elif not is_blank and page.extracted_by == ExtractedBy.BLANK_AUTO:
            # Was auto-blank, no longer is — clear so the LLM can run.
            page.xml_content = ""
            page.extracted_by = ""
            page.status = ExtractionStatus.PENDING
            page.save(update_fields=["xml_content", "extracted_by", "status"])

        prompt_text = build_user_prompt(page) or ""
        if prompt_text:
            current = page.user_prompt
            if current is None or current.text != prompt_text:
                new_prompt = Prompt.objects.create(
                    name=f"scan {scan.pk} p{page_index:04d}",
                    prompt_type=PromptTypes.USER,
                    text=prompt_text,
                )
                page.user_prompt = new_prompt
                page.save(update_fields=["user_prompt"])
        n += 1

    return n


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
