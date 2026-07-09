"""Process viewer and scan processing action views."""

import json
import logging
import os
from pathlib import Path

import fitz
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import (
    FileResponse,
    HttpRequest,
    HttpResponse,
    JsonResponse,
)
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.views.decorators.http import require_POST

from scanning.models import (
    CheckName,
    Detection,
    Issue,
    OpinionScan,
    PageDeletion,
    PageInsert,
    QueuedAction,
    Scan,
    Stage,
    Status,
)
from scanning.utils import compute_coverage_gaps, find_ocr_pdf

logger = logging.getLogger(__name__)


def _unmatched_detection_dict(
    det: Detection, idx_to_logical: dict[int, int]
) -> dict:
    """Build the template dict for an unmatched detection.

    :param det: The Detection instance.
    :param idx_to_logical: Mapping from pdf_index to logical page number.
    :returns: Dict of detection metadata for the template.
    :rtype: dict
    """
    return {
        "id": det.pk,
        "pdf_page": det.page_index + 1,
        "page_index": det.page_index,
        "label_id": det.label_id,
        "logical_page": idx_to_logical.get(det.page_index, det.page_index + 1),
        "conf": round(det.confidence, 2),
        "bbox": [det.x0, det.y0, det.x1, det.y1],
        "img_width": det.img_width,
        "img_height": det.img_height,
    }


def _caption_is_continuation(
    det: Detection, paired_keys_sorted: list[tuple[int, int, int]]
) -> bool:
    """Check if a caption detection falls in a key-icon span.

    If the caption is between two paired key icons, it's a
    continuation of the opinion in that span, not a missed opinion.

    :param det: A Detection instance with page_index, y0.
    :param paired_keys_sorted: Sorted list of (page, x, y) tuples
        for paired key icons.
    :returns: True if the caption falls in an existing span.
    :rtype: bool
    """
    for i, (kp, kx, ky) in enumerate(paired_keys_sorted):
        # A caption is only a continuation when it falls between two
        # *actual* paired keys. The open-ended span past the last key
        # is a new opinion whose closing key isn't on this scan
        # (likely continues into the next volume), so leave it as
        # unmatched.
        if i + 1 >= len(paired_keys_sorted):
            break
        next_kp, _, next_ky = paired_keys_sorted[i + 1]
        after_key = det.page_index > kp or (
            det.page_index == kp and det.y0 > ky
        )
        before_next = det.page_index < next_kp or (
            det.page_index == next_kp and det.y0 < next_ky
        )
        if after_key and before_next:
            return True
    return False


