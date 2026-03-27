import json
import os
import threading

import fitz
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import views as auth_views
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db.models import Count
from django.http import FileResponse, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from scanning import services
from scanning.forms import (
    OpinionScanReviewForm,
    OpinionScanUploadForm,
    ScanReviewForm,
    ScanUploadForm,
)
from scanning.models import (
    Detection,
    Issue,
    LLMScan,
    OpinionScan,
    Volume,
    OpinionStatus,
    PageDeletion,
    PageInsert,
    Priority,
    QueueStatus,
    Reporter,
    Scan,
    Source,
    Stage,
    Status,
)
from scanning.utils import get_volume, get_output_base, find_json_file, find_ocr_pdf


def login_view(request):
    """Display the login page using Django's built-in LoginView.

    :param request: The current HTTP request.
    :type request: django.http.HttpRequest
    :returns: The rendered login page.
    :rtype: django.http.HttpResponse
    """
    return auth_views.LoginView.as_view(
        template_name="scanning/login.html",
    )(request)


def logout_view(request):
    """Log the user out and redirect to login.

    :param request: The current HTTP request.
    :type request: django.http.HttpRequest
    :returns: A redirect to the login page.
    :rtype: django.http.HttpResponseRedirect
    """
    return auth_views.LogoutView.as_view(
        next_page="/login/",
    )(request)


@login_required
def scan_list(request):
    """List scans with opinion count annotation.

    :param request: The current HTTP request.
    :type request: django.http.HttpRequest
    :returns: The rendered scan list page.
    :rtype: django.http.HttpResponse
    """
    scans = (
        Scan.objects.select_related("reporter")
        .annotate(opinion_count=Count("opinions"))
        .order_by("-date_created")
    )

    # Filtering
    status_filter = request.GET.get("status")
    if status_filter:
        scans = scans.filter(status=status_filter)

    reporter_filter = request.GET.get("reporter")
    if reporter_filter and reporter_filter.isdigit():
        scans = scans.filter(reporter_id=reporter_filter)

    source_filter = request.GET.get("source")
    if source_filter:
        scans = scans.filter(source=source_filter)

    volume_filter = request.GET.get("volume")
    if volume_filter:
        if volume_filter.isdigit():
            scans = scans.filter(volume=volume_filter)
        else:
            messages.error(request, "Volume must be a number.")
            volume_filter = ""

    paginator = Paginator(scans, 25)
    page_number = request.GET.get("page")
    page_obj = paginator.get_page(page_number)

    return render(
        request,
        "scanning/scan_list.html",
        {
            "page_obj": page_obj,
            "status_choices": Status.choices,
            "reporter_choices": [
                (str(r.pk), f"{r.full_name} ({r.short_name})")
                for r in Reporter.objects.all()
            ],
            "source_choices": Source.choices,
            "current_status": status_filter or "",
            "current_reporter": reporter_filter or "",
            "current_source": source_filter or "",
            "current_volume": volume_filter or "",
        },
    )


@login_required
def scan_upload(request):
    """Handle scan upload form.

    :param request: The current HTTP request.
    :type request: django.http.HttpRequest
    :returns: The rendered upload form or a redirect on success.
    :rtype: django.http.HttpResponse
    """
    if request.method == "POST":
        form = ScanUploadForm(request.POST, request.FILES)
        if form.is_valid():
            scan = form.save(commit=False)
            scan.uploaded_by = request.user
            scan.status = Status.UPLOADED
            scan.save()
            messages.success(request, "Scan uploaded successfully.")
            return redirect("scan_process", pk=scan.pk)
    else:
        form = ScanUploadForm()

    return render(
        request,
        "scanning/scan_upload.html",
        {"form": form},
    )


@login_required
def scan_detail(request, pk):
    """Display scan detail and handle staff review.

    :param request: The current HTTP request.
    :type request: django.http.HttpRequest
    :param pk: The primary key of the scan.
    :type pk: int
    :returns: The rendered detail page.
    :rtype: django.http.HttpResponse
    """
    scan = get_object_or_404(Scan.objects.select_related("reporter"), pk=pk)

    # Redirect to processing view if scan has processing data
    if scan.stage and scan.output_dir:
        return redirect("scan_process", pk=scan.pk)

    opinion_count = scan.opinions.count()

    review_form = None
    if request.user.is_staff and scan.status != Status.APPROVED:
        if request.method == "POST":
            review_form = ScanReviewForm(request.POST, instance=scan)
            if review_form.is_valid():
                updated_scan = review_form.save(commit=False)
                if updated_scan.status == Status.APPROVED:
                    updated_scan.processed_at = timezone.now()
                    messages.success(request, "Scan approved.")
                else:
                    updated_scan.processed_at = None
                    messages.info(
                        request,
                        "Scan rejected, status reset to Uploaded.",
                    )
                updated_scan.save()
                return redirect("scan_detail", pk=scan.pk)
        else:
            review_form = ScanReviewForm(instance=scan)

    return render(
        request,
        "scanning/scan_detail.html",
        {
            "scan": scan,
            "review_form": review_form,
            "opinion_count": opinion_count,
        },
    )


