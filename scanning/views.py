import json
import os
import shutil
import tempfile
from pathlib import Path

import fitz
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import views as auth_views
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db.models import Count
from django.http import (
    FileResponse,
    Http404,
    HttpRequest,
    HttpResponse,
    JsonResponse,
)
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from scanning import services
from scanning.forms import (
    OpinionScanUploadForm,
    ScanReviewForm,
    ScanUploadForm,
)
from scanning.models import (
    Detection,
    Issue,
    OpinionScan,
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
    Volume,
)
from scanning.utils import (
    find_json_file,
    find_ocr_pdf,
    get_output_base,
    get_volume,
)


def login_view(request: HttpRequest) -> HttpResponse:
    """Display the login page using Django's built-in LoginView.

    :param request: The current HTTP request.
    :return: The rendered login page.
    """
    return auth_views.LoginView.as_view(
        template_name="scanning/login.html",
    )(request)


def logout_view(request: HttpRequest) -> HttpResponse:
    """Log the user out and redirect to login.

    :param request: The current HTTP request.
    :return: A redirect to the login page.
    """
    return auth_views.LogoutView.as_view(
        next_page="/login/",
    )(request)


@login_required
def scan_list(request: HttpRequest) -> HttpResponse:
    """List scans with opinion count annotation.

    :param request: The current HTTP request.
    :return: The rendered scan list page.
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
def scan_upload(request: HttpRequest) -> HttpResponse:
    """Handle scan upload form.

    :param request: The current HTTP request.
    :return: The rendered upload form or a redirect on success.
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
def scan_detail(request: HttpRequest, pk: int) -> HttpResponse:
    """Display scan detail and handle staff review.

    :param request: The current HTTP request.
    :param pk: The primary key of the scan.
    :return: The rendered detail page.
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
def opinion_list(request: HttpRequest) -> HttpResponse:
    """List opinion scans with filters for scan, reporter, and status.

    :param request: The current HTTP request.
    :return: The rendered opinion list page.
    """
    opinions = OpinionScan.objects.select_related("reporter", "scan").order_by(
        "reporter__short_name", "volume", "page_start"
    )

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

    paginator = Paginator(opinions, 50)
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
            "current_reporter": reporter_filter or "",
            "current_status": status_filter or "",
            "current_volume": volume_filter or "",
        },
    )


@login_required
def opinion_detail(request: HttpRequest, pk: int) -> HttpResponse:
    """Display opinion scan detail with side-by-side PDF iframes.

    :param request: The current HTTP request.
    :param pk: The primary key of the opinion scan.
    :return: The rendered opinion detail page.
    """
    opinion = get_object_or_404(
        OpinionScan.objects.select_related("reporter", "scan", "uploaded_by"),
        pk=pk,
    )

    return render(
        request,
        "scanning/opinion_detail.html",
        {"opinion": opinion},
    )


@login_required
def opinion_upload(request: HttpRequest) -> HttpResponse:
    """Handle standalone opinion upload form (superuser only).

    :param request: The current HTTP request.
    :return: The rendered upload form or a redirect on success.
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
def queue_view(request: HttpRequest) -> HttpResponse:
    """Scanner work queue -- volumes that need scanning, with filters.

    :param request: The current HTTP request.
    :return: The rendered queue page.
    """
    reporters = Reporter.objects.all()
    selected_reporter = request.GET.get("reporter", "")
    status_filter = request.GET.get("status", "")
    priority_filter = request.GET.get("priority", "")

    volumes = (
        Volume.objects.select_related("reporter", "assigned_to")
        .prefetch_related("scans")
        .order_by("reporter__short_name", "volume_number")
    )

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
def queue_detail_view(
    request: HttpRequest, reporter_slug: str, vol: int
) -> HttpResponse:
    """Detail page for a volume in the queue.

    Shows volume info, assignment, and all scans (parts) with
    upload buttons for each.

    :param request: The HTTP request.
    :param reporter_slug: Short-name slug identifying the reporter.
    :param vol: Volume number within the reporter.
    :return: Rendered queue detail page.
    """
    volume = get_object_or_404(
        Volume.objects.select_related("reporter", "assigned_to"),
        reporter__short_name=reporter_slug,
        volume_number=vol,
    )
    scans = volume.scans.select_related("uploaded_by").order_by("start_page")

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
    """Claim or unclaim a volume for scanning.

    :param request: The HTTP request.
    :param reporter_slug: Short-name slug identifying the reporter.
    :param vol: Volume number within the reporter.
    :return: Redirect to the queue detail page.
    """
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

    if request.POST.get("next") == "queue":
        return redirect("queue")
    return redirect(
        "queue_detail",
        reporter_slug=reporter_slug,
        vol=vol,
    )


