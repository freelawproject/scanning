"""JSON API endpoints and file-serving views for the process page."""

import json
import logging
import os
import shutil
import tempfile
import traceback
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import fitz
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import (
    FileResponse,
    Http404,
    HttpRequest,
    HttpResponse,
    JsonResponse,
    StreamingHttpResponse,
)
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils.cache import get_conditional_response
from django.utils.http import http_date
from django.views.decorators.clickjacking import xframe_options_sameorigin
from django.views.decorators.http import require_POST

from scanning.models import (
    CheckName,
    Detection,
    Issue,
    OpinionScan,
    QueuedAction,
    Scan,
    Stage,
    Status,
)
from scanning.utils import (
    compute_coverage_gaps,
    find_ocr_pdf,
)

logger = logging.getLogger(__name__)


def _parse_json_body(request: HttpRequest) -> dict | JsonResponse:
    """Parse a JSON request body or return an error response.

    :param request: The HTTP request.
    :returns: The parsed dict on success, or a ``JsonResponse`` with a
        400 error on malformed JSON.
    :rtype: dict | JsonResponse
    """
    try:
        return json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON"}, status=400)


def _rounded_rect(adjusted: dict) -> dict:
    """Round ``x0``/``y0``/``x1``/``y1`` to one decimal place.

    :param adjusted: Dict with ``x0``, ``y0``, ``x1``, ``y1`` keys.
    :returns: Dict with the same keys, values rounded to 1 decimal.
    :rtype: dict
    """
    return {
        "x0": round(adjusted["x0"], 1),
        "y0": round(adjusted["y0"], 1),
        "x1": round(adjusted["x1"], 1),
        "y1": round(adjusted["y1"], 1),
    }


