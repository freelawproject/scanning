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
    Http404,
    HttpRequest,
    HttpResponse,
    JsonResponse,
)
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.views.decorators.http import require_POST

from scanning.models import (
    BUSY_STATUSES,
    CheckName,
    Detection,
    Issue,
    JobStage,
    JobStatus,
    OpinionScan,
    PageDeletion,
    PageInsert,
    Scan,
    Stage,
    Status,
)
from scanning.utils import (
    PIPELINE_PAUSED_MESSAGE,
    compute_coverage_gaps,
    find_processing_pdf,
    local_original_pdf,
)

logger = logging.getLogger(__name__)

#: Flashed by the review-1 actions of issue #151. They are constants so
#: the tests assert the copy the curator actually reads.
PAGE_REVIEW_APPROVED_MESSAGE = (
    "Thank you. This scan is marked as page complete."
)
PAGE_REVIEW_ALREADY_DONE_MESSAGE = (
    "This scan is already marked as page complete."
)
PAGE_REVIEW_NOT_READY_MESSAGE = (
    "This scan is not ready for the page completeness review."
)
LEGACY_OCR_RECOMPUTE_MESSAGE = (
    "The old OCR engine that read this scan no longer runs here. Run "
    "OCR again to recompute the page numbers."
)
RECOMPUTE_DONE_MESSAGE = "The page number issues are recomputed."
REVALIDATE_UNAVAILABLE_MESSAGE = (
    "This scan does not need a re-run. Sharding, the bitonal "
    "conversion and dots.mocr are deterministic, so they would give "
    "the same result again. Ask an admin to re-queue the scan if it "
    "really must be processed a second time."
)
PENDING_EDITS_SAVED_MESSAGE = (
    "Your page inserts and deletes are saved. We do not apply them to "
    "the volume yet."
)


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
    is_processing = scan.status in BUSY_STATUSES
    # Breadcrumb for the web-pod observability trail (issue #115): this view
    # does an S3 pull plus a render over potentially large detection sets, so a
    # hang/OOM here should leave a marker in the pod logs and Sentry.
    logger.info(
        "scan_process_view: rendering scan=%s status=%s", scan.pk, scan.status
    )

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
        elif scan.status == Status.PAGE_COMPLETENESS_REVIEW_DONE:
            # Review 1 is done (#154), so land on the detection review
            # when detections exist. The detection stage (#195) writes
            # no scan status, so its output is the only signal.
            if Detection.objects.filter(scan=scan).exists():
                step = 2
            else:
                step = 1
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

    # The dots.mocr stage writes no scan status by design (#190), so its
    # rows are the only place its progress lives.
    from scanning import dots_mocr

    dots_run = dots_mocr.run_summary(scan)

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
            "dots_run": dots_run,
            **_review_flags(scan),
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
    # The dots.mocr stage moves no scan status, so a viewer polling this
    # would otherwise see nothing happen for the whole run (#190).
    from scanning import dots_mocr

    dots_run = dots_mocr.run_summary(scan)
    if dots_run:
        data["dots_run"] = dots_run
    return JsonResponse(data)


# Suffix for every "not ready" message the viewer shows (issue #185):
# uploaders must know the tab is not doing the work.
CLOSE_TAB_NOTE = " You can close this tab. The work continues on the server."

# Stage-specific wait messages, keyed by scan status. Each names what
# runs right now, so the viewer explains the wait instead of a generic
# "still processing" (issue #185).
PREVIEW_WAIT_MESSAGES = {
    Status.UPLOADED: (
        "Your upload is complete and safe. The scan waits for the "
        "processing queue." + CLOSE_TAB_NOTE
    ),
    Status.QUEUED: (
        "Your upload is complete and safe. The scan waits in the "
        "processing queue." + CLOSE_TAB_NOTE
    ),
    Status.PROCESSING: (
        "We are preparing the scan. We cut the PDF into parts, so "
        "servers can work on them in parallel." + CLOSE_TAB_NOTE
    ),
    Status.AWAITING: (
        "We are converting the scan to a small black-and-white preview, "
        "so it loads fast. This takes some minutes, and the preview "
        "appears here automatically." + CLOSE_TAB_NOTE
    ),
}