@login_required
@require_POST
def queue_upload(request, reporter_slug, vol):
    """Upload a PDF for a specific scan within a volume.

    :param request: The HTTP request.
    :param reporter_slug: Short-name slug identifying the reporter.
    :param vol: Volume number within the reporter.
    :return: Redirect to queue detail or scan processing page.
    """
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
        scan = get_object_or_404(Scan, pk=scan_pk, volume_obj=volume)
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
        messages.error(request, "The uploaded file is not a valid PDF.")
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

    output_dir = (
        Path(settings.MEDIA_ROOT)
        / "processed"
        / str(scan.pk)
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

    # Always keep a local copy in output_dir for the processing pipeline
    local_path = output_dir / original_name
    with open(local_path, "wb") as f:
        for chunk in pdf.chunks():
            f.write(chunk)

    # Save through Django's storage backend (S3 in prod, local in dev).
    # Reset the file pointer so .save() can read the full content.
    pdf.seek(0)
    scan.original_pdf.save(original_name, pdf, save=False)
    scan.save(update_fields=["output_dir", "original_pdf"])

    action = request.POST.get("action", "upload_only")
    if action == "upload_validate":
        scan.status = Status.QUEUED
        scan.stage = Stage.VALIDATE
        scan.queued_action = "full_pipeline"
        scan.progress_message = "Queued for processing..."
        scan.save()
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
    """Update a volume's queue status.

    :param request: The HTTP request.
    :param reporter_slug: Short-name slug identifying the reporter.
    :param vol: Volume number within the reporter.
    :return: Redirect to the queue detail page.
    """
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
            f"Status updated to {volume.get_queue_status_display()}.",
        )

    return redirect(
        "queue_detail",
        reporter_slug=reporter_slug,
        vol=vol,
    )


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
        return JsonResponse(json.loads(scan.opinions_json), safe=False)
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
        return JsonResponse(json.loads(scan.margin_rects), safe=False)
    output_base = get_output_base(scan)
    ocr_pdf = find_ocr_pdf(output_base) if output_base.is_dir() else None
    if not ocr_pdf:
        return JsonResponse([], safe=False)
    from blackletter.margins import compute_margin_rects

    from scanning.services import _adjust_margins_for_detections

    rects = compute_margin_rects(ocr_pdf)
    rects = _adjust_margins_for_detections(rects, output_base)
    Scan.objects.filter(pk=pk).update(margin_rects=json.dumps(rects))
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
        return JsonResponse(json.loads(scan.redaction_rects), safe=False)
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
    data = json.loads(request.body)
    page_idx = data["page_index"]
    action = data.get("action", "update")
    original = data.get("original", {})
    adjusted = data.get("adjusted", {})
    rect_type = data.get("type", "")
    fill = data.get("fill", "black")
    if not scan.redaction_rects:
        return JsonResponse({"error": "No redaction rects"}, status=404)
    rects = json.loads(scan.redaction_rects)
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
        Scan.objects.filter(pk=pk).update(redaction_rects=json.dumps(rects))
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
    Scan.objects.filter(pk=pk).update(redaction_rects=json.dumps(rects))
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
    data = json.loads(request.body)
    page_idx = data["page_index"]
    original = data.get("original", {})
    action = data.get("action", "update")
    adjusted = data.get("adjusted", {})
    if not scan.margin_rects:
        return JsonResponse({"error": "No margin rects"}, status=404)
    rects = json.loads(scan.margin_rects)
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
        Scan.objects.filter(pk=pk).update(margin_rects=json.dumps(rects))
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
    Scan.objects.filter(pk=pk).update(margin_rects=json.dumps(rects))
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
    if not scan.output_dir:
        return JsonResponse({"error": "No output directory"}, status=400)
    output_dir = get_output_base(scan)
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
        scan.opinions_json = json.dumps(opinions)
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
    if not scan.output_dir:
        return JsonResponse({"error": "No output directory"}, status=400)
    if not scan.opinions_json:
        return JsonResponse({"error": "No opinions paired yet"}, status=400)
    output_dir = scan.output_dir
    pdf_path = find_ocr_pdf(output_dir) or scan.pdf_path
    try:
        rects = _compute_and_save_redaction_rects(pk, pdf_path, output_dir)
        _compute_and_save_margin_rects(pk, pdf_path, output_dir)
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
    scan.queued_action = "generate_files"
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
    # Save to temp file then move — fitz can't save to the same path it opened
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
    data = json.loads(request.body)
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
    """Serve the redacted PDF for a scan, falling back to any available PDF.

    :param request: The HTTP request.
    :param pk: Scan primary key.
    :return: File response streaming the PDF.
    """
    scan = get_object_or_404(Scan, pk=pk)
    if scan.redacted_pdf_path and os.path.isfile(scan.redacted_pdf_path):
        return FileResponse(
            open(scan.redacted_pdf_path, "rb"), content_type="application/pdf"
        )
    if scan.output_dir:
    
        for f in sorted(Path(scan.output_dir).glob("*.pdf")):
            if "redacted" not in f.name and "bitonal" not in f.name:
                return FileResponse(
                    open(f, "rb"), content_type="application/pdf"
                )
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
        return JsonResponse(json.loads(scan.ocr_results), safe=False)
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
    data = json.loads(request.body)
    message = data.get("message", "").strip()
    page = data.get("page_number")
    metadata = data.get("metadata", {})
    if not message:
        return JsonResponse(
            {"status": "error", "message": "Message required"}, status=400
        )
    check_name = "process_flag"
    if metadata.get("type") == "suppress_detection":
        check_name = "suppress_detection"
    elif metadata.get("type") == "add_detection":
        check_name = "add_detection"
    elif metadata.get("type") == "approve_detection":
        check_name = "approve_detection"
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
            "process_flag",
            "suppress_detection",
            "add_detection",
            "approve_detection",
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
    data = json.loads(request.body)
    page_index = data["page_index"]
    label = data.get("label", "")
    label_id = data.get("label_id")
    bbox = data["bbox"]
    # Deactivate matching Detection(s) in DB — match on label string (reliable)
    # and fall back to label_id if label not provided
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
    output_base = get_output_base(scan)
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
    """Add or boost a single detection for a scan.

    :param request: The HTTP request (JSON body with page_index,
        label_id, bbox, img_width, and img_height).
    :param pk: Scan primary key.
    :return: JSON response indicating whether a new detection was added.
    """
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
            ).update(confidence=1.0, model_name="approved")
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
                model_name="manual",
                model_count=1,
                found_by=json.dumps([{"model": "manual", "confidence": 1.0}]),
            )
        except Exception:
            pass
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
    data = json.loads(request.body)
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
    output_base = get_output_base(scan)
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
    if not scan.output_dir:
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