@login_required
def scan_process_view(request: HttpRequest, pk: int) -> HttpResponse:
    """Unified scan processing page with 3-step workflow.

    :param request: The HTTP request.
    :param pk: Scan primary key.
    :return: Rendered processing page.
    """
    scan = get_object_or_404(Scan.objects.select_related("reporter"), pk=pk)
    is_processing = scan.status in (Status.PROCESSING, Status.QUEUED)

    # No eager S3 pull here: this page renders entirely from the DB
    # (page_map, ocr_results, opinions_json, detections, redaction_rects),
    # so it never reads the processing files off disk. Pulling them here
    # blocked the response on I/O it doesn't need -- worst right after a
    # fresh upload, when the only object in the prefix is the multi-GB
    # original and the download took minutes. The PDF and crop assets are
    # streamed by serve_scan_pdf / serve_original_crop, which lazily pull
    # from S3 on demand.

    try:
        step = int(request.GET.get("step", 0))
    except ValueError:
        step = 0
    if step < 1 or step > 3:
        if is_processing:
            step = 1
        elif scan.stage == Stage.APPROVED:
            step = 3
        elif scan.stage == Stage.PROCESS or scan.opinions_json:
            # Stay on step 1 if there are unresolved issues
            has_issues = scan.issues.exclude(
                check_name=CheckName.SUPPRESS_DETECTION
            ).exists()
            has_missing = bool(scan.missing_pages)
            if has_issues or has_missing:
                step = 1
            else:
                step = 2
        else:
            step = 1

    issues = list(scan.issues.all())
    inserts = {ins.logical_page_number: ins for ins in scan.inserts.all()}

    page_map = scan.page_map
    missing_pages = scan.missing_pages

    for entry in page_map:
        if entry["type"] == "missing" and entry["logical_number"] in inserts:
            entry["type"] = "inserted"
            entry["insert_url"] = inserts[entry["logical_number"]].image.url

    # Map pdf_index → logical page number for navigation
    idx_to_logical = {}
    logical_to_indices: dict[int, list[int]] = {}
    for entry in page_map:
        if entry.get("type") == "pdf_page":
            idx_to_logical[entry["pdf_index"]] = entry["logical_number"]
            logical_to_indices.setdefault(entry["logical_number"], []).append(
                entry["pdf_index"]
            )

    # PDF page indices the page_map flags as duplicates (a detected page
    # number that already appeared on an earlier page). The PDF viewer marks
    # these with a DUPLICATE badge; the sidebar page list mirrors the same set
    # so the two views stay consistent. Unlike a consecutive-only check this
    # also catches duplicates whose copies are far apart (e.g. the same
    # printed "page 1" appearing on several pages).
    duplicate_indices = {
        entry["pdf_index"]
        for entry in page_map
        if entry.get("type") == "pdf_page" and entry.get("duplicate")
    }

    # The viewer highlights flagged pages by pdf_index (a page's physical
    # position), which is unique. An issue's ``page_number`` means a physical
    # PDF page for some checks and a logical/printed page number for others;
    # logical numbers can repeat when unnumbered front matter borrows numbers
    # from the real pages (issue #90), so they must be resolved through the
    # page_map rather than matched directly.
    physical_page_checks = {
        CheckName.NO_PAGE_NUMBER,
        CheckName.SUSPICIOUS_READING,
        CheckName.AUTO_CORRECTED,
        CheckName.BLANK_PAGE,
        CheckName.ORIENTATION,
    }
    flagged_indices: set[int] = set()
    for i in issues:
        # Resolve each issue to PDF page indices (unique physical positions),
        # used both for the red-border highlight and for click-to-navigate.
        # ``nav_pdf_index`` is the first resolved index, or None when the issue
        # has no page (or points at a missing page absent from the page_map).
        i.nav_pdf_index = None
        if i.page_number is None:
            continue
        if i.check_name in physical_page_checks:
            indices = [i.page_number - 1]
        else:
            indices = logical_to_indices.get(i.page_number, [])
        flagged_indices.update(indices)
        if indices:
            i.nav_pdf_index = indices[0]

    ocr_results = scan.ocr_results
    ocr_by_page = {}
    for r in ocr_results:
        ocr_by_page[r["pdf_page"]] = r

    # Annotate sequence issues for the sidebar page list. Duplicates are taken
    # from ``duplicate_indices`` (the same page_map data the viewer uses);
    # ``seq_issue`` only covers ordering anomalies (backward / gap).
    prev_num = None
    for r in ocr_results:
        r["seq_issue"] = ""
        r["is_duplicate"] = (r["pdf_page"] - 1) in duplicate_indices
        if not r.get("detected") or r.get("type") == "range":
            prev_num = None
            continue
        try:
            num = int(r["detected"])
        except (ValueError, TypeError):
            prev_num = None
            continue
        if prev_num is not None:
            diff = num - prev_num
            if diff < 0:
                r["seq_issue"] = "backward"
            elif diff > 2:
                r["seq_issue"] = "gap"
        prev_num = num

    has_pending_inserts = scan.inserts.exists()
    has_pending_changes = scan.deletions.exists() or has_pending_inserts
    has_detections = Detection.objects.filter(scan=scan).exists()

    opinions = scan.opinions_json

    # Build a set of page indices that contain IMAGE detections
    image_page_indices = set(
        Detection.objects.filter(
            scan=scan, label="IMAGE", active=True
        ).values_list("page_index", flat=True)
    )

    # Attach image_pages to each opinion. Each entry carries the logical
    # number to display (``num``) and the pdf_index to navigate to (``idx``);
    # logical numbers can repeat (#90), so navigation must use the index.
    for op in opinions:
        cp = op.get("caption_page", 0)
        ep = op.get("page_end", op.get("key_page", cp))
        op["image_pages"] = [
            {"num": idx_to_logical.get(idx, idx + 1), "idx": idx}
            for idx in range(cp, ep + 1)
            if idx in image_page_indices
        ]

    has_redaction_rects = bool(scan.redaction_rects)

    # Find HEADNOTE detections not covered by headnote redaction rects
    uncovered_hn_pages = set()
    if has_redaction_rects:
        rects_data = scan.redaction_rects
        hn_rects_by_page = {}
        for entry in rects_data:
            hn_rects_by_page[entry["page_index"]] = [
                r for r in entry["rects"] if r.get("type") == "headnote"
            ]
        for d in Detection.objects.filter(
            scan=scan, label="HEADNOTE", active=True
        ).filter(confidence__gte=0.8):
            page_rects = hn_rects_by_page.get(d.page_index, [])
            cx = (d.x0 + d.x1) / 2
            cy = (d.y0 + d.y1) / 2
            covered = any(
                r["x0"] <= cx <= r["x1"] and r["y0"] <= cy <= r["y1"]
                for r in page_rects
            )
            if not covered:
                uncovered_hn_pages.add(d.page_index)

    for op in opinions:
        cp = op.get("caption_page", 0)
        ep = op.get("page_end", op.get("key_page", cp))
        op["uncovered_headnote_pages"] = [
            {"num": idx_to_logical.get(idx, idx + 1), "idx": idx}
            for idx in range(cp, ep + 1)
            if idx in uncovered_hn_pages
        ]

    opinion_scans = []
    if step == 3:
        for s in OpinionScan.objects.filter(scan=scan).order_by(
            "opinion_order"
        ):
            s.redacted_filename = (
                os.path.basename(s.redacted_pdf.name)
                if s.redacted_pdf and s.redacted_pdf.name
                else ""
            )
            s.unredacted_filename = (
                os.path.basename(s.original_pdf.name)
                if s.original_pdf and s.original_pdf.name
                else ""
            )
            opinion_scans.append(s)

    # Detection warnings for step 2
    detect_warnings = []
    unmatched_keys = []
    unmatched_captions = []
    if step >= 2 and opinions:
        # Build suppressed set from issues
        suppressed = set()
        for iss in scan.issues.filter(check_name=CheckName.SUPPRESS_DETECTION):
            if iss.metadata:
                m = iss.metadata
                bb = m.get("bbox", [0, 0, 0, 0])
                suppressed.add(
                    (
                        m.get("page_index", 0),
                        m.get("label_id", 0),
                        round(bb[0]),
                        round(bb[1]),
                    )
                )

        paired_caption_keys = set()
        paired_key_keys = set()
        for op in opinions:
            cb = op.get("caption_bbox", [0, 0, 0, 0])
            kb = op.get("key_bbox", [0, 0, 0, 0])
            paired_caption_keys.add(
                (op.get("caption_page", 0), round(cb[0]), round(cb[1]))
            )
            paired_key_keys.add(
                (op.get("key_page", 0), round(kb[0]), round(kb[1]))
            )

        for d in Detection.objects.filter(
            scan=scan, active=True, label="KEY_ICON"
        ).order_by("page_index"):
            if (d.page_index, round(d.x0), round(d.y0)) not in paired_key_keys:
                if (
                    d.page_index,
                    d.label_id,
                    round(d.x0),
                    round(d.y0),
                ) not in suppressed:
                    unmatched_keys.append(
                        _unmatched_detection_dict(d, idx_to_logical)
                    )

        # Build sorted list of paired key icon positions so we can
        # determine which key-icon span an unmatched caption falls in.
        # If a span already has a paired caption, extra captions in
        # that span are continuations — not missed opinions.
        paired_keys_sorted = sorted(paired_key_keys)

        for d in Detection.objects.filter(
            scan=scan, active=True, label="CASE_CAPTION"
        ).order_by("page_index", "y0"):
            if (d.page_index, round(d.x0), round(d.y0)) in paired_caption_keys:
                continue
            if (
                d.page_index,
                d.label_id,
                round(d.x0),
                round(d.y0),
            ) in suppressed:
                continue
            if _caption_is_continuation(d, paired_keys_sorted):
                continue
            unmatched_captions.append(
                _unmatched_detection_dict(d, idx_to_logical)
            )

        if unmatched_keys:
            detect_warnings.append(
                f"{len(unmatched_keys)} KEY_ICON(s) not matched to any opinion"
            )
        if unmatched_captions:
            detect_warnings.append(
                f"{len(unmatched_captions)} CASE_CAPTION(s) not matched to any opinion"
            )

        # Coverage gaps
        for start, end, count in compute_coverage_gaps(
            opinions, scan.start_page, scan.end_page
        ):
            detect_warnings.append(
                f"Pages {start}-{end}"
                f" ({count} pages) not covered"
                " by any opinion"
            )

    return render(
        request,
        "scanning/scan_process.html",
        {
            "scan": scan,
            "step": step,
            "issues": issues,
            "page_map_json": json.dumps(page_map),
            "missing_pages": missing_pages,
            "flagged_indices_json": json.dumps(sorted(flagged_indices)),
            "ocr_results": ocr_results,
            "ocr_by_page_json": json.dumps(ocr_by_page),
            "has_pending_changes": has_pending_changes,
            "has_pending_inserts": has_pending_inserts,
            "has_detections": has_detections,
            "is_processing": is_processing,
            "opinions": opinions,
            "opinions_json": json.dumps(opinions),
            "has_redaction_rects": has_redaction_rects,
            "opinion_scans": opinion_scans,
            "detect_warnings": detect_warnings,
            "unmatched_keys": unmatched_keys,
            "unmatched_captions": unmatched_captions,
            "deleted_pages_json": json.dumps(
                list(scan.deletions.values_list("pdf_page", flat=True))
            ),
        },
    )


