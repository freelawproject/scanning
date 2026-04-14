"""JSON API endpoints and file-serving views for the process page."""

import json
import logging
import os
import shutil
import tempfile
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
from django.shortcuts import get_object_or_404, redirect
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
from scanning.utils import find_json_file, find_ocr_pdf

logger = logging.getLogger(__name__)


@login_required
def serve_detections(request, pk):
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
def serve_opinions(request, pk):
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
def serve_margin_rects(request, pk):
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
def serve_redaction_rects(request, pk):
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
def save_redaction_rect(request, pk):
    """Create, update, or delete a redaction rectangle on disk.

    :param request: The HTTP request (JSON body with page_index,
        action, original, adjusted, type, and fill).
    :param pk: Scan primary key.
    :return: JSON response confirming the operation.
    """
    scan = get_object_or_404(Scan, pk=pk)
    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON"}, status=400)
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
                r["x0"] = round(adjusted["x0"], 1)
                r["y0"] = round(adjusted["y0"], 1)
                r["x1"] = round(adjusted["x1"], 1)
                r["y1"] = round(adjusted["y1"], 1)
                found = True
                break
        if found:
            break
    if not found:
        for page_data in rects:
            if page_data["page_index"] == page_idx:
                page_data["rects"].append(
                    {
                        "x0": round(adjusted["x0"], 1),
                        "y0": round(adjusted["y0"], 1),
                        "x1": round(adjusted["x1"], 1),
                        "y1": round(adjusted["y1"], 1),
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
def save_margin_rect(request, pk):
    """Update or delete a margin rectangle on disk.

    :param request: The HTTP request (JSON body with page_index,
        action, original, and adjusted).
    :param pk: Scan primary key.
    :return: JSON response confirming the operation.
    """
    scan = get_object_or_404(Scan, pk=pk)
    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON"}, status=400)
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
                r["x0"] = round(adjusted["x0"], 1)
                r["y0"] = round(adjusted["y0"], 1)
                r["x1"] = round(adjusted["x1"], 1)
                r["y1"] = round(adjusted["y1"], 1)
                found = True
                break
        if found:
            break
    Scan.objects.filter(pk=pk).update(margin_rects=rects)
    return JsonResponse({"status": "ok", "found": found})


@login_required
@require_POST
def pair_opinions_api(request, pk):
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
    pdf_path = None
    bitonal = output_dir / "bitonal.pdf"
    if bitonal.exists():
        pdf_path = str(bitonal)
    else:
        pdf_path = scan.pdf_path
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
        gaps = []
        if opinions and scan.start_page and scan.end_page:
            covered = set()
            for op in opinions:
                cp = op.get("caption_page", 0) + (scan.start_page or 1)
                kp = (
                    op.get("key_page", 0)
                    + (scan.start_page or 1)
                    + op.get("page_count", 1)
                    - 1
                )
                for p in range(cp, kp + 1):
                    covered.add(p)
            expected = set(range(scan.start_page, scan.end_page + 1))
            missing = sorted(expected - covered)
            if missing:
                import itertools

                for _, g in itertools.groupby(
                    enumerate(missing), lambda x: x[0] - x[1]
                ):
                    run = [v for _, v in g]
                    gaps.append(
                        {"start": run[0], "end": run[-1], "count": len(run)}
                    )
        return JsonResponse(
            {"status": "ok", "opinions": opinions, "gaps": gaps}
        )
    except Exception:
        import traceback

        traceback.print_exc()
        return JsonResponse({"error": "Opinion pairing failed"}, status=500)


@login_required
@require_POST
def compute_redactions_api(request, pk):
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
        rects = _compute_and_save_redaction_rects(pk, pdf_path, output_dir)
        _compute_and_save_margin_rects(pk, pdf_path, output_dir)
        Scan.objects.filter(pk=pk).update(s3_uploaded=False)
        total_rects = sum(len(r["rects"]) for r in rects)
        return JsonResponse(
            {"status": "ok", "pages": len(rects), "rects": total_rects}
        )
    except Exception:
        import traceback

        traceback.print_exc()
        return JsonResponse(
            {"error": "Redaction computation failed"}, status=500
        )


@login_required
@require_POST
def generate_files(request, pk):
    """Queue a scan for opinion file generation (step 4).

    :param request: The HTTP request.
    :param pk: Scan primary key.
    :return: Redirect to the scan processing page (step 4).
    """
    scan = get_object_or_404(Scan, pk=pk)
    if scan.status in (Status.PROCESSING, Status.QUEUED):
        return redirect(f"/scans/{scan.pk}/process/?step=3")
    scan.stage = Stage.PROCESS
    scan.status = Status.QUEUED
    scan.queued_action = QueuedAction.GENERATE_FILES
    scan.s3_uploaded = False
    scan.progress_message = "Queued for file generation..."
    scan.save()
    return redirect(f"/scans/{scan.pk}/process/?step=3")


@login_required
@require_POST
def approve_scan(request, pk):
    """Approve a scan and upload final files to S3.

    Validates that generated files exist before approving. Uploads
    redacted, masked, original, and redacted PDFs to the private S3
    bucket (in prod) or logs a dev message (in dev).

    :param request: The HTTP request.
    :param pk: Scan primary key.
    :return: Redirect to the process page.
    """
    from scanning.services import upload_approved_files

    scan = get_object_or_404(Scan, pk=pk)

    result = upload_approved_files(scan.pk)
    if result.startswith("Before approving"):
        messages.error(request, result)
        return redirect("scan_process", pk=scan.pk)

    scan.stage = Stage.APPROVED
    scan.save(update_fields=["stage"])
    messages.success(request, result)
    return redirect("scan_process", pk=scan.pk)


@login_required
def serve_opinionscan_pdf(request, pk, variant):
    """Serve a PDF for an OpinionScan by variant (redacted/original/masked).

    The file path comes from the model field, not from user input.

    :param request: The HTTP request.
    :param pk: OpinionScan primary key.
    :param variant: One of 'redacted', 'original', or 'masked'.
    :return: File response streaming the PDF.
    """
    opinion = get_object_or_404(OpinionScan, pk=pk)
    field_map = {
        "redacted": opinion.redacted_pdf,
        "original": opinion.original_pdf,
        "masked": opinion.masked_pdf,
    }
    field = field_map.get(variant)
    if not field or not field.name:
        raise Http404
    return FileResponse(field.open("rb"), content_type="application/pdf")


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
    doc = fitz.open(pdf_path)
    if page_index < 0 or page_index >= doc.page_count:
        doc.close()
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
    doc.close()
    shutil.move(tmp_path, pdf_path)


@login_required
@require_POST
def apply_rect_to_opinion(request, pk, opinion_pk):
    """Apply a redaction rectangle to an opinion's redacted and masked PDFs.

    :param request: The HTTP request (JSON body with page_index,
        x0, y0, x1, y1, and fill).
    :param pk: Scan primary key.
    :param opinion_pk: OpinionScan primary key.
    :return: JSON response confirming the operation.
    """
    scan = get_object_or_404(Scan, pk=pk)
    opinion = get_object_or_404(OpinionScan, pk=opinion_pk, scan=scan)
    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON"}, status=400)
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

    # Also apply to masked PDF if it exists
    if opinion.masked_pdf and opinion.masked_pdf.name:
        masked_path = os.path.join(
            scan.output_dir,
            "masked",
            os.path.basename(opinion.masked_pdf.name),
        )
        if os.path.isfile(masked_path):
            try:
                _apply_rect_to_pdf(
                    masked_path, page_index, x0, y0, x1, y1, fill
                )
            except ValueError:
                pass  # masked PDF may have different page count

    return JsonResponse({"status": "ok"})


@login_required
def serve_redacted_pdf(request, pk):
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

    return HttpResponse("No PDF available", status=404)


@login_required
def serve_ocr_results(request, pk):
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
def flag_issue(request, pk):
    """Create a user-flagged issue on a scan.

    :param request: The HTTP request (JSON body with message,
        page_number, and metadata).
    :param pk: Scan primary key.
    :return: JSON response with the new issue ID.
    """
    scan = get_object_or_404(Scan, pk=pk)
    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON"}, status=400)
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
        metadata=json.dumps(metadata) if metadata else "",
    )
    return JsonResponse({"status": "ok", "id": issue.pk})


@login_required
@require_POST
def remove_flag(request, pk, flag_id):
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
def delete_detection(request, pk):
    """Deactivate a detection in the database and remove it from disk.

    :param request: The HTTP request (JSON body with page_index,
        label, label_id, and bbox).
    :param pk: Scan primary key.
    :return: JSON response with the count of deactivated detections.
    """
    scan = get_object_or_404(Scan, pk=pk)
    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON"}, status=400)
    page_index = data["page_index"]
    label = data.get("label", "")
    label_id = data.get("label_id")
    bbox = data["bbox"]
    # Deactivate matching Detection(s) in DB
    db_filter = dict(
        scan=scan,
        page_index=page_index,
        x0__gte=bbox[0] - 15,
        x0__lte=bbox[0] + 15,
        y0__gte=bbox[1] - 15,
        y0__lte=bbox[1] + 15,
    )
    if label:
        db_filter["label"] = label
    elif label_id is not None:
        db_filter["label_id"] = label_id
    qs = Detection.objects.filter(**db_filter)
    count = qs.update(active=False)
    # Remove from detections.json on disk
    output_base = Path(scan.output_dir)
    det_path = find_json_file(output_base, "detections.json")
    if det_path:
        existing = json.loads(det_path.read_text())
        existing = [
            e
            for e in existing
            if not (
                e["page_index"] == page_index
                and (e.get("label") == label or e.get("label_id") == label_id)
                and abs(e["bbox"][0] - bbox[0]) < 15
                and abs(e["bbox"][1] - bbox[1]) < 15
            )
        ]
        det_path.write_text(json.dumps(existing))
    return JsonResponse({"status": "ok", "deleted": count})


@login_required
@require_POST
def add_single_detection(request, pk):
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
    scan = get_object_or_404(Scan, pk=pk)
    try:
        det = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON"}, status=400)
    output_base = Path(scan.output_dir)
    det_path = find_json_file(output_base, "detections.json")
    if not det_path:
        return JsonResponse({"error": "No detections.json"}, status=404)
    existing = json.loads(det_path.read_text())
    boosted = False
    for e in existing:
        if e["page_index"] != det["page_index"]:
            continue
        if e["label_id"] != det["label_id"]:
            continue
        if (
            abs(e["bbox"][0] - det["bbox"][0]) < 15
            and abs(e["bbox"][1] - det["bbox"][1]) < 15
        ):
            e["confidence"] = 1.0
            boosted = True
            Detection.objects.filter(
                scan=scan,
                page_index=det["page_index"],
                label_id=det["label_id"],
                x0__gte=det["bbox"][0] - 15,
                x0__lte=det["bbox"][0] + 15,
                y0__gte=det["bbox"][1] - 15,
                y0__lte=det["bbox"][1] + 15,
            ).update(confidence=1.0)
            break
    if not boosted:
        det["confidence"] = 1.0
        existing.append(det)
        from blackletter.models import Label

        try:
            label_name = Label(det["label_id"]).name
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
        except Exception:
            logger.exception("Failed to create manual detection")
    det_path.write_text(json.dumps(existing))
    return JsonResponse({"status": "ok", "added": not boosted})


@login_required
@require_POST
def approve_detection(request: HttpRequest, pk: int) -> JsonResponse:
    """Set a detection's confidence to 1.0 in the DB and on disk.

    :param request: The HTTP request (JSON body with page_index,
        label, label_id, and bbox).
    :param pk: Scan primary key.
    :return: JSON response with the count of updated detections.
    """
    scan = get_object_or_404(Scan, pk=pk)
    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON"}, status=400)
    page_index = data["page_index"]
    label = data.get("label", "")
    label_id = data.get("label_id")
    bbox = data["bbox"]

    # Update matching Detection(s) in DB
    db_filter = dict(
        scan=scan,
        page_index=page_index,
        x0__gte=bbox[0] - 15,
        x0__lte=bbox[0] + 15,
        y0__gte=bbox[1] - 15,
        y0__lte=bbox[1] + 15,
    )
    if label:
        db_filter["label"] = label
    elif label_id is not None:
        db_filter["label_id"] = label_id
    count = Detection.objects.filter(**db_filter).update(confidence=1.0)

    # Update confidence in detections.json on disk
    output_base = Path(scan.output_dir)
    det_path = find_json_file(output_base, "detections.json")
    if det_path:
        existing = json.loads(det_path.read_text())
        for e in existing:
            if e["page_index"] != page_index:
                continue
            if label and e.get("label") != label:
                continue
            if (
                not label
                and label_id is not None
                and e.get("label_id") != label_id
            ):
                continue
            if (
                abs(e["bbox"][0] - bbox[0]) < 15
                and abs(e["bbox"][1] - bbox[1]) < 15
            ):
                e["confidence"] = 1.0
        det_path.write_text(json.dumps(existing))

    return JsonResponse({"status": "ok", "updated": count})


@login_required
@require_POST
def bake_redactions(request, pk):
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
def export_pdf(request, pk):
    """Export a corrected PDF with deletions and inserts applied.

    :param request: The HTTP request.
    :param pk: Scan primary key.
    :return: PDF file download response.
    """
    scan = get_object_or_404(Scan, pk=pk)
    pdf_doc = fitz.open(scan.pdf_path)
    page_map = scan.page_map
    deleted_pages = set(d.pdf_page for d in scan.deletions.all())
    for pdf_page in sorted(deleted_pages, reverse=True):
        pdf_index = pdf_page - 1
        if 0 <= pdf_index < len(pdf_doc):
            pdf_doc.delete_page(pdf_index)
    inserts = {ins.logical_page_number: ins for ins in scan.inserts.all()}
    insert_ops = []
    for i, entry in enumerate(page_map):
        if entry["type"] == "missing" and entry["logical_number"] in inserts:
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
            insert_pdf = fitz.open(img_path)
            pdf_doc.insert_pdf(
                insert_pdf,
                from_page=0,
                to_page=insert_pdf.page_count - 1,
                start_at=pno,
            )
            offset += insert_pdf.page_count - 1
            insert_pdf.close()
        else:
            new_page = pdf_doc.new_page(pno=pno, width=w, height=h)
            new_page.insert_image(new_page.rect, filename=img_path)
        offset += 1
    pdf_bytes = pdf_doc.tobytes()
    pdf_doc.close()
    filename = f"{scan.reporter.short_name}_{scan.volume}_corrected.pdf"
    response = HttpResponse(pdf_bytes, content_type="application/pdf")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response
