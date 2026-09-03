"""Process viewer and scan processing action views."""

import itertools
import json
import logging
import os
import re
from collections.abc import Callable
from pathlib import Path

import fitz
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import IntegrityError, transaction
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

from scanning import dots_mocr, jobs, page_edits, s3_sync, yolo
from scanning.models import (
    BUSY_STATUSES,
    PAGE_EDIT_ROTATIONS,
    PHYSICAL_PAGE_CHECKS,
    CheckName,
    Detection,
    ExternalJob,
    Issue,
    JobEngine,
    JobStage,
    JobStatus,
    OpinionScan,
    PageEdit,
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
    "This scan cannot be re-run from here. Sharding, the bitonal "
    "conversion and dots.mocr are deterministic, so a re-run adds "
    "nothing. Ask an admin to re-queue the scan if it really must be "
    "processed a second time."
)
PAGE_REVIEW_APPROVAL_REQUIRED_MESSAGE = (
    "Approve the page completeness review first. Then continue to detection."
)
PENDING_EDITS_SAVED_MESSAGE = (
    "Your page changes are saved. We do not build the corrected "
    "volume from them yet. Each inserted or replaced page must go "
    "through the conversion and the OCR on its own, and that pass is "
    "not built (#206). Approve this volume when the pages are "
    "complete. We apply your changes for you when the pass is ready."
)

#: The largest page file a curator may upload (#232). One page is one
#: image or a short PDF. A bigger file is a whole volume sent by
#: mistake, and the web pod would read it into memory to check it.
PAGE_UPLOAD_MAX_BYTES = 50 * 1024 * 1024

#: What a page file may start with, and the extension that says what
#: it is. The content type is the browser's word, and the stored
#: extension decides how ``views_api.export_pdf`` and the apply (#206)
#: read the file later, so the first bytes decide -- for an image as
#: much as for a PDF. The image formats are the ones MuPDF opens:
#: ``insert_image`` fails on any other, and a file it refuses would be
#: found out at the apply rather than at the upload. WEBP and SVG are
#: absent for that reason.
_PDF_MAGIC = b"%PDF-"
_IMAGE_MAGIC = (
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"\xff\xd8\xff", "jpg"),
    (b"GIF87a", "gif"),
    (b"GIF89a", "gif"),
    (b"II*\x00", "tif"),
    (b"MM\x00*", "tif"),
    (b"BM", "bmp"),
)
_MAGIC_LENGTH = max(len(_PDF_MAGIC), *(len(m) for m, _ in _IMAGE_MAGIC))

UPLOAD_TOO_LARGE_MESSAGE = (
    "This file is larger than 50 MB. Upload one page, not a volume."
)
UPLOAD_WRONG_TYPE_MESSAGE = (
    "Upload an image of the page (PNG, JPEG, GIF, TIFF or BMP), or a "
    "PDF of it."
)
UPLOAD_LOST_RACE_MESSAGE = (
    "Somebody else changed this page at the same moment. Reload the "
    "page to see their file."
)
UPLOAD_BAD_PDF_MESSAGE = (
    "This PDF could not be opened. Upload it again, or send an image."
)
REPLACEMENT_IS_ONE_PAGE_MESSAGE = (
    "A replacement stands for one page, and this PDF holds {pages}. "
    "Upload the one page that replaces it."
)
# What a curator sees when they ask for step 2 on a volume with no
# detections and no detection run. Since #250 the daemon starts the
# run by itself once per shard set, so a volume with no run at all is
# a legacy volume with no shard set, an environment with the stage
# off, or a sweep that has not ticked yet.
NO_DETECTIONS_MESSAGE = (
    "This volume has no detections yet. Detection starts by itself "
    "after the upload, and the redactions appear here when it "
    "finishes. If nothing shows after a few minutes, ask a staff "
    "member."
)