@login_required
def progress_api(request: HttpRequest, pk: int) -> JsonResponse:
    """Return current processing progress for a scan.

    :param request: The HTTP request.
    :param pk: Scan primary key.
    :return: JSON response with status, progress, and log fields.
    """
    scan = get_object_or_404(Scan, pk=pk)
    data = {
        "status": scan.status,
        "current": scan.progress_current,
        "total": scan.progress_total,
        "message": scan.progress_message,
        "log": scan.progress_log,
    }
    # Include ocr_results when available so the frontend can render
    # the pages sidebar live without a full page reload.
    if scan.ocr_results:
        data["ocr_results"] = scan.ocr_results
    return JsonResponse(data)


@login_required
def serve_scan_pdf(request: HttpRequest, pk: int) -> HttpResponse:
    """Serve the small processed PDF (OCR text layer, else bitonal).

    The viewer only ever gets the small, browser-viewable preview. The
    multi-GB original is never streamed here: it blows past the gunicorn
    worker timeout (the connection dies mid-stream, so the browser sees a
    truncated body and reports ERR_CONTENT_LENGTH_MISMATCH) and pdf.js
    can't handle a file that large anyway. The original stays reachable
    only for server-side crops (``serve_original_crop``).

    Resolution:

    1. Serve the processed PDF (OCR > bitonal) if it is already local.
    2. Otherwise pull *only* the preview PDF(s) from S3 and look again.
       This covers prod, where the daemon and web run in separate
       containers with separate ephemeral ``/tmp/`` volumes. The targeted
       pull skips the original and images/, so opening a scan never drags
       gigabytes across the network.
    3. If no preview PDF exists yet (scan still processing, or errored),
       return HTTP 202 with a status message so the viewer can show a
       "still processing" state instead of a hard error.

    :param request: The HTTP request.
    :param pk: Scan primary key.
    :return: File response streaming the preview PDF, or a 202 JSON
        response when no preview is available yet.
    """
    scan = get_object_or_404(Scan, pk=pk)

    def _processed_local() -> FileResponse | None:
        """Return the OCR pdf, else ``bitonal.pdf``, from ``output_dir``."""
        output = Path(scan.output_dir)
        if not output.is_dir():
            return None
        ocr = find_ocr_pdf(scan.output_dir)
        if ocr:
            return FileResponse(ocr.open("rb"), content_type="application/pdf")
        bitonal = output / "bitonal.pdf"
        if bitonal.exists():
            return FileResponse(
                bitonal.open("rb"), content_type="application/pdf"
            )
        return None

    # 1. Prefer the small processed PDF if it is already on disk.
    response = _processed_local()
    if response is not None:
        return response

    # 2. Not local: pull only the preview PDF(s) from S3 and re-check.
    try:
        from scanning import s3_sync

        s3_sync.download_preview_pdf(scan)
    except Exception:
        logger.exception("Lazy S3 preview pull failed for scan %s", scan.pk)

    response = _processed_local()
    if response is not None:
        return response

    # 3. No preview PDF anywhere. Distinguish transient states (still
    #    producing a preview -- the viewer should poll) from terminal ones
    #    (a preview will never appear -- the viewer should show the message
    #    and stop). 202 means "retry"; 409 means "give up".
    if scan.status in (Status.UPLOADED, Status.QUEUED, Status.PROCESSING):
        return JsonResponse(
            {
                "status": "not_ready",
                "scan_status": scan.status,
                "message": (
                    "We're still processing this file. The preview will "
                    "appear here automatically once it's ready."
                ),
            },
            status=202,
        )

    if scan.status in (Status.ERROR, Status.ERROR_MAX_RETRIES):
        message = (
            "This scan hit an error during processing, so there's no "
            "preview to show."
        )
    else:
        message = "No preview is available for this scan."
    return JsonResponse(
        {
            "status": "unavailable",
            "scan_status": scan.status,
            "message": message,
        },
        status=409,
    )