@login_required
def serve_scan_pdf(request: HttpRequest, pk: int) -> HttpResponse:
    """Serve the small processed PDF (``bitonal.pdf``).

    The viewer only ever gets the small, browser-viewable preview. The
    multi-GB original is never streamed here: it blows past the gunicorn
    worker timeout (the connection dies mid-stream, so the browser sees a
    truncated body and reports ERR_CONTENT_LENGTH_MISMATCH) and pdf.js
    can't hold a file that large in memory anyway. The viewer reads the
    original straight from S3 instead, via ``scan_original_url``
    (issue #185); server-side crops keep their own path
    (``serve_original_crop``).

    Resolution:

    1. Serve the processed PDF if it is already local.
    2. Otherwise pull *only* the preview PDF(s) from S3 and look again.
       This covers prod, where the daemon and web run in separate
       containers with separate ephemeral ``/tmp/`` volumes. The targeted
       pull skips the original and images/, so opening a scan never drags
       gigabytes across the network.
    3. No preview exists. Distinguish transient states (a preview is
       being produced -- the viewer should poll) from terminal ones (a
       preview will never appear -- the viewer should stop and offer the
       original instead). 202 means "poll again"; 409 means "give up".
       Both carry a stage-specific message and ``original_available``,
       which tells the viewer it can offer the "load the original"
       button.

    AWAITING is transient: a bitonal is being made right now (#176), so
    polling gets one. AWAITING_VALIDATION is terminal here: a scan parks
    there either converted (so a preview exists and step 3 is never
    reached) or deliberately unconverted (skipped, failed, or a
    pre-#176 post-cutover upload), and then no poll will ever find one.
    The two #154 review states are also 409s, but with their own
    message: they guarantee a stored preview, so reaching step 3 under
    them means the S3 pull just failed, and a reload (not a poll)
    retries it. The same failed-pull case under AWAITING_VALIDATION
    stays folded into its generic 409.

    A served preview carries ``X-Scan-Preview: bitonal`` so the viewer
    knows it shows the lower-quality conversion and can offer the
    original (issue #185).

    :param request: The HTTP request.
    :param pk: Scan primary key.
    :return: File response streaming the preview PDF, or a 202/409 JSON
        response when there is none.
    """
    scan = get_object_or_404(Scan, pk=pk)
    # Breadcrumb (issue #115): serving may pull from S3; mark the start
    # so a stall here is attributable in the trail.
    logger.info("serve_scan_pdf: resolving pdf for scan=%s", scan.pk)

    def _processed_local() -> FileResponse | None:
        """Return ``bitonal.pdf`` (or a legacy OCR pdf) from ``output_dir``."""
        output = Path(scan.output_dir)
        if not output.is_dir():
            return None
        base_pdf = find_processing_pdf(scan.output_dir)
        if base_pdf:
            response = FileResponse(
                base_pdf.open("rb"), content_type="application/pdf"
            )
            # "bitonal" names the preview class, not the exact file: a
            # legacy OCR PDF (same geometry, pre-#145 scans) reports the
            # same value, and the banner text stays true for it.
            response["X-Scan-Preview"] = "bitonal"
            return response
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

    # 3. No preview anywhere. Answer with a stage-specific message and
    #    whether the original is there to offer instead.
    original_available = bool(scan.original_pdf and scan.original_pdf.name)

    wait_message = PREVIEW_WAIT_MESSAGES.get(scan.status)
    if wait_message:
        return JsonResponse(
            {
                "status": "not_ready",
                "scan_status": scan.status,
                "message": wait_message,
                "original_available": original_available,
            },
            status=202,
        )

    if scan.status in (
        Status.READY_FOR_PAGE_COMPLETENESS_REVIEW,
        Status.PAGE_COMPLETENESS_REVIEW_DONE,
    ):
        # These statuses guarantee a stored preview (#154): #149 sets
        # the first one only after the bitonal merge. Landing here means
        # the S3 pull above just failed, and a reload retries it.
        message = (
            "The preview did not load. Reload the page to try again, "
            "or load the original scan instead."
        )
    elif scan.status == Status.AWAITING_VALIDATION:
        message = (
            "This scan has no small preview. You can load the original "
            "scan instead."
        )
    elif scan.status in (Status.ERROR, Status.ERROR_MAX_RETRIES):
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
            "original_available": original_available,
        },
        status=409,
    )


