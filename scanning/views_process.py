"""Process viewer and scan processing action views."""

import json
import os
from pathlib import Path

import fitz
from django.contrib.auth.decorators import login_required
from django.http import (
    FileResponse,
    HttpRequest,
    HttpResponse,
    JsonResponse,
)
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from scanning import services
from scanning.models import (
    Detection,
    Issue,
    OpinionScan,
    PageDeletion,
    PageInsert,
    Scan,
    Stage,
    Status,
)
from scanning.utils import find_ocr_pdf


@login_required
def scan_process_view(request, pk):
    """Unified scan processing page with 3-step workflow.

    :param request: The HTTP request.
    :param pk: Scan primary key.
    :return: Rendered processing page.
    """
    scan = get_object_or_404(Scan.objects.select_related("reporter"), pk=pk)
    is_processing = scan.status in (Status.PROCESSING, Status.QUEUED)

    step = int(request.GET.get("step", 0))
    if step < 1 or step > 3:
        if is_processing:
            step = int(request.GET.get("step", 1))
        elif scan.stage == Stage.APPROVED:
            step = 3
        elif scan.stage == Stage.PROCESS or scan.opinions_json:
            # Stay on step 1 if there are unresolved issues
            has_issues = scan.issues.exclude(
                check_name="suppress_detection"
            ).exists()
            has_missing = bool(
                scan.missing_pages and json.loads(scan.missing_pages)
            )
            if has_issues or has_missing:
                step = 1
            else:
                step = 2
        else:
            step = 1

    issues = scan.issues.all()
    inserts = {ins.logical_page_number: ins for ins in scan.inserts.all()}

    page_map = json.loads(scan.page_map) if scan.page_map else []
    missing_pages = (
        json.loads(scan.missing_pages) if scan.missing_pages else []
    )

    for entry in page_map:
        if entry["type"] == "missing" and entry["logical_number"] in inserts:
            entry["type"] = "inserted"
            entry["insert_url"] = inserts[entry["logical_number"]].image.url

    # Map pdf_index → logical page number for navigation
    idx_to_logical = {}
    for entry in page_map:
        if entry.get("type") == "pdf_page":
            idx_to_logical[entry["pdf_index"]] = entry["logical_number"]

    flagged_pages = sorted(
        set(i.page_number for i in issues if i.page_number is not None)
    )

    ocr_results = json.loads(scan.ocr_results) if scan.ocr_results else []
    ocr_by_page = {}
    for r in ocr_results:
        ocr_by_page[r["pdf_page"]] = r

    # Annotate sequence issues for the sidebar page list
    prev_num = None
    for r in ocr_results:
        r["seq_issue"] = ""
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
            if diff == 0:
                r["seq_issue"] = "duplicate"
            elif diff < 0:
                r["seq_issue"] = "backward"
            elif diff > 2:
                r["seq_issue"] = "gap"
        prev_num = num

    has_pending_changes = scan.deletions.exists() or scan.inserts.exists()

    opinions = json.loads(scan.opinions_json) if scan.opinions_json else []

    # Build a set of page indices that contain IMAGE detections
    image_page_indices = set(
        Detection.objects.filter(
            scan=scan, label="IMAGE", active=True
        ).values_list("page_index", flat=True)
    )

    # Attach image_pages (logical page numbers) to each opinion
    for op in opinions:
        cp = op.get("caption_page", 0)
        ep = op.get("page_end", op.get("key_page", cp))
        op["image_pages"] = sorted(
            idx_to_logical.get(idx, idx + 1)
            for idx in range(cp, ep + 1)
            if idx in image_page_indices
        )

    has_redaction_rects = bool(scan.redaction_rects)

    # Find HEADNOTE detections not covered by headnote redaction rects
    uncovered_hn_pages = set()
    if has_redaction_rects:
        rects_data = json.loads(scan.redaction_rects)
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
        op["uncovered_headnote_pages"] = sorted(
            idx_to_logical.get(idx, idx + 1)
            for idx in range(cp, ep + 1)
            if idx in uncovered_hn_pages
        )

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
            s.masked_filename = (
                os.path.basename(s.masked_pdf.name)
                if s.masked_pdf and s.masked_pdf.name
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
        for iss in scan.issues.filter(check_name="suppress_detection"):
            if iss.metadata:
                try:
                    m = json.loads(iss.metadata)
                    bb = m.get("bbox", [0, 0, 0, 0])
                    suppressed.add(
                        (
                            m.get("page_index", 0),
                            m.get("label_id", 0),
                            round(bb[0]),
                            round(bb[1]),
                        )
                    )
                except Exception:
                    pass

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
                        {
                            "pdf_page": d.page_index + 1,
                            "page_index": d.page_index,
                            "label_id": d.label_id,
                            "logical_page": idx_to_logical.get(
                                d.page_index, d.page_index + 1
                            ),
                            "conf": round(d.confidence, 2),
                            "bbox": [d.x0, d.y0, d.x1, d.y1],
                            "img_width": d.img_width,
                            "img_height": d.img_height,
                        }
                    )

        # Build sorted list of paired key icon positions so we can
        # determine which key-icon span an unmatched caption falls in.
        # If a span already has a paired caption, extra captions in
        # that span are continuations — not missed opinions.
        paired_keys_sorted = sorted(paired_key_keys)

        def _caption_is_continuation(det):
            """Check if det falls in a key-icon span that already
            has a paired caption."""
            # Find which key-icon span this caption is in
            for i, (kp, kx, ky) in enumerate(paired_keys_sorted):
                if i + 1 < len(paired_keys_sorted):
                    next_kp, _, next_ky = paired_keys_sorted[i + 1]
                else:
                    next_kp, next_ky = float("inf"), float("inf")
                # Caption is in this span if it's after this key
                # and before the next key
                after_key = det.page_index > kp or (
                    det.page_index == kp and det.y0 > ky
                )
                before_next = det.page_index < next_kp or (
                    det.page_index == next_kp and det.y0 < next_ky
                )
                if after_key and before_next:
                    # There's already a paired caption in this span
                    return True
            return False

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
            if _caption_is_continuation(d):
                continue
            unmatched_captions.append(
                {
                    "pdf_page": d.page_index + 1,
                    "page_index": d.page_index,
                    "label_id": d.label_id,
                    "logical_page": idx_to_logical.get(
                        d.page_index, d.page_index + 1
                    ),
                    "conf": round(d.confidence, 2),
                    "bbox": [d.x0, d.y0, d.x1, d.y1],
                    "img_width": d.img_width,
                    "img_height": d.img_height,
                }
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
                    enumerate(missing),
                    lambda x: x[0] - x[1],
                ):
                    run = [v for _, v in g]
                    detect_warnings.append(
                        f"Pages {run[0]}-{run[-1]}"
                        f" ({len(run)} pages) not covered"
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
            "flagged_pages_json": json.dumps(flagged_pages),
            "ocr_results": ocr_results,
            "ocr_by_page_json": json.dumps(ocr_by_page),
            "has_pending_changes": has_pending_changes,
            "is_processing": is_processing,
            "opinions": opinions,
            "opinions_json": json.dumps(opinions),
            "has_redaction_rects": has_redaction_rects,
            "opinion_scans": opinion_scans,
            "detect_warnings": detect_warnings,
            "unmatched_keys": unmatched_keys,
            "unmatched_captions": unmatched_captions,
        },
    )


