"""JSON API endpoints and file-serving views for the process page."""

import json
import logging
import os
import shutil
import tempfile
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
from django.views.decorators.http import require_POST

from scanning.models import (
    CheckName,
    Detection,
    DetectionDecision,
    Issue,
    OpinionScan,
    Scan,
    Stage,
    Status,
)
from scanning.utils import (
    PIPELINE_PAUSED_MESSAGE,
    find_processing_pdf,
    local_original_pdf,
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
    dets = (
        Detection.objects.live()
        .filter(scan=scan)
        .select_related("decision")
        .order_by("page_index", "y0")
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
            # The viewer draws a hand-added detection dashed, and shows
            # it whatever its label. It sets this flag itself when the
            # reviewer draws the box, so until it came from here a
            # hand-added box lost both on the next page load (PR #167).
            "manual": d.model_name == Detection.ModelName.MANUAL,
            # The standing curator decision on a model row (#240):
            # "approve" here is why the confidence reads 1.0.
            "decision": d.decision.kind if d.decision_id else None,
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
    # Shared with the pipeline rather than reimplemented: computing these
    # here with its own detection lookup is how a viewer request that
    # arrived before this machine had detections.json cached margins with
    # no top strips, permanently. The PDF is the one the geometry reads
    # (#269): the corrected volume's copy when the rows are measured
    # against the standing apply run, the review-1 copy otherwise.
    from scanning import apply, review_states, yolo
    from scanning.services import (
        _compute_and_save_margin_rects,
        geometry_pdf_path,
    )

    run = review_states.final_run(scan)
    if run is not None and yolo.redactions_current(
        yolo.live_detect_jobs(scan), run
    ):
        try:
            base_pdf = geometry_pdf_path(scan, run)
        except apply.ApplyError:
            logger.exception(
                "serve_margin_rects: the corrected volume of scan %s did "
                "not load",
                scan.pk,
            )
            return JsonResponse([], safe=False)
        output_base.mkdir(parents=True, exist_ok=True)
    else:
        base_pdf = (
            find_processing_pdf(output_base) if output_base.is_dir() else None
        )
    if not base_pdf:
        return JsonResponse([], safe=False)

    rects = _compute_and_save_margin_rects(pk, str(base_pdf), str(output_base))
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


#: Whether a curator may ask for the redaction computation from review
#: 2. Off for now (#196): the computation renders every page of the
#: volume and takes the scan out of review for a minute or more, and
#: the one run the daemon starts after a detection run is the only one
#: wanted until the stage has been watched on a few volumes (#211).
#: Turning it back on is this flag plus the "Re-pair Opinions" button
#: in ``_process_actions.html``; the queueing code below is kept.
REPAIR_ON_REQUEST_ENABLED = False

REPAIR_DISABLED_MESSAGE = (
    "Re-pairing on request is off for now. The redactions are computed "
    "once, when the detection run finishes."
)

#: The gate of step 3 in the view (#263/#269): a volume of the new
#: pipeline reaches the file generation through the review-2 approval.
GENERATE_REQUIRES_REDACTION_REVIEW_MESSAGE = (
    "The redaction review of this volume is not approved yet. Approve "
    "it in step 2 before the files are generated."
)


@login_required
@require_POST
def pair_opinions_api(request: HttpRequest, pk: int) -> JsonResponse:
    """Ask the daemon to pair the opinions again, with the geometry.

    A curator presses this after they add or delete a detection, and
    what they want is every consequence of that edit: the pairing, the
    redaction rects and the margin strips, which are all measured from
    the same detections. One queued action computes all three (#196),
    so none of them can be left describing the boxes of an hour ago.

    It runs on the daemon rather than here, because the measurement
    renders every page of the volume: 83 seconds for 1364 pages. The
    viewer reloads, sees the scan busy, and its progress poll reloads
    again when the daemon parks it.

    :param request: The HTTP request.
    :param pk: Scan primary key.
    :return: JSON response saying the work is queued, or 409 while
        re-pairing on request is off (``REPAIR_ON_REQUEST_ENABLED``).
    """
    scan = get_object_or_404(Scan, pk=pk)
    if not REPAIR_ON_REQUEST_ENABLED:
        return JsonResponse({"error": REPAIR_DISABLED_MESSAGE}, status=409)
    if not Detection.objects.filter(scan=scan, active=True).exists():
        return JsonResponse({"error": "No detections found"}, status=400)

    from scanning.services import queue_redaction_compute

    queued, message = queue_redaction_compute(scan)
    if not queued:
        return JsonResponse({"error": message}, status=409)
    return JsonResponse({"status": "queued", "message": message}, status=202)


@login_required
@require_POST
def compute_redactions_api(request: HttpRequest, pk: int) -> JsonResponse:
    """Ask the daemon to compute this scan's redaction geometry.

    The same queued action as :func:`pair_opinions_api`, and for the
    same reason: the measurement renders every page of the volume, so
    it cannot run inside a request (#196).

    :param request: The HTTP request.
    :param pk: Scan primary key.
    :return: JSON response saying the work is queued, or 409 while
        re-pairing on request is off (``REPAIR_ON_REQUEST_ENABLED``).
    """
    scan = get_object_or_404(Scan, pk=pk)
    if not REPAIR_ON_REQUEST_ENABLED:
        return JsonResponse({"error": REPAIR_DISABLED_MESSAGE}, status=409)
    if not Detection.objects.filter(scan=scan, active=True).exists():
        return JsonResponse({"error": "No detections found"}, status=400)

    from scanning.services import queue_redaction_compute

    queued, message = queue_redaction_compute(scan)
    if not queued:
        return JsonResponse({"error": message}, status=409)
    return JsonResponse({"status": "queued", "message": message}, status=202)


@login_required
@require_POST
def generate_files(request: HttpRequest, pk: int) -> HttpResponse:
    """Refuse to generate opinion files while the pipeline is paused.

    File generation is post-review-1 processing, which issue #173
    stops until the new OCR stack reaches that stage. The generation
    code (``services.run_generate_files``) is kept, but nothing queues
    it; this view fails with the unified pipeline-paused message.

    The review-2 approval is the gate of step 3 (#263), and it is
    checked here first (#269), before the paused flash: a template gate
    alone cannot refuse a direct POST, the rule ``start_detect`` follows
    for review 1. A legacy volume (``PENDING_REVIEW``) never holds the
    approval and keeps its way in. The order holds the day #206
    connects the generation.

    :param request: The HTTP request.
    :param pk: Scan primary key.
    :return: Redirect to the scan processing page.
    """
    scan = get_object_or_404(Scan, pk=pk)
    if scan.status not in (
        Status.REDACTION_REVIEW_DONE,
        Status.PENDING_REVIEW,
    ):
        messages.warning(request, GENERATE_REQUIRES_REDACTION_REVIEW_MESSAGE)
        return redirect(
            f"{reverse('scan_process', kwargs={'pk': scan.pk})}?step=2"
        )
    messages.warning(request, PIPELINE_PAUSED_MESSAGE)
    return redirect("scan_process", pk=scan.pk)


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

                # Just this file: see s3_sync.download_processing_file for
                # why pulling the whole prefix here hangs the process.
                s3_sync.download_processing_file(opinion.scan, field.name)
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

    Falls back to the processing PDF if the redacted version hasn't been
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

    # 2. Fall back to the processing PDF (for preview before generation)
    output = Path(scan.output_dir)
    if output.is_dir():
        base_pdf = find_processing_pdf(output)
        if base_pdf:
            return FileResponse(
                base_pdf.open("rb"), content_type="application/pdf"
            )

    # 3. Lazy S3 pull + retry: handles the case where the daemon just
    # finished Generate Files and this container's /tmp/ is stale. Pull only
    # the file being served -- the whole prefix runs to gigabytes on a full
    # volume, and every sync view shares one executor under ASGI.
    try:
        from scanning import s3_sync

        if scan.redacted_pdf_path:
            s3_sync.download_processing_file(
                scan, os.path.basename(scan.redacted_pdf_path)
            )
        else:
            s3_sync.download_preview_pdf(scan)
    except Exception:
        logger.exception("Lazy S3 pull failed for scan %s", scan.pk)
    if scan.redacted_pdf_path and os.path.isfile(scan.redacted_pdf_path):
        return FileResponse(
            Path(scan.redacted_pdf_path).open("rb"),
            content_type="application/pdf",
        )
    if output.is_dir():
        base_pdf = find_processing_pdf(output)
        if base_pdf:
            return FileResponse(
                base_pdf.open("rb"), content_type="application/pdf"
            )

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
    """Take a detection out of the volume.

    A model row gets a ``deactivate`` decision (#240): the row is
    deleted and written again at the next import, and the decision is
    what carries the curator's choice onto the new row. A hand-drawn
    row is withdrawn, and gives back the model box it replaced, if any.
    Nothing is deleted.

    :param request: The HTTP request (JSON body with ``detection_id``
        (int, DB pk)).
    :param pk: Scan primary key.
    :return: JSON response with ``deleted`` count, or 404 if not found.
    """
    from scanning import detections

    scan = get_object_or_404(Scan, pk=pk)
    data = _parse_json_body(request)
    if isinstance(data, JsonResponse):
        return data
    row = Detection.objects.filter(pk=data["detection_id"], scan=scan).first()
    if row is None:
        return JsonResponse(
            {"status": "error", "message": "Detection not found"}, status=404
        )
    if row.model_name == Detection.ModelName.MANUAL:
        detections.withdraw_manual(row, request.user)
    else:
        try:
            detections.decide(
                scan, row, DetectionDecision.Kind.DEACTIVATE, request.user
            )
        except detections.UnaddressableDetection:
            return _unaddressable()
    _drop_orphaned_redaction_rects(scan, row.page_index)
    return JsonResponse({"status": "ok", "deleted": 1})


def _drop_orphaned_redaction_rects(scan: Scan, page_index: int | None) -> None:
    """Forget a page's saved rects once its last detection is gone.

    ``redaction_rects`` is a snapshot in image pixels, and the scale that
    converts it to points comes from the page's own detections. Deleting the
    last one on a page leaves rects that cannot be placed, which stops
    Generate Files outright, and nothing in the review UI recomputes them.

    Dropping them loses nothing: a page with no detections left has nothing
    on it to redact, and the reviewer saying so is what deleting the last
    detection means. Rects on every other page, including any the reviewer
    adjusted by hand, are untouched.

    :param scan: The scan whose rects to prune.
    :param page_index: Page the deleted detection was on, if known.
    """
    if page_index is None or not scan.redaction_rects:
        return
    if Detection.objects.filter(
        scan=scan, page_index=page_index, active=True
    ).exists():
        return
    remaining = [
        entry
        for entry in scan.redaction_rects
        if entry.get("page_index") != page_index
    ]
    if len(remaining) != len(scan.redaction_rects):
        logger.info(
            "Dropped saved redaction rects for scan %s page %s: "
            "no active detections left on it",
            scan.pk,
            page_index,
        )
        Scan.objects.filter(pk=scan.pk).update(redaction_rects=remaining)


@login_required
@require_POST
def update_detection(request: HttpRequest, pk: int) -> JsonResponse:
    """Move or resize a detection box.

    A hand-drawn row is the curator's own and is written in place. A
    model row is not written (#240): it is deactivated by a decision
    and a hand-drawn row is created where the curator put the box, so
    the move survives the next import. The response names the row that
    now holds the box, and the viewer must address that one from then
    on.

    :param request: The HTTP request (JSON body with ``detection_id``
        (int, DB pk) and ``new_bbox`` (list[float], ``[x0,y0,x1,y1]``)).
    :param pk: Scan primary key.
    :return: JSON response with ``updated`` count and ``detection_id``,
        or 404 if not found.
    """
    from scanning import detections

    scan = get_object_or_404(Scan, pk=pk)
    data = _parse_json_body(request)
    if isinstance(data, JsonResponse):
        return data
    new_bbox = data["new_bbox"]
    row = Detection.objects.filter(pk=data["detection_id"], scan=scan).first()
    if row is None:
        return JsonResponse(
            {"status": "error", "message": "Detection not found"}, status=404
        )
    if row.model_name == Detection.ModelName.MANUAL:
        Detection.objects.filter(pk=row.pk).update(
            x0=new_bbox[0], y0=new_bbox[1], x1=new_bbox[2], y1=new_bbox[3]
        )
        holder = row
    else:
        try:
            holder = detections.move_model_row(
                scan, row, new_bbox, request.user
            )
        except detections.UnaddressableDetection:
            return _unaddressable()
    return JsonResponse(
        {"status": "ok", "updated": 1, "detection_id": holder.pk}
    )


#: The 409 of a decision about a box no address can be written for
#: (``detections.UnaddressableDetection``).
DETECTION_UNADDRESSABLE_MESSAGE = (
    "This box cannot be addressed in the current volume, so the change "
    "cannot be kept. Reload the page; if it stays, ask a staff member."
)


def _unaddressable() -> JsonResponse:
    """Return the 409 for a refused decision.

    :returns: The response.
    """
    return JsonResponse(
        {"status": "error", "message": DETECTION_UNADDRESSABLE_MESSAGE},
        status=409,
    )


#: How far, in image pixels, a drawn box may sit from a model box and
#: still mean "that one": the add endpoint then approves the model box
#: rather than draw a second one over it.
BOOST_TOLERANCE_PX = 15


@login_required
@require_POST
def add_single_detection(request: HttpRequest, pk: int) -> JsonResponse:
    """Add a detection by hand, or approve the model box it lands on.

    A box drawn within ``BOOST_TOLERANCE_PX`` of a live model box with
    the same label is that box, and the model box gets an ``approve``
    decision (#240). Otherwise a hand-drawn row is written, addressed
    by its source page. The rows are the only store; nothing here reads
    or writes a file.

    :param request: The HTTP request (JSON body with page_index,
        label_id, bbox, img_width, and img_height).
    :param pk: Scan primary key.
    :return: JSON response with ``added=True`` and the new row's
        ``detection_id`` if new, ``added=False`` and the approved row's
        id if an existing detection was approved.
    """
    from blackletter.models import Label

    from scanning import detections

    scan = get_object_or_404(Scan, pk=pk)
    det = _parse_json_body(request)
    if isinstance(det, JsonResponse):
        return det
    try:
        page_index = int(det["page_index"])
        label_id = int(det["label_id"])
        bbox = [float(v) for v in det["bbox"]]
        if len(bbox) != 4:
            raise ValueError("bbox needs four numbers")
        label_name = Label(label_id).name
    except (KeyError, TypeError, ValueError):
        # The detail goes to the log, not to the browser (CodeQL).
        logger.warning(
            "add_single_detection: scan %s: malformed body", pk, exc_info=True
        )
        return JsonResponse(
            {
                "error": "Bad detection: page_index, label_id and a bbox "
                "of four numbers are required"
            },
            status=400,
        )

    run = detections.measured_run(scan)
    # Any live row, hand-drawn ones included: a second click on the
    # curator's own box must be a no-op, not a second box over it.
    near = (
        Detection.objects.live()
        .filter(
            scan=scan,
            page_index=page_index,
            label_id=label_id,
            x0__gte=bbox[0] - BOOST_TOLERANCE_PX,
            x0__lte=bbox[0] + BOOST_TOLERANCE_PX,
            y0__gte=bbox[1] - BOOST_TOLERANCE_PX,
            y0__lte=bbox[1] + BOOST_TOLERANCE_PX,
        )
        .order_by("pk")
        .first()
    )
    if near is not None:
        if near.model_name != Detection.ModelName.MANUAL:
            try:
                detections.decide(
                    scan,
                    near,
                    DetectionDecision.Kind.APPROVE,
                    request.user,
                    run=run,
                )
            except detections.UnaddressableDetection:
                return _unaddressable()
        return JsonResponse(
            {"status": "ok", "added": False, "detection_id": near.pk}
        )
    row = detections.add_manual(
        scan,
        page_index,
        label_name,
        label_id,
        bbox,
        int(det.get("img_width") or 0),
        int(det.get("img_height") or 0),
        run=run,
    )
    return JsonResponse(
        {"status": "ok", "added": True, "detection_id": row.pk}
    )


@login_required
@require_POST
def approve_detection(request: HttpRequest, pk: int) -> JsonResponse:
    """Approve a detection: its confidence reads 1.0 from now on.

    A model row gets an ``approve`` decision (#240), which the next
    import lands on the same box again. A hand-drawn row is the
    curator's already and needs none.

    :param request: The HTTP request (JSON body with ``detection_id``
        (int, DB pk)).
    :param pk: Scan primary key.
    :return: JSON response with ``updated`` count, or 404 if not found.
    """
    from scanning import detections

    scan = get_object_or_404(Scan, pk=pk)
    data = _parse_json_body(request)
    if isinstance(data, JsonResponse):
        return data
    row = Detection.objects.filter(pk=data["detection_id"], scan=scan).first()
    if row is None:
        return JsonResponse(
            {"status": "error", "message": "Detection not found"}, status=404
        )
    if row.model_name != Detection.ModelName.MANUAL:
        try:
            detections.decide(
                scan, row, DetectionDecision.Kind.APPROVE, request.user
            )
        except detections.UnaddressableDetection:
            return _unaddressable()
    return JsonResponse({"status": "ok", "updated": 1})


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


#: The 409 of ``export_pdf``: the standing page edits cannot be built
#: into a volume. The fault itself is logged, never sent.
EXPORT_NOT_BUILDABLE_MESSAGE = (
    "The corrected PDF cannot be built from the page edits as they "
    "stand. Check the step-1 page changes, or ask a staff member."
)


def _unlink_quietly(path: str) -> None:
    """Remove a temp file, and swallow a file that is already gone.

    :param path: The file.
    :return: None.
    """
    try:
        os.unlink(path)
    except OSError:
        pass


@login_required
def export_pdf(
    request: HttpRequest, pk: int
) -> StreamingHttpResponse | HttpResponse:
    """Export the corrected PDF, as the apply builds it.

    Reads the curator's decisions off the ``PageEdit`` rows (#214), in
    the physical space of the original, and runs the walk the apply
    runs (``apply.build_final_pdf``, #224): a page marked for deletion
    is dropped, an uploaded page stands in for a replaced one, a
    rotated page is turned, and each inserted file is placed in the gap
    its row names. The export and the final PDF are one walk, so what
    a curator downloads is what the apply builds.

    :param request: The HTTP request.
    :param pk: Scan primary key.
    :return: PDF file download response, a 404 when the original PDF
        cannot be made available locally, or a 409 naming the fault when
        the rows cannot be built into a volume.
    """
    from scanning import apply

    scan = get_object_or_404(Scan, pk=pk)
    # Resolve the source PDF before the temp file exists, so a missing
    # original is a clean 404 rather than a leaked temp file.
    original = local_original_pdf(scan)
    if not original:
        return HttpResponse(status=404)
    tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
    tmp.close()
    tmp_path = tmp.name
    try:
        with fitz.open(original) as pdf_doc:
            # The plan is built over ``Scan.page_count``, so a row that
            # disagrees with the file would address a page the walk
            # cannot reach and raise inside ``build_final_pdf``. The
            # old walk clamped every index; this reads the file, which
            # is what ``apply._build`` checks too.
            scan.page_count = pdf_doc.page_count
        # Each uploaded file is read once, for the plan and the walk.
        files, counts = apply.preload_edit_files(scan)
        plan = apply.plan_run(scan, counts)
        with fitz.open(original) as source:
            with apply.build_final_pdf(
                source, plan, read_file=lambda edit: files[edit.pk]
            ) as pdf_doc:
                pdf_doc.save(tmp_path)
    except apply.ApplyError as exc:
        # The rows do not build into a volume: a shard with fewer pages
        # than the map asks for, a file that is gone. The apply counts
        # the same fault on its run. The detail goes to the log and not
        # to the browser (CodeQL: an exception's text is not for an
        # external user); the answer says what to do.
        _unlink_quietly(tmp_path)
        logger.warning(
            "export_pdf: scan %s: the page edits do not build: %s", pk, exc
        )
        return HttpResponse(
            EXPORT_NOT_BUILDABLE_MESSAGE, status=409, content_type="text/plain"
        )
    except Exception:
        _unlink_quietly(tmp_path)
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
