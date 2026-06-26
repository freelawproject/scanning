"""Background processing pipelines and business logic.

Functions in this module run outside the request/response cycle, in the
daemon process.  They must NOT import Django HTTP machinery
(HttpResponse, render, redirect, etc.).
"""

import json
import logging
import os
import re
import shutil
import traceback
from collections import Counter
from pathlib import Path

import django
import fitz
import pdfplumber
from blackletter.api import (
    bitonal as bl_bitonal,
)
from blackletter.api import (
    pair as bl_pair,
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
from blackletter.process import compute_redaction_rects
from blackletter.scanner import _pair_opinions
from blackletter.validate import (
    _auto_correct,
    _build_issues,
    _parse_expected_range,
    _split_in_out_of_range,
)
from django.conf import settings
from django.db.models import Case, F, Value, When

from scanning.models import (
    CheckName,
    Detection,
    Issue,
    OpinionScan,
    OpinionStatus,
    QueueStatus,
    Scan,
    Stage,
    Status,
    Volume,
)
from scanning.utils import ensure_output_dir, find_ocr_pdf, has_s3_credentials

logger = logging.getLogger(__name__)

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

    :param scan_pk: Primary key of the scan that failed.
    :param exc: The exception that was raised.
    :param context: Short label for log messages (e.g. ``"pipeline"``,
        ``"validate"``, ``"detect"``).
    """
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

    traceback.print_exc()
    print(f"[{context}] ERROR: {exc}", flush=True)
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


def _ensure_bitonal(scan: "Scan", output_dir: Path) -> Path:
    """Convert to bitonal if needed, returning the bitonal PDF path.

    Skips conversion if ``bitonal.pdf`` already exists. Updates the
    scan's ``page_count`` after conversion.

    :param scan: The Scan instance.
    :param output_dir: Directory where ``bitonal.pdf`` is stored.
    :return: Path to the bitonal PDF.
    :rtype: Path
    """
    bitonal_path = output_dir / "bitonal.pdf"
    if not bitonal_path.exists():

        def _bitonal_progress(current, total, message):
            _update_progress(scan.pk, message, current=current, total=total)

        bl_bitonal(
            scan.pdf_path,
            str(output_dir),
            progress_callback=_bitonal_progress,
        )
        # Persist as soon as it exists so a later crash, or a reprocess
        # that skips the end-of-pipeline push, still leaves it in S3.
        _push_generated_file_to_s3(scan.pk, "bitonal.pdf")

    with fitz.open(str(bitonal_path)) as pdf:
        page_count = pdf.page_count
    Scan.objects.filter(pk=scan.pk).update(page_count=page_count)
    return bitonal_path


def _run_ocr(
    scan_pk: int,
    bitonal_path: str,
    output_dir: str,
    reporter: str,
    volume: str,
    first_page: int,
) -> str:
    """Run Tesseract OCR via blackletter.

    :param scan_pk: Primary key of the scan (for progress updates).
    :param bitonal_path: Path to the bitonal PDF.
    :param output_dir: Directory to write the OCR'd PDF into.
    :param reporter: Reporter short name (e.g. "f3d").
    :param volume: Volume number as a string.
    :param first_page: First logical page number in the PDF.
    :return: Path to the OCR'd PDF.
    """
    with fitz.open(bitonal_path) as pdf:
        total_pages = pdf.page_count
    _update_progress(
        scan_pk,
        f"Running Tesseract OCR (0/{total_pages} pages)...",
        current=0,
        total=total_pages,
    )
    ocr_path = _ocr(
        scan_pk,
        bitonal_path,
        output_dir,
        reporter=reporter,
        volume=volume,
        first_page=first_page,
        total_pages=total_pages,
    )
    # Persist the OCR PDF as soon as it exists (see _ensure_bitonal).
    _push_generated_file_to_s3(scan_pk, Path(ocr_path).name)
    return ocr_path


def _ocr(
    scan_pk: int,
    pdf_path: str | Path,
    output_dir: str | Path,
    reporter: str = "",
    volume: str = "",
    first_page: int = 1,
    total_pages: int = 0,
    language: str = "eng",
) -> Path:
    """OCR a PDF (add text layer via ocrmypdf/Tesseract).

    Temporary local copy of blackletter's ``ocr`` function with
    progress callback support. Will be moved to blackletter once
    validated.

    :param scan_pk: Primary key of the scan (for progress updates).
    :param pdf_path: Path to the input PDF.
    :param output_dir: Directory to write the OCR'd PDF into.
    :param reporter: Reporter short name (e.g. "f3d").
    :param volume: Volume number as a string.
    :param first_page: First logical page number in the PDF.
    :param total_pages: Total page count (for progress messages).
    :param language: Tesseract language code.
    :return: Path to the OCR'd PDF.
    """
    import logging
    import time

    import ocrmypdf
    from ocrmypdf import hookimpl
    from ocrmypdf._plugin_manager import get_plugin_manager

    pdf_path = Path(pdf_path)
    output_dir = Path(output_dir)

    # Build output filename
    last_page = first_page + total_pages - 1
    parts = [
        p
        for p in [reporter, str(volume), str(first_page), str(last_page)]
        if p
    ]
    scan_name = ".".join(parts) if parts else pdf_path.stem
    output_path = output_dir / f"{scan_name}.pdf"

    # Suppress noisy loggers
    for name in (
        "pikepdf",
        "fontTools",
        "fontTools.subset",
        "fontTools.ttLib",
        "ocrmypdf",
    ):
        logging.getLogger(name).setLevel(logging.ERROR)
    for _n in list(logging.root.manager.loggerDict):
        if _n.startswith("ocrmypdf"):
            logging.getLogger(_n).setLevel(logging.ERROR)

    # Progress bar class that updates the scan record per page
    class _ScanProgressBar:
        def __init__(
            self, *, total=None, desc=None, unit=None, disable=False, **kw
        ):
            self._total = total or total_pages
            self._unit = unit
            self._desc = desc
            self._current = 0
            print(
                f"  [progress] unit={unit!r} desc={desc!r} total={total}",
                flush=True,
            )

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def update(self, n=1, *, completed=None):
            self._current += n
            if self._unit == "page" and (
                self._current % 10 == 0 or self._current == self._total
            ):
                _update_progress(
                    scan_pk,
                    f"Tesseract OCR: {self._current}/{self._total} pages...",
                    current=self._current,
                    total=self._total,
                )

    class _ScanProgressPlugin:
        @hookimpl
        def get_progressbar_class(self):
            return _ScanProgressBar

    pm = get_plugin_manager()
    pm._pm.register(_ScanProgressPlugin())

    print(
        f"  OCR {total_pages} pages... ({os.cpu_count() or 1} CPUs)",
        flush=True,
    )
    t0 = time.time()
    ocrmypdf.ocr(
        str(pdf_path),
        str(output_path),
        pdf_renderer="auto",
        optimize=1,
        output_type="pdf",
        language=[language],
        tesseract_timeout=120,
        progress_bar=True,
        plugin_manager=pm,
    )
    print(f"  OCR done ({time.time() - t0:.0f}s)", flush=True)
    return output_path


def _run_yolo(scan_pk: int, pdf_path: str, output_dir: str) -> None:
    """Run YOLO detection via runpod_client (local or remote).

    In local mode (``RUNPOD_ENABLED=False``) the client calls
    ``bl_detect`` in-process; its per-model and per-batch progress is
    printed to stdout, which ``_ProgressWriter`` captures and relays
    to the scan's progress fields. In remote mode the client's own
    coarse events (``_remote_progress``) drive progress instead, and
    the stdout capture simply sees no blackletter output.

    :param scan_pk: Primary key of the scan (for progress updates).
    :param pdf_path: Path to the PDF to run detection on.
    :param output_dir: Directory where detections.json will be saved.
    """
    import io
    import sys

    from scanning import runpod_client

    _update_progress(scan_pk, "YOLO detection: loading models...")
    scan = Scan.objects.get(pk=scan_pk)

    real_stdout = sys.stdout

    class _ProgressWriter(io.TextIOBase):
        """Intercept bl_detect stdout and relay to _update_progress."""

        def __init__(self):
            self._buf = ""

        def write(self, s):
            real_stdout.write(s)
            real_stdout.flush()
            self._buf += s
            while "\n" in self._buf:
                line, self._buf = self._buf.split("\n", 1)
                self._handle_line(line.strip())
            return len(s)

        def flush(self):
            real_stdout.flush()

        def _handle_line(self, line):
            if not line:
                return
            # "Detecting with small..."
            if line.startswith("Detecting with"):
                model = line.replace("Detecting with", "").strip().rstrip(".")
                _update_progress(scan_pk, f"YOLO detection: {model}...")
            # "  23/23 pages"
            elif "/" in line and "pages" in line:
                _update_progress(scan_pk, f"YOLO detection: {line}")
            # "  small done (2s)"
            elif "done" in line:
                _update_progress(scan_pk, f"YOLO: {line}")
            # "363 detections (786 raw from 3 models)"
            elif "detections" in line:
                _update_progress(scan_pk, f"YOLO: {line}")

    def _remote_progress(current, total, message):
        _update_progress(scan_pk, message)

    sys.stdout = _ProgressWriter()
    try:
        detections = runpod_client.detect(
            scan,
            pdf_path,
            models=["small", "medium", "large"],
            progress_callback=_remote_progress,
        )
    finally:
        sys.stdout = real_stdout

    # ``bl_detect`` used to write detections.json as a side effect;
    # preserve that here so ``_import_detections_from_json`` (and any
    # other disk consumers) still see the file.
    (Path(output_dir) / "detections.json").write_text(json.dumps(detections))
    _update_progress(scan_pk, f"YOLO: {len(detections)} detections")


def _import_detections_from_json(scan_pk: int, output_dir: str) -> list:
    """Load detections.json from disk into Detection model.

    Clears existing detections first.

    :param scan_pk: Primary key of the scan to import detections for.
    :param output_dir: Directory containing detections.json.
    :return: The raw detection list from the JSON file.
    """
    det_path = Path(output_dir) / "detections.json"
    dets = json.loads(det_path.read_text())

    Detection.objects.filter(scan_id=scan_pk).delete()
    det_objects = []
    for d in dets:
        try:
            label_name = Label(d["label_id"]).name
        except (ValueError, KeyError):
            label_name = d.get("label", "UNKNOWN")
        det_objects.append(
            Detection(
                scan_id=scan_pk,
                page_index=d["page_index"],
                label=label_name,
                label_id=d["label_id"],
                confidence=d["confidence"],
                x0=d["bbox"][0],
                y0=d["bbox"][1],
                x1=d["bbox"][2],
                y1=d["bbox"][3],
                img_width=d.get("img_width", 0),
                img_height=d.get("img_height", 0),
                model_name=d.get("found_by", [{}])[0].get("model", ""),
                model_count=d.get("model_count", 1),
                found_by=d.get("found_by", []),
            )
        )
    Detection.objects.bulk_create(det_objects)
    return dets


def _delete_page_and_detections(
    scan: "Scan",
    scan_pk: int,
    page_idx: int,
    output_dir: Path | None,
) -> None:
    """Remove a page from all PDFs, then delete/shift its detections.

    For each PDF in ``_collect_pdf_paths(scan, output_dir)``, deletes
    the page at ``page_idx`` (if in bounds) and saves incrementally.
    Then deletes Detection rows on that page and shifts the
    ``page_index`` of subsequent detections down by 1.

    :param scan: The Scan instance.
    :param scan_pk: Primary key of the scan.
    :param page_idx: Zero-based index of the page to delete.
    :param output_dir: Output directory (passed to
        ``_collect_pdf_paths``).
    """
    for pdf_path in _collect_pdf_paths(scan, output_dir):
        with fitz.open(str(pdf_path)) as doc:
            if 0 <= page_idx < doc.page_count:
                doc.delete_page(page_idx)
                doc.saveIncr()

    Detection.objects.filter(scan_id=scan_pk, page_index=page_idx).delete()
    Detection.objects.filter(scan_id=scan_pk, page_index__gt=page_idx).update(
        page_index=F("page_index") - 1
    )


def _detect_sequence_issues(
    ocr_results: list, out_of_range_pages: set
) -> list[tuple]:
    """Classify duplicate / backward / gap page-number runs.

    Walks ocr_results in order, tracks the previous detected page number,
    and yields an issue tuple whenever the difference between consecutive
    pages is 0 (duplicate), negative (backward), or > 2 (gap).

    :param ocr_results: List of per-page OCR result dicts.
    :param out_of_range_pages: PDF page numbers already flagged as
        out-of-range; these are skipped so they don't anchor the
        prev_num chain.
    :returns: List of issue tuples. GAP tuples include a trailing list
        of missing page numbers; others have 5 elements.
    :rtype: list[tuple]
    """
    seq_issues: list[tuple] = []
    prev_num = prev_pdf = None
    for r in ocr_results:
        if not r["detected"] or r.get("type") == "range":
            prev_num = None
            continue
        try:
            num = int(r["detected"])
        except ValueError:
            continue
        if r["pdf_page"] in out_of_range_pages:
            continue
        if prev_num is not None:
            diff = num - prev_num
            if diff == 0:
                seq_issues.append(
                    ("DUPLICATE", r["pdf_page"], num, prev_pdf, prev_num)
                )
            elif diff < 0:
                seq_issues.append(
                    ("BACKWARD", r["pdf_page"], num, prev_pdf, prev_num)
                )
            elif diff > 2:
                seq_issues.append(
                    (
                        "GAP",
                        r["pdf_page"],
                        num,
                        prev_pdf,
                        prev_num,
                        list(range(prev_num + 1, num)),
                    )
                )
        prev_num = num
        prev_pdf = r["pdf_page"]
    return seq_issues


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


def _push_processing_files_to_s3(scan_pk: int) -> None:
    """Upload a scan's intermediate processing files to S3.

    Wraps ``s3_sync.upload_processing_files`` with an exception guard so
    a failed S3 call never fails the pipeline that just succeeded. Logs
    a distinct ``logger.error`` when creds are missing in prod so the
    Sentry alert makes the root cause obvious (otherwise
    ``upload_processing_files`` silently returns 0).

    :param scan_pk: Primary key of the scan whose files to upload.
    :return: None.
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
        return
    try:
        scan = Scan.objects.get(pk=scan_pk)
        s3_sync.upload_processing_files(scan)
    except Exception:
        logger.exception(
            "Failed to push processing files to S3 for scan %s", scan_pk
        )


def _push_generated_file_to_s3(scan_pk: int, relative_path: str) -> None:
    """Upload a single just-generated processing file to S3 immediately.

    Persists derived artifacts (``bitonal.pdf``, the OCR PDF) the moment
    they are produced rather than only at the end-of-pipeline push, so a
    later crash, or a ``reprocess`` run that never does the full push,
    still leaves them in S3. Mirrors the credential guard in
    ``_push_processing_files_to_s3`` so a missing-creds misconfiguration
    in prod surfaces a distinct Sentry error.

    Prod-only in practice: in local dev ``upload_file_to_s3`` is a no-op
    (S3 disabled), and the file is already on disk under ``output_dir``,
    so it is served locally without any upload.

    :param scan_pk: Primary key of the scan the file belongs to.
    :param relative_path: Path relative to the scan's output dir.
    :return: None.
    """
    from scanning import s3_sync
    from scanning.utils import has_s3_credentials

    if (
        not settings.DEVELOPMENT
        and not getattr(settings, "TESTING", False)
        and not has_s3_credentials()
    ):
        logger.error(
            "Skipping S3 push of %s for scan %s: AWS credentials not "
            "configured",
            relative_path,
            scan_pk,
        )
        return
    try:
        scan = Scan.objects.get(pk=scan_pk)
        s3_sync.upload_file_to_s3(scan, relative_path)
    except Exception:
        logger.exception(
            "Failed to push %s to S3 for scan %s", relative_path, scan_pk
        )


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

    document = _build_document_from_detections(scan, det_data, pdf_path)
    opinions = _pair_opinions(document)
    rects = compute_redaction_rects(document, opinions, skip_doctr=True)

    # Snap headnote rect x-bounds to TEXT_COLUMN detections for consistency.
    # Only snap to a TEXT_COLUMN on the same side of the page midpoint —
    # otherwise a missing left TEXT_COLUMN causes the left rect to be
    # snapped to the right column, destroying it.
    tc_by_page = {}
    for d in Detection.objects.filter(scan=scan, active=True, label_id=16):
        tc_by_page.setdefault(d.page_index, []).append(d)
    img_mid = document.pages[0].img_width / 2 if document.pages else 850
    for page_entry in rects:
        pi = page_entry["page_index"]
        cols = tc_by_page.get(pi, [])
        if not cols:
            continue
        for r in page_entry["rects"]:
            if r.get("type") != "headnote":
                continue
            cx = (r["x0"] + r["x1"]) / 2
            # Only consider TEXT_COLUMNs on the same side of the midpoint
            same_side = (
                [c for c in cols if (c.x0 + c.x1) / 2 < img_mid]
                if cx < img_mid
                else [c for c in cols if (c.x0 + c.x1) / 2 >= img_mid]
            )
            if not same_side:
                continue
            best = min(same_side, key=lambda c: abs((c.x0 + c.x1) / 2 - cx))
            r["x0"] = round(best.x0, 1)
            r["x1"] = round(best.x1, 1)

    # Split headnote blocks at HEADNOTE detection boundaries
    _GAP = 6
    hn_dets_by_page = {}
    for d in Detection.objects.filter(
        scan=scan, active=True, label="HEADNOTE"
    ):
        hn_dets_by_page.setdefault(d.page_index, []).append(d)
    for page_entry in rects:
        pi = page_entry["page_index"]
        if pi not in hn_dets_by_page:
            continue
        hn_dets = sorted(hn_dets_by_page[pi], key=lambda d: d.y0)
        new_page_rects = []
        for r in page_entry["rects"]:
            if r.get("type") != "headnote":
                new_page_rects.append(r)
                continue
            col_dets = sorted(
                (
                    d
                    for d in hn_dets
                    if r["y0"] + _GAP < d.y0 < r["y1"] - _GAP
                    and d.x0 < r["x1"]
                    and d.x1 > r["x0"]
                ),
                key=lambda d: d.y0,
            )
            merged = []
            for d in col_dets:
                if merged and d.y0 < merged[-1][1]:
                    merged[-1] = (merged[-1][0], max(merged[-1][1], d.y1))
                else:
                    merged.append((d.y0, d.y1))
            splits = [m[0] for m in merged]
            if not splits:
                new_page_rects.append(r)
                continue
            prev_y = r["y0"]
            for sp in splits:
                if sp - _GAP / 2 > prev_y:
                    new_page_rects.append(
                        {
                            **r,
                            "y0": round(prev_y, 1),
                            "y1": round(sp - _GAP / 2, 1),
                        }
                    )
                prev_y = sp + _GAP / 2
            new_page_rects.append(
                {
                    **r,
                    "y0": round(prev_y, 1),
                    "y1": round(r["y1"], 1),
                }
            )
        page_entry["rects"] = new_page_rects

    Scan.objects.filter(pk=scan_pk).update(
        redaction_rects=rects,
    )
    return rects


def _compute_and_save_margin_rects(
    scan_pk: int, pdf_path: str, output_dir: str
) -> list:
    """Compute margin rects, adjust for detections, and save to the Scan model.

    After computing raw margins from the PDF, shrink any margin rect
    that overlaps with an active detection so that key icons, captions,
    and other content near page edges are not masked.

    :param scan_pk: Primary key of the scan.
    :param pdf_path: Path to the PDF to compute margins for.
    :param output_dir: Directory (used for detection lookup only).
    :return: The computed margin rects list.
    """
    scan = Scan.objects.get(pk=scan_pk)
    if scan.margin_rects:
        return scan.margin_rects
    output_dir = Path(output_dir)
    margin_rects = compute_margin_rects(str(pdf_path))
    margin_rects = _adjust_margins_for_detections(margin_rects, output_dir)
    Scan.objects.filter(pk=scan_pk).update(
        margin_rects=margin_rects,
    )
    return margin_rects


def _build_combined_redactions(scan_pk: int) -> Path:
    """Combine margin_rects, redaction_rects, and opinions into redactions.json.

    All coordinates in the output are in PDF points. This file is passed
    to blackletter's ``generate`` API as the single source of redaction data.

    :param scan_pk: Primary key of the scan.
    :return: Path to the generated redactions.json.
    """
    scan = Scan.objects.get(pk=scan_pk)
    output_dir = Path(scan.output_dir)

    det_path = output_dir / "detections.json"

    margins_data = scan.margin_rects
    rects_data = scan.redaction_rects
    opinions = scan.opinions_json

    # Add filenames based on reporter, volume, and page numbers
    reporter = scan.reporter.short_name or ""
    volume = str(scan.volume) or ""
    prefix = f"{reporter}.{volume}" if reporter and volume else ""
    for op in opinions:
        if prefix:
            first = op.get("first_page_number", 0)
            last = op.get("last_page_number", first)
            op["filename"] = f"{prefix}.{first:04d}-{last:04d}.pdf"

    # Image dims for pixel→PDF conversion
    img_dims = {}
    if det_path.exists():
        for d in json.loads(det_path.read_text()):
            pi = d["page_index"]
            if pi not in img_dims:
                img_dims[pi] = (d.get("img_width", 1), d.get("img_height", 1))

    # PDF page dims
    pdf_path = find_ocr_pdf(scan.output_dir) or scan.pdf_path
    pdf_dims = {}
    with fitz.open(str(pdf_path)) as src:
        for i in range(src.page_count):
            r = src[i].rect
            pdf_dims[i] = (r.width, r.height)

    pages: dict[int, list] = {}

    # Margin rects (already in PDF points)
    for entry in margins_data:
        pi = entry["page_index"]
        if pi not in pages:
            pages[pi] = []
        for r in entry.get("rects", []):
            pages[pi].append(
                {
                    "x0": round(r["x0"], 1),
                    "y0": round(r["y0"], 1),
                    "x1": round(r["x1"], 1),
                    "y1": round(r["y1"], 1),
                    "fill": "white",
                    "type": "margin",
                }
            )

    # Redaction rects (pixels → PDF points)
    for entry in rects_data:
        pi = entry["page_index"]
        if pi not in pages:
            pages[pi] = []

        iw, ih = img_dims.get(pi, (1, 1))
        pdf_w, pdf_h = pdf_dims.get(pi, (612.0, 792.0))
        to_x = pdf_w / iw if iw > 1 else 1.0
        to_y = pdf_h / ih if ih > 1 else 1.0

        for r in entry.get("rects", []):
            x0 = r["x0"] * to_x
            y0 = r["y0"] * to_y
            x1 = r["x1"] * to_x
            y1 = r["y1"] * to_y
            if x0 >= x1 or y0 >= y1:
                continue
            pages[pi].append(
                {
                    "x0": round(x0, 1),
                    "y0": round(y0, 1),
                    "x1": round(x1, 1),
                    "y1": round(y1, 1),
                    "fill": r["fill"],
                    "type": r["type"],
                }
            )

    combined = {
        "opinions": opinions,
        "pages": {str(k): v for k, v in sorted(pages.items())},
    }

    out_path = output_dir / "redactions.json"
    out_path.write_text(json.dumps(combined))
    n_rects = sum(len(v) for v in pages.values())
    print(
        f"  Combined redactions: {len(pages)} pages, "
        f"{n_rects} rects, {len(opinions)} opinions",
        flush=True,
    )
    return out_path


def _adjust_margins_for_detections(
    margin_rects: list, output_dir: Path
) -> list:
    """Shrink margin rects that overlap with active detections.

    For each page, convert detection bboxes from image coords to PDF
    coords and push back the edge of any margin rect that would cover
    a detection.

    :param margin_rects: List of ``{"page_index": int, "rects": [...]}``
        dicts from ``compute_margin_rects``.
    :param output_dir: Output directory containing detections.json.
    :return: The adjusted margin rects list (modified in place).
    """
    det_path = output_dir / "detections.json"
    if not det_path.exists():
        return margin_rects

    detections = json.loads(det_path.read_text())

    # Labels that should not push back margins (noisy edge detections)
    ignore_labels = {"PAGE_NUMBER", "PAGE_HEADER", "STATE_ABBREVIATION"}

    # Group detections by page_index, skipping ignored labels
    dets_by_page: dict[int, list] = {}
    for d in detections:
        if d.get("label", "") in ignore_labels:
            continue
        dets_by_page.setdefault(d["page_index"], []).append(d)

    padding = 0.0  # extra PDF-pt clearance around detections

    for page_entry in margin_rects:
        if not page_entry["rects"]:
            continue
        page_idx = page_entry["page_index"]
        page_dets = dets_by_page.get(page_idx)
        if not page_dets:
            continue

        # Get image dimensions from the first detection on this page
        img_w = page_dets[0].get("img_width", 1)
        img_h = page_dets[0].get("img_height", 1)
        if not img_w or not img_h:
            continue

        # Determine PDF page size from the margin rects themselves
        # (full-width rects span x0=0 to x1=page_width, etc.)
        pdf_w = max(r["x1"] for r in page_entry["rects"])
        pdf_h = max(r["y1"] for r in page_entry["rects"])
        sx = pdf_w / img_w
        sy = pdf_h / img_h

        # Convert detection bboxes to PDF coords
        pdf_dets = []
        for d in page_dets:
            bb = d["bbox"]
            pdf_dets.append((bb[0] * sx, bb[1] * sy, bb[2] * sx, bb[3] * sy))

        # Adjust each margin rect
        for rect in page_entry["rects"]:
            for dx0, dy0, dx1, dy1 in pdf_dets:
                # Check if detection overlaps this rect
                if (
                    dx0 < rect["x1"]
                    and dx1 > rect["x0"]
                    and dy0 < rect["y1"]
                    and dy1 > rect["y0"]
                ):
                    # Shrink the rect edge that intrudes on the detection
                    # Bottom margin (rect covers lower portion of page)
                    if rect["y0"] > 0 and rect["y1"] >= pdf_h - 1:
                        rect["y0"] = max(rect["y0"], dy1 + padding)
                    # Top margin (rect covers upper portion of page)
                    if rect["y0"] <= 1 and rect["y1"] < pdf_h - 1:
                        rect["y1"] = min(rect["y1"], dy0 - padding)
                    # Left margin (rect covers left portion of page)
                    if rect["x0"] <= 1 and rect["x1"] < pdf_w - 1:
                        rect["x1"] = min(rect["x1"], dx0 - padding)
                    # Right margin (rect covers right portion of page)
                    if (
                        rect["x0"] > 0
                        and rect["y0"] <= 1
                        and rect["y1"] >= pdf_h - 1
                    ):
                        rect["x0"] = max(rect["x0"], dx1 + padding)

    return margin_rects


def _re_pair_opinions(scan_pk: int) -> list:
    """Re-pair opinions from current DB detections.

    :param scan_pk: Primary key of the scan to re-pair.
    :return: The list of paired opinion dicts.
    """
    scan = Scan.objects.get(pk=scan_pk)
    det_data = _sync_detections_to_disk(scan_pk)
    if not det_data:
        return []

    pdf_path = find_ocr_pdf(scan.output_dir) or scan.pdf_path
    opinions = bl_pair(
        det_data,
        str(pdf_path),
        reporter=scan.reporter.short_name or "",
        volume=str(scan.volume) or "",
        first_page=scan.start_page or 1,
    )
    Scan.objects.filter(pk=scan_pk).update(
        opinions_json=opinions,
    )
    return opinions


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _dispatch_analyze(
    scan_pk: int,
    pdf_path: str,
    stream_partial: bool = False,
) -> list[dict]:
    """Shared scaffolding for submitting an analyze job via runpod_client.

    Fetches the scan, builds ``exp_start`` / ``exp_end``, wires up a
    progress callback (with optional live partial writes), calls
    ``runpod_client.analyze``, and returns the raw results list.

    :param scan_pk: Primary key of the scan to analyze.
    :param pdf_path: Path to the PDF to run analysis on.
    :param stream_partial: If ``True``, the progress callback writes
        per-page partial results to ``Scan.ocr_results`` as pages
        arrive (local mode only; remote mode emits ``current=None``
        so no partial writes happen there). If ``False``, only coarse
        submit/queued events fire.
    :returns: The ``results`` list from the analyze response.
    :rtype: list[dict]
    """
    from scanning import runpod_client

    scan = Scan.objects.get(pk=scan_pk)
    exp_start = scan.start_page or 1
    exp_end = scan.end_page

    all_results: list[dict] = []

    def _progress(current, total, message):
        _update_progress(scan_pk, message, current=current, total=total)
        if (
            stream_partial
            and current is not None
            and current <= len(all_results)
        ):
            Scan.objects.filter(pk=scan_pk).update(
                ocr_results=all_results[:current],
            )

    result = runpod_client.analyze(
        scan,
        pdf_path,
        exp_start=exp_start,
        exp_end=exp_end,
        num_workers=1,
        progress_callback=_progress,
    )
    all_results = result["results"]
    return all_results


def run_paddleocr_validation(scan_pk: int, pdf_path: str) -> None:
    """Validate page numbers using the runpod_client analyze action.

    In local mode (``RUNPOD_ENABLED=False``) this calls
    ``blackletter.analyze.analyze_pdf`` in-process with the same
    ``num_workers=1`` the previous implementation used. In remote
    mode the analysis runs on a RunPod Serverless GPU worker and
    only coarse ``(None, None, status)`` progress events fire
    (per-page partial results are lost).

    :param scan_pk: Primary key of the scan to validate.
    :param pdf_path: Path to the PDF to run validation on.
    """
    all_results = _dispatch_analyze(scan_pk, pdf_path, stream_partial=False)
    Scan.objects.filter(pk=scan_pk).update(ocr_results=all_results)
    _rebuild_issues_from_results(scan_pk, all_results)


def run_incremental_validation(scan_pk: int, pdf_path: str) -> None:
    """Run YOLO + PaddleOCR on every page via the runpod_client.

    In local mode, ``blackletter.analyze.analyze_pdf`` is called with
    its default parallelism and the per-page progress callback drives
    live partial results into ``ocr_results`` so the frontend can
    render the sidebar incrementally. In remote mode, the entire run
    happens on the GPU worker and only coarse progress events fire;
    the live partial display is skipped because ``current`` is
    ``None`` until the final return value arrives.

    :param scan_pk: Primary key of the scan to validate.
    :param pdf_path: Path to the PDF to run validation on.
    """
    all_results = _dispatch_analyze(scan_pk, pdf_path, stream_partial=True)

    # Save detections from each page result
    Detection.objects.filter(scan_id=scan_pk).delete()
    all_detections = []
    for r in all_results:
        page_idx = r["pdf_page"] - 1
        img_w = r.get("img_width", 0)
        img_h = r.get("img_height", 0)
        for d in r.get("detections", []):
            try:
                label_name = Label(d["label_id"]).name
            except (ValueError, KeyError):
                continue
            all_detections.append(
                Detection(
                    scan_id=scan_pk,
                    page_index=page_idx,
                    label=label_name,
                    label_id=d["label_id"],
                    confidence=d["confidence"],
                    x0=d["bbox"][0],
                    y0=d["bbox"][1],
                    x1=d["bbox"][2],
                    y1=d["bbox"][3],
                    img_width=img_w,
                    img_height=img_h,
                    model_name=Detection.ModelName.LARGE,
                    model_count=1,
                    found_by=[
                        {"model": "large", "confidence": d["confidence"]}
                    ],
                )
            )
    if all_detections:
        Detection.objects.bulk_create(all_detections)

    Scan.objects.filter(pk=scan_pk).update(
        ocr_results=all_results,
    )

    _sync_detections_to_disk(scan_pk)

    _rebuild_issues_from_results(scan_pk, all_results)


# ---------------------------------------------------------------------------
# Recalculate (rebuild issues without re-running OCR)
# ---------------------------------------------------------------------------


def _split_detected(
    ocr_results: list, exp_start: int | None, exp_end: int | None
) -> tuple[list, dict]:
    """Partition detected single page numbers into out-of-range and in-range.

    A detected number is out of range when it is below 1 or, when an expected
    range is known, falls more than 5 outside it. Range pages and pages
    without a detected number are skipped.

    :param ocr_results: Per-page OCR results.
    :param exp_start: Expected first page number, or None.
    :param exp_end: Expected last page number, or None.
    :returns: ``(out_of_range, seen_nums)`` where ``seen_nums`` maps each
        in-range number to the list of PDF pages it appears on.
    :rtype: tuple[list, dict]
    """
    out_of_range: list = []
    seen_nums: dict = {}
    for r in ocr_results:
        if not r["detected"] or r.get("type") == "range":
            continue
        try:
            num = int(r["detected"])
        except (ValueError, TypeError):
            continue
        if num < 1:
            out_of_range.append(r)
        elif exp_start is not None and (
            num < exp_start - 5 or num > exp_end + 5
        ):
            out_of_range.append(r)
        else:
            seen_nums.setdefault(num, []).append(r["pdf_page"])
    return out_of_range, seen_nums


def _build_analysis(
    ocr_results: list, exp_start: int | None, exp_end: int | None
) -> dict:
    """Analyze ocr_results into the structure ``_build_issues`` expects.

    Computes out-of-range, duplicate, sequence, and missing-page data from
    the current page numbers without re-running OCR or opening the PDF.

    :param ocr_results: Per-page OCR results (already finalized).
    :param exp_start: Expected first page number, or None.
    :param exp_end: Expected last page number, or None.
    :returns: The analysis dict consumed by ``_build_issues``.
    :rtype: dict
    """
    out_of_range, seen_nums = _split_detected(ocr_results, exp_start, exp_end)
    out_of_range_pages = {r["pdf_page"] for r in out_of_range}
    all_nums = sorted(seen_nums.keys())
    duplicates = {k: v for k, v in seen_nums.items() if len(v) > 1}
    seq_issues = _detect_sequence_issues(ocr_results, out_of_range_pages)

    range_re = re.compile(r"^(\d{1,4})\s*[–\-]\s*(\d{1,4})$")
    range_pages = set()
    ranges_found = [r for r in ocr_results if r.get("type") == "range"]
    for r in ranges_found:
        m = range_re.match(r["detected"].replace("–", "-"))
        if m:
            for pg in range(int(m.group(1)), int(m.group(2)) + 1):
                range_pages.add(pg)

    if exp_start is not None and all_nums:
        missing_pages = sorted(
            (set(range(exp_start, exp_end + 1)) - set(all_nums))
            - range_pages
            - {0}
        )
    elif all_nums:
        missing_pages = sorted(
            (set(range(all_nums[0], all_nums[-1] + 1)) - set(all_nums))
            - range_pages
            - {0}
        )
    else:
        missing_pages = []

    return {
        "results": ocr_results,
        "seq_issues": seq_issues,
        "duplicates": duplicates,
        "seen_nums": seen_nums,
        "all_nums": all_nums,
        "missing_pages": missing_pages,
        "ranges_found": ranges_found,
        "not_detected": [r for r in ocr_results if not r["detected"]],
        "out_of_range": out_of_range,
    }


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
    try:
        exp_start, exp_end = _parse_expected_range(scan.pdf_path)
    except FileNotFoundError:
        exp_start, exp_end = _parse_expected_range(scan.original_pdf.name)
    analysis = _build_analysis(ocr_results, exp_start, exp_end)
    result = _build_issues(
        analysis, scan.page_count, exp_start=exp_start, exp_end=exp_end
    )
    scan.page_map = result["page_map"]
    scan.missing_pages = result["missing_pages"]
    scan.save()


def recalculate_issues(scan: "Scan") -> None:
    """Rebuild issues from scan.ocr_results without re-running OCR.

    :param scan: The Scan instance to recalculate issues for.
    """

    ocr_results = scan.ocr_results
    if not ocr_results:
        return

    exp_start, exp_end = _parse_expected_range(scan.pdf_path)

    out_of_range, seen_nums = _split_detected(ocr_results, exp_start, exp_end)

    auto_corrected = []
    if out_of_range and seen_nums:
        in_range_by_page = {
            p: num for num, pages in seen_nums.items() for p in pages
        }
        in_range_sorted = sorted(in_range_by_page.items())
        offsets = {}
        for r in out_of_range:
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

    analysis = _build_analysis(ocr_results, exp_start, exp_end)

    result = _build_issues(
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

    with fitz.open(scan.pdf_path) as pdf_fitz:
        scan.page_count = len(pdf_fitz)

    scan.page_map = result["page_map"]
    scan.missing_pages = result["missing_pages"]

    scan.status = Status.PENDING_REVIEW
    scan.s3_uploaded = False
    scan.progress_message = "Done"
    scan.save()

    scan.issues.all().delete()
    Issue.objects.bulk_create(
        [Issue(scan=scan, **i) for i in result["issues"]]
    )


# ---------------------------------------------------------------------------
# Validate with bitonal (shared by queue_upload and start_validate)
# ---------------------------------------------------------------------------


def run_validate_with_bitonal(scan_pk: int) -> None:
    """Convert to bitonal (if needed) then run incremental validation.

    Designed to run in the daemon process.

    :param scan_pk: Primary key of the scan to validate.
    """
    django.db.connections.close_all()
    _pull_processing_files_from_s3(scan_pk)

    try:
        scan = Scan.objects.get(pk=scan_pk)
    except Exception:
        traceback.print_exc()
        return

    try:
        print(
            f"[validate] scan {scan_pk} pdf_path={scan.pdf_path}", flush=True
        )
        output_dir = ensure_output_dir(scan)
        bitonal_path = _ensure_bitonal(scan, output_dir)
        run_incremental_validation(scan_pk, str(bitonal_path))
        _push_processing_files_to_s3(scan_pk)

    except Exception as exc:
        _handle_pipeline_exception(scan_pk, exc, context="validate")


# ---------------------------------------------------------------------------
# Full pipeline (upload and walk away)
# ---------------------------------------------------------------------------


def run_full_pipeline(scan_pk: int) -> None:
    """Run the full processing pipeline: bitonal → OCR → YOLO → validate → pair → rects.

    Designed to run in the daemon process. After this completes, the scan
    is ready for review -- the user only needs to approve and generate.

    :param scan_pk: Primary key of the scan to process.
    """
    django.db.connections.close_all()
    _pull_processing_files_from_s3(scan_pk)

    try:
        scan = Scan.objects.get(pk=scan_pk)
    except Exception:
        traceback.print_exc()
        return

    try:
        # 1. Ensure output directory exists
        output_dir = ensure_output_dir(scan)

        # 2. Bitonal conversion
        bitonal_path = _ensure_bitonal(scan, output_dir)

        # 3. Tesseract OCR (on bitonal)
        scan.refresh_from_db()
        ocr_pdf = find_ocr_pdf(str(output_dir))
        if not ocr_pdf:
            ocr_pdf = _run_ocr(
                scan_pk,
                str(bitonal_path),
                str(output_dir),
                reporter=scan.reporter.short_name or "",
                volume=str(scan.volume) or "",
                first_page=scan.start_page or 1,
            )

        # 4. YOLO detection (all 3 models on bitonal)
        _run_yolo(scan_pk, str(bitonal_path), str(output_dir))

        # 5. Import detections into DB
        _update_progress(scan_pk, "Importing detections...")
        dets = _import_detections_from_json(scan_pk, str(output_dir))

        # 6. PaddleOCR validation (on original PDF for better OCR)
        _update_progress(
            scan_pk,
            f"{len(dets)} detections imported. Running page number validation...",
        )
        run_paddleocr_validation(scan_pk, scan.pdf_path)

        # 7. Pair opinions
        _update_progress(scan_pk, "Pairing opinions...")
        opinions = _re_pair_opinions(scan_pk)

        # 8. Compute redaction rects (margin rects computed lazily when viewer requests them)
        _update_progress(scan_pk, "Computing redaction rects...")
        pdf_path = str(ocr_pdf) if ocr_pdf else scan.pdf_path
        _compute_and_save_redaction_rects(scan_pk, pdf_path)

        Scan.objects.filter(pk=scan_pk).update(
            status=Status.PENDING_REVIEW,
            progress_message=(
                f"Done, {len(dets)} detections, {len(opinions)} opinions"
            ),
        )
        _push_processing_files_to_s3(scan_pk)

    except Exception as exc:
        _handle_pipeline_exception(scan_pk, exc, context="pipeline")


# ---------------------------------------------------------------------------
# Reprocess (apply fixes, re-bitonal, re-OCR, re-validate)
# ---------------------------------------------------------------------------


def _rebuild_issues_from_results(scan_pk: int, all_results: list) -> None:
    """Re-analyze ocr_results and rebuild issues/page_map without re-running OCR.

    Preserves the actual OCR results (including manual assignments) but
    recalculates sequence issues, missing pages, duplicates, etc.

    :param scan_pk: Primary key of the scan to rebuild issues for.
    :param all_results: List of per-page OCR result dicts.
    """
    scan = Scan.objects.get(pk=scan_pk)
    total = len(all_results)
    exp_start = scan.start_page or 1
    exp_end = scan.end_page

    out_of_range, seen_nums = _split_in_out_of_range(
        all_results, exp_start, exp_end
    )
    all_results, corrections = _auto_correct(
        all_results, out_of_range, seen_nums
    )
    if corrections:
        out_of_range, seen_nums = _split_in_out_of_range(
            all_results, exp_start, exp_end
        )

    all_nums = sorted(seen_nums.keys())
    duplicates = {k: v for k, v in seen_nums.items() if len(v) > 1}
    not_detected = [x for x in all_results if not x["detected"]]
    out_of_range_pages = {r["pdf_page"] for r in out_of_range}

    seq_issues = _detect_sequence_issues(all_results, out_of_range_pages)

    if exp_start is not None and exp_end is not None:
        expected = set(range(exp_start, exp_end + 1))
        found = {n for n in seen_nums if exp_start <= n <= exp_end}
        missing_pages = sorted(expected - found)
    else:
        missing_pages = []

    ranges_found = [r for r in all_results if r.get("type") == "range"]

    analysis = {
        "total_pages": total,
        "results": all_results,
        "seen_nums": seen_nums,
        "all_nums": all_nums,
        "duplicates": duplicates,
        "not_detected": not_detected,
        "seq_issues": seq_issues,
        "missing_pages": missing_pages,
        "ranges_found": ranges_found,
    }

    result = _build_issues(analysis, total, exp_start, exp_end)

    scan.refresh_from_db()
    scan.page_count = total
    scan.page_map = result.get("page_map", [])
    scan.missing_pages = result.get("missing_pages", [])
    scan.ocr_results = all_results

    scan.status = Status.PENDING_REVIEW
    scan.s3_uploaded = False
    scan.progress_message = "Done"
    scan.save()

    Issue.objects.filter(scan=scan).exclude(
        check_name=CheckName.SUPPRESS_DETECTION
    ).delete()
    Issue.objects.bulk_create(
        [Issue(scan=scan, **d) for d in result.get("issues", [])]
    )


def run_reprocess(scan_pk: int) -> None:
    """Apply PDF fixes (inserts/deletions) using smart edits.

    Only OCRs newly inserted pages. Deleted pages are removed from
    ocr_results. Manual page assignments are preserved.

    Designed to run in the daemon process.

    :param scan_pk: Primary key of the scan to reprocess.
    """
    django.db.connections.close_all()
    _pull_processing_files_from_s3(scan_pk)

    scan = Scan.objects.get(pk=scan_pk)

    try:
        has_deletions = scan.deletions.exists()
        has_inserts = scan.inserts.exists()

        if not has_deletions and not has_inserts:
            # Nothing changed — just rebuild issues from existing results
            _update_progress(scan_pk, "Re-checking...")
            all_results = scan.ocr_results
            _rebuild_issues_from_results(scan_pk, all_results)
            _re_pair_opinions(scan_pk)
            return

        output_dir = Path(scan.output_dir) if scan.output_dir else None
        page_map = scan.page_map
        all_results = scan.ocr_results

        # Stale margin/redaction rects are indexed by old page positions, clear them
        Scan.objects.filter(pk=scan_pk).update(
            margin_rects="", redaction_rects=""
        )

        # Apply deletions (process in reverse order to keep indices stable)
        if has_deletions:
            _update_progress(scan_pk, "Deleting pages...")
            deleted_pdf_pages = sorted(
                (d.pdf_page for d in scan.deletions.all()), reverse=True
            )
            for pdf_page in deleted_pdf_pages:
                page_idx = pdf_page - 1
                _delete_page_and_detections(
                    scan, scan_pk, page_idx, output_dir
                )

                # Remove from ocr_results and shift pdf_page numbers
                all_results = [
                    r for r in all_results if r["pdf_page"] != pdf_page
                ]
                for r in all_results:
                    if r["pdf_page"] > pdf_page:
                        r["pdf_page"] -= 1

            scan.deletions.all().delete()

        # After deletions, rebuild page_map so inserts use correct pdf indices
        if has_deletions:
            Scan.objects.filter(pk=scan_pk).update(ocr_results=all_results)
            _rebuild_issues_from_results(scan_pk, all_results)
            scan.refresh_from_db()
            page_map = scan.page_map

        # Apply inserts
        if has_inserts:
            _update_progress(scan_pk, "Inserting pages...")
            inserts = {
                ins.logical_page_number: ins for ins in scan.inserts.all()
            }
            for i, entry in enumerate(page_map):
                if entry.get("type") != "missing":
                    continue
                logical_num = entry.get("logical_number")
                if logical_num not in inserts:
                    continue
                run_smart_insert(
                    scan_pk, logical_num, inserts[logical_num].image.path
                )

            scan.inserts.all().delete()

        # run_smart_insert already handled OCR, detection, validation,
        # re-pairing, and rect computation for each insert. Reload the
        # final state from the DB.
        scan.refresh_from_db()
        all_results = scan.ocr_results

        # Final rebuild with the fully updated results
        _update_progress(scan_pk, "Rebuilding issues...")
        _rebuild_issues_from_results(scan_pk, all_results)
        _re_pair_opinions(scan_pk)

        if output_dir:
            pdf_path = find_ocr_pdf(str(output_dir)) or scan.pdf_path
            Scan.objects.filter(pk=scan_pk).update(
                margin_rects="", redaction_rects=""
            )
            _compute_and_save_margin_rects(scan_pk, pdf_path, str(output_dir))
            _compute_and_save_redaction_rects(scan_pk, pdf_path)

        Scan.objects.filter(pk=scan_pk).update(
            status=Status.PENDING_REVIEW,
            s3_uploaded=False,
            progress_message="Done",
        )
        _push_processing_files_to_s3(scan_pk)

    except Exception as exc:
        _handle_pipeline_exception(scan_pk, exc, context="reprocess")


# ---------------------------------------------------------------------------
# Smart page edits (patch single pages without full re-run)
# ---------------------------------------------------------------------------


def run_smart_delete(scan_pk: int, pdf_page: int) -> None:
    """Delete a single page and patch detections/validation in place.

    Removes the page from the original PDF, bitonal PDF, and OCR PDF;
    deletes detections on that page; shifts subsequent detection
    page_index values down by 1; re-runs PaddleOCR validation and re-pairs.

    :param scan_pk: Primary key of the scan to edit.
    :param pdf_page: 1-based PDF page number to delete.
    """
    scan = Scan.objects.get(pk=scan_pk)
    page_idx = pdf_page - 1
    output_dir = Path(scan.output_dir) if scan.output_dir else None

    _delete_page_and_detections(scan, scan_pk, page_idx, output_dir)

    # Update page count
    with fitz.open(scan.pdf_path) as pdf:
        new_count = pdf.page_count
    Scan.objects.filter(pk=scan_pk).update(page_count=new_count)

    # Sync detections to disk and re-validate/re-pair
    if output_dir:
        _sync_detections_to_disk(scan_pk)

    run_paddleocr_validation(scan_pk, scan.pdf_path)
    _re_pair_opinions(scan_pk)

    if output_dir:
        pdf_path = find_ocr_pdf(str(output_dir)) or scan.pdf_path
        _compute_and_save_redaction_rects(scan_pk, pdf_path)


def _ocr_single_page(ocr_pdf_path: str, page_idx: int) -> None:
    """Run Tesseract OCR on a single page of a PDF to add a text layer.

    Extracts the page to a temp file, OCRs it, then replaces the page
    in the original PDF with the OCR'd version.

    :param ocr_pdf_path: Path to the OCR PDF to update.
    :param page_idx: Zero-based page index to OCR.
    """
    import tempfile

    import ocrmypdf

    ocr_pdf_path = str(ocr_pdf_path)
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        # Extract the single page
        single_path = tmp / "page.pdf"
        with fitz.open(ocr_pdf_path) as doc:
            with fitz.open() as single:
                single.insert_pdf(doc, from_page=page_idx, to_page=page_idx)
                single.save(str(single_path))

            # OCR it
            ocrd_path = tmp / "page_ocr.pdf"
            ocrmypdf.ocr(
                str(single_path),
                str(ocrd_path),
                pdf_renderer="auto",
                optimize=1,
                output_type="pdf",
                language=["eng"],
                tesseract_timeout=120,
                progress_bar=False,
            )

            # Replace the page in the OCR PDF
            with fitz.open(str(ocrd_path)) as ocrd_doc:
                doc.delete_page(page_idx)
                doc.insert_pdf(
                    ocrd_doc, from_page=0, to_page=0, start_at=page_idx
                )
                doc.saveIncr()


def run_smart_insert(
    scan_pk: int, logical_page_number: int, image_path: str
) -> None:
    """Insert a page and run detection/validation on just that page.

    Inserts the page image into the original PDF, bitonal PDF, and OCR PDF
    at the correct position; shifts subsequent detection page_index values up
    by 1; re-validates and re-pairs.

    :param scan_pk: Primary key of the scan to edit.
    :param logical_page_number: The logical page number to insert at.
    :param image_path: Path to the image or PDF file to insert.
    """
    scan = Scan.objects.get(pk=scan_pk)
    output_dir = Path(scan.output_dir) if scan.output_dir else None

    # Determine insertion position from page_map
    page_map = scan.page_map
    with fitz.open(scan.pdf_path) as pdf:
        insert_idx = len(pdf)  # default: append
    for i, entry in enumerate(page_map):
        if entry.get("logical_number") == logical_page_number:
            # Insert before the next pdf_page entry
            for j in range(i + 1, len(page_map)):
                if page_map[j].get("type") == "pdf_page":
                    insert_idx = page_map[j]["pdf_index"]
                    break
            break

    # Insert into all PDFs
    for pdf_path in _collect_pdf_paths(scan, output_dir):
        with fitz.open(str(pdf_path)) as doc:
            ref_page = doc[min(insert_idx, doc.page_count - 1)]
            w, h = ref_page.rect.width, ref_page.rect.height

            if str(image_path).lower().endswith(".pdf"):
                with fitz.open(str(image_path)) as insert_pdf:
                    doc.insert_pdf(
                        insert_pdf,
                        from_page=0,
                        to_page=insert_pdf.page_count - 1,
                        start_at=insert_idx,
                    )
            else:
                new_page = doc.new_page(pno=insert_idx, width=w, height=h)
                new_page.insert_image(new_page.rect, filename=str(image_path))
            doc.saveIncr()

    # OCR the inserted page in the OCR PDF so it gets a text layer
    if output_dir:
        ocr_pdf_path = find_ocr_pdf(str(output_dir))
        if ocr_pdf_path:
            _ocr_single_page(ocr_pdf_path, insert_idx)

    # Shift subsequent detections up
    Detection.objects.filter(
        scan_id=scan_pk, page_index__gte=insert_idx
    ).update(page_index=F("page_index") + 1)

    # Update page count
    with fitz.open(scan.pdf_path) as pdf:
        new_count = pdf.page_count
    Scan.objects.filter(pk=scan_pk).update(page_count=new_count)

    # Sync detections to disk
    if output_dir:
        _sync_detections_to_disk(scan_pk)

    # Re-validate, re-pair, recompute rects
    scan.refresh_from_db()
    pdf_path = find_ocr_pdf(str(output_dir)) if output_dir else scan.pdf_path
    run_incremental_validation(scan_pk, pdf_path or scan.pdf_path)
    _re_pair_opinions(scan_pk)

    if output_dir:
        Scan.objects.filter(pk=scan_pk).update(
            margin_rects="", redaction_rects=""
        )
        _compute_and_save_margin_rects(
            scan_pk, pdf_path or scan.pdf_path, str(output_dir)
        )
        _compute_and_save_redaction_rects(scan_pk, pdf_path or scan.pdf_path)


def _collect_pdf_paths(scan: "Scan", output_dir: str | None) -> list[Path]:
    """Collect all PDF paths that need page edits applied.

    :param scan: The Scan instance to collect paths for.
    :param output_dir: Output directory to check for bitonal/OCR PDFs.
    :return: List of Path objects for all relevant PDFs.
    """
    paths = [Path(scan.pdf_path)]
    if output_dir:
        output_dir = Path(output_dir)
        bitonal = output_dir / "bitonal.pdf"
        if bitonal.exists():
            paths.append(bitonal)
        ocr_pdf = find_ocr_pdf(str(output_dir))
        if ocr_pdf and Path(ocr_pdf) not in paths:
            paths.append(Path(ocr_pdf))
    return paths


# ---------------------------------------------------------------------------
# Generate (split into redacted/unredacted opinions)
# ---------------------------------------------------------------------------


def _stamp_original_images(scan: "Scan", ocr_pdf_path: str) -> str:
    """Overlay original-quality image regions onto a *copy* of the OCR'd PDF.

    For each active IMAGE detection, renders the bounding box from the
    original scan PDF and inserts it into a copy of the OCR'd PDF at the
    same position.  This preserves full-quality photographs/illustrations
    that would otherwise be degraded by bitonal conversion.

    :param scan: The Scan instance with the original PDF path.
    :param ocr_pdf_path: Path to the OCR'd PDF to stamp onto.
    :return: Path to the stamped copy, or the original path if no
        images needed stamping (so the OCR PDF is never modified).
    """

    stamped_path = os.path.join(os.path.dirname(ocr_pdf_path), "stamped.pdf")

    image_dets = list(
        Detection.objects.filter(scan=scan, label="IMAGE", active=True)
        .order_by("page_index")
        .values(
            "page_index", "x0", "y0", "x1", "y1", "img_width", "img_height"
        )
    )
    if not image_dets:
        # Always copy so the OCR PDF is never modified by downstream steps
        shutil.copy2(ocr_pdf_path, stamped_path)
        return stamped_path

    # Save extracted images to images/ directory
    images_dir = Path(os.path.dirname(ocr_pdf_path)) / "images"
    images_dir.mkdir(exist_ok=True)
    page_numbers = _page_number_lookup(scan)
    img_count_by_page: dict[int, int] = {}

    with (
        fitz.open(scan.pdf_path) as original_doc,
        fitz.open(ocr_pdf_path) as ocr_doc,
    ):
        for det in image_dets:
            page_idx = det["page_index"]
            if (
                page_idx >= original_doc.page_count
                or page_idx >= ocr_doc.page_count
            ):
                continue

            orig_page = original_doc[page_idx]
            ocr_page = ocr_doc[page_idx]

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

            # Stamp onto the OCR'd PDF
            ocr_page.insert_image(pdf_rect, stream=png_bytes)

            # Save image to images/ directory
            pn = page_numbers.get(page_idx)
            page_num = pn[0] if pn else page_idx + (scan.start_page or 1)
            img_count_by_page[page_idx] = (
                img_count_by_page.get(page_idx, 0) + 1
            )
            img_name = f"{page_num}-{img_count_by_page[page_idx]:03d}.png"
            (images_dir / img_name).write_bytes(png_bytes)

        ocr_doc.save(stamped_path, garbage=3, deflate=True)
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
        ocr_pdf = find_ocr_pdf(str(output))
        if not ocr_pdf:
            raise ValueError("No OCR'd PDF found in output directory")

        # Stamp original-quality images into a copy, leaves OCR PDF untouched
        gen_pdf = _stamp_original_images(scan, str(ocr_pdf))

        # Write current DB detections -> detections.json (includes page numbers)
        det_data = _sync_detections_to_disk(scan_pk)
        Scan.objects.filter(pk=scan_pk).update(
            progress_message=f"Generating files ({len(det_data or [])} detections)..."
        )

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


def _page_body_textless(
    pdf_path: Path,
    header_height_pts: float = 60.0,
    text_floor: int = 5,
) -> bool:
    """Return True when the rendered page has no text below the header.

    The per-page PDFs handed to the LLM are post-redaction, so a body
    entirely covered by ``headnote`` rects extracts to nothing. Used as
    the boundary-case confirmation for the sandwich rule: when only
    one neighbor of a page has headnotes (the page is the leading or
    trailing edge of a multi-page block), we verify the redaction
    actually wiped the body text before calling the page blank.

    Returns False on any exception so a corrupt PDF doesn't mis-flag
    a page as blank.

    :param pdf_path: Filesystem path to the per-page PDF.
    :param header_height_pts: PDF-point band at the top of the page
        treated as "header" and excluded from the text check. The
        printed page number lives here.
    :param text_floor: Chars of body text below which the page is
        considered effectively empty.
    :return: True if the cropped body extracts to fewer than
        ``text_floor`` chars.
    """
    try:
        with pdfplumber.open(pdf_path) as pdf:
            pg = pdf.pages[0]
            body = pg.crop((0, header_height_pts, pg.width, pg.height))
            return len((body.extract_text() or "").strip()) < text_floor
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
        Confirm with a text-extraction check on the rendered PDF;
        since the per-page PDFs are post-redaction, an actually-empty
        body extracts to nothing.

    :param page_index: 0-based PDF page index.
    :param opinions: ``scan.opinions_json`` — the curated opinion
        list, each entry with ``caption_page`` / ``key_page`` /
        ``page_count``.
    :param redactions_pages: ``redactions["pages"]`` — per-page rect
        list keyed by stringified page_index.
    :param page_detections: All YOLO detections on this page (used
        only to check for ``FOOTNOTES``).
    :param pdf_path: Path to the per-page PDF, used for the boundary
        text-extraction check. If omitted, the boundary case can't
        fire and only strict sandwich pages are flagged.
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
        # Boundary case: only one side has headnotes. Confirm via the
        # rendered PDF — if the body is genuinely empty (post-redaction),
        # it really is a blank page even though sandwich doesn't fire.
        if (prev_hn or next_hn) and pdf_path is not None:
            if _page_body_textless(pdf_path):
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
# Detect (run YOLO detection + opinion pairing)
# ---------------------------------------------------------------------------


def run_detect(scan_pk: int) -> None:
    """Run YOLO detection and pair opinions.

    Designed to run in the daemon process.

    :param scan_pk: Primary key of the scan to detect on.
    """
    django.db.connections.close_all()
    _pull_processing_files_from_s3(scan_pk)

    scan = Scan.objects.get(pk=scan_pk)
    try:
        output_dir = Path(scan.output_dir)
        bitonal = output_dir / "bitonal.pdf"

        # OCR if needed (no existing OCR PDF)
        if not find_ocr_pdf(str(output_dir)) and bitonal.exists():
            _run_ocr(
                scan_pk,
                str(bitonal),
                str(output_dir),
                reporter=scan.reporter.short_name or "",
                volume=str(scan.volume) or "",
                first_page=scan.start_page or 1,
            )

        pdf_path = str(bitonal) if bitonal.exists() else scan.pdf_path

        _run_yolo(scan_pk, pdf_path, str(output_dir))
        dets = _import_detections_from_json(scan_pk, str(output_dir))

        _update_progress(
            scan_pk,
            f"{len(dets)} detections. Pairing opinions...",
        )

        opinions = _re_pair_opinions(scan_pk)

        Scan.objects.filter(pk=scan_pk).update(
            status=Status.PENDING_REVIEW,
            s3_uploaded=False,
            progress_message=f"Done, {len(dets)} detections, {len(opinions)} opinions",
        )
        _push_processing_files_to_s3(scan_pk)

    except Exception as exc:
        _handle_pipeline_exception(scan_pk, exc, context="detect")


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