@login_required
def serve_original_crop(request: HttpRequest, pk: int) -> HttpResponse:
    """Render a cropped region from the original (non-bitonal) PDF as PNG.

    :param request: The HTTP request (crop coordinates via query params).
    :param pk: Scan primary key.
    :return: PNG image response of the cropped region.
    """
    scan = get_object_or_404(Scan, pk=pk)
    try:
        page = int(request.GET.get("page", 0))
        x0 = float(request.GET.get("x0", 0))
        y0 = float(request.GET.get("y0", 0))
        x1 = float(request.GET.get("x1", 0))
        y1 = float(request.GET.get("y1", 0))
        dpi = min(max(int(request.GET.get("dpi", 150)), 72), 300)
    except ValueError:
        return HttpResponse(status=400)

    with fitz.open(scan.pdf_path) as doc:
        if page < 0 or page >= doc.page_count:
            return HttpResponse(status=404)
        clip = fitz.Rect(x0, y0, x1, y1)
        pix = doc[page].get_pixmap(clip=clip, dpi=dpi)
        png_bytes = pix.tobytes("png")
        # Release the pixmap's C-side buffer immediately rather than
        # waiting for GC; per-request crops at dpi=300 are 5-20 MB.
        pix = None
    resp = HttpResponse(png_bytes, content_type="image/png")
    resp["Cache-Control"] = "max-age=3600"
    return resp