@login_required
def scan_original_url(request: HttpRequest, pk: int) -> JsonResponse:
    """Return a URL the browser can read the original PDF from.

    The viewer calls this when the user asks for the original (issue
    #185). With S3 active, the answer is a presigned GET on the bucket:
    pdf.js reads the (up to 3 GB) file with range requests, straight
    from S3, so the web pod never streams it. Without S3 (dev, tests),
    the answer is our own ``serve_scan_original`` stream, and
    ``embedded_whole`` tells the viewer to load it in one piece --
    local files are small and local.

    The URL is minted per request, so every click gets a fresh
    signature.

    :param request: The HTTP request.
    :param pk: Scan primary key.
    :return: JSON with ``url`` and ``embedded_whole``, or a 404 when
        the scan has no original file.
    """
    scan = get_object_or_404(Scan, pk=pk)
    if not (scan.original_pdf and scan.original_pdf.name):
        return JsonResponse(
            {"error": "This scan has no original PDF."}, status=404
        )

    from scanning import s3_sync

    url = s3_sync.presign_original_get(scan)
    if url:
        return JsonResponse({"url": url, "embedded_whole": False})
    return JsonResponse(
        {
            "url": reverse("serve_scan_original", kwargs={"pk": scan.pk}),
            "embedded_whole": True,
        }
    )


@login_required
def serve_scan_original(request: HttpRequest, pk: int) -> FileResponse:
    """Stream the original PDF from local disk.

    The no-S3 fallback behind ``scan_original_url``: in dev and tests
    the original never left this machine, so a plain stream serves it.
    With S3 active this view refuses with a 404 instead of streaming:
    it would pull the whole original to the pod and push it through
    gunicorn -- the exact slow, truncating path #185 removed from the
    preview endpoint -- and nothing links here in that mode, since the
    viewer gets a presigned URL from ``scan_original_url``.

    :param request: The HTTP request.
    :param pk: Scan primary key.
    :return: File response streaming the original PDF.
    :raises Http404: When S3 is active, or when no local copy can be
        made available.
    """
    scan = get_object_or_404(Scan, pk=pk)

    from scanning import s3_sync

    if s3_sync.s3_active():
        raise Http404("The original PDF is read from storage, not from here.")

    original = local_original_pdf(scan)
    if not original:
        raise Http404("No original PDF is available for this scan.")
    response = FileResponse(
        open(original, "rb"), content_type="application/pdf"
    )
    response["X-Scan-Preview"] = "original"
    return response


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

    # Breadcrumb (issue #115): the pixmap render below is the clearest
    # per-request memory spike in the web pod (5-20 MB at dpi=300), so log it
    # with its scale before allocating.
    logger.info(
        "serve_original_crop: rendering scan=%s page=%s dpi=%s",
        scan.pk,
        page,
        dpi,
    )
    # Prod: the original lives only in S3 (direct-to-S3 upload, and the
    # classic prod path streams straight to S3 too). The process view no
    # longer eagerly lands it locally, and download_preview_pdf excludes
    # the original, so this pulls just the original when it is missing.
    original = local_original_pdf(scan)
    if not original:
        return HttpResponse(status=404)

    with fitz.open(original) as doc:
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