@login_required
def progress_api(request, pk):
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
        data["ocr_results"] = json.loads(scan.ocr_results)
    return JsonResponse(data)


@login_required
def serve_scan_pdf(request, pk):
    """Serve the best available PDF for a scan (OCR, bitonal, or original).

    :param request: The HTTP request.
    :param pk: Scan primary key.
    :return: File response streaming the PDF.
    """
    scan = get_object_or_404(Scan, pk=pk)
    if scan.output_dir:
        ocr = find_ocr_pdf(scan.output_dir)
        if ocr:
            return FileResponse(
                open(ocr, "rb"), content_type="application/pdf"
            )

        bitonal = Path(scan.output_dir) / "bitonal.pdf"
        if bitonal.exists():
            return FileResponse(
                open(bitonal, "rb"), content_type="application/pdf"
            )
    return FileResponse(
        open(scan.pdf_path, "rb"), content_type="application/pdf"
    )


@login_required
def serve_original_crop(request: HttpRequest, pk: int) -> HttpResponse:
    """Render a cropped region from the original (non-bitonal) PDF as PNG.

    :param request: The HTTP request (crop coordinates via query params).
    :param pk: Scan primary key.
    :return: PNG image response of the cropped region.
    """
    scan = get_object_or_404(Scan, pk=pk)
    page = int(request.GET.get("page", 0))
    x0 = float(request.GET.get("x0", 0))
    y0 = float(request.GET.get("y0", 0))
    x1 = float(request.GET.get("x1", 0))
    y1 = float(request.GET.get("y1", 0))
    dpi = min(max(int(request.GET.get("dpi", 150)), 72), 300)

    doc = fitz.open(scan.pdf_path)
    if page < 0 or page >= doc.page_count:
        doc.close()
        return HttpResponse(status=404)
    clip = fitz.Rect(x0, y0, x1, y1)
    pix = doc[page].get_pixmap(clip=clip, dpi=dpi)
    png_bytes = pix.tobytes("png")
    doc.close()
    resp = HttpResponse(png_bytes, content_type="image/png")
    resp["Cache-Control"] = "max-age=3600"
    return resp


@login_required
@require_POST
def start_validate(request, pk):
    """Queue a scan for validation (first step of the full pipeline).

    :param request: The HTTP request.
    :param pk: Scan primary key.
    :return: Redirect to the scan processing page.
    """
    scan = get_object_or_404(Scan, pk=pk)
    scan.status = Status.QUEUED
    scan.stage = Stage.VALIDATE
    scan.queued_action = "full_pipeline"
    scan.progress_message = "Queued for processing..."
    scan.save()
    return redirect("scan_process", pk=scan.pk)