def _block_if_pending_changes(
    request: HttpRequest, scan: Scan
) -> HttpResponse | None:
    """Redirect back to step 1 when the scan has unapplied page changes.

    The validate, detect, and recheck actions ignore pending
    ``PageDeletion`` / ``PageInsert`` rows, so running them would
    silently strand the user's edits (and, for a full re-validation,
    spend a RunPod run for nothing). Pending changes must be applied via
    "Rebuild & Validate" (the ``reprocess`` view).

    :param request: The HTTP request.
    :type request: HttpRequest
    :param scan: The scan to check for pending changes.
    :type scan: Scan
    :returns: A redirect response if there are pending changes, else
        ``None``.
    :rtype: HttpResponse | None
    """
    if scan.deletions.exists() or scan.inserts.exists():
        messages.warning(
            request,
            'Apply your pending page changes with "Rebuild & Validate" '
            "before running this.",
        )
        return redirect(
            reverse("scan_process", kwargs={"pk": scan.pk}) + "?step=1"
        )
    return None


@login_required
def process_actions(request: HttpRequest, pk: int) -> JsonResponse:
    """Render the step action bar as an HTML fragment.

    Lets the step-1/step-2 viewers refresh the action buttons in place
    after a page is marked for (or restored from) deletion, so the
    correct buttons appear without a full page reload.

    :param request: The HTTP request (optional ``step`` query param).
    :type request: HttpRequest
    :param pk: Scan primary key.
    :type pk: int
    :returns: JSON with the rendered ``html`` and the current
        ``has_pending_changes`` flag.
    :rtype: JsonResponse
    """
    scan = get_object_or_404(Scan, pk=pk)
    try:
        step = int(request.GET.get("step", 1))
    except ValueError:
        step = 1
    if step < 1 or step > 3:
        step = 1

    has_pending_inserts = scan.inserts.exists()
    has_pending_changes = scan.deletions.exists() or has_pending_inserts
    context = {
        "scan": scan,
        "step": step,
        "is_processing": scan.status in (Status.PROCESSING, Status.QUEUED),
        "has_pending_changes": has_pending_changes,
        "has_pending_inserts": has_pending_inserts,
        "issues": scan.issues.all(),
        "missing_pages": scan.missing_pages,
        "has_detections": Detection.objects.filter(scan=scan).exists(),
        "opinions": scan.opinions_json,
    }
    html = render_to_string(
        "scanning/_process_actions.html", context, request=request
    )
    return JsonResponse(
        {"html": html, "has_pending_changes": has_pending_changes}
    )