def _review_flags(scan: Scan) -> dict:
    """Return the review-1 flags the step-1 button bar reads (#151).

    Both :func:`scan_process_view` and the :func:`process_actions`
    fragment render that bar, so the flags come from one place. A bar
    that disagreed with itself would offer an approve button the view
    refuses, or hide the one it accepts.

    :param scan: The scan the bar is rendered for.
    :returns: ``page_review_ready``, ``page_review_done`` and
        ``has_legacy_ocr`` for the template context.
    :rtype: dict
    """
    from scanning import services

    return {
        "page_review_ready": (
            scan.status == Status.READY_FOR_PAGE_COMPLETENESS_REVIEW
        ),
        "page_review_done": (
            scan.status == Status.PAGE_COMPLETENESS_REVIEW_DONE
        ),
        "has_legacy_ocr": services.has_legacy_ocr(scan),
    }


def _block_if_pending_changes(
    request: HttpRequest, scan: Scan
) -> HttpResponse | None:
    """Redirect back to step 1 when the scan has unapplied page changes.

    The detect action ignores pending ``PageDeletion`` /
    ``PageInsert`` rows, so running it would silently strand the user's
    edits. Pending changes must be applied via "Rebuild & Validate"
    (the ``reprocess`` view).

    The recompute of review 1 no longer calls this: "Rebuild &
    Validate" refuses while the pipeline is paused (#173), so the
    redirect was a dead end. That view warns and continues instead
    (#151).

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

    from scanning import dots_mocr

    has_pending_inserts = scan.inserts.exists()
    has_pending_changes = scan.deletions.exists() or has_pending_inserts
    context = {
        "scan": scan,
        "step": step,
        "is_processing": scan.status in BUSY_STATUSES,
        "has_pending_changes": has_pending_changes,
        "has_pending_inserts": has_pending_inserts,
        "issues": scan.issues.all(),
        "missing_pages": scan.missing_pages,
        "has_detections": Detection.objects.filter(scan=scan).exists(),
        "opinions": scan.opinions_json,
        "dots_run": dots_mocr.run_summary(scan),
        **_review_flags(scan),
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
    """Refuse to re-run the pipeline. Say why, per scan.

    This button used to re-queue the full pipeline from scratch,
    invalidating the stored GPU results first so nothing was reused.
    The stages it re-ran -- bitonal, YOLO detect, PaddleOCR validation
    -- were disconnected by issue #173.

    A **new-pipeline scan is refused for good** (#151), not until the
    replacements land: sharding, the bitonal conversion and dots.mocr
    are deterministic, so a second run returns what the first one
    already stored, at the price of another doctor conversion and
    another park out of the review flow. Nothing here is a recompute of
    the page numbers either -- that is the recompute button, over the
    stored readings. The escape hatch for a volume that genuinely must
    be processed again is the admin re-queue, which is deliberately not
    a curator's button.

    A legacy row keeps the paused message: its stages are gone rather
    than pointless, and the bar still offers it one (#173).

    :param request: The HTTP request.
    :param pk: Scan primary key.
    :return: Redirect to the scan processing page.
    """
    from scanning import services

    scan = get_object_or_404(Scan, pk=pk)
    if services.has_legacy_ocr(scan):
        messages.warning(request, PIPELINE_PAUSED_MESSAGE)
    else:
        messages.warning(request, REVALIDATE_UNAVAILABLE_MESSAGE)
    return redirect("scan_process", pk=scan.pk)


@login_required
@require_POST
def start_detect(request: HttpRequest, pk: int) -> HttpResponse:
    """Skip to review when detections exist; otherwise refuse (#173).

    YOLO detection was disconnected with the legacy pipeline, so the
    only thing left of this action is its shortcut: a scan that already
    has detections goes straight to step 2. Running detection anew
    fails with the unified pipeline-paused message.

    :param request: The HTTP request.
    :param pk: Scan primary key.
    :return: Redirect to the scan processing page (step 2).
    """
    scan = get_object_or_404(Scan, pk=pk)
    guard = _block_if_pending_changes(request, scan)
    if guard:
        return guard
    if Detection.objects.filter(scan=scan).exists():
        # Detections already exist (from the old full pipeline).
        return redirect(
            reverse("scan_process", kwargs={"pk": scan.pk}) + "?step=2"
        )

    messages.warning(request, PIPELINE_PAUSED_MESSAGE)
    return redirect("scan_process", pk=scan.pk)


@login_required
@require_POST
def start_dots_mocr(request: HttpRequest, pk: int) -> HttpResponse:
    """Start the dots.mocr stage over a scan's original shards (#190).

    Staff only, and deliberately manual. Every press starts real
    graphics processing unit (GPU) work on RunPod that costs money, so
    while the stage is being debugged a person decides, not the daemon.

    **This request makes no call to RunPod.** It writes one
    ``ExternalJob`` row per shard and returns; the daemon's next
    ``submit_external_jobs`` tick sends them, and ``collect_external_jobs``
    polls and retries them. That keeps a request thread off a slow HTTP
    call, and it is what makes the run survive a redeployed web pod.

    It also never cuts shards. ``sharding.committed_manifest`` verifies
    the stored set against the original with one ``head_object``, so this
    view neither downloads a multi-gigabyte PDF nor reads ``shards/``
    directly. A stale or missing set is refused, because re-cutting is
    the pipeline's job.

    :param request: The HTTP request.
    :param pk: Scan primary key.
    :return: Redirect to the scan processing page.
    """
    from scanning import dots_mocr, sharding

    scan = get_object_or_404(Scan, pk=pk)
    back = redirect("scan_process", pk=scan.pk)

    if not request.user.is_staff:
        messages.error(
            request,
            "Only staff can start OCR: each run costs GPU time.",
        )
        return back

    if not dots_mocr.enabled():
        messages.warning(
            request,
            "dots.mocr is not switched on in this environment. Set "
            "DOTS_MOCR_ENABLED and RUNPOD_DOTSMOCR_ENDPOINT_ID first.",
        )
        return back

    # An open run means the daemon is still working on the last press.
    # A finished run is reused rather than refused, which is what keeps
    # ``ensure_analyze_jobs`` from paying twice for shards already read.
    summary = dots_mocr.run_summary(scan)
    if summary and summary["open"]:
        messages.info(
            request,
            f"OCR run {summary['run']} is already going: "
            f"{summary['done']} of {summary['total']} part(s) done.",
        )
        return back

    manifest, reason = sharding.committed_manifest(scan)
    if manifest is None:
        messages.warning(request, reason)
        return back

    created = dots_mocr.ensure_analyze_jobs(scan, manifest)
    queued = sum(1 for job in created if job.status == JobStatus.PENDING)
    logger.info(
        "start_dots_mocr: scan=%s user=%s run=%s shards=%d queued=%d",
        scan.pk,
        request.user.pk,
        created[0].run if created else "?",
        len(created),
        queued,
    )
    if queued:
        messages.success(
            request,
            f"Queued OCR for {queued} part(s) of this volume. The "
            "daemon sends them to RunPod within a few seconds.",
        )
    else:
        # ``ensure_analyze_jobs`` reused a run that is already done, so
        # nothing was queued and nothing will be sent. Saying otherwise
        # would have staff waiting on a dispatch that is not coming.
        messages.info(
            request,
            f"This volume was already read: run {created[0].run} covers "
            f"all {len(created)} part(s). Nothing new was queued.",
        )
    return back


@login_required
@require_POST
def cancel_processing(request: HttpRequest, pk: int) -> HttpResponse:
    """Cancel an in-progress scan processing task.

    AWAITING counts as in progress: a scan spends its whole conversion
    there (#176) and the viewer shows it as busy, so ignoring it would
    make cancel a silent no-op for the longest part of the run. The job
    rows go with it, or a finished shard's outcome would land on a scan
    the user stopped.

    :param request: The HTTP request.
    :param pk: Scan primary key.
    :return: Redirect to the scan processing page.
    """
    from scanning import jobs

    scan = get_object_or_404(Scan, pk=pk)
    if scan.status in (Status.PROCESSING, Status.AWAITING):
        if scan.status == Status.AWAITING:
            jobs.abandon_open(
                scan, "Cancelled by user", stage=JobStage.CONVERT
            )
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
    """Rebuild the page number Issues from the stored readings.

    The "Recompute page number issues" button of review 1 (#151). It
    runs on stored data only, so it works on a web pod that never
    pulled the scan's files from S3 (#153).

    Two cases it answers rather than obeys. A scan the retired
    PaddleOCR stage read gets the legacy message and no recompute: the
    readings cannot change, so a rebuild would only look like work
    (#173). A scan carrying pending inserts or deletes gets the recompute
    plus a warning that those edits are saved but not applied, because
    the button that used to apply them ("Rebuild & Validate") now
    refuses (#173) -- blocking here would leave the curator with no way
    forward at all.

    :param request: The HTTP request.
    :param pk: Scan primary key.
    :return: Redirect to the scan processing page.
    """
    scan = get_object_or_404(Scan, pk=pk)
    if not scan.ocr_results:
        return redirect("scan_process", pk=pk)
    from scanning import services

    if services.has_legacy_ocr(scan):
        messages.warning(request, LEGACY_OCR_RECOMPUTE_MESSAGE)
        return redirect("scan_process", pk=pk)
    if scan.deletions.exists() or scan.inserts.exists():
        messages.warning(request, PENDING_EDITS_SAVED_MESSAGE)

    # Breadcrumb (issue #115): recalculation runs synchronously on the request
    # thread over the scan's OCR results.
    logger.info("recalculate: recomputing issues for scan=%s", scan.pk)
    services.recalculate_issues(scan)
    messages.success(request, RECOMPUTE_DONE_MESSAGE)
    return redirect("scan_process", pk=pk)


@login_required
@require_POST
def approve_page_completeness(request: HttpRequest, pk: int) -> HttpResponse:
    """Record that a person reviewed the scan for page completeness.

    The approve button of review 1 (#151), and the only writer of
    ``PAGE_COMPLETENESS_REVIEW_DONE`` (#154). Every logged-in user may
    press it: review 1 is the scanners' own step, not a staff one.

    The write is one compare-and-swap on READY, never a full instance
    save. The collect tick can write READY over the same row at the
    same moment (``services.run_compute_issues``), and a scan that is
    cancelled, errored, or still waiting on its inputs must not be
    approved by a stale page a curator left open.

    :param request: The HTTP request.
    :param pk: Scan primary key.
    :return: Redirect to step 1 of the scan processing page.
    """
    scan = get_object_or_404(Scan, pk=pk)
    approved = Scan.objects.filter(
        pk=scan.pk, status=Status.READY_FOR_PAGE_COMPLETENESS_REVIEW
    ).update(status=Status.PAGE_COMPLETENESS_REVIEW_DONE)
    if approved:
        # Breadcrumb (issue #115): this is a human decision, and the
        # only record of who made it.
        logger.info(
            "approve_page_completeness: scan=%s approved by user=%s",
            scan.pk,
            request.user.pk,
        )
        messages.success(request, PAGE_REVIEW_APPROVED_MESSAGE)
    elif scan.status == Status.PAGE_COMPLETENESS_REVIEW_DONE:
        messages.info(request, PAGE_REVIEW_ALREADY_DONE_MESSAGE)
    else:
        messages.warning(request, PAGE_REVIEW_NOT_READY_MESSAGE)
    return redirect(
        reverse("scan_process", kwargs={"pk": scan.pk}) + "?step=1"
    )


@login_required
@require_POST
def reprocess(request: HttpRequest, pk: int) -> HttpResponse:
    """Refuse to apply pending page edits while the pipeline is paused.

    Applying inserts/deletions re-ran OCR on the edited pages through
    the retired PaddleOCR path (issue #173), so this fails with the
    unified message until the dots.mocr replacement lands. Pending
    ``PageDeletion`` / ``PageInsert`` rows stay recorded and will be
    applicable again then.

    :param request: The HTTP request.
    :param pk: Scan primary key.
    :return: Redirect to the scan processing page.
    """
    scan = get_object_or_404(Scan, pk=pk)
    messages.warning(request, PIPELINE_PAUSED_MESSAGE)
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