@login_required
def opinion_list(request):
    """List opinion scans with filters for scan, reporter, and status.

    :param request: The current HTTP request.
    :type request: django.http.HttpRequest
    :returns: The rendered opinion list page.
    :rtype: django.http.HttpResponse
    """
    opinions = OpinionScan.objects.select_related(
        "reporter", "scan", "uploaded_by"
    ).all()

    # Filtering
    scan_filter = request.GET.get("scan")
    if scan_filter and scan_filter.isdigit():
        opinions = opinions.filter(scan_id=scan_filter)

    reporter_filter = request.GET.get("reporter")
    if reporter_filter and reporter_filter.isdigit():
        opinions = opinions.filter(reporter_id=reporter_filter)

    status_filter = request.GET.get("status")
    if status_filter:
        opinions = opinions.filter(status=status_filter)

    volume_filter = request.GET.get("volume")
    if volume_filter:
        if volume_filter.isdigit():
            opinions = opinions.filter(volume=volume_filter)
        else:
            messages.error(request, "Volume must be a number.")
            volume_filter = ""

    paginator = Paginator(opinions, 25)
    page_number = request.GET.get("page")
    page_obj = paginator.get_page(page_number)

    return render(
        request,
        "scanning/opinion_list.html",
        {
            "page_obj": page_obj,
            "status_choices": OpinionStatus.choices,
            "reporter_choices": [
                (str(r.pk), f"{r.full_name} ({r.short_name})")
                for r in Reporter.objects.all()
            ],
            "current_scan": scan_filter or "",
            "current_reporter": reporter_filter or "",
            "current_status": status_filter or "",
            "current_volume": volume_filter or "",
        },
    )


@login_required
def opinion_detail(request, pk):
    """Display opinion scan detail with side-by-side PDF iframes.

    :param request: The current HTTP request.
    :type request: django.http.HttpRequest
    :param pk: The primary key of the opinion scan.
    :type pk: int
    :returns: The rendered opinion detail page.
    :rtype: django.http.HttpResponse
    """
    opinion = get_object_or_404(
        OpinionScan.objects.select_related("reporter", "scan", "uploaded_by"),
        pk=pk,
    )

    review_form = None
    if request.user.is_staff and opinion.status != OpinionStatus.OK:
        if request.method == "POST":
            review_form = OpinionScanReviewForm(request.POST, instance=opinion)
            if review_form.is_valid():
                updated_opinion = review_form.save(commit=False)
                if updated_opinion.status == OpinionStatus.OK:
                    messages.success(request, "Opinion scan approved.")
                else:
                    messages.info(
                        request,
                        "Opinion scan rejected, status reset to No status.",
                    )
                updated_opinion.save()
                return redirect("opinion_detail", pk=opinion.pk)
        else:
            review_form = OpinionScanReviewForm(instance=opinion)

    return render(
        request,
        "scanning/opinion_detail.html",
        {"opinion": opinion, "review_form": review_form},
    )


@login_required
def opinion_upload(request):
    """Handle standalone opinion upload form (superuser only).

    :param request: The current HTTP request.
    :type request: django.http.HttpRequest
    :returns: The rendered upload form or a redirect on success.
    :rtype: django.http.HttpResponse
    :raises PermissionDenied: If the user is not a superuser.
    """
    if not request.user.is_superuser:
        raise PermissionDenied

    if request.method == "POST":
        form = OpinionScanUploadForm(request.POST, request.FILES)
        if form.is_valid():
            opinion = form.save(commit=False)
            opinion.uploaded_by = request.user
            opinion.save()
            messages.success(request, "Opinion uploaded successfully.")
            return redirect("opinion_detail", pk=opinion.pk)
    else:
        form = OpinionScanUploadForm()

    return render(
        request,
        "scanning/opinion_upload.html",
        {"form": form},
    )


# ---------------------------------------------------------------------------
# Queue views
# ---------------------------------------------------------------------------


@login_required
def queue_view(request):
    """Scanner work queue -- volumes that need scanning, with filters.

    :param request: The current HTTP request.
    :type request: django.http.HttpRequest
    :returns: The rendered queue page.
    :rtype: django.http.HttpResponse
    """
    reporters = Reporter.objects.all()
    selected_reporter = request.GET.get("reporter", "")
    status_filter = request.GET.get("status", "")
    priority_filter = request.GET.get("priority", "")

    volumes = Volume.objects.select_related(
        "reporter", "assigned_to"
    ).order_by("reporter__short_name", "volume_number")

    if selected_reporter:
        volumes = volumes.filter(reporter__short_name=selected_reporter)
    if status_filter:
        volumes = volumes.filter(queue_status=status_filter)
    if priority_filter:
        volumes = volumes.filter(priority=priority_filter)

    # Stats
    total = Volume.objects.count()
    by_status = dict(
        Volume.objects.values_list("queue_status")
        .annotate(c=Count("id"))
        .values_list("queue_status", "c")
    )

    return render(
        request,
        "scanning/queue.html",
        {
            "volumes": volumes,
            "reporters": reporters,
            "selected_reporter": selected_reporter,
            "status_filter": status_filter,
            "priority_filter": priority_filter,
            "stats": {
                "total": total,
                "needs_scanning": by_status.get("needs_scanning", 0),
                "assigned": by_status.get("assigned", 0),
                "scanning": by_status.get("scanning", 0),
                "complete": by_status.get("complete", 0),
            },
            "queue_statuses": QueueStatus.choices,
            "priorities": Priority.choices,
        },
    )


@login_required
def queue_detail_view(request, reporter_slug, vol):
    """Detail page for a volume in the queue.

    Shows volume info, assignment, and all scans (parts) with
    upload buttons for each.
    """
    volume = get_object_or_404(
        Volume.objects.select_related("reporter", "assigned_to"),
        reporter__short_name=reporter_slug,
        volume_number=vol,
    )
    scans = volume.scans.select_related(
        "uploaded_by"
    ).order_by("start_page")

    return render(
        request,
        "scanning/queue_detail.html",
        {
            "volume": volume,
            "scans": scans,
            "queue_statuses": QueueStatus.choices,
        },
    )