@login_required
@require_POST
def start_validate(request: HttpRequest, pk: int) -> HttpResponse:
    """Queue a scan for validation (first step of the full pipeline).

    :param request: The HTTP request.
    :param pk: Scan primary key.
    :return: Redirect to the scan processing page.
    """
    scan = get_object_or_404(Scan, pk=pk)
    guard = _block_if_pending_changes(request, scan)
    if guard:
        return guard
    scan.status = Status.QUEUED
    scan.stage = Stage.VALIDATE
    scan.queued_action = QueuedAction.FULL_PIPELINE
    scan.s3_uploaded = False
    scan.progress_message = "Queued for processing..."
    scan.save()
    return redirect("scan_process", pk=scan.pk)


@login_required
@require_POST
def start_detect(request: HttpRequest, pk: int) -> HttpResponse:
    """Queue a scan for detection or skip to review if detections exist.

    :param request: The HTTP request.
    :param pk: Scan primary key.
    :return: Redirect to the scan processing page (step 2).
    """
    scan = get_object_or_404(Scan, pk=pk)
    guard = _block_if_pending_changes(request, scan)
    if guard:
        return guard
    if Detection.objects.filter(scan=scan).exists():
        # Detections already exist (from full pipeline). Skip to review.
        return redirect(
            reverse("scan_process", kwargs={"pk": scan.pk}) + "?step=2"
        )

    scan.status = Status.QUEUED
    scan.stage = Stage.PROCESS
    scan.queued_action = QueuedAction.DETECT
    scan.s3_uploaded = False
    scan.progress_message = "Queued for detection..."
    scan.save()
    return redirect(
        reverse("scan_process", kwargs={"pk": scan.pk}) + "?step=2"
    )