def detection_message(summary: dict | None) -> str:
    """Say where a volume's detection stands, for a curator (#250).

    One text for the "Next: Detect" title and the flash the view sends
    when it cannot walk to step 2, so the bar and the view agree.
    ``summary`` is ``yolo.run_summary(scan)``: ``None`` when the stage
    has never run, else the counts of the live run.

    :param summary: The run summary, or ``None``.
    :returns: The message.
    :rtype: str
    """
    if not summary:
        return NO_DETECTIONS_MESSAGE
    if summary["failed"]:
        code = summary["error_code"] or "no error code"
        return (
            f"Detection failed on {summary['failed']} of "
            f"{summary['total']} part(s) ({code}). Ask a staff member "
            "to look into it."
        )
    if summary["open"]:
        return (
            f"Detection is running: {summary['done']} of "
            f"{summary['total']} part(s) done. The redactions appear "
            "here when it finishes."
        )
    return (
        "Detection finished. The redactions are computed within a "
        "minute of the page completeness approval."
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

    # Neither GPU stage writes a scan status by design (#190, #195), so
    # their rows are the only place their progress lives.
    dots_run = dots_mocr.run_summary(scan)
    yolo_run = yolo.run_summary(scan)

    # Each uploaded image is shown at the gap its row names, and every
    # remaining placeholder is stamped with the physical page it
    # follows, so an upload can send that address back (#214).
    page_map = page_edits.project_inserts(scan, scan.page_map)
    missing_pages = scan.missing_pages

    # The pages a curator replaced (#232). The viewer draws a note on
    # each one, with a link that opens the file the curator uploaded.
    replaced_pages = page_edits.replacements_by_page(scan)

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
    # page_map rather than matched directly. The set is shared with the
    # dismissal, which keeps its address in the same two spaces (#214).
    flagged_indices: set[int] = set()
    for i in issues:
        # Resolve each issue to PDF page indices (unique physical positions),
        # used both for the red-border highlight and for click-to-navigate.
        # ``nav_pdf_index`` is the first resolved index, or None when the issue
        # has no page (or points at a missing page absent from the page_map).
        i.nav_pdf_index = None
        if i.page_number is None:
            continue
        if i.check_name in PHYSICAL_PAGE_CHECKS:
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
        r["is_replaced"] = r["pdf_page"] in replaced_pages
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
            "has_detections": has_detections,
            "is_processing": is_processing,
            "dots_run": dots_run,
            "yolo_run": yolo_run,
            "detect_message": detection_message(yolo_run),
            **_review_flags(scan),
            "opinions": opinions,
            "opinions_json": json.dumps(opinions),
            "has_redaction_rects": has_redaction_rects,
            "opinion_scans": opinion_scans,
            "detect_warnings": detect_warnings,
            "unmatched_keys": unmatched_keys,
            "unmatched_captions": unmatched_captions,
            "deleted_pages_json": json.dumps(
                sorted(page_edits.deleted_pages(scan))
            ),
            "replaced_pages_json": json.dumps(
                {
                    str(page): {
                        "edit_id": edit.pk,
                        "url": reverse(
                            "page_edit_file",
                            kwargs={"pk": scan.pk, "edit_id": edit.pk},
                        ),
                        "kind": page_edits.uploaded_kind(edit),
                    }
                    for page, edit in replaced_pages.items()
                }
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
    # Neither GPU stage moves a scan status, so a viewer polling this
    # would otherwise see nothing happen for a whole run (#190, #195).
    dots_run = dots_mocr.run_summary(scan)
    if dots_run:
        data["dots_run"] = dots_run
    yolo_run = yolo.run_summary(scan)
    if yolo_run:
        data["yolo_run"] = yolo_run
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


#: Lifetime of a presigned GET minted by the glued-output routes (#243).
#: One click is one download, so ten minutes is ample.
#: ``ORIGINAL_VIEW_PRESIGN_TTL`` (8h) serves a viewer that scrolls for
#: hours and is the wrong size here.
GLUED_OUTPUT_PRESIGN_TTL = 600

#: Slug -> (stage, engine, glued key function): the two glued documents
#: of issue #243. The outputs differ in nothing else, so a third engine
#: is one more entry, not a view.
GLUED_OUTPUTS: dict[str, tuple[str, str, Callable[[Scan, int], str]]] = {
    "dots-mocr": (
        JobStage.ANALYZE,
        JobEngine.DOTS_MOCR,
        dots_mocr.glued_result_key,
    ),
    "yolo": (JobStage.DETECT, JobEngine.BLACKLETTER, yolo.merged_result_key),
}

NO_S3_GLUED_OUTPUT_MESSAGE = (
    "No glued output exists without S3: the daemon glues into the "
    "bucket, and the workers write their results there."
)


def _json_404(message: str, **fields) -> JsonResponse:
    """Answer a 404 as JSON, the shape every answer of these routes has.

    ``Http404`` renders an HTML page, and a ``curl`` user would get two
    formats from one API. The scan lookup keeps ``get_object_or_404``
    on purpose: a missing scan looks the same on every scan route.

    :param message: What is missing.
    :param fields: More keys for the body (``run``, ``label``).
    :returns: The response.
    """
    return JsonResponse({"error": message, **fields}, status=404)


def _unknown_output(output: str) -> JsonResponse:
    """Answer for a slug :data:`GLUED_OUTPUTS` does not know."""
    return _json_404(
        f"Unknown glued output {output!r}. "
        f"Known: {', '.join(sorted(GLUED_OUTPUTS))}."
    )


def _glued_run_rows(
    scan: Scan, stage: str, engine: str, run: int
) -> list[ExternalJob]:
    """Return one run's rows in shard order; empty for a run nobody made.

    :param scan: The scan.
    :param stage: A :class:`~scanning.models.JobStage` value.
    :param engine: A :class:`~scanning.models.JobEngine` value.
    :param run: The run number.
    :returns: The rows ordered by ``shard_index``.
    """
    return list(
        ExternalJob.objects.filter(
            scan=scan, stage=stage, engine=engine, opinion=None, run=run
        ).order_by("shard_index")
    )


def _redirect_to_object(
    scan: Scan,
    output: str,
    run: int,
    key: str,
    *,
    filename: str,
    missing_message: str,
    label: str,
) -> HttpResponse:
    """Send the browser to one object of the bucket, or say why not.

    A redirect to a presigned GET, not a stream (#243): a glued
    document of a long volume holds every cell and the text of every
    page, and #185 already took the large stream out of the preview
    endpoint for the gunicorn timeout. The bytes never cross the web
    pod. A navigation to S3 needs no CORS rule.

    One ``head_object`` first. Without it a run that is not glued yet,
    or a shard that never completed, would send the browser to an S3
    XML error. A non-missing S3 error is left to raise: a throttle or
    an IAM fault must reach Sentry, not read as "not there".

    :param scan: The scan the object belongs to.
    :param output: The slug, for the log line.
    :param run: The run number, for the log line and the answer.
    :param key: Object key inside the private bucket.
    :param filename: The name the browser saves the file under.
    :param missing_message: The 404 message when the object is absent.
    :param label: The run's status counts, for the 404 body.
    :returns: A 302 to the presigned URL, or a 404 JSON response.
    """
    if not s3_sync.s3_active():
        return _json_404(NO_S3_GLUED_OUTPUT_MESSAGE, run=run, label=label)
    if not s3_sync.object_exists(key):
        return _json_404(missing_message, run=run, label=label)
    logger.info(
        "glued output: scan=%s output=%s run=%s key=%s",
        scan.pk,
        output,
        run,
        key,
    )
    url = s3_sync.presign_get(
        key,
        GLUED_OUTPUT_PRESIGN_TTL,
        content_disposition=f'attachment; filename="{filename}"',
    )
    return redirect(url)


def _shard_entry(scan: Scan, output: str, row: ExternalJob) -> dict:
    """Describe one shard row for the glued-output index.

    ``from_page`` and ``to_page`` are 1-based volume pages, the
    convention of the log lines (``jobs._failure_location``); the
    stored manifest holds fitz indexes. The dots.mocr page lists stay
    shard-local, as the worker reports them: an offset would put two
    conventions in one document. An absent key means "not known" and
    an empty value means "none", twice: ``url`` is absent on a row
    with no result, and the page lists are absent on a row with no
    stored summary -- a row an S3 HEAD completed stores ``output=None``
    until the glue stamps the lists, and a carried row copies none. The
    index is the triage tool for #242, so "no holes" must not be
    inferred from "nothing recorded".

    :param scan: The scan.
    :param output: The slug the row is listed under.
    :param row: The row.
    :returns: One entry of the ``shards`` list.
    """
    manifest = row.input_manifest or {}
    from_page = manifest.get("from_page")
    to_page = manifest.get("to_page")
    entry = {
        "shard_index": row.shard_index,
        "shard_count": row.shard_count,
        "attempt": row.attempt,
        "status": row.status,
        "error_code": row.error_code,
        "from_page": from_page + 1 if isinstance(from_page, int) else None,
        "to_page": to_page + 1 if isinstance(to_page, int) else None,
        "page_count": manifest.get("page_count"),
    }
    has_summary = isinstance((row.provider_meta or {}).get("output"), dict)
    if row.engine == JobEngine.DOTS_MOCR and has_summary:
        entry.update(jobs.page_lists(row))
    if row.result_key:
        entry["url"] = reverse(
            "serve_glued_shard",
            kwargs={
                "pk": scan.pk,
                "output": output,
                "run": row.run,
                "shard": row.shard_index,
            },
        )
    return entry


@login_required
def glued_output_index(
    request: HttpRequest, pk: int, output: str
) -> JsonResponse:
    """List every run and every shard of one glued output (#243).

    The answer to "how many shards are there, and which one failed":
    the runs newest first, each with its shards, their page ranges,
    their states, and the URL of each file. Every fact is on the rows,
    so the index makes no S3 call and answers in every environment.
    ``glued`` is "every row of the run is CONSUMED", which the glue and
    the merge write; the volume route still checks the object before
    it redirects. A scan with no rows gets an empty list, not an
    error: nothing ran is a fact.

    :param request: The HTTP request.
    :param pk: Scan primary key.
    :param output: A key of :data:`GLUED_OUTPUTS`.
    :return: JSON with ``scan``, ``output``, ``stage``, ``engine``,
        ``live_run`` and ``runs``.
    """
    spec = GLUED_OUTPUTS.get(output)
    if spec is None:
        return _unknown_output(output)
    stage, engine, _key_fn = spec
    scan = get_object_or_404(Scan, pk=pk)
    rows = ExternalJob.objects.filter(
        scan=scan, stage=stage, engine=engine, opinion=None
    ).order_by("-run", "shard_index")
    runs = []
    for run, group in itertools.groupby(rows, key=lambda row: row.run):
        group = list(group)
        runs.append(
            {
                "run": run,
                "glued": all(
                    row.status == JobStatus.CONSUMED for row in group
                ),
                "label": jobs.rows_label(group),
                "volume_url": reverse(
                    "serve_glued_volume",
                    kwargs={"pk": scan.pk, "output": output, "run": run},
                ),
                "shards": [_shard_entry(scan, output, row) for row in group],
            }
        )
    return JsonResponse(
        {
            "scan": scan.pk,
            "output": output,
            "stage": stage,
            "engine": engine,
            "live_run": runs[0]["run"] if runs else None,
            "runs": runs,
        }
    )


@login_required
def serve_glued_volume(
    request: HttpRequest, pk: int, output: str, run: int
) -> HttpResponse:
    """Send the browser to one run's glued volume document (#243).

    The 404 for an absent object follows the rows: an open run is "not
    glued yet", while a run whose every row is CONSUMED was glued and
    has lost its object (swept, or expired), and saying "not glued yet:
    2 result applied" would contradict itself.

    :param request: The HTTP request.
    :param pk: Scan primary key.
    :param output: A key of :data:`GLUED_OUTPUTS`.
    :param run: The run number.
    :return: A 302 to a presigned GET, or a 404 JSON response when the
        run is not glued yet or no S3 is active.
    """
    spec = GLUED_OUTPUTS.get(output)
    if spec is None:
        return _unknown_output(output)
    stage, engine, key_fn = spec
    scan = get_object_or_404(Scan, pk=pk)
    rows = _glued_run_rows(scan, stage, engine, run)
    if not rows:
        return _json_404(f"Run {run} does not exist for this scan.", run=run)
    label = jobs.rows_label(rows)
    if all(row.status == JobStatus.CONSUMED for row in rows):
        missing = (
            f"Run {run} was glued, but its document is not in the bucket "
            f"({label})."
        )
    else:
        missing = f"Run {run} is not glued yet: {label}."
    return _redirect_to_object(
        scan,
        output,
        run,
        key_fn(scan, run),
        filename=f"scan-{scan.pk}-{output}-r{run}.json",
        missing_message=missing,
        label=label,
    )


@login_required
def serve_glued_shard(
    request: HttpRequest, pk: int, output: str, run: int, shard: int
) -> HttpResponse:
    """Send the browser to one shard's result object (#243).

    The worker's own answer, at the row's ``result_key``: for dots.mocr
    it holds ``raw``, the answer as the model wrote it, which the glue
    leaves out of the volume document (#238) and which #242 needs. A
    carried row (#190) names the previous attempt's object, and that
    is the right file.

    :param request: The HTTP request.
    :param pk: Scan primary key.
    :param output: A key of :data:`GLUED_OUTPUTS`.
    :param run: The run number.
    :param shard: The ``shard_index`` inside the run.
    :return: A 302 to a presigned GET, or a 404 JSON response when the
        shard has no result or no S3 is active.
    """
    spec = GLUED_OUTPUTS.get(output)
    if spec is None:
        return _unknown_output(output)
    stage, engine, _key_fn = spec
    scan = get_object_or_404(Scan, pk=pk)
    rows = _glued_run_rows(scan, stage, engine, run)
    if not rows:
        return _json_404(f"Run {run} does not exist for this scan.", run=run)
    label = jobs.rows_label(rows)
    row = next((row for row in rows if row.shard_index == shard), None)
    if row is None:
        return _json_404(
            f"Run {run} has no shard {shard}: it has {len(rows)} shard(s).",
            run=run,
            label=label,
        )
    status = row.get_status_display().lower()
    if not row.result_key:
        return _json_404(
            f"Shard {shard} of run {run} has no result ({status}).",
            run=run,
            label=label,
        )
    return _redirect_to_object(
        scan,
        output,
        run,
        row.result_key,
        filename=f"scan-{scan.pk}-{output}-r{run}-s{shard}.json",
        missing_message=(
            f"The result of shard {shard} of run {run} is not in the "
            f"bucket ({status})."
        ),
        label=label,
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
    :returns: ``page_review_ready``, ``page_review_done``,
        ``has_legacy_ocr`` and the two pending-edit flags, for the
        template context.
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
        **page_edits.pending_edit_flags(scan),
    }


def _block_if_pending_changes(
    request: HttpRequest, scan: Scan
) -> HttpResponse | None:
    """Redirect back to step 1 when the scan has unapplied page changes.

    The detect action ignores the structural page edits -- a delete, an
    insert, a replacement, a rotation -- so running it would silently
    strand the curator's work. They must be applied first (#206), and
    the apply runs after the review-1 approval.

    Only ``start_detect`` calls this, which is step 2, and it checks
    the approval before it calls this, so the message here never asks
    an approved reviewer to approve. The recompute of review 1 does
    not call this: it warns and continues (#151), and the approve
    button of review 1 does not either (#232) -- the approval is what
    the apply waits for.

    :param request: The HTTP request.
    :type request: HttpRequest
    :param scan: The scan to check for pending changes.
    :type scan: Scan
    :returns: A redirect response if there are pending changes, else
        ``None``.
    :rtype: HttpResponse | None
    """
    if page_edits.has_pending_changes(scan):
        messages.warning(
            request,
            "Your page changes are not built into the volume yet, so "
            "this step would ignore them. The pass that builds them "
            "(#206) is not ready.",
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

    yolo_run = yolo.run_summary(scan)
    context = {
        "scan": scan,
        "step": step,
        "is_processing": scan.status in BUSY_STATUSES,
        "issues": scan.issues.all(),
        "missing_pages": scan.missing_pages,
        "has_detections": Detection.objects.filter(scan=scan).exists(),
        "opinions": scan.opinions_json,
        "dots_run": dots_mocr.run_summary(scan),
        "yolo_run": yolo_run,
        "detect_message": detection_message(yolo_run),
        **_review_flags(scan),
    }
    html = render_to_string(
        "scanning/_process_actions.html", context, request=request
    )
    return JsonResponse(
        {
            "html": html,
            "has_pending_changes": context["has_pending_changes"],
        }
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
    """Skip to review 2 when detections exist; otherwise explain.

    The only thing left of this action is its shortcut: a scan that has
    detections goes straight to step 2. It starts nothing itself -- the
    daemon starts the detection run once per shard set (#250) -- so a
    volume with no detections is told where its run stands
    (:func:`detection_message`).

    Approval is the gate (#151), in the view and not only in the bar:
    a scan still in READY_FOR_PAGE_COMPLETENESS_REVIEW is sent back to
    step 1, so a direct POST cannot walk past the review the template
    hides the button for. READY is the one status where the approval
    is pending; a legacy scan never holds it, so the old rows keep
    their shortcut.

    :param request: The HTTP request.
    :param pk: Scan primary key.
    :return: Redirect to the scan processing page (step 2).
    """
    scan = get_object_or_404(Scan, pk=pk)
    # The approval first, then the pending edits: a scan already
    # approved with an open edit must hear about the edit, not be
    # asked to approve again (#232).
    if scan.status == Status.READY_FOR_PAGE_COMPLETENESS_REVIEW:
        messages.warning(request, PAGE_REVIEW_APPROVAL_REQUIRED_MESSAGE)
        return redirect(
            reverse("scan_process", kwargs={"pk": scan.pk}) + "?step=1"
        )
    guard = _block_if_pending_changes(request, scan)
    if guard:
        return guard
    if Detection.objects.filter(scan=scan).exists():
        # Detections exist: the #196 apply imported them, or the old
        # full pipeline left them behind.
        return redirect(
            reverse("scan_process", kwargs={"pk": scan.pk}) + "?step=2"
        )

    messages.info(request, detection_message(yolo.run_summary(scan)))
    return redirect("scan_process", pk=scan.pk)


@login_required
@require_POST
def start_dots_mocr(request: HttpRequest, pk: int) -> HttpResponse:
    """Start the dots.mocr stage over a scan's original shards (#190).

    Staff only. Since #207 the pipeline enqueues this stage for every
    new upload, so this button is the manual way in: a fresh run over
    an edited volume, or a backfill for a scan uploaded while the
    stage was button-only. Every press can start real graphics
    processing unit (GPU) work on RunPod that costs money, which is
    why it stays behind the staff gate.

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
    from scanning import sharding

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
def recalculate(request: HttpRequest, pk: int) -> HttpResponse:
    """Rebuild the page number Issues from the stored readings.

    The "Recompute page number issues" button of review 1 (#151). It
    runs on stored data only, so it works on a web pod that never
    pulled the scan's files from S3 (#153).

    Two cases it answers rather than obeys. A scan the retired
    PaddleOCR stage read gets the legacy message and no recompute: the
    readings cannot change, so a rebuild would only look like work
    (#173). A scan carrying pending inserts or deletes gets the recompute
    plus the warning that says what is and is not done with those
    rows: nothing applies them until #206. Blocking here would leave
    the curator with no way forward at all, which is the fault that
    took the approve button away until #232.

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
    if page_edits.has_pending_changes(scan):
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
    else:
        # The write lost, so the fetch above is stale: a concurrent
        # approval moved the row between the read and the write.
        # Re-read it so the message describes the row as it is.
        scan.refresh_from_db()
        if scan.status == Status.PAGE_COMPLETENESS_REVIEW_DONE:
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
    unified message until the dots.mocr replacement lands. The
    ``PageEdit`` rows stay recorded and will be applicable again then
    (#206).

    :param request: The HTTP request.
    :param pk: Scan primary key.
    :return: Redirect to the scan processing page.
    """
    scan = get_object_or_404(Scan, pk=pk)
    messages.warning(request, PIPELINE_PAUSED_MESSAGE)
    return redirect("scan_process", pk=scan.pk)


def _page_number_value(raw) -> str | None:
    """Return a curator's page number entry, normalized, or None.

    Accepts a positive whole number, and a printed range like
    ``678-686`` for the one PDF page that carries several book pages --
    the shape ``CheckName.PAGE_RANGE`` exists for, and the shape
    ``Page.book_page`` has always documented. A blank entry is the
    curator clearing the number, which is a decision, so it returns the
    empty string rather than None.

    A reporter prints the range with an en dash (``913–925``), and a
    curator types what the page shows, so an en dash and an em dash
    read as the hyphen (issue #233). The stored value carries one
    hyphen, which is the shape every reader of a range parses
    (``services._page_number_lookup``,
    ``blackletter.validate.RANGE_RE``).

    :param raw: The ``page_number`` field of the request body.
    :returns: The value for ``PageEdit.value``, or None when the entry
        is not a page number at all.
    :rtype: str | None
    """
    if raw is None:
        return ""
    text = str(raw).strip().replace("–", "-").replace("—", "-")
    if not text:
        return ""
    parts = [part.strip() for part in text.split("-")]
    if len(parts) > 2 or not all(p.isdigit() and int(p) >= 1 for p in parts):
        return None
    if len(parts) == 2 and int(parts[0]) >= int(parts[1]):
        return None
    return "-".join(str(int(p)) for p in parts)


@login_required
@require_POST
def assign_page(request: HttpRequest, pk: int) -> JsonResponse:
    """Record the page number a curator read off the page itself.

    One ``PageEdit`` row per page, since #214: the decision used to be
    an edit of one entry inside the ``Scan.ocr_results`` list, written
    back whole, so two curators on two pages of one volume lost one of
    the two numbers with no error and no trace.

    A blank ``page_number`` clears the number, marking the page as
    having none (front matter the model mis-tagged, say). It is the
    same kind of row with a blank value, so the row's existence still
    separates "a person cleared this" from "the model read nothing".
    Any other value is a printed number, or a printed range like
    ``678-686`` when one PDF page carries several book pages, which is
    what ``page_range`` is raised for.

    The blob is then rebuilt from the run plus the rows, so the viewer
    shows the number and its duplicate flags at once.

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
    page_value = _page_number_value(data["page_number"])
    if page_value is None:
        return JsonResponse(
            {
                "error": (
                    "Page number must be a positive whole number, or a "
                    "range like 678-686."
                )
            },
            status=400,
        )

    ocr_results = scan.ocr_results
    entry = next((r for r in ocr_results if r["pdf_page"] == pdf_page), None)
    if entry is None:
        return JsonResponse({"error": "Unknown PDF page."}, status=404)

    PageEdit.objects.update_or_create(
        scan=scan,
        kind=PageEdit.Kind.SET_NUMBER,
        pdf_page=pdf_page,
        applied_at=None,
        withdrawn_at=None,
        defaults={
            "author": request.user,
            "value": page_value,
            # The reading this number overrules. It is rebuilt from
            # the run on every recompute, so this row is the only
            # record that a person disagreed with the model.
            "previous_value": str(entry.get("detected") or ""),
            "source_fingerprint": scan.source_fingerprint,
        },
    )

    # Clear the page's no-page-number flag; the rebuild does not touch Issue
    # rows, so a full Recheck re-derives the issue list (and any new flag).
    scan.issues.filter(
        check_name=CheckName.NO_PAGE_NUMBER, page_number=pdf_page
    ).delete()

    # Rebuild page_map so the viewer/sidebar duplicate flags reflect the edit
    # immediately. It overlays the rows, so it also writes the new number
    # into the cached ocr_results.
    from scanning import services

    services.rebuild_page_map(scan)

    duplicate = any(
        e.get("type") == "pdf_page"
        and e.get("pdf_index") == pdf_page - 1
        and e.get("duplicate")
        for e in scan.page_map
    )
    return JsonResponse(
        {
            "status": "ok",
            "detected": page_value or None,
            "duplicate": duplicate,
        }
    )


def _uploaded_page_file(upload) -> str | None:
    """Return ``"pdf"`` or ``"image"`` for an accepted upload, else None.

    A curator scans a page as often to a PDF as to an image (#232), so
    both endpoints that take a page take both. Three rules:

    - **The bytes decide, not the content type.** A browser names the
      type, and a person can name it wrong. A file that says PDF and
      does not start with ``%PDF-`` is refused, and a file that starts
      with it is a PDF whatever the browser said. An image is judged
      the same way, against ``_IMAGE_MAGIC``: a file sent as
      ``image/svg+xml`` used to pass on its content type alone and
      fail in ``insert_image`` at the export.
    - **The stored name carries the right extension.** A browser may
      send a PDF with no extension at all, or a JPEG named ``.png``.
      ``models.page_edit_image_path`` keeps the extension of the name,
      and ``views_api.export_pdf`` reads it to decide between
      ``insert_pdf`` and ``insert_image``, so a wrong extension puts a
      page through the wrong call.
    - **The upload is rewound.** Both readers here read from the file
      the storage backend then saves.

    :param upload: The ``UploadedFile``, or None.
    :returns: The kind, or None when the file is neither.
    :rtype: str | None
    """
    if upload is None:
        return None
    head = upload.read(_MAGIC_LENGTH)
    upload.seek(0)
    if head.startswith(_PDF_MAGIC):
        kind, ext = "pdf", "pdf"
    else:
        ext = next(
            (ext for magic, ext in _IMAGE_MAGIC if head.startswith(magic)),
            None,
        )
        if ext is None:
            return None
        kind = "image"
    base = (upload.name or "page").rsplit("/", 1)[-1].rsplit(".", 1)[0]
    upload.name = f"{base[:64] or 'page'}.{ext}"
    return kind


def _pdf_page_count(upload) -> int | None:
    """Return how many pages an uploaded PDF holds, or None.

    :param upload: The ``UploadedFile``, rewound by
        :func:`_uploaded_page_file`.
    :returns: The page count, or None when the file will not open.
    :rtype: int | None
    """
    data = upload.read()
    upload.seek(0)
    try:
        with fitz.open(stream=data, filetype="pdf") as doc:
            return doc.page_count
    except Exception:
        return None


def _accept_page_upload(upload, one_page: bool) -> tuple[str | None, str]:
    """Judge one uploaded page file for either endpoint.

    :param upload: The ``UploadedFile``, or None.
    :param one_page: Whether the file must hold exactly one page. True
        for a replacement: one page stands for one page, and a volume
        uploaded by mistake would land whole on that address. An
        insert takes every page, because a missing leaf is often two
        and ``export_pdf`` already places them all.
    :returns: The kind and an empty message, or None and the message
        the curator reads.
    :rtype: tuple[str | None, str]
    """
    if upload is None:
        return None, "Missing file"
    if upload.size and upload.size > PAGE_UPLOAD_MAX_BYTES:
        return None, UPLOAD_TOO_LARGE_MESSAGE
    kind = _uploaded_page_file(upload)
    if kind is None:
        return None, UPLOAD_WRONG_TYPE_MESSAGE
    if kind == "pdf":
        pages = _pdf_page_count(upload)
        if pages is None or pages < 1:
            return None, UPLOAD_BAD_PDF_MESSAGE
        if one_page and pages != 1:
            return None, REPLACEMENT_IS_ONE_PAGE_MESSAGE.format(pages=pages)
    return kind, ""


def _save_page_file_row(edit: PageEdit, upload, before=None) -> bool:
    """Store an upload's file, then its row; drop the file if the row loses.

    Two curators who act on one page at the same moment both find no
    row to withdraw, and the second insert loses to the partial unique
    key. ``PageEdit.objects.create`` would have uploaded the file inside
    the row's own save, so the losing request left an object in the
    bucket that no row names. Here the file goes to the storage first
    and the row after it, and a refused row takes its file back.

    :param edit: The unsaved row. Its ``image`` is empty.
    :param upload: The ``UploadedFile`` to store under it.
    :param before: Run inside the row's transaction, before the save:
        the withdrawal of the rows this one supersedes.
    :returns: Whether the row was saved. False when the unique key
        refused it, which is the race above.
    :rtype: bool
    """
    edit.image.save(upload.name, upload, save=False)
    try:
        with transaction.atomic():
            if before is not None:
                before()
            edit.save()
    except IntegrityError:
        edit.image.delete(save=False)
        return False
    return True


def _pdf_page_of(scan: Scan, raw) -> int | None:
    """Return a 1-based page of this volume, or None.

    An address the volume does not have is refused rather than stored:
    a row naming a page that is not there is the drift this model
    exists to prevent.

    :param scan: The scan the page belongs to.
    :param raw: The page number from the request.
    :returns: The page, or None when it is not one of this volume's.
    :rtype: int | None
    """
    try:
        page = int(raw)
    except (TypeError, ValueError):
        return None
    if page < 1:
        return None
    if scan.page_count and page > scan.page_count:
        return None
    return page


#: What a printed page number may be made of. Not digits alone: a
#: volume prints roman numerals in its front matter ("xiv"), letter
#: suffixes on inserted leaves ("1075a"), and section numbers ("A-3").
#: Everything outside this refuses -- which is every character markup
#: is made of, plus every control character. The label is a person's
#: typing that every viewer of the scan then sees, so it is narrowed
#: here *and* escaped where the viewer draws it (``escapeHtml`` in
#: ``shared.js``): narrowing alone would be one regex away from an
#: injection, and escaping alone would keep junk in the column.
_PAGE_LABEL_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z .\u2013/-]*$")


def _page_label(raw: str) -> str | None:
    """Return the printed page number a curator filed a page under.

    :param raw: The ``page_number`` form field.
    :returns: The label, or None when it is not a page number at all.
        An empty entry is allowed and returns the empty string: an
        inserted page need not carry a printed number.
    :rtype: str | None
    """
    label = (raw or "").strip()
    if not label:
        return ""
    if len(label) > 32 or not _PAGE_LABEL_RE.match(label):
        return None
    return label


def _anchor_of(scan: Scan, raw, label: str) -> int | None:
    """Return the original page an uploaded image follows, or None.

    The viewer sends the anchor it rendered. An older viewer sends only
    the printed number, so the anchor is resolved from the stored page
    map instead -- the same walk the viewer's stamp comes from.

    :param scan: The scan the insert belongs to.
    :param raw: The ``anchor_pdf_page`` field, when the viewer sent one.
    :param label: The printed page number the placeholder showed.
    :returns: The anchor, or None when the position cannot be resolved.
    :rtype: int | None
    """
    if raw not in (None, ""):
        try:
            anchor = int(raw)
        except (TypeError, ValueError):
            return None
        if 0 <= anchor <= (scan.page_count or anchor):
            return anchor
        return None
    if not label:
        return None
    for entry in page_edits.project_inserts(scan, scan.page_map):
        if entry.get("type") == "missing" and str(
            entry.get("logical_number")
        ) == str(label):
            return entry.get("anchor_pdf_page")
    return None


@login_required
@require_POST
def delete_page(request: HttpRequest, pk: int) -> HttpResponse:
    """Mark a page of the original for deletion.

    A duplicate page, or a blank one: two of the issue types review 1
    exists to find. One ``PageEdit`` row, addressed by the physical
    page (#214). The apply (#206) decides what a delete does to the
    volume; until then the row is a saved decision and nothing else.

    :param request: The HTTP request (JSON body with pdf_page).
    :param pk: Scan primary key.
    :return: JSON response confirming the deletion record.
    """
    scan = get_object_or_404(Scan, pk=pk)
    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON"}, status=400)
    pdf_page = _pdf_page_of(scan, data.get("pdf_page"))
    if pdf_page is None:
        return JsonResponse({"error": "Unknown PDF page."}, status=404)
    # Both stamps are in the lookup, not just the apply's (#232): a
    # withdrawn row is history, and matching it would hand the caller
    # a row that marks nothing.
    PageEdit.objects.get_or_create(
        scan=scan,
        kind=PageEdit.Kind.DELETE_PAGE,
        pdf_page=pdf_page,
        applied_at=None,
        withdrawn_at=None,
        defaults={
            "author": request.user,
            "source_fingerprint": scan.source_fingerprint,
        },
    )
    return JsonResponse({"status": "ok"})


@login_required
@require_POST
def undo_delete_page(request: HttpRequest, pk: int) -> HttpResponse:
    """Take back a page deletion, restoring the page.

    The row is stamped, not deleted (#232): a decision a curator took
    back is history too, and the audit must show that somebody marked
    this page and somebody unmarked it.

    :param request: The HTTP request (JSON body with pdf_page).
    :param pk: Scan primary key.
    :return: JSON response confirming the undo.
    """
    scan = get_object_or_404(Scan, pk=pk)
    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON"}, status=400)
    page_edits.withdraw(
        page_edits.open_edits(scan, PageEdit.Kind.DELETE_PAGE).filter(
            pdf_page=data.get("pdf_page")
        ),
        request.user,
    )
    return JsonResponse({"status": "ok"})


@login_required
@require_POST
def add_page_insert(request: HttpRequest, pk: int) -> JsonResponse:
    """Upload the image of a page the volume is missing.

    The address is the gap, not the printed number (#214):
    ``anchor_pdf_page`` is the original page the image follows, and 0
    puts it before page 1. The viewer stamps that anchor on every
    ``missing`` placeholder it renders and sends it back here, so the
    physical position is resolved once, by the person who can see it.
    A printed number cannot address anything -- front matter has none,
    and two pages can print the same one -- so it is kept beside the
    address as the label.

    The label is free text on purpose: a printed page number is not
    always a whole number ("xiv", "1075a", "A-3"), so casting it to an
    integer would lose what the curator read off the page. It is
    narrowed to the alphabet a printed number uses (``_page_label``)
    and escaped where the viewer draws it, rather than cast.

    The file may be an image of the page or a PDF of it (#232), and a
    PDF may hold several pages: a missing leaf is often two, and
    ``views_api.export_pdf`` already places every page of one.

    :param request: The HTTP request (form data with ``image``,
        ``anchor_pdf_page`` and the ``page_number`` label).
    :param pk: Scan primary key.
    :return: JSON response with the insert URL and page number.
    """
    scan = get_object_or_404(Scan, pk=pk)
    image_file = request.FILES.get("image")
    kind, refusal = _accept_page_upload(image_file, one_page=False)
    if kind is None:
        return JsonResponse({"error": refusal}, status=400)
    label = _page_label(request.POST.get("page_number"))
    if label is None:
        return JsonResponse(
            {
                "error": (
                    "A page number may hold letters and digits, spaces, "
                    "a dot, a slash and a dash, and no more than 32 of "
                    "them."
                )
            },
            status=400,
        )
    anchor = _anchor_of(scan, request.POST.get("anchor_pdf_page"), label)
    if anchor is None:
        return JsonResponse(
            {
                "error": (
                    "This page could not be placed in the volume. "
                    "Reload the page and try again."
                )
            },
            status=400,
        )

    edit = PageEdit(
        scan=scan,
        kind=PageEdit.Kind.INSERT_PAGE,
        author=request.user,
        anchor_pdf_page=anchor,
        ordinal=page_edits.next_ordinal(scan, anchor),
        logical_page=label,
        source_fingerprint=scan.source_fingerprint,
    )
    # Two inserts into one gap at the same moment compute one ordinal;
    # the second loses to the partial key and leaves no file behind.
    if not _save_page_file_row(edit, image_file):
        return JsonResponse({"error": UPLOAD_LOST_RACE_MESSAGE}, status=409)
    return JsonResponse(
        {
            "status": "ok",
            "page_number": label,
            "edit_id": edit.pk,
            "image_url": edit.image.url,
            "kind": kind,
            "file_url": reverse(
                "page_edit_file", kwargs={"pk": scan.pk, "edit_id": edit.pk}
            ),
        }
    )


@login_required
@require_POST
def remove_page_insert(request: HttpRequest, pk: int) -> JsonResponse:
    """Take back an uploaded page image.

    The portal had no way to undo an insert: a deletion had its undo
    and an insert did not, so a wrong image could only be replaced by
    another one. The row is stamped and the file is kept (#232): the
    audit shows what a person uploaded, and an object nobody reads
    costs less than a row that names a file which is gone.

    :param request: The HTTP request (JSON body with ``edit_id``).
    :param pk: Scan primary key.
    :return: JSON response confirming the removal.
    """
    scan = get_object_or_404(Scan, pk=pk)
    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON"}, status=400)
    edit = (
        page_edits.open_edits(scan, PageEdit.Kind.INSERT_PAGE)
        .filter(pk=data.get("edit_id"))
        .first()
    )
    if edit is None:
        return JsonResponse({"error": "Unknown page insert."}, status=404)
    page_edits.withdraw(
        page_edits.open_edits(scan, PageEdit.Kind.INSERT_PAGE).filter(
            pk=edit.pk
        ),
        request.user,
    )
    return JsonResponse({"status": "ok"})


@login_required
@require_POST
def replace_page(request: HttpRequest, pk: int) -> JsonResponse:
    """Upload a page to stand in for one that cannot be read.

    The Replace button of review 1 (#232). A blurry page is a kind of
    missing page (#205), and the portal could not record it at all.
    One kind, not a delete beside an insert: those two would need an
    address in two spaces to say "this page stands where that page
    stood", and a curator taking the replacement back would have to
    take back both.

    The upload may be an image or a PDF of one page. **A second upload
    for one page withdraws the row before it and writes a new one**,
    rather than writing over its ``image`` field: the audit must show
    every file a person uploaded, and an overwritten field leaves its
    object in the bucket with no row that names it.

    Nothing applies the row to the volume yet. The pass that does is
    #206, and it runs each replacement through the stages as a
    one-page shard.

    :param request: The HTTP request (form data with ``pdf_page`` and
        ``image``).
    :param pk: Scan primary key.
    :return: JSON response with the edit id and the file URL.
    """
    scan = get_object_or_404(Scan, pk=pk)
    image_file = request.FILES.get("image")
    kind, refusal = _accept_page_upload(image_file, one_page=True)
    if kind is None:
        return JsonResponse({"error": refusal}, status=400)
    pdf_page = _pdf_page_of(scan, request.POST.get("pdf_page"))
    if pdf_page is None:
        return JsonResponse({"error": "Unknown PDF page."}, status=404)

    def withdraw_earlier():
        """Close the replacement this one supersedes, if any."""
        page_edits.withdraw(
            page_edits.open_edits(scan, PageEdit.Kind.REPLACE_PAGE).filter(
                pdf_page=pdf_page
            ),
            request.user,
        )

    edit = PageEdit(
        scan=scan,
        kind=PageEdit.Kind.REPLACE_PAGE,
        author=request.user,
        pdf_page=pdf_page,
        source_fingerprint=scan.source_fingerprint,
    )
    # Two replacements of one page at the same moment both withdraw
    # nothing and both insert; the second loses to the partial unique
    # key, and its file is taken back with it.
    if not _save_page_file_row(edit, image_file, before=withdraw_earlier):
        return JsonResponse({"error": UPLOAD_LOST_RACE_MESSAGE}, status=409)
    return JsonResponse(
        {
            "status": "ok",
            "edit_id": edit.pk,
            "image_url": edit.image.url,
            "kind": kind,
            "file_url": reverse(
                "page_edit_file", kwargs={"pk": scan.pk, "edit_id": edit.pk}
            ),
        }
    )


@login_required
@require_POST
def undo_replace_page(request: HttpRequest, pk: int) -> JsonResponse:
    """Take back the replacement of a page.

    The row is stamped and the file is kept (#232), like every other
    undo of review 1. The page then stands as it was scanned, and it
    may be replaced again: the unique key is partial over the rows
    that carry neither stamp.

    An undo of a replacement that no longer stands is a no-op, as it
    is in ``undo_delete_page``: a second tab, or a second click, must
    not show an error for a page that is already back.

    :param request: The HTTP request (JSON body with ``pdf_page``).
    :param pk: Scan primary key.
    :return: JSON response confirming the undo.
    """
    scan = get_object_or_404(Scan, pk=pk)
    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON"}, status=400)
    page_edits.withdraw(
        page_edits.open_edits(scan, PageEdit.Kind.REPLACE_PAGE).filter(
            pdf_page=data.get("pdf_page")
        ),
        request.user,
    )
    return JsonResponse({"status": "ok"})


@login_required
def page_edit_file(
    request: HttpRequest, pk: int, edit_id: int
) -> HttpResponse:
    """Send the reader to the file a curator uploaded for one page.

    A redirect, and not the URL itself in the page (#232): the default
    storage signs its URLs and the signature expires in an hour, while
    a review page stays open for longer. Signing at the moment of the
    click also keeps the link working with the local storage of a
    development environment.

    A withdrawn or applied row is served too, so a link in the audit
    keeps working after the decision is closed.

    :param request: The HTTP request.
    :param pk: Scan primary key.
    :param edit_id: The ``PageEdit`` whose file is wanted.
    :return: A redirect to the file.
    """
    scan = get_object_or_404(Scan, pk=pk)
    edit = get_object_or_404(PageEdit, pk=edit_id, scan=scan)
    if not edit.image:
        raise Http404("This page edit carries no file.")
    return redirect(edit.image.url)


@login_required
@require_POST
def rotate_page(request: HttpRequest, pk: int) -> JsonResponse:
    """Record that a page is printed the wrong way up.

    The answer to ``CheckName.ORIENTATION``, which the portal could
    raise but never resolve. The value is clockwise degrees, and only a
    quarter turn is a legal one.

    The endpoint lands with the model; the button belongs with #206 and
    #151.

    :param request: The HTTP request (JSON body with ``pdf_page`` and
        ``degrees``).
    :param pk: Scan primary key.
    :return: JSON response confirming the rotation.
    """
    scan = get_object_or_404(Scan, pk=pk)
    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON"}, status=400)
    pdf_page = _pdf_page_of(scan, data.get("pdf_page"))
    if pdf_page is None:
        return JsonResponse({"error": "Unknown PDF page."}, status=404)
    degrees = str(data.get("degrees", "")).strip()
    if degrees not in PAGE_EDIT_ROTATIONS:
        return JsonResponse(
            {
                "error": (
                    "A rotation must be one of "
                    f"{', '.join(PAGE_EDIT_ROTATIONS)} degrees."
                )
            },
            status=400,
        )
    PageEdit.objects.update_or_create(
        scan=scan,
        kind=PageEdit.Kind.ROTATE_PAGE,
        pdf_page=pdf_page,
        applied_at=None,
        withdrawn_at=None,
        defaults={
            "author": request.user,
            "value": degrees,
            "source_fingerprint": scan.source_fingerprint,
        },
    )
    return JsonResponse({"status": "ok"})


@login_required
@require_POST
def dismiss_issue(request: HttpRequest, pk: int) -> JsonResponse:
    """Record that a curator judged one issue not worth acting on.

    One ``PageEdit`` row, since #214, and no longer a ``DELETE`` of the
    ``Issue`` row: every rebuild deletes the derived issues and writes
    them again with new primary keys, so a dismissal used to come back
    on the next press of the recompute button. The rebuild now reads
    these rows as an input, the way it already keeps
    ``suppress_detection``.

    The row keeps the address in the space its check uses -- a physical
    PDF page for the checks in ``PHYSICAL_PAGE_CHECKS``, the printed
    number for the rest -- because those two are different spaces and a
    printed number is not unique.

    The issue row itself is deleted too, so the card goes away at once
    without a page reload. The pending-changes guard is gone with the
    convention it protected: a dismissal is durable now, so it needs no
    apply and cannot be lost by one.

    :param request: The HTTP request (JSON body with issue_id).
    :param pk: Scan primary key.
    :return: JSON response confirming dismissal.
    """
    scan = get_object_or_404(Scan, pk=pk)
    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON"}, status=400)
    issue = Issue.objects.filter(pk=data.get("issue_id"), scan=scan).first()
    if issue is None:
        return JsonResponse({"error": "Unknown issue."}, status=404)

    physical = issue.check_name in PHYSICAL_PAGE_CHECKS
    PageEdit.objects.update_or_create(
        scan=scan,
        kind=PageEdit.Kind.DISMISS_ISSUE,
        pdf_page=issue.page_number if physical else None,
        logical_page=(
            ""
            if physical or issue.page_number is None
            else str(issue.page_number)
        ),
        value=issue.check_name,
        applied_at=None,
        withdrawn_at=None,
        defaults={
            "author": request.user,
            "source_fingerprint": scan.source_fingerprint,
        },
    )
    issue.delete()
    return JsonResponse({"status": "ok"})