@login_required
@require_POST
def claim_scan(request, reporter_slug, vol):
    """Claim or unclaim a volume for scanning."""
    volume = get_volume(reporter_slug, vol)

    if request.POST.get("unclaim") == "1":
        if volume.assigned_to == request.user:
            volume.queue_status = QueueStatus.NEEDS_SCANNING
            volume.assigned_to = None
            volume.assigned_at = None
            volume.save()
            messages.info(request, "Volume unclaimed.")
    elif volume.queue_status == QueueStatus.NEEDS_SCANNING:
        volume.queue_status = QueueStatus.ASSIGNED
        volume.assigned_to = request.user
        volume.assigned_at = timezone.now()
        volume.save()
        messages.success(request, "Volume claimed.")

    return redirect(
        "queue_detail",
        reporter_slug=reporter_slug,
        vol=vol,
    )


@login_required
@require_POST
def queue_upload(request, reporter_slug, vol):
    """Upload a PDF for a specific scan within a volume."""
    volume = get_volume(reporter_slug, vol)

    if request.POST.get("new_scan") == "1":
        # Create a new scan under this volume
        scan = Scan(
            volume_obj=volume,
            reporter=volume.reporter,
            volume=volume.volume_number,
            part_label=request.POST.get("part_label", "").strip(),
            source=Source.FULL,
            has_state_abbrev="has_state_abbrev" in request.POST,
        )
    else:
        scan_pk = request.POST.get("scan_pk")
        if not scan_pk:
            messages.error(request, "No scan specified.")
            return redirect(
                "queue_detail",
                reporter_slug=reporter_slug,
                vol=vol,
            )
        scan = get_object_or_404(
            Scan, pk=scan_pk, volume_obj=volume
        )
    pdf = request.FILES.get("original_pdf")
    if not pdf:
        messages.error(request, "No PDF file provided.")
        return redirect(
            "queue_detail",
            reporter_slug=reporter_slug,
            vol=vol,
        )

    if not pdf.name.lower().endswith(".pdf"):
        messages.error(request, "Only PDF files are accepted.")
        return redirect(
            "queue_detail",
            reporter_slug=reporter_slug,
            vol=vol,
        )

    header = pdf.read(5)
    pdf.seek(0)
    if header != b"%PDF-":
        messages.error(
            request, "The uploaded file is not a valid PDF."
        )
        return redirect(
            "queue_detail",
            reporter_slug=reporter_slug,
            vol=vol,
        )

    # Update page range if provided
    first_page = request.POST.get("first_page", "").strip()
    last_page = request.POST.get("last_page", "").strip()
    if first_page:
        scan.start_page = int(first_page)
    if last_page:
        scan.end_page = int(last_page)

    scan.has_state_abbrev = "has_state_abbrev" in request.POST
    scan.uploaded_by = request.user
    scan.status = Status.UPLOADED
    scan.save()  # Save first to get a PK

    # Create processing directory and save original PDF there
    from pathlib import Path as _P
    output_dir = (
        _P(settings.MEDIA_ROOT) / "processed" / str(scan.pk)
        / volume.reporter.short_name
        / str(volume.volume_number)
        / str(scan.start_page or 1)
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    scan.output_dir = str(output_dir)

    original_name = (
        f"{volume.reporter.short_name}.{volume.volume_number}"
        f".{scan.start_page or 1}.{scan.end_page or 0}"
        f".original.pdf"
    )
    original_path = output_dir / original_name
    with open(original_path, "wb") as f:
        for chunk in pdf.chunks():
            f.write(chunk)

    scan.original_pdf.name = str(
        original_path.relative_to(_P(settings.MEDIA_ROOT))
    )
    scan.save(update_fields=["output_dir", "original_pdf"])

    action = request.POST.get("action", "upload_only")
    if action == "upload_validate":
        # Kick off validation
        scan.status = Status.PROCESSING
        scan.stage = Stage.VALIDATE
        scan.progress_message = "Converting to bitonal..."
        scan.save()

        t = threading.Thread(
            target=services.run_validate_with_bitonal, args=(scan.pk,), daemon=True
        )
        t.start()
        return redirect("scan_process", pk=scan.pk)

    messages.success(request, "PDF uploaded successfully.")
    return redirect(
        "queue_detail",
        reporter_slug=reporter_slug,
        vol=vol,
    )


@login_required
@require_POST
def update_scan_status(request, reporter_slug, vol):
    """Update a volume's queue status."""
    volume = get_volume(reporter_slug, vol)
    new_status = request.POST.get("status")

    if new_status and new_status in dict(QueueStatus.choices):
        volume.queue_status = new_status
        if new_status == QueueStatus.NEEDS_SCANNING:
            volume.assigned_to = None
            volume.assigned_at = None
        volume.save()
        messages.success(
            request,
            f"Status updated to"
            f" {volume.get_queue_status_display()}.",
        )

    return redirect(
        "queue_detail",
        reporter_slug=reporter_slug,
        vol=vol,
    )


@login_required
def scan_process_view(request, pk):
    """Unified scan processing page with 4-step workflow."""
    scan = get_object_or_404(Scan.objects.select_related("reporter"), pk=pk)
    is_processing = scan.status == Status.PROCESSING

    step = int(request.GET.get("step", 0))
    if step < 1 or step > 4:
        if is_processing:
            step = int(request.GET.get("step", 1))
        elif scan.stage == Stage.APPROVED:
            step = 4
        elif scan.opinions_json:
            step = 3
        elif scan.stage == Stage.PROCESS:
            step = 2
        else:
            step = 1

    issues = scan.issues.all()
    inserts = {ins.logical_page_number: ins for ins in scan.inserts.all()}

    page_map = json.loads(scan.page_map) if scan.page_map else []
    missing_pages = json.loads(scan.missing_pages) if scan.missing_pages else []

    for entry in page_map:
        if entry["type"] == "missing" and entry["logical_number"] in inserts:
            entry["type"] = "inserted"
            entry["insert_url"] = inserts[entry["logical_number"]].image.url

    flagged_pages = sorted(set(i.page_number for i in issues if i.page_number is not None))

    ocr_results = json.loads(scan.ocr_results) if scan.ocr_results else []
    ocr_by_page = {}
    for r in ocr_results:
        ocr_by_page[r["pdf_page"]] = r

    has_pending_changes = scan.deletions.exists() or scan.inserts.exists()

    opinions = json.loads(scan.opinions_json) if scan.opinions_json else []

    has_redaction_rects = False
    if scan.output_dir:
        from pathlib import Path as _P
        has_redaction_rects = (_P(scan.output_dir) / "redaction_rects.json").exists()

    opinion_scans = []
    if step == 4:
        for s in OpinionScan.objects.filter(scan=scan).order_by("opinion_order"):
            s.redacted_filename = os.path.basename(s.redacted_pdf.name) if s.redacted_pdf and s.redacted_pdf.name else ""
            s.masked_filename = os.path.basename(s.masked_pdf.name) if s.masked_pdf and s.masked_pdf.name else ""
            s.unredacted_filename = os.path.basename(s.original_pdf.name) if s.original_pdf and s.original_pdf.name else ""
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
                    suppressed.add((m.get("page_index", 0), m.get("label_id", 0), round(bb[0]), round(bb[1])))
                except Exception:
                    pass

        paired_caption_keys = set()
        paired_key_keys = set()
        for op in opinions:
            cb = op.get("caption_bbox", [0, 0, 0, 0])
            kb = op.get("key_bbox", [0, 0, 0, 0])
            paired_caption_keys.add((op.get("caption_page", 0), round(cb[0]), round(cb[1])))
            paired_key_keys.add((op.get("key_page", 0), round(kb[0]), round(kb[1])))

        for d in Detection.objects.filter(scan=scan, active=True, label="KEY_ICON").order_by("page_index"):
            if (d.page_index, round(d.x0), round(d.y0)) not in paired_key_keys:
                if (d.page_index, d.label_id, round(d.x0), round(d.y0)) not in suppressed:
                    unmatched_keys.append({"pdf_page": d.page_index + 1, "page_index": d.page_index, "conf": round(d.confidence, 2)})

        for d in Detection.objects.filter(scan=scan, active=True, label="CASE_CAPTION").order_by("page_index"):
            if (d.page_index, round(d.x0), round(d.y0)) not in paired_caption_keys:
                if (d.page_index, d.label_id, round(d.x0), round(d.y0)) not in suppressed:
                    unmatched_captions.append({"pdf_page": d.page_index + 1, "page_index": d.page_index, "conf": round(d.confidence, 2)})

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
                cp = op.get("caption_page", 0) + (
                    scan.start_page or 1
                )
                kp = (
                    op.get("key_page", 0)
                    + (scan.start_page or 1)
                    + op.get("page_count", 1)
                    - 1
                )
                for p in range(cp, kp + 1):
                    covered.add(p)
            expected = set(
                range(scan.start_page, scan.end_page + 1)
            )
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

    return render(request, "scanning/scan_process.html", {
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
    })


@login_required
def progress_api(request, pk):
    scan = get_object_or_404(Scan, pk=pk)
    return JsonResponse({
        "status": scan.status,
        "current": scan.progress_current,
        "total": scan.progress_total,
        "message": scan.progress_message,
        "log": scan.progress_log,
    })


@login_required
def serve_scan_pdf(request, pk):
    scan = get_object_or_404(Scan, pk=pk)
    if scan.output_dir:
        ocr = find_ocr_pdf(scan.output_dir)
        if ocr:
            return FileResponse(open(ocr, "rb"), content_type="application/pdf")
        from pathlib import Path as _P
        bitonal = _P(scan.output_dir) / "bitonal.pdf"
        if bitonal.exists():
            return FileResponse(open(bitonal, "rb"), content_type="application/pdf")
    return FileResponse(open(scan.pdf_path, "rb"), content_type="application/pdf")


@login_required
@require_POST
def start_validate(request, pk):
    scan = get_object_or_404(Scan, pk=pk)
    scan.status = Status.PROCESSING
    scan.stage = Stage.VALIDATE
    scan.progress_message = "Converting to bitonal..."
    scan.save()

    t = threading.Thread(target=services.run_validate_with_bitonal, args=(scan.pk,), daemon=True)
    t.start()
    return redirect("scan_process", pk=scan.pk)


@login_required
@require_POST
def start_detect(request, pk):
    scan = get_object_or_404(Scan, pk=pk)
    scan.status = Status.PROCESSING
    scan.stage = Stage.PROCESS
    scan.progress_current = 0
    scan.progress_total = 0
    scan.progress_message = "Starting detection..."
    scan.save()

    t = threading.Thread(target=services.run_detect, args=(scan.pk,), daemon=True)
    t.start()
    return redirect(f"/scans/{scan.pk}/process/?step=2")


@login_required
@require_POST
def cancel_processing(request, pk):
    scan = get_object_or_404(Scan, pk=pk)
    if scan.status == Status.PROCESSING:
        if scan.stage == Stage.PROCESS:
            Scan.objects.filter(pk=pk).update(
                status=Status.APPROVED, stage=Stage.VALIDATE,
                progress_message="", progress_log="",
                redacted_pdf_path="", opinions_json="",
            )
        else:
            Scan.objects.filter(pk=pk).update(
                status=Status.CANCELLED, progress_message="Cancelled by user.",
            )
    return redirect("scan_list")


@login_required
@require_POST
def recalculate(request, pk):
    scan = get_object_or_404(Scan, pk=pk)
    if not scan.ocr_results:
        return redirect("scan_process", pk=pk)
    services.recalculate_issues(scan)
    return redirect("scan_process", pk=pk)


@login_required
@require_POST
def reprocess(request, pk):
    scan = get_object_or_404(Scan, pk=pk)
    scan.status = Status.PROCESSING
    scan.progress_current = 0
    scan.progress_total = 0
    scan.progress_message = "Starting reprocess..."
    scan.save()
    t = threading.Thread(target=services.run_reprocess, args=(scan.pk,), daemon=True)
    t.start()
    return redirect("scan_process", pk=scan.pk)


@login_required
@require_POST
def assign_page(request, pk):
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
    scan.issues.filter(check_name="no_page_number", page_number=pdf_page).delete()
    if not scan.issues.exists():
        scan.has_issues = False
    scan.save()
    return JsonResponse({"status": "ok"})


@login_required
@require_POST
def delete_page(request, pk):
    scan = get_object_or_404(Scan, pk=pk)
    data = json.loads(request.body)
    pdf_page = data["pdf_page"]
    PageDeletion.objects.get_or_create(scan=scan, pdf_page=pdf_page)
    return JsonResponse({"status": "ok"})


@login_required
@require_POST
def add_page_insert(request, pk):
    scan = get_object_or_404(Scan, pk=pk)
    page_number = int(request.POST.get("page_number", 0))
    image_file = request.FILES.get("image")
    if not page_number or not image_file:
        return JsonResponse({"error": "Missing page_number or image"}, status=400)
    insert, _created = PageInsert.objects.update_or_create(
        scan=scan, logical_page_number=page_number,
        defaults={"image": image_file},
    )
    return JsonResponse({
        "status": "ok", "page_number": page_number, "image_url": insert.image.url,
    })


@login_required
@require_POST
def dismiss_issue(request, pk):
    scan = get_object_or_404(Scan, pk=pk)
    has_pending = scan.deletions.exists() or scan.inserts.exists()
    if has_pending:
        return JsonResponse(
            {"status": "error", "message": "Reprocess first -- there are pending changes."},
            status=400,
        )
    data = json.loads(request.body)
    issue_id = data.get("issue_id")
    issue = Issue.objects.filter(pk=issue_id, scan=scan).first()
    Issue.objects.filter(pk=issue_id, scan=scan).delete()
    if not scan.issues.exists():
        scan.has_issues = False
        scan.save()
    return JsonResponse({"status": "ok"})


@login_required
@require_POST
def dismiss_issues(request, pk):
    scan = get_object_or_404(Scan, pk=pk)
    scan.issues.all().delete()
    scan.has_issues = False
    scan.save()
    return redirect("scan_list")


@login_required
def serve_detections(request, pk):
    scan = get_object_or_404(Scan, pk=pk)
    dets = Detection.objects.filter(scan=scan, active=True).order_by("page_index", "y0")
    data = [{
        "page_index": d.page_index, "label": d.label, "label_id": d.label_id,
        "confidence": d.confidence, "bbox": [d.x0, d.y0, d.x1, d.y1],
        "img_width": d.img_width, "img_height": d.img_height,
        "model_count": d.model_count,
    } for d in dets]
    return JsonResponse(data, safe=False)


@login_required
def serve_opinions(request, pk):
    scan = get_object_or_404(Scan, pk=pk)
    if scan.opinions_json:
        return JsonResponse(json.loads(scan.opinions_json), safe=False)
    return JsonResponse([], safe=False)


@login_required
def serve_margin_rects(request, pk):
    scan = get_object_or_404(Scan, pk=pk)
    output_base = get_output_base(scan)
    path = find_json_file(output_base, "margin_rects.json")
    if path:
        return JsonResponse(json.loads(path.read_text()), safe=False)
    ocr_pdf = find_ocr_pdf(output_base) if output_base.is_dir() else None
    if not ocr_pdf:
        return JsonResponse([], safe=False)
    from blackletter.margins import compute_margin_rects
    rects = compute_margin_rects(ocr_pdf)
    return JsonResponse(rects, safe=False)


@login_required
def serve_redaction_rects(request, pk):
    scan = get_object_or_404(Scan, pk=pk)
    output_base = get_output_base(scan)
    path = find_json_file(output_base, "redaction_rects.json")
    if path:
        return JsonResponse(json.loads(path.read_text()), safe=False)
    return JsonResponse([], safe=False)


@login_required
@require_POST
def save_redaction_rect(request, pk):
    scan = get_object_or_404(Scan, pk=pk)
    data = json.loads(request.body)
    page_idx = data["page_index"]
    action = data.get("action", "update")
    original = data.get("original", {})
    adjusted = data.get("adjusted", {})
    rect_type = data.get("type", "")
    fill = data.get("fill", "black")
    output_base = get_output_base(scan)
    rects_path = find_json_file(output_base, "redaction_rects.json")
    if not rects_path:
        return JsonResponse({"error": "No redaction_rects.json"}, status=404)
    rects = json.loads(rects_path.read_text())
    if action == "delete":
        for page_data in rects:
            if page_data["page_index"] != page_idx:
                continue
            page_data["rects"] = [
                r for r in page_data["rects"]
                if not (abs(r["x0"] - original["x0"]) < 2
                        and abs(r["y0"] - original["y0"]) < 2
                        and r.get("type", "") == rect_type)
            ]
            break
        rects_path.write_text(json.dumps(rects))
        return JsonResponse({"status": "ok", "action": "deleted"})
    found = False
    for page_data in rects:
        if page_data["page_index"] != page_idx:
            continue
        for r in page_data["rects"]:
            if (abs(r["x0"] - original.get("x0", -999)) < 2
                    and abs(r["y0"] - original.get("y0", -999)) < 2
                    and r.get("type") == rect_type):
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
                page_data["rects"].append({
                    "x0": round(adjusted["x0"], 1), "y0": round(adjusted["y0"], 1),
                    "x1": round(adjusted["x1"], 1), "y1": round(adjusted["y1"], 1),
                    "fill": fill, "type": rect_type,
                })
                found = True
                break
    rects_path.write_text(json.dumps(rects))
    return JsonResponse({"status": "ok", "found": found})


@login_required
@require_POST
def save_margin_rect(request, pk):
    scan = get_object_or_404(Scan, pk=pk)
    data = json.loads(request.body)
    page_idx = data["page_index"]
    original = data.get("original", {})
    action = data.get("action", "update")
    adjusted = data.get("adjusted", {})
    output_base = get_output_base(scan)
    rects_path = find_json_file(output_base, "margin_rects.json")
    if not rects_path:
        return JsonResponse({"error": "No margin_rects.json"}, status=404)
    rects = json.loads(rects_path.read_text())
    if action == "delete":
        for page_data in rects:
            if page_data["page_index"] != page_idx:
                continue
            page_data["rects"] = [
                r for r in page_data["rects"]
                if not (abs(r["x0"] - original.get("x0", -999)) < 2
                        and abs(r["y0"] - original.get("y0", -999)) < 2)
            ]
            break
        rects_path.write_text(json.dumps(rects))
        return JsonResponse({"status": "ok", "action": "deleted"})
    found = False
    for page_data in rects:
        if page_data["page_index"] != page_idx:
            continue
        for r in page_data["rects"]:
            if (abs(r["x0"] - original.get("x0", -999)) < 2
                    and abs(r["y0"] - original.get("y0", -999)) < 2):
                r["x0"] = round(adjusted["x0"], 1)
                r["y0"] = round(adjusted["y0"], 1)
                r["x1"] = round(adjusted["x1"], 1)
                r["y1"] = round(adjusted["y1"], 1)
                found = True
                break
        if found:
            break
    rects_path.write_text(json.dumps(rects))
    return JsonResponse({"status": "ok", "found": found})


@login_required
@require_POST
def pair_opinions_api(request, pk):
    scan = get_object_or_404(Scan, pk=pk)
    if not scan.output_dir:
        return JsonResponse({"error": "No output directory"}, status=400)
    output_dir = get_output_base(scan)
    det_path = output_dir / "detections.json"
    dets = Detection.objects.filter(scan=scan, active=True).order_by("page_index", "y0")
    det_data = [{
        "page_index": d.page_index, "label": d.label, "label_id": d.label_id,
        "confidence": d.confidence, "bbox": [d.x0, d.y0, d.x1, d.y1],
        "img_width": d.img_width, "img_height": d.img_height, "model_count": d.model_count,
    } for d in dets]
    if not det_data:
        return JsonResponse({"error": "No detections found"}, status=400)
    det_path.write_text(json.dumps(det_data))
    pdf_path = None
    bitonal = output_dir / "bitonal.pdf"
    if bitonal.exists():
        pdf_path = str(bitonal)
    else:
        pdf_path = scan.pdf_path
    try:
        from blackletter.api import pair as bl_pair
        opinions = bl_pair(
            str(det_path), pdf_path,
            reporter=scan.reporter.short_name, volume=str(scan.volume),
            first_page=scan.start_page or 1,
        )
        scan.opinions_json = json.dumps(opinions)
        scan.save()
        gaps = []
        if opinions and scan.start_page and scan.end_page:
            covered = set()
            for op in opinions:
                cp = op.get("caption_page", 0) + (scan.start_page or 1)
                kp = op.get("key_page", 0) + (scan.start_page or 1) + op.get("page_count", 1) - 1
                for p in range(cp, kp + 1):
                    covered.add(p)
            expected = set(range(scan.start_page, scan.end_page + 1))
            missing = sorted(expected - covered)
            if missing:
                import itertools
                for _, g in itertools.groupby(enumerate(missing), lambda x: x[0] - x[1]):
                    run = [v for _, v in g]
                    gaps.append({"start": run[0], "end": run[-1], "count": len(run)})
        return JsonResponse({"status": "ok", "opinions": opinions, "gaps": gaps})
    except Exception as exc:
        return JsonResponse({"error": str(exc)[:255]}, status=500)


@login_required
@require_POST
def compute_redactions_api(request, pk):
    from pathlib import Path as _P
    scan = get_object_or_404(Scan, pk=pk)
    if not scan.output_dir:
        return JsonResponse({"error": "No output directory"}, status=400)
    if not scan.opinions_json:
        return JsonResponse({"error": "No opinions paired yet"}, status=400)
    output_dir = _P(scan.output_dir)
    try:
        dets = Detection.objects.filter(scan=scan, active=True).order_by("page_index", "y0")
        det_data = [{
            "page_index": d.page_index, "label": d.label, "label_id": d.label_id,
            "confidence": d.confidence, "bbox": [d.x0, d.y0, d.x1, d.y1],
            "img_width": d.img_width, "img_height": d.img_height,
        } for d in dets]
        if not det_data:
            return JsonResponse({"error": "No detections found"}, status=400)
        (output_dir / "detections.json").write_text(json.dumps(det_data))
        pdf_path = scan.pdf_path
        for f in sorted(output_dir.glob("*.pdf")):
            if f.name not in ("bitonal.pdf",) and not f.name.endswith(".redacted.pdf") and not f.name.endswith(".original.pdf"):
                pdf_path = str(f)
                break
        else:
            bitonal = output_dir / "bitonal.pdf"
            if bitonal.exists():
                pdf_path = str(bitonal)
    except Exception as exc:
        import traceback
        traceback.print_exc()
        return JsonResponse({"error": str(exc)[:500]}, status=500)
    try:
        from blackletter.models import BBox, Detection as BLDetection, Document as BLDoc, Label, Page
        from blackletter.scanner import _pair_opinions
        from blackletter.process import compute_redaction_rects
        src_pdf = fitz.open(pdf_path)
        pages_data = {}
        for entry in det_data:
            pi = entry["page_index"]
            if pi not in pages_data:
                pages_data[pi] = {"img_width": entry.get("img_width", 1),
                                  "img_height": entry.get("img_height", 1), "detections": []}
            pages_data[pi]["detections"].append(entry)
        pages = []
        for pi in sorted(pages_data.keys()):
            pd = pages_data[pi]
            if pi < src_pdf.page_count:
                pw, ph = src_pdf[pi].rect.width, src_pdf[pi].rect.height
            else:
                pw, ph = 612.0, 792.0
            page = Page(index=pi, pdf_width=pw, pdf_height=ph,
                        img_width=pd["img_width"], img_height=pd["img_height"])
            for d in pd["detections"]:
                b = d.get("bbox", [0, 0, 1, 1])
                page.detections.append(BLDetection(
                    bbox=BBox(x1=b[0], y1=b[1], x2=b[2], y2=b[3]),
                    label=Label(d["label_id"]), confidence=d["confidence"], page_index=pi,
                ))
            pages.append(page)
        src_pdf.close()
        document = BLDoc(
            pdf_path=pdf_path, pages=pages,
            reporter=scan.reporter.short_name or "", volume=str(scan.volume) or "",
            first_page=scan.start_page or 1, ocr_applied=True,
        )
        opinions = _pair_opinions(document)
        rects = compute_redaction_rects(document, opinions, skip_doctr=True)

        # Split each headnote block at HEADNOTE detection boundaries
        # so the big block becomes N sub-blocks with visible gaps between headnotes
        _GAP = 6  # px gap between sub-blocks
        hn_dets_by_page = {}
        for d in Detection.objects.filter(scan=scan, active=True, label="HEADNOTE"):
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
                # Find HEADNOTE detections whose y0 falls inside this block
                # Get detections in this column, sorted by y0
                col_dets = sorted(
                    (d for d in hn_dets
                     if r["y0"] + _GAP < d.y0 < r["y1"] - _GAP
                     and d.x0 < r["x1"] and d.x1 > r["x0"]),
                    key=lambda d: d.y0
                )
                # Merge overlapping detections into non-overlapping groups
                merged = []
                for d in col_dets:
                    if merged and d.y0 < merged[-1][1]:  # overlaps previous
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
                        new_page_rects.append({**r, "y0": round(prev_y, 1), "y1": round(sp - _GAP / 2, 1)})
                    prev_y = sp + _GAP / 2
                new_page_rects.append({**r, "y0": round(prev_y, 1), "y1": round(r["y1"], 1)})
            page_entry["rects"] = new_page_rects

        rects_path = output_dir / "redaction_rects.json"
        rects_path.write_text(json.dumps(rects))
        total_rects = sum(len(r["rects"]) for r in rects)
        margin_rects_path = output_dir / "margin_rects.json"
        if not margin_rects_path.exists():
            from blackletter.margins import compute_margin_rects
            margin_rects = compute_margin_rects(pdf_path)
            margin_rects_path.write_text(json.dumps(margin_rects))
        return JsonResponse({"status": "ok", "pages": len(rects), "rects": total_rects})
    except Exception as exc:
        import traceback
        traceback.print_exc()
        return JsonResponse({"error": str(exc)[:500]}, status=500)


@login_required
@require_POST
def generate_files(request, pk):
    scan = get_object_or_404(Scan, pk=pk)
    if scan.status == Status.PROCESSING:
        return redirect(f"/scans/{scan.pk}/process/?step=4")
    scan.stage = Stage.PROCESS
    scan.status = Status.PROCESSING
    scan.progress_current = 0
    scan.progress_total = 0
    scan.progress_message = "Generating files..."
    scan.progress_log = ""
    scan.save()
    t = threading.Thread(target=services.run_generate_files, args=(scan.pk,), daemon=True)
    t.start()
    return redirect(f"/scans/{scan.pk}/process/?step=4")


@login_required
@require_POST
def approve_scan(request, pk):
    scan = get_object_or_404(Scan, pk=pk)
    scan.stage = Stage.APPROVED
    scan.save()
    return redirect("scan_list")


@login_required
def serve_opinion_pdf(request, pk, filename):
    scan = get_object_or_404(Scan, pk=pk)
    if not scan.output_dir:
        return HttpResponse("No output directory", status=404)
    file_path = os.path.join(scan.output_dir, "redacted", filename)
    if not os.path.isfile(file_path):
        return HttpResponse("Opinion PDF not found", status=404)
    return FileResponse(open(file_path, "rb"), content_type="application/pdf")


@login_required
def serve_unredacted_opinion_pdf(request, pk, filename):
    scan = get_object_or_404(Scan, pk=pk)
    if not scan.output_dir:
        return HttpResponse("No output directory", status=404)
    file_path = os.path.join(scan.output_dir, "unredacted", filename)
    if not os.path.isfile(file_path):
        return HttpResponse("Unredacted opinion PDF not found", status=404)
    return FileResponse(open(file_path, "rb"), content_type="application/pdf")


@login_required
def serve_masked_opinion_pdf(request, pk, filename):
    scan = get_object_or_404(Scan, pk=pk)
    if not scan.output_dir:
        return HttpResponse("No output directory", status=404)
    file_path = os.path.join(scan.output_dir, "masked", filename)
    if not os.path.isfile(file_path):
        return HttpResponse("Masked opinion PDF not found", status=404)
    return FileResponse(open(file_path, "rb"), content_type="application/pdf")


@login_required
def serve_redacted_pdf(request, pk):
    scan = get_object_or_404(Scan, pk=pk)
    if scan.redacted_pdf_path and os.path.isfile(scan.redacted_pdf_path):
        return FileResponse(open(scan.redacted_pdf_path, "rb"), content_type="application/pdf")
    if scan.output_dir:
        from pathlib import Path as _P
        for f in sorted(_P(scan.output_dir).glob("*.pdf")):
            if "redacted" not in f.name and "bitonal" not in f.name:
                return FileResponse(open(f, "rb"), content_type="application/pdf")
    return HttpResponse("No PDF available", status=404)


@login_required
def serve_ocr_results(request, pk):
    scan = get_object_or_404(Scan, pk=pk)
    if scan.ocr_results:
        return JsonResponse(json.loads(scan.ocr_results), safe=False)
    return JsonResponse([], safe=False)


@login_required
@require_POST
def flag_issue(request, pk):
    scan = get_object_or_404(Scan, pk=pk)
    data = json.loads(request.body)
    message = data.get("message", "").strip()
    page = data.get("page_number")
    metadata = data.get("metadata", {})
    if not message:
        return JsonResponse({"status": "error", "message": "Message required"}, status=400)
    check_name = "process_flag"
    if metadata.get("type") == "suppress_detection":
        check_name = "suppress_detection"
    elif metadata.get("type") == "add_detection":
        check_name = "add_detection"
    elif metadata.get("type") == "approve_detection":
        check_name = "approve_detection"
    issue = Issue.objects.create(
        scan=scan, page_number=page, check_name=check_name,
        severity="warning", message=message,
        metadata=json.dumps(metadata) if metadata else "",
    )
    return JsonResponse({"status": "ok", "id": issue.pk})


@login_required
@require_POST
def remove_flag(request, pk, flag_id):
    scan = get_object_or_404(Scan, pk=pk)
    Issue.objects.filter(
        pk=flag_id, scan=scan,
        check_name__in=["process_flag", "suppress_detection", "add_detection", "approve_detection"],
    ).delete()
    return JsonResponse({"status": "ok"})


@login_required
@require_POST
def add_single_detection(request, pk):
    scan = get_object_or_404(Scan, pk=pk)
    det = json.loads(request.body)
    output_base = get_output_base(scan)
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
        if abs(e["bbox"][0] - det["bbox"][0]) < 15 and abs(e["bbox"][1] - det["bbox"][1]) < 15:
            e["confidence"] = 1.0
            boosted = True
            Detection.objects.filter(
                scan=scan, page_index=det["page_index"], label_id=det["label_id"],
                x0__gte=det["bbox"][0] - 15, x0__lte=det["bbox"][0] + 15,
                y0__gte=det["bbox"][1] - 15, y0__lte=det["bbox"][1] + 15,
            ).update(confidence=1.0, model_name="approved")
            break
    if not boosted:
        det["confidence"] = 1.0
        existing.append(det)
        from blackletter.models import Label
        try:
            label_name = Label(det["label_id"]).name
            Detection.objects.create(
                scan=scan, page_index=det["page_index"],
                label=label_name, label_id=det["label_id"], confidence=1.0,
                x0=det["bbox"][0], y0=det["bbox"][1],
                x1=det["bbox"][2], y1=det["bbox"][3],
                img_width=det.get("img_width", 0), img_height=det.get("img_height", 0),
                model_name="manual", model_count=1,
                found_by=json.dumps([{"model": "manual", "confidence": 1.0}]),
            )
        except Exception:
            pass
    det_path.write_text(json.dumps(existing))
    return JsonResponse({"status": "ok", "added": not boosted})


@login_required
@require_POST
def bake_redactions(request, pk):
    scan = get_object_or_404(Scan, pk=pk)
    if not scan.output_dir:
        return JsonResponse({"status": "error", "message": "No output dir"}, status=400)
    return JsonResponse({"status": "ok", "message": "No redactions to bake", "count": 0})


@login_required
def export_pdf(request, pk):
    scan = get_object_or_404(Scan, pk=pk)
    pdf_doc = fitz.open(scan.pdf_path)
    page_map = json.loads(scan.page_map) if scan.page_map else []
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
            insert_ops.append((insert_before, inserts[entry["logical_number"]]))
    offset = 0
    for insert_before, insert_obj in insert_ops:
        img_path = insert_obj.image.path
        if insert_before is not None:
            adjusted = insert_before - len([d for d in deleted_pages if d <= insert_before])
            pno = adjusted + offset
            ref_page = pdf_doc.load_page(min(pno, len(pdf_doc) - 1))
        else:
            pno = len(pdf_doc)
            ref_page = pdf_doc.load_page(len(pdf_doc) - 1)
        w, h = ref_page.rect.width, ref_page.rect.height
        if img_path.lower().endswith(".pdf"):
            insert_pdf = fitz.open(img_path)
            pdf_doc.insert_pdf(insert_pdf, from_page=0, to_page=insert_pdf.page_count - 1, start_at=pno)
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