@login_required
def serve_detections(request: HttpRequest, pk: int) -> JsonResponse:
    """Return active detections for a scan as JSON.

    :param request: The HTTP request.
    :param pk: Scan primary key.
    :return: JSON response with a list of detection dicts.
    """
    scan = get_object_or_404(Scan, pk=pk)
    dets = Detection.objects.filter(scan=scan, active=True).order_by(
        "page_index", "y0"
    )
    data = [
        {
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
        for d in dets
    ]
    return JsonResponse(data, safe=False)


@login_required
def serve_opinions(request: HttpRequest, pk: int) -> JsonResponse:
    """Return paired opinion data for a scan as JSON.

    :param request: The HTTP request.
    :param pk: Scan primary key.
    :return: JSON response with a list of opinion dicts.
    """
    scan = get_object_or_404(Scan, pk=pk)
    if scan.opinions_json:
        return JsonResponse(scan.opinions_json, safe=False)
    return JsonResponse([], safe=False)


@login_required
def serve_margin_rects(request: HttpRequest, pk: int) -> JsonResponse:
    """Return margin rectangles for a scan, computing them if absent.

    :param request: The HTTP request.
    :param pk: Scan primary key.
    :return: JSON response with per-page margin rect data.
    """
    scan = get_object_or_404(Scan, pk=pk)
    if scan.margin_rects:
        return JsonResponse(scan.margin_rects, safe=False)
    output_base = Path(scan.output_dir)
    ocr_pdf = find_ocr_pdf(output_base) if output_base.is_dir() else None
    if not ocr_pdf:
        return JsonResponse([], safe=False)
    from blackletter.margins import compute_margin_rects

    from scanning.services import _adjust_margins_for_detections

    rects = compute_margin_rects(ocr_pdf)
    rects = _adjust_margins_for_detections(rects, output_base)
    Scan.objects.filter(pk=pk).update(margin_rects=rects)
    return JsonResponse(rects, safe=False)


@login_required
def serve_redaction_rects(request: HttpRequest, pk: int) -> JsonResponse:
    """Return redaction rectangles for a scan as JSON.

    :param request: The HTTP request.
    :param pk: Scan primary key.
    :return: JSON response with per-page redaction rect data.
    """
    scan = get_object_or_404(Scan, pk=pk)
    if scan.redaction_rects:
        return JsonResponse(scan.redaction_rects, safe=False)
    return JsonResponse([], safe=False)


@login_required
@require_POST
def save_redaction_rect(request: HttpRequest, pk: int) -> JsonResponse:
    """Create, update, or delete a redaction rectangle on disk.

    :param request: The HTTP request (JSON body with page_index,
        action, original, adjusted, type, and fill).
    :param pk: Scan primary key.
    :return: JSON response confirming the operation.
    """
    scan = get_object_or_404(Scan, pk=pk)
    data = _parse_json_body(request)
    if isinstance(data, JsonResponse):
        return data
    page_idx = data["page_index"]
    action = data.get("action", "update")
    original = data.get("original", {})
    adjusted = data.get("adjusted", {})
    rect_type = data.get("type", "")
    fill = data.get("fill", "black")
    if not scan.redaction_rects:
        return JsonResponse({"error": "No redaction rects"}, status=404)
    rects = scan.redaction_rects
    if action == "delete":
        for page_data in rects:
            if page_data["page_index"] != page_idx:
                continue
            page_data["rects"] = [
                r
                for r in page_data["rects"]
                if not (
                    abs(r["x0"] - original["x0"]) < 2
                    and abs(r["y0"] - original["y0"]) < 2
                    and r.get("type", "") == rect_type
                )
            ]
            break
        Scan.objects.filter(pk=pk).update(redaction_rects=rects)
        return JsonResponse({"status": "ok", "action": "deleted"})
    found = False
    for page_data in rects:
        if page_data["page_index"] != page_idx:
            continue
        for r in page_data["rects"]:
            if (
                abs(r["x0"] - original.get("x0", -999)) < 2
                and abs(r["y0"] - original.get("y0", -999)) < 2
                and r.get("type") == rect_type
            ):
                r.update(_rounded_rect(adjusted))
                found = True
                break
        if found:
            break
    if not found:
        for page_data in rects:
            if page_data["page_index"] == page_idx:
                page_data["rects"].append(
                    {
                        **_rounded_rect(adjusted),
                        "fill": fill,
                        "type": rect_type,
                    }
                )
                found = True
                break
    Scan.objects.filter(pk=pk).update(redaction_rects=rects)
    return JsonResponse({"status": "ok", "found": found})


@login_required
@require_POST
def save_margin_rect(request: HttpRequest, pk: int) -> JsonResponse:
    """Update or delete a margin rectangle on disk.

    :param request: The HTTP request (JSON body with page_index,
        action, original, and adjusted).
    :param pk: Scan primary key.
    :return: JSON response confirming the operation.
    """
    scan = get_object_or_404(Scan, pk=pk)
    data = _parse_json_body(request)
    if isinstance(data, JsonResponse):
        return data
    page_idx = data["page_index"]
    original = data.get("original", {})
    action = data.get("action", "update")
    adjusted = data.get("adjusted", {})
    if not scan.margin_rects:
        return JsonResponse({"error": "No margin rects"}, status=404)
    rects = scan.margin_rects
    if action == "delete":
        for page_data in rects:
            if page_data["page_index"] != page_idx:
                continue
            page_data["rects"] = [
                r
                for r in page_data["rects"]
                if not (
                    abs(r["x0"] - original.get("x0", -999)) < 2
                    and abs(r["y0"] - original.get("y0", -999)) < 2
                )
            ]
            break
        Scan.objects.filter(pk=pk).update(margin_rects=rects)
        return JsonResponse({"status": "ok", "action": "deleted"})
    found = False
    for page_data in rects:
        if page_data["page_index"] != page_idx:
            continue
        for r in page_data["rects"]:
            if (
                abs(r["x0"] - original.get("x0", -999)) < 2
                and abs(r["y0"] - original.get("y0", -999)) < 2
            ):
                r.update(_rounded_rect(adjusted))
                found = True
                break
        if found:
            break
    Scan.objects.filter(pk=pk).update(margin_rects=rects)
    return JsonResponse({"status": "ok", "found": found})


@login_required
@require_POST
def pair_opinions_api(request: HttpRequest, pk: int) -> JsonResponse:
    """Run opinion pairing on detections and save the result.

    :param request: The HTTP request.
    :param pk: Scan primary key.
    :return: JSON response with paired opinions and coverage gaps.
    """
    scan = get_object_or_404(Scan, pk=pk)
    output_dir = Path(scan.output_dir)
    if not output_dir.is_dir():
        return JsonResponse({"error": "No output directory"}, status=400)
    det_path = output_dir / "detections.json"
    from scanning.services import _sync_detections_to_disk

    det_data = _sync_detections_to_disk(scan.pk)
    if not det_data:
        return JsonResponse({"error": "No detections found"}, status=400)
    bitonal = output_dir / "bitonal.pdf"
    pdf_path = str(bitonal) if bitonal.exists() else scan.pdf_path
    try:
        from blackletter.api import pair as bl_pair

        opinions = bl_pair(
            str(det_path),
            pdf_path,
            reporter=scan.reporter.short_name,
            volume=str(scan.volume),
            first_page=scan.start_page or 1,
        )
        scan.opinions_json = opinions
        scan.s3_uploaded = False
        scan.save()
        gaps = [
            {"start": start, "end": end, "count": count}
            for start, end, count in compute_coverage_gaps(
                opinions, scan.start_page, scan.end_page
            )
        ]
        return JsonResponse(
            {"status": "ok", "opinions": opinions, "gaps": gaps}
        )
    except Exception:
        traceback.print_exc()
        return JsonResponse({"error": "Opinion pairing failed"}, status=500)


@login_required
@require_POST
def compute_redactions_api(request: HttpRequest, pk: int) -> JsonResponse:
    """Compute and save redaction and margin rectangles for a scan.

    :param request: The HTTP request.
    :param pk: Scan primary key.
    :return: JSON response with page and rect counts.
    """
    from scanning.services import (
        _compute_and_save_margin_rects,
        _compute_and_save_redaction_rects,
    )

    scan = get_object_or_404(Scan, pk=pk)
    if not Path(scan.output_dir).is_dir():
        return JsonResponse({"error": "No output directory"}, status=400)
    if not scan.opinions_json:
        return JsonResponse({"error": "No opinions paired yet"}, status=400)
    output_dir = scan.output_dir
    pdf_path = find_ocr_pdf(output_dir) or scan.pdf_path
    try:
        rects = _compute_and_save_redaction_rects(pk, pdf_path)
        _compute_and_save_margin_rects(pk, pdf_path, output_dir)
        Scan.objects.filter(pk=pk).update(s3_uploaded=False)
        total_rects = sum(len(r["rects"]) for r in rects)
        return JsonResponse(
            {"status": "ok", "pages": len(rects), "rects": total_rects}
        )
    except Exception:
        traceback.print_exc()
        return JsonResponse(
            {"error": "Redaction computation failed"}, status=500
        )


@login_required
@require_POST
def generate_files(request: HttpRequest, pk: int) -> HttpResponse:
    """Queue a scan for opinion file generation (step 4).

    :param request: The HTTP request.
    :param pk: Scan primary key.
    :return: Redirect to the scan processing page (step 4).
    """
    scan = get_object_or_404(Scan, pk=pk)
    if scan.status in (Status.PROCESSING, Status.QUEUED):
        return redirect(
            reverse("scan_process", kwargs={"pk": scan.pk}) + "?step=3"
        )
    scan.stage = Stage.PROCESS
    scan.status = Status.QUEUED
    scan.queued_action = QueuedAction.GENERATE_FILES
    scan.s3_uploaded = False
    scan.progress_message = "Queued for file generation..."
    scan.save()
    return redirect(
        reverse("scan_process", kwargs={"pk": scan.pk}) + "?step=3"
    )


@login_required
@require_POST
def approve_scan(request: HttpRequest, pk: int) -> HttpResponse:
    """Mark a scan as approved.

    Generate Files already pushed every output file to
    ``processing/<pk>/...`` on S3, so this view is a pure status flip:
    it validates that file generation has run, then sets
    ``status=APPROVED``. Phase 2 will wire Approve into the
    LLM-extraction handoff.

    :param request: The HTTP request.
    :param pk: Scan primary key.
    :return: Redirect to the process page.
    """
    from scanning.services import refresh_volume_queue_status_for_scan

    scan = get_object_or_404(Scan, pk=pk)

    if scan.stage != Stage.APPROVED:
        messages.error(
            request, "Before approving you need to generate the files."
        )
        return redirect("scan_process", pk=scan.pk)

    scan.status = Status.APPROVED
    scan.save(update_fields=["status"])
    refresh_volume_queue_status_for_scan(scan)
    messages.success(request, "Scan approved.")
    return redirect("scan_process", pk=scan.pk)


def _resolve_opinion_pdf(
    field: Any, opinion: OpinionScan
) -> tuple[int, datetime, Any] | None:
    """Locate an OpinionScan PDF and return its size, mtime, and opener.

    The FileField name is either a MEDIA_ROOT-relative path (DEV,
    resolves via the storage backend) or a scan.output_dir-relative
    path (prod, file lives under /tmp/scanning/{pk}/...). Try the
    storage backend first; fall back to resolving against the scan's
    output_dir, with a lazy S3 pull as a last resort.

    :param field: The FileField from the OpinionScan instance.
    :param opinion: The OpinionScan instance.
    :returns: ``(size_bytes, mtime, opener)`` where ``opener`` is a
        zero-arg callable returning an open binary file handle, or
        ``None`` if the file cannot be located.
    :rtype: tuple[int, datetime, Any] | None
    """
    if field.storage.exists(field.name):
        size = field.storage.size(field.name)
        mtime = field.storage.get_modified_time(field.name)
        return size, mtime, lambda: field.open("rb")

    if opinion.scan:
        candidate = Path(opinion.scan.output_dir) / field.name
        if not candidate.is_file():
            try:
                from scanning import s3_sync

                s3_sync.download_processing_files(opinion.scan)
            except Exception:
                logger.exception(
                    "Lazy S3 pull failed for opinion %s", opinion.pk
                )
        if candidate.is_file():
            stat = candidate.stat()
            mtime = datetime.fromtimestamp(stat.st_mtime, tz=UTC)
            return stat.st_size, mtime, lambda: candidate.open("rb")

    return None


@login_required
def serve_opinionscan_pdf(
    request: HttpRequest, pk: int, variant: str
) -> FileResponse | HttpResponse:
    """Serve a PDF for an OpinionScan by variant (redacted/original).

    The file path comes from the model field, not from user input. The
    response carries ``ETag`` / ``Last-Modified`` / ``Cache-Control:
    no-cache`` so the browser always revalidates but skips re-downloading
    the file when its cached copy is still fresh (returns 304). The
    ``ETag`` is derived from the file's mtime and size, so it
    invalidates automatically when a redaction edit rewrites the file.

    :param request: The HTTP request.
    :param pk: OpinionScan primary key.
    :param variant: One of 'redacted' or 'original'.
    :return: File response streaming the PDF, or a 304 Not Modified
        response when the client's cached copy is still fresh.
    :rtype: FileResponse | HttpResponse
    """
    opinion = get_object_or_404(OpinionScan, pk=pk)
    field_map = {
        "redacted": opinion.redacted_pdf,
        "original": opinion.original_pdf,
    }
    field = field_map.get(variant)
    if not field or not field.name:
        raise Http404

    resolved = _resolve_opinion_pdf(field, opinion)
    if resolved is None:
        raise Http404
    size, mtime, opener = resolved

    last_modified = int(mtime.timestamp())
    etag = f'"{last_modified}-{size}"'
    conditional = get_conditional_response(
        request, etag=etag, last_modified=last_modified
    )
    if conditional is not None:
        response: FileResponse | HttpResponse = conditional
    else:
        response = FileResponse(opener(), content_type="application/pdf")
    response["Cache-Control"] = "private, no-cache"
    response["ETag"] = etag
    response["Last-Modified"] = http_date(last_modified)
    return response


@login_required
@xframe_options_sameorigin
def serve_page_pdf(request: HttpRequest, pk: int) -> FileResponse:
    """Serve the per-page PDF for a ``Page`` row.

    Resolves ``page.pdf_path`` (relative to ``scan.output_dir``) to a
    local file, falling back to an S3 pull when the file is missing
    locally — same pattern as ``serve_opinionscan_pdf``.

    :param request: The HTTP request.
    :param pk: Page primary key.
    :return: File response streaming the PDF.
    :raises Http404: When the page or its file can't be found.
    """
    from scanning.models import Page

    page = get_object_or_404(Page.objects.select_related("scan"), pk=pk)
    if not page.pdf_path or not page.scan:
        raise Http404

    candidate = Path(page.scan.output_dir) / page.pdf_path
    if candidate.is_file():
        return FileResponse(
            candidate.open("rb"), content_type="application/pdf"
        )
    try:
        from scanning import s3_sync

        s3_sync.download_processing_files(page.scan)
    except Exception:
        logger.exception("Lazy S3 pull failed for page %s", page.pk)
    if candidate.is_file():
        return FileResponse(
            candidate.open("rb"), content_type="application/pdf"
        )
    raise Http404


def _apply_rect_to_pdf(
    pdf_path: str,
    page_index: int,
    x0: float,
    y0: float,
    x1: float,
    y1: float,
    fill: str,
) -> None:
    """Apply a redaction rectangle directly to a PDF file on disk.

    :param pdf_path: Filesystem path to the PDF to modify.
    :param page_index: Zero-based page index.
    :param x0: Left coordinate of the rectangle.
    :param y0: Top coordinate of the rectangle.
    :param x1: Right coordinate of the rectangle.
    :param y1: Bottom coordinate of the rectangle.
    :param fill: Fill color, either ``"black"`` or ``"white"``.
    :return: None.
    """
    with fitz.open(pdf_path) as doc:
        if page_index < 0 or page_index >= doc.page_count:
            raise ValueError(
                f"Page index {page_index} out of range (0-{doc.page_count - 1})"
            )
        page = doc.load_page(page_index)
        rect = fitz.Rect(x0, y0, x1, y1)
        color = (0, 0, 0) if fill == "black" else (1, 1, 1)
        page.add_redact_annot(rect, fill=color)
        page.apply_redactions()
        # Save to temp file then move -- fitz can't save to the same path it opened
        fd, tmp_path = tempfile.mkstemp(
            suffix=".pdf", dir=os.path.dirname(pdf_path)
        )
        os.close(fd)
        doc.save(tmp_path, garbage=3, deflate=True)
    shutil.move(tmp_path, pdf_path)


@login_required
@require_POST
def apply_rect_to_opinion(
    request: HttpRequest, pk: int, opinion_pk: int
) -> JsonResponse:
    """Apply a redaction rectangle to an opinion's redacted PDF.

    :param request: The HTTP request (JSON body with page_index,
        x0, y0, x1, y1, and fill).
    :param pk: Scan primary key.
    :param opinion_pk: OpinionScan primary key.
    :return: JSON response confirming the operation.
    """
    scan = get_object_or_404(Scan, pk=pk)
    opinion = get_object_or_404(OpinionScan, pk=opinion_pk, scan=scan)
    data = _parse_json_body(request)
    if isinstance(data, JsonResponse):
        return data
    page_index = data["page_index"]
    x0, y0, x1, y1 = data["x0"], data["y0"], data["x1"], data["y1"]
    fill = data.get("fill", "black")

    # Always apply to the redacted PDF
    redacted_path = os.path.join(
        scan.output_dir,
        "redacted",
        os.path.basename(opinion.redacted_pdf.name),
    )
    if os.path.isfile(redacted_path):
        _apply_rect_to_pdf(redacted_path, page_index, x0, y0, x1, y1, fill)

    return JsonResponse({"status": "ok"})


@login_required
def serve_redacted_pdf(
    request: HttpRequest, pk: int
) -> FileResponse | HttpResponse:
    """Serve the redacted PDF for a scan.

    Falls back to the OCR PDF if the redacted version hasn't been
    generated yet, so the viewer has something to display.

    :param request: The HTTP request.
    :param pk: Scan primary key.
    :return: File response streaming the PDF.
    """
    scan = get_object_or_404(Scan, pk=pk)

    # 1. Try the explicit redacted PDF path
    if scan.redacted_pdf_path and os.path.isfile(scan.redacted_pdf_path):
        return FileResponse(
            Path(scan.redacted_pdf_path).open("rb"),
            content_type="application/pdf",
        )

    # 2. Fall back to the OCR PDF (for preview before generation)
    output = Path(scan.output_dir)
    if output.is_dir():
        ocr = find_ocr_pdf(output)
        if ocr:
            return FileResponse(ocr.open("rb"), content_type="application/pdf")

    # 3. Lazy S3 pull + retry: handles the case where the daemon just
    # finished Generate Files and the web container's /tmp/ is stale.
    try:
        from scanning import s3_sync

        s3_sync.download_processing_files(scan)
    except Exception:
        logger.exception("Lazy S3 pull failed for scan %s", scan.pk)
    if scan.redacted_pdf_path and os.path.isfile(scan.redacted_pdf_path):
        return FileResponse(
            Path(scan.redacted_pdf_path).open("rb"),
            content_type="application/pdf",
        )
    if output.is_dir():
        ocr = find_ocr_pdf(output)
        if ocr:
            return FileResponse(ocr.open("rb"), content_type="application/pdf")

    return HttpResponse("No PDF available", status=404)


@login_required
def serve_ocr_results(request: HttpRequest, pk: int) -> JsonResponse:
    """Return OCR page-number results for a scan as JSON.

    :param request: The HTTP request.
    :param pk: Scan primary key.
    :return: JSON response with a list of OCR result dicts.
    """
    scan = get_object_or_404(Scan, pk=pk)
    if scan.ocr_results:
        return JsonResponse(scan.ocr_results, safe=False)
    return JsonResponse([], safe=False)


@login_required
@require_POST
def flag_issue(request: HttpRequest, pk: int) -> JsonResponse:
    """Create a user-flagged issue on a scan.

    :param request: The HTTP request (JSON body with message,
        page_number, and metadata).
    :param pk: Scan primary key.
    :return: JSON response with the new issue ID.
    """
    scan = get_object_or_404(Scan, pk=pk)
    data = _parse_json_body(request)
    if isinstance(data, JsonResponse):
        return data
    message = data.get("message", "").strip()
    page = data.get("page_number")
    metadata = data.get("metadata", {})
    if not message:
        return JsonResponse(
            {"status": "error", "message": "Message required"}, status=400
        )
    type_to_check = {
        "suppress_detection": CheckName.SUPPRESS_DETECTION,
        "add_detection": CheckName.ADD_DETECTION,
        "approve_detection": CheckName.APPROVE_DETECTION,
    }
    check_name = type_to_check.get(
        metadata.get("type", ""), CheckName.PROCESS_FLAG
    )
    issue = Issue.objects.create(
        scan=scan,
        page_number=page,
        check_name=check_name,
        severity="warning",
        message=message,
        metadata=metadata or {},
    )
    return JsonResponse({"status": "ok", "id": issue.pk})


@login_required
@require_POST
def remove_flag(request: HttpRequest, pk: int, flag_id: int) -> JsonResponse:
    """Remove a user-flagged issue from a scan.

    :param request: The HTTP request.
    :param pk: Scan primary key.
    :param flag_id: Primary key of the Issue to remove.
    :return: JSON response confirming removal.
    """
    scan = get_object_or_404(Scan, pk=pk)
    Issue.objects.filter(
        pk=flag_id,
        scan=scan,
        check_name__in=[
            CheckName.PROCESS_FLAG,
            CheckName.SUPPRESS_DETECTION,
            CheckName.ADD_DETECTION,
            CheckName.APPROVE_DETECTION,
        ],
    ).delete()
    return JsonResponse({"status": "ok"})


@login_required
@require_POST
def delete_detection(request: HttpRequest, pk: int) -> JsonResponse:
    """Deactivate a detection in the database and sync the JSON file.

    :param request: The HTTP request (JSON body with ``detection_id``
        (int, DB pk)).
    :param pk: Scan primary key.
    :return: JSON response with ``deleted`` count, or 404 if not found.
    """
    from scanning.services import (
        _sync_detections_to_disk,
        _sync_redaction_rects_from_detections,
    )

    scan = get_object_or_404(Scan, pk=pk)
    data = _parse_json_body(request)
    if isinstance(data, JsonResponse):
        return data
    detection_id = data["detection_id"]
    count = Detection.objects.filter(pk=detection_id, scan=scan).update(
        active=False
    )
    if count == 0:
        return JsonResponse(
            {"status": "error", "message": "Detection not found"}, status=404
        )
    output_dir = Path(scan.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    _sync_detections_to_disk(scan.pk)
    _sync_redaction_rects_from_detections(scan.pk)
    return JsonResponse({"status": "ok", "deleted": count})


@login_required
@require_POST
def update_detection(request: HttpRequest, pk: int) -> JsonResponse:
    """Update the bounding box of an existing detection.

    Looks up the detection by its DB primary key, updates the bbox
    columns, then rebuilds ``detections.json`` from the DB via
    ``_sync_detections_to_disk`` so the file and S3 stay in sync.

    :param request: The HTTP request (JSON body with ``detection_id``
        (int, DB pk) and ``new_bbox`` (list[float], ``[x0,y0,x1,y1]``)).
    :param pk: Scan primary key.
    :return: JSON response with ``updated`` count, or 404 if not found.
    """
    from scanning.services import (
        _sync_detections_to_disk,
        _sync_redaction_rects_from_detections,
    )

    scan = get_object_or_404(Scan, pk=pk)
    data = _parse_json_body(request)
    if isinstance(data, JsonResponse):
        return data
    detection_id = data["detection_id"]
    new_bbox = data["new_bbox"]
    count = Detection.objects.filter(pk=detection_id, scan=scan).update(
        x0=new_bbox[0],
        y0=new_bbox[1],
        x1=new_bbox[2],
        y1=new_bbox[3],
    )
    if count == 0:
        return JsonResponse(
            {"status": "error", "message": "Detection not found"}, status=404
        )
    output_dir = Path(scan.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    _sync_detections_to_disk(scan.pk)
    _sync_redaction_rects_from_detections(scan.pk)
    return JsonResponse({"status": "ok", "updated": count})


@login_required
@require_POST
def add_single_detection(request: HttpRequest, pk: int) -> JsonResponse:
    """Add a new detection or boost an existing one.

    If a detection with the same label and approximate position already
    exists, its confidence is "boosted" to 1.0 (confirmed by the user).
    Otherwise a new detection is created with confidence 1.0 and
    model_name "manual".

    Updates both the Detection DB record and detections.json on disk.

    :param request: The HTTP request (JSON body with page_index,
        label_id, bbox, img_width, and img_height).
    :param pk: Scan primary key.
    :return: JSON response with ``added=True`` if new, ``added=False``
        if an existing detection was boosted.
    """
    from scanning.services import (
        _sync_detections_to_disk,
        _sync_redaction_rects_from_detections,
    )

    scan = get_object_or_404(Scan, pk=pk)
    det = _parse_json_body(request)
    if isinstance(det, JsonResponse):
        return det
    output_dir = Path(scan.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Match against the DB (source of truth). The previous implementation
    # iterated detections.json, which could carry a stale entry for a
    # soft-deleted row and trick the boost branch into silently dropping
    # the user's new draw.
    match = (
        Detection.objects.filter(
            scan=scan,
            active=True,
            page_index=det["page_index"],
            label_id=det["label_id"],
            x0__gte=det["bbox"][0] - 15,
            x0__lte=det["bbox"][0] + 15,
            y0__gte=det["bbox"][1] - 15,
            y0__lte=det["bbox"][1] + 15,
        )
        .order_by("pk")
        .first()
    )
    if match is not None:
        match.confidence = 1.0
        match.save(update_fields=["confidence"])
    else:
        from blackletter.models import Label

        try:
            label_name = Label(det["label_id"]).name
        except ValueError:
            return JsonResponse(
                {"status": "error", "message": "Unknown label_id"},
                status=400,
            )
        Detection.objects.create(
            scan=scan,
            page_index=det["page_index"],
            label=label_name,
            label_id=det["label_id"],
            confidence=1.0,
            x0=det["bbox"][0],
            y0=det["bbox"][1],
            x1=det["bbox"][2],
            y1=det["bbox"][3],
            img_width=det.get("img_width", 0),
            img_height=det.get("img_height", 0),
            model_name=Detection.ModelName.MANUAL,
            model_count=1,
            found_by=[{"model": "manual", "confidence": 1.0}],
        )

    _sync_detections_to_disk(scan.pk)
    _sync_redaction_rects_from_detections(scan.pk)
    return JsonResponse({"status": "ok", "added": match is None})


@login_required
@require_POST
def approve_detection(request: HttpRequest, pk: int) -> JsonResponse:
    """Set a detection's confidence to 1.0 in the DB and sync the JSON file.

    :param request: The HTTP request (JSON body with ``detection_id``
        (int, DB pk)).
    :param pk: Scan primary key.
    :return: JSON response with ``updated`` count, or 404 if not found.
    """
    from scanning.services import (
        _sync_detections_to_disk,
        _sync_redaction_rects_from_detections,
    )

    scan = get_object_or_404(Scan, pk=pk)
    data = _parse_json_body(request)
    if isinstance(data, JsonResponse):
        return data
    detection_id = data.get("detection_id")
    if detection_id is None:
        return JsonResponse(
            {"status": "error", "message": "detection_id required"},
            status=400,
        )
    count = Detection.objects.filter(pk=detection_id, scan=scan).update(
        confidence=1.0
    )
    if count == 0:
        return JsonResponse(
            {"status": "error", "message": "Detection not found"}, status=404
        )
    output_dir = Path(scan.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    _sync_detections_to_disk(scan.pk)
    _sync_redaction_rects_from_detections(scan.pk)
    return JsonResponse({"status": "ok", "updated": count})


@login_required
@require_POST
def bake_redactions(request: HttpRequest, pk: int) -> JsonResponse:
    """Bake pending redaction rectangles into the scan PDF (no-op stub).

    :param request: The HTTP request.
    :param pk: Scan primary key.
    :return: JSON response with the bake result.
    """
    scan = get_object_or_404(Scan, pk=pk)
    if not Path(scan.output_dir).is_dir():
        return JsonResponse(
            {"status": "error", "message": "No output dir"}, status=400
        )
    return JsonResponse(
        {"status": "ok", "message": "No redactions to bake", "count": 0}
    )


@login_required
def export_pdf(request: HttpRequest, pk: int) -> StreamingHttpResponse:
    """Export a corrected PDF with deletions and inserts applied.

    :param request: The HTTP request.
    :param pk: Scan primary key.
    :return: PDF file download response.
    """
    scan = get_object_or_404(Scan, pk=pk)
    tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
    tmp.close()
    tmp_path = tmp.name
    try:
        with fitz.open(scan.pdf_path) as pdf_doc:
            page_map = scan.page_map
            deleted_pages = set(d.pdf_page for d in scan.deletions.all())
            for pdf_page in sorted(deleted_pages, reverse=True):
                pdf_index = pdf_page - 1
                if 0 <= pdf_index < len(pdf_doc):
                    pdf_doc.delete_page(pdf_index)
            inserts = {
                ins.logical_page_number: ins for ins in scan.inserts.all()
            }
            insert_ops = []
            for i, entry in enumerate(page_map):
                if (
                    entry["type"] == "missing"
                    and entry["logical_number"] in inserts
                ):
                    insert_before = None
                    for j in range(i + 1, len(page_map)):
                        if page_map[j]["type"] == "pdf_page":
                            insert_before = page_map[j]["pdf_index"]
                            break
                    insert_ops.append(
                        (insert_before, inserts[entry["logical_number"]])
                    )
            offset = 0
            for insert_before, insert_obj in insert_ops:
                img_path = insert_obj.image.path
                if insert_before is not None:
                    adjusted = insert_before - len(
                        [d for d in deleted_pages if d <= insert_before]
                    )
                    pno = adjusted + offset
                    ref_page = pdf_doc.load_page(min(pno, len(pdf_doc) - 1))
                else:
                    pno = len(pdf_doc)
                    ref_page = pdf_doc.load_page(len(pdf_doc) - 1)
                w, h = ref_page.rect.width, ref_page.rect.height
                if img_path.lower().endswith(".pdf"):
                    with fitz.open(img_path) as insert_pdf:
                        pdf_doc.insert_pdf(
                            insert_pdf,
                            from_page=0,
                            to_page=insert_pdf.page_count - 1,
                            start_at=pno,
                        )
                        offset += insert_pdf.page_count - 1
                else:
                    new_page = pdf_doc.new_page(pno=pno, width=w, height=h)
                    new_page.insert_image(new_page.rect, filename=img_path)
                offset += 1
            pdf_doc.save(tmp_path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise

    filename = f"{scan.reporter.short_name}_{scan.volume}_corrected.pdf"

    def _stream_and_cleanup(path: str, chunk_size: int = 64 * 1024):
        try:
            with open(path, "rb") as fh:
                while True:
                    chunk = fh.read(chunk_size)
                    if not chunk:
                        break
                    yield chunk
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass

    response = StreamingHttpResponse(
        _stream_and_cleanup(tmp_path), content_type="application/pdf"
    )
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response