@login_required
@require_POST
def cancel_processing(request: HttpRequest, pk: int) -> HttpResponse:
    """Cancel an in-progress scan processing task.

    :param request: The HTTP request.
    :param pk: Scan primary key.
    :return: Redirect to the scan processing page.
    """
    scan = get_object_or_404(Scan, pk=pk)
    if scan.status == Status.PROCESSING:
        if scan.stage == Stage.PROCESS:
            Scan.objects.filter(pk=pk).update(
                status=Status.PENDING_REVIEW,
                stage=Stage.VALIDATE,
                progress_message="",
                progress_log="",
                redacted_pdf_path="",
                opinions_json=[],
            )
        else:
            Scan.objects.filter(pk=pk).update(
                status=Status.CANCELLED,
                progress_message="Cancelled by user.",
            )
    return redirect("scan_process", pk=pk)


@login_required
@require_POST
def recalculate(request: HttpRequest, pk: int) -> HttpResponse:
    """Recalculate validation issues from existing OCR results.

    :param request: The HTTP request.
    :param pk: Scan primary key.
    :return: Redirect to the scan processing page.
    """
    scan = get_object_or_404(Scan, pk=pk)
    guard = _block_if_pending_changes(request, scan)
    if guard:
        return guard
    if not scan.ocr_results:
        return redirect("scan_process", pk=pk)
    from scanning import services

    services.recalculate_issues(scan)
    return redirect("scan_process", pk=pk)


@login_required
@require_POST
def reprocess(request: HttpRequest, pk: int) -> HttpResponse:
    """Queue a scan for reprocessing (re-run the full pipeline).

    :param request: The HTTP request.
    :param pk: Scan primary key.
    :return: Redirect to the scan processing page.
    """
    scan = get_object_or_404(Scan, pk=pk)
    scan.status = Status.QUEUED
    scan.queued_action = QueuedAction.REPROCESS
    scan.s3_uploaded = False
    scan.progress_message = "Queued for reprocessing..."
    scan.save()
    return redirect("scan_process", pk=scan.pk)


@login_required
@require_POST
def assign_page(request: HttpRequest, pk: int) -> JsonResponse:
    """Manually set or clear a PDF page's detected page number.

    A blank/empty ``page_number`` clears the number, marking the page as
    having none (e.g. front matter the model mis-tagged). Any other value
    must be a positive integer. After updating, the page_map is rebuilt so
    duplicate flags stay in sync with the change.

    :param request: The HTTP request (JSON body with ``pdf_page`` and
        ``page_number``; ``page_number`` may be null/empty to clear).
    :param pk: Scan primary key.
    :return: JSON response with the stored value and duplicate flag.
    """
    scan = get_object_or_404(Scan, pk=pk)
    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON"}, status=400)
    pdf_page = data.get("pdf_page")
    if pdf_page is None:
        return JsonResponse({"error": "pdf_page is required."}, status=400)
    if "page_number" not in data:
        return JsonResponse(
            {"error": 'page_number is required (send null or "" to clear).'},
            status=400,
        )
    raw = data["page_number"]

    clear = raw is None or (isinstance(raw, str) and not raw.strip())
    page_value = None
    if not clear:
        try:
            number = int(str(raw).strip())
        except (TypeError, ValueError):
            return JsonResponse(
                {"error": "Page number must be a positive whole number."},
                status=400,
            )
        if number < 1:
            return JsonResponse(
                {"error": "Page number must be a positive whole number."},
                status=400,
            )
        page_value = str(number)

    ocr_results = scan.ocr_results
    if not any(r["pdf_page"] == pdf_page for r in ocr_results):
        return JsonResponse({"error": "Unknown PDF page."}, status=404)
    for r in ocr_results:
        if r["pdf_page"] == pdf_page:
            if clear:
                r["detected"] = None
                r["type"] = None
                r["score"] = None
            else:
                r["detected"] = page_value
                r["type"] = "single"
                r["score"] = 1.0
            r["zone"] = "manual"
            r["ocr"] = "manual"
            break
    scan.ocr_results = ocr_results

    # Clear the page's no-page-number flag; the rebuild does not touch Issue
    # rows, so a full Recheck re-derives the issue list (and any new flag).
    scan.issues.filter(
        check_name=CheckName.NO_PAGE_NUMBER, page_number=pdf_page
    ).delete()

    # Rebuild page_map so the viewer/sidebar duplicate flags reflect the edit
    # immediately (saves the scan, including the updated ocr_results).
    from scanning import services

    services.rebuild_page_map(scan)

    duplicate = any(
        e.get("type") == "pdf_page"
        and e.get("pdf_index") == pdf_page - 1
        and e.get("duplicate")
        for e in scan.page_map
    )
    return JsonResponse(
        {"status": "ok", "detected": page_value, "duplicate": duplicate}
    )