@login_required
@require_POST
def start_detect(request, pk):
    """Queue a scan for detection or skip to review if detections exist.

    :param request: The HTTP request.
    :param pk: Scan primary key.
    :return: Redirect to the scan processing page (step 2).
    """
    scan = get_object_or_404(Scan, pk=pk)
    if Detection.objects.filter(scan=scan).exists():
        # Detections already exist (from full pipeline). Skip to review.
        return redirect(f"/scans/{scan.pk}/process/?step=2")

    scan.status = Status.QUEUED
    scan.stage = Stage.PROCESS
    scan.queued_action = "detect"
    scan.progress_message = "Queued for detection..."
    scan.save()
    return redirect(f"/scans/{scan.pk}/process/?step=2")


@login_required
@require_POST
def cancel_processing(request, pk):
    """Cancel an in-progress scan processing task.

    :param request: The HTTP request.
    :param pk: Scan primary key.
    :return: Redirect to the scan processing page.
    """
    scan = get_object_or_404(Scan, pk=pk)
    if scan.status == Status.PROCESSING:
        if scan.stage == Stage.PROCESS:
            Scan.objects.filter(pk=pk).update(
                status=Status.APPROVED,
                stage=Stage.VALIDATE,
                progress_message="",
                progress_log="",
                redacted_pdf_path="",
                opinions_json="",
            )
        else:
            Scan.objects.filter(pk=pk).update(
                status=Status.CANCELLED,
                progress_message="Cancelled by user.",
            )
    return redirect("scan_process", pk=pk)


@login_required
@require_POST
def recalculate(request, pk):
    """Recalculate validation issues from existing OCR results.

    :param request: The HTTP request.
    :param pk: Scan primary key.
    :return: Redirect to the scan processing page.
    """
    scan = get_object_or_404(Scan, pk=pk)
    if not scan.ocr_results:
        return redirect("scan_process", pk=pk)
    services.recalculate_issues(scan)
    return redirect("scan_process", pk=pk)


@login_required
@require_POST
def reprocess(request, pk):
    """Queue a scan for reprocessing (re-run the full pipeline).

    :param request: The HTTP request.
    :param pk: Scan primary key.
    :return: Redirect to the scan processing page.
    """
    scan = get_object_or_404(Scan, pk=pk)
    scan.status = Status.QUEUED
    scan.queued_action = "reprocess"
    scan.progress_message = "Queued for reprocessing..."
    scan.save()
    return redirect("scan_process", pk=scan.pk)


@login_required
@require_POST
def assign_page(request, pk):
    """Manually assign a page number to a PDF page.

    :param request: The HTTP request (JSON body with pdf_page and
        page_number).
    :param pk: Scan primary key.
    :return: JSON response confirming the assignment.
    """
    scan = get_object_or_404(Scan, pk=pk)
    data = json.loads(request.body)
    pdf_page = data["pdf_page"]
    page_number = data["page_number"]
    ocr_results = json.loads(scan.ocr_results) if scan.ocr_results else []
    for r in ocr_results:
        if r["pdf_page"] == pdf_page:
            r["detected"] = page_number
            r["type"] = "single"
            r["zone"] = "manual"
            r["score"] = 1.0
            r["ocr"] = "manual"
            break
    scan.ocr_results = json.dumps(ocr_results)
    scan.issues.filter(
        check_name="no_page_number", page_number=pdf_page
    ).delete()
    if not scan.issues.exists():
        scan.has_issues = False
    scan.save()
    return JsonResponse({"status": "ok"})


@login_required
@require_POST
def delete_page(request, pk):
    """Mark a PDF page for deletion during reprocessing.

    :param request: The HTTP request (JSON body with pdf_page).
    :param pk: Scan primary key.
    :return: JSON response confirming the deletion record.
    """
    scan = get_object_or_404(Scan, pk=pk)
    data = json.loads(request.body)
    pdf_page = data["pdf_page"]
    PageDeletion.objects.get_or_create(scan=scan, pdf_page=pdf_page)
    return JsonResponse({"status": "ok"})


@login_required
@require_POST
def add_page_insert(request, pk):
    """Upload an image to insert at a missing page position.

    :param request: The HTTP request (form data with page_number and
        image file).
    :param pk: Scan primary key.
    :return: JSON response with the insert URL and page number.
    """
    scan = get_object_or_404(Scan, pk=pk)
    page_number = int(request.POST.get("page_number", 0))
    image_file = request.FILES.get("image")
    if not page_number or not image_file:
        return JsonResponse(
            {"error": "Missing page_number or image"}, status=400
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
def dismiss_issue(request, pk):
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
    data = json.loads(request.body)
    issue_id = data.get("issue_id")
    Issue.objects.filter(pk=issue_id, scan=scan).delete()
    if not scan.issues.exists():
        scan.has_issues = False
        scan.save()
    return JsonResponse({"status": "ok"})


@login_required
@require_POST
def dismiss_issues(request, pk):
    """Dismiss all validation issues for a scan.

    :param request: The HTTP request.
    :param pk: Scan primary key.
    :return: Redirect to the scan list.
    """
    scan = get_object_or_404(Scan, pk=pk)
    scan.issues.all().delete()
    scan.has_issues = False
    scan.save()
    return redirect("scan_list")