@login_required
@require_POST
def delete_page(request: HttpRequest, pk: int) -> HttpResponse:
    """Mark a PDF page for deletion during reprocessing.

    :param request: The HTTP request (JSON body with pdf_page).
    :param pk: Scan primary key.
    :return: JSON response confirming the deletion record.
    """
    scan = get_object_or_404(Scan, pk=pk)
    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON"}, status=400)
    pdf_page = data["pdf_page"]
    PageDeletion.objects.get_or_create(scan=scan, pdf_page=pdf_page)
    return JsonResponse({"status": "ok"})


@login_required
@require_POST
def undo_delete_page(request: HttpRequest, pk: int) -> HttpResponse:
    """Remove a page deletion record, restoring the page.

    :param request: The HTTP request (JSON body with pdf_page).
    :param pk: Scan primary key.
    :return: JSON response confirming the undo.
    """
    scan = get_object_or_404(Scan, pk=pk)
    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON"}, status=400)
    pdf_page = data["pdf_page"]
    PageDeletion.objects.filter(scan=scan, pdf_page=pdf_page).delete()
    return JsonResponse({"status": "ok"})


@login_required
@require_POST
def add_page_insert(request: HttpRequest, pk: int) -> JsonResponse:
    """Upload an image to insert at a missing page position.

    :param request: The HTTP request (form data with page_number and
        image file).
    :param pk: Scan primary key.
    :return: JSON response with the insert URL and page number.
    """
    scan = get_object_or_404(Scan, pk=pk)
    try:
        page_number = int(request.POST.get("page_number", 0))
    except ValueError:
        page_number = 0
    image_file = request.FILES.get("image")
    if not page_number or not image_file:
        return JsonResponse(
            {"error": "Missing page_number or image"}, status=400
        )
    if not image_file.content_type.startswith("image/"):
        return JsonResponse(
            {"error": "Uploaded file must be an image"}, status=400
        )
    insert, _created = PageInsert.objects.update_or_create(
        scan=scan,
        logical_page_number=page_number,
        defaults={"image": image_file},
    )
    return JsonResponse(
        {
            "status": "ok",
            "page_number": page_number,
            "image_url": insert.image.url,
        }
    )


@login_required
@require_POST
def dismiss_issue(request: HttpRequest, pk: int) -> JsonResponse:
    """Dismiss a single validation issue for a scan.

    :param request: The HTTP request (JSON body with issue_id).
    :param pk: Scan primary key.
    :return: JSON response confirming dismissal.
    """
    scan = get_object_or_404(Scan, pk=pk)
    has_pending = scan.deletions.exists() or scan.inserts.exists()
    if has_pending:
        return JsonResponse(
            {
                "status": "error",
                "message": "Reprocess first -- there are pending changes.",
            },
            status=400,
        )
    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON"}, status=400)
    issue_id = data.get("issue_id")
    Issue.objects.filter(pk=issue_id, scan=scan).delete()
    return JsonResponse({"status": "ok"})
