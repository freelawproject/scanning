import json
import os
import shutil
import tempfile
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


def _get_volume(reporter_slug, vol):
    """Helper to look up a Volume by reporter slug and number."""
    return get_object_or_404(
        Volume.objects.select_related("reporter", "assigned_to"),
        reporter__short_name=reporter_slug,
        volume_number=vol,
    )


@login_required
@require_POST
def claim_scan(request, reporter_slug, vol):
    """Claim or unclaim a volume for scanning."""
    volume = _get_volume(reporter_slug, vol)

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
    volume = _get_volume(reporter_slug, vol)

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

        def _bg_validate(scan_pk):
            import django
            import traceback as _tb
            django.db.connections.close_all()
            from pathlib import Path

            try:
                scan = Scan.objects.get(pk=scan_pk)
            except Exception as _e:
                _tb.print_exc()
                return
            try:
                print(f"[validate] scan {scan_pk} pdf_path={scan.pdf_path}", flush=True)
                if not scan.output_dir:
                    output_dir = Path(settings.MEDIA_ROOT) / "processed" / str(scan_pk)
                    if scan.reporter and scan.volume:
                        output_dir = (
                            output_dir / scan.reporter.short_name
                            / str(scan.volume) / str(scan.start_page or 1)
                        )
                    output_dir.mkdir(parents=True, exist_ok=True)
                    Scan.objects.filter(pk=scan_pk).update(output_dir=str(output_dir))
                else:
                    output_dir = Path(scan.output_dir)
                    output_dir.mkdir(parents=True, exist_ok=True)

                bitonal_path = output_dir / "bitonal.pdf"
                if not bitonal_path.exists():
                    from blackletter.api import bitonal as bl_bitonal
                    def _prog(current, total, message):
                        Scan.objects.filter(pk=scan_pk).update(
                            progress_message=message,
                            progress_current=current,
                            progress_total=total,
                        )
                    bl_bitonal(scan.pdf_path, str(output_dir), progress_callback=_prog)
                    pdf = fitz.open(str(bitonal_path))
                    count = pdf.page_count
                    pdf.close()
                    Scan.objects.filter(pk=scan_pk).update(page_count=count)

                _run_incremental_validation(scan_pk, str(bitonal_path))

            except Exception as exc:
                _tb.print_exc()
                print(f"[validate] ERROR: {exc}", flush=True)
                Scan.objects.filter(pk=scan_pk).update(
                    status=Status.ERROR, progress_message=str(exc)[:255],
                )

        t = threading.Thread(
            target=_bg_validate, args=(scan.pk,), daemon=True
        )
        t.start()
        # messages.success(
        #     request, "PDF uploaded — validation starting."
        # )
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
    volume = _get_volume(reporter_slug, vol)
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


# ---------------------------------------------------------------------------
# Processing: background helpers
# ---------------------------------------------------------------------------


class _Cancelled(Exception):
    pass


def _run_incremental_validation(scan_pk, pdf_path):
    """Validate page by page, saving results incrementally."""
    os.environ["PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK"] = "True"
    from blackletter.analyze import DEFAULT_ANALYZE_MODEL, _process_page
    from blackletter.models import Label
    from pathlib import Path as _P

    scan = Scan.objects.get(pk=scan_pk)
    total = scan.page_count
    if not total:
        pdf = fitz.open(pdf_path)
        total = pdf.page_count
        pdf.close()

    model_path = str(DEFAULT_ANALYZE_MODEL)
    exp_start = scan.start_page or 1
    exp_end = scan.end_page

    all_results = []
    all_detections = []

    for i in range(total):
        status = (
            Scan.objects.filter(pk=scan_pk)
            .values_list("status", flat=True)
            .first()
        )
        if status == Status.CANCELLED:
            return

        try:
            result = _process_page(
                (i, pdf_path, exp_start, exp_end, model_path)
            )
        except Exception as exc:
            import traceback

            print(f"  Page {i + 1} FAILED: {exc}", flush=True)
            traceback.print_exc()
            result = {
                "pdf_page": i + 1,
                "detected": None,
                "type": "error",
                "zone": "error",
                "score": 0,
                "ocr": "failed",
                "detections": [],
                "img_width": 0,
                "img_height": 0,
            }

        all_results.append(result)

        page_dets = result.get("detections", [])
        img_w = result.get("img_width", 0)
        img_h = result.get("img_height", 0)
        for d in page_dets:
            try:
                label_name = Label(d["label_id"]).name
            except (ValueError, KeyError):
                continue
            all_detections.append(
                Detection(
                    scan_id=scan_pk,
                    page_index=i,
                    label=label_name,
                    label_id=d["label_id"],
                    confidence=d["confidence"],
                    x0=d["bbox"][0],
                    y0=d["bbox"][1],
                    x1=d["bbox"][2],
                    y1=d["bbox"][3],
                    img_width=img_w,
                    img_height=img_h,
                    model_name="large",
                    model_count=1,
                    found_by=json.dumps(
                        [{"model": "large", "confidence": d["confidence"]}]
                    ),
                )
            )

        detected = result.get("detected")
        page_status = f"#{detected}" if detected else "no #"
        Scan.objects.filter(pk=scan_pk).update(
            progress_current=i + 1,
            progress_total=total,
            progress_message=f"Page {i + 1}/{total}: {page_status}",
            ocr_results=json.dumps(all_results),
        )

        if (i + 1) % 10 == 0 or i == total - 1:
            if all_detections:
                if i < 10:
                    Detection.objects.filter(scan_id=scan_pk).delete()
                Detection.objects.bulk_create(all_detections)
                all_detections = []

    if scan.output_dir:
        all_saved = Detection.objects.filter(scan_id=scan_pk).order_by(
            "page_index", "y0"
        )
        det_data = [
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
            for d in all_saved
        ]
        det_path = _P(scan.output_dir) / "detections.json"
        det_path.write_text(json.dumps(det_data))

    from blackletter.validate import _build_issues, _split_in_out_of_range, _auto_correct

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

    seq_issues = []
    prev_num = prev_pdf = None
    for r in all_results:
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
    scan.page_map = json.dumps(result.get("page_map", []))
    scan.missing_pages = json.dumps(result.get("missing_pages", []))
    scan.ocr_results = json.dumps(all_results)
    scan.has_issues = len(result.get("issues", [])) > 0
    scan.checked = True
    scan.status = Status.APPROVED
    scan.progress_message = "Done"
    scan.save()

    Issue.objects.filter(scan=scan).delete()
    Issue.objects.bulk_create(
        [Issue(scan=scan, **issue_data) for issue_data in result.get("issues", [])]
    )


def _do_recalculate(scan):
    """Rebuild issues from scan.ocr_results without re-running OCR."""
    import re as re_mod
    from collections import Counter
    from blackletter.validate import _parse_expected_range
    from blackletter.validate import _build_issues as _build_from_ocr

    ocr_results = json.loads(scan.ocr_results) if scan.ocr_results else []
    if not ocr_results:
        return

    exp_start, exp_end = _parse_expected_range(scan.pdf_path)

    def _split(results):
        oor, s = [], {}
        for r in results:
            if not r["detected"] or r.get("type") == "range":
                continue
            try:
                num = int(r["detected"])
            except ValueError:
                continue
            if num < 1:
                oor.append(r)
            elif exp_start is not None and (
                num < exp_start - 5 or num > exp_end + 5
            ):
                oor.append(r)
            else:
                s.setdefault(num, []).append(r["pdf_page"])
        return oor, s

    out_of_range, seen_nums = _split(ocr_results)

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
                    n_b
                    + (n_a - n_b) / max(pp_a - pp_b, 1) * (p - pp_b)
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
            modal_offset, modal_count = Counter(offset_vals).most_common(1)[
                0
            ]
            if modal_count >= len(offset_vals) * 0.5 and modal_offset != 0:
                to_fix = {
                    p
                    for p, (d, e, o) in offsets.items()
                    if o == modal_offset
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
                scan.ocr_results = json.dumps(ocr_results)
                out_of_range, seen_nums = _split(ocr_results)

    out_of_range_pages = {r["pdf_page"] for r in out_of_range}
    all_nums = sorted(seen_nums.keys())
    duplicates = {k: v for k, v in seen_nums.items() if len(v) > 1}

    prev_num = prev_pdf = None
    seq_issues = []
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

    range_re = re_mod.compile(r"^(\d{1,4})\s*[–\-]\s*(\d{1,4})$")
    range_pages = set()
    ranges_found = [r for r in ocr_results if r.get("type") == "range"]
    for r in ranges_found:
        m = range_re.match(r["detected"].replace("\u2013", "-"))
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

    analysis = {
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

    result = _build_from_ocr(
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

    pdf_fitz = fitz.open(scan.pdf_path)
    scan.page_count = len(pdf_fitz)
    pdf_fitz.close()

    scan.page_map = json.dumps(result["page_map"])
    scan.missing_pages = json.dumps(result["missing_pages"])
    scan.has_issues = len(result["issues"]) > 0
    scan.status = Status.APPROVED
    scan.progress_message = "Done"
    scan.save()

    scan.issues.all().delete()
    Issue.objects.bulk_create(
        [Issue(scan=scan, **i) for i in result["issues"]]
    )


def _run_reprocess_background(scan_pk):
    """Apply PDF fixes (inserts/deletions), rebuild, bitonal -> OCR -> validate."""
    import django
    django.db.connections.close_all()
    from pathlib import Path as _P

    scan = Scan.objects.get(pk=scan_pk)

    try:
        has_changes = scan.deletions.exists() or scan.inserts.exists()

        if has_changes:
            Scan.objects.filter(pk=scan_pk).update(
                progress_message="Applying fixes..."
            )

            pdf_doc = fitz.open(scan.pdf_path)
            page_map = json.loads(scan.page_map) if scan.page_map else []

            deleted_pdf_pages = sorted(d.pdf_page for d in scan.deletions.all())
            for pdf_page in sorted(deleted_pdf_pages, reverse=True):
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
                        (insert_before, entry["logical_number"], inserts[entry["logical_number"]])
                    )

            offset = 0
            for insert_before, logical_num, insert_obj in insert_ops:
                img_path = insert_obj.image.path
                if insert_before is not None:
                    adjusted = insert_before - len(
                        [d for d in deleted_pdf_pages if d <= insert_before]
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
                        insert_pdf, from_page=0,
                        to_page=insert_pdf.page_count - 1, start_at=pno,
                    )
                    offset += insert_pdf.page_count - 1
                    insert_pdf.close()
                else:
                    new_page = pdf_doc.new_page(pno=pno, width=w, height=h)
                    new_page.insert_image(new_page.rect, filename=img_path)
                offset += 1

            tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
            pdf_doc.save(tmp.name, deflate=True)
            pdf_doc.close()
            shutil.move(tmp.name, scan.pdf_path)

            scan.deletions.all().delete()
            scan.inserts.all().delete()
            scan.save()

        if scan.output_dir:
            output_dir = _P(scan.output_dir)
            for old_file in output_dir.glob("*.pdf"):
                if old_file.name != _P(scan.pdf_path).name:
                    old_file.unlink(missing_ok=True)
            for old_file in output_dir.glob("*.json"):
                old_file.unlink(missing_ok=True)

        Detection.objects.filter(scan_id=scan_pk).delete()

        Scan.objects.filter(pk=scan_pk).update(
            ocr_results="", opinions_json="", page_map="", missing_pages="",
        )

        scan.refresh_from_db()
        if not scan.output_dir:
            output_dir = _P(settings.MEDIA_ROOT) / "processed" / str(scan_pk)
            output_dir.mkdir(parents=True, exist_ok=True)
            Scan.objects.filter(pk=scan_pk).update(output_dir=str(output_dir))
        else:
            output_dir = _P(scan.output_dir)
            output_dir.mkdir(parents=True, exist_ok=True)

        Scan.objects.filter(pk=scan_pk).update(progress_message="Converting to bitonal...")
        from blackletter.api import bitonal as bl_bitonal

        def _bitonal_progress(current, total, message):
            Scan.objects.filter(pk=scan_pk).update(
                progress_message=message, progress_current=current, progress_total=total,
            )

        bitonal_path = bl_bitonal(
            scan.pdf_path, str(output_dir), progress_callback=_bitonal_progress,
        )

        pdf = fitz.open(str(bitonal_path))
        Scan.objects.filter(pk=scan_pk).update(page_count=pdf.page_count)
        pdf.close()

        scan.refresh_from_db()
        from blackletter.api import ocr as bl_ocr
        Scan.objects.filter(pk=scan_pk).update(progress_message="Running Tesseract OCR...")
        ocr_path = bl_ocr(
            str(bitonal_path), str(output_dir),
            reporter=scan.reporter.short_name or "",
            volume=str(scan.volume) or "",
            first_page=scan.start_page or 1,
        )

        _run_incremental_validation(scan_pk, str(ocr_path))

    except _Cancelled:
        pass
    except Exception as exc:
        import traceback
        traceback.print_exc()
        Scan.objects.filter(pk=scan_pk).update(
            status=Status.ERROR, progress_message=str(exc)[:255],
        )


def _run_generate_background(scan_pk):
    """Generate redacted/split files from existing detections."""
    import subprocess
    import sys
    os.environ["PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK"] = "True"

    import django
    django.db.connections.close_all()
    from pathlib import Path

    scan = Scan.objects.get(pk=scan_pk)

    try:
        Scan.objects.filter(pk=scan_pk).update(progress_message="Generating files...", progress_log="")

        reporter = scan.reporter.short_name
        volume = str(scan.volume)
        first_page = scan.start_page or 1

        output_base = Path(settings.MEDIA_ROOT) / "processed" / str(scan_pk)

        ocr_pdf = None
        if scan.output_dir:
            for f in sorted(Path(scan.output_dir).glob("*.pdf")):
                if "redacted" not in f.name and "bitonal" not in f.name and not f.name.endswith(".original.pdf"):
                    ocr_pdf = f
                    break
        if not ocr_pdf:
            raise ValueError("No OCR'd PDF found in output directory")

        suppression_excluded = set()
        for issue in scan.issues.filter(check_name="suppress_detection"):
            if issue.metadata:
                try:
                    meta = json.loads(issue.metadata)
                    bbox = meta.get("bbox", [0, 0, 0, 0])
                    suppression_excluded.add((
                        meta.get("page_index", 0), meta.get("label_id", 0),
                        round(bbox[0]), round(bbox[1]),
                    ))
                except Exception:
                    pass
        suppression_excluded_json = json.dumps(list(suppression_excluded))

        script = f"""
import os, json
os.environ["PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK"] = "True"
from pathlib import Path
from blackletter.process import generate_files

excluded_raw = json.loads('{suppression_excluded_json}')
excluded = set(tuple(e) for e in excluded_raw) if excluded_raw else None

generate_files(
    ocr_pdf="{ocr_pdf}",
    output=Path("{scan.output_dir if scan.output_dir else output_base}"),
    reporter="{reporter}",
    volume="{volume}",
    first_page={first_page},
    unredacted=True,
    excluded=excluded,
)
"""
        log_path = output_base / "generate.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        proc = subprocess.Popen(
            [sys.executable, "-c", script],
            stdout=open(log_path, "w"),
            stderr=subprocess.STDOUT,
            cwd=str(Path(settings.INSTALL_ROOT)),
        )

        import re as _re
        import time
        _progress_re = _re.compile(r"(\d+(?:\.\d+)?)\s*/\s*(\d+)")
        while proc.poll() is None:
            time.sleep(1)
            try:
                log_text = log_path.read_text(errors="replace").replace("\x00", "")
                lines = [l for l in log_text.strip().split("\n") if l.strip()]
                msg = lines[-1].strip() if lines else "Generating..."
                current, total = 0, 0
                for l in reversed(lines):
                    m = _progress_re.search(l)
                    if m:
                        current = int(float(m.group(1)))
                        total = int(m.group(2))
                        break
                Scan.objects.filter(pk=scan_pk).update(
                    progress_message=msg[:255], progress_log=log_text[-5000:],
                    progress_current=current, progress_total=total,
                )
            except Exception:
                pass

        log_text = log_path.read_text(errors="replace").replace("\x00", "")
        if proc.returncode != 0:
            raise RuntimeError(
                f"generate_files failed (exit {proc.returncode}): {log_text[-500:]}"
            )

        output_dir = output_base
        for d in sorted(output_base.rglob("redacted"), key=lambda p: len(p.parts)):
            output_dir = d.parent
            break

        redacted_dir = output_dir / "redacted"
        redacted_files = sorted(redacted_dir.glob("*.pdf")) if redacted_dir.is_dir() else []

        scan.refresh_from_db()
        existing_opinions = json.loads(scan.opinions_json) if scan.opinions_json else []

        if existing_opinions and "caption_page" in existing_opinions[0]:
            for i, op in enumerate(existing_opinions):
                if i < len(redacted_files):
                    op["filename"] = redacted_files[i].name
        else:
            existing_opinions = []
            for f in redacted_files:
                existing_opinions.append({"filename": f.name, "first_page": 0, "last_page": 0})

        redacted_pdf = list(output_dir.glob("*.redacted.pdf"))

        scan.output_dir = str(output_dir)
        scan.redacted_pdf_path = str(redacted_pdf[0]) if redacted_pdf else ""
        scan.opinions_json = json.dumps(existing_opinions)
        scan.stage = Stage.APPROVED
        scan.status = Status.APPROVED
        scan.progress_message = f"Generated {len(existing_opinions)} opinions"
        scan.progress_log = log_text[-5000:]
        scan.save()

        OpinionScan.objects.filter(scan=scan).delete()
        LLMScan.objects.filter(scan=scan).delete()
        unredacted_dir = output_dir / "unredacted"
        masked_dir = output_dir / "masked"
        for i, op in enumerate(existing_opinions):
            page_start = op.get("caption_page", op.get("first_page", 0))
            if isinstance(page_start, int) and "caption_page" in op:
                page_start += scan.start_page or 1
            page_end = op.get("key_page", op.get("last_page", 0))
            if isinstance(page_end, int) and "key_page" in op:
                page_end += (scan.start_page or 1) + op.get("page_count", 1) - 1
            fname = op.get("filename", "")
            opinion = OpinionScan.objects.create(
                scan=scan, reporter=scan.reporter, volume=scan.volume,
                opinion_order=i, page_start=page_start or 1,
                page_end=page_end or page_start or 1,
                caption_page_index=op.get("caption_page"),
                key_page_index=op.get("key_page"),
                has_image=op.get("has_image", False),
                status=OpinionStatus.OK, uploaded_by=scan.uploaded_by,
            )
            if fname:
                rp = redacted_dir / fname
                if rp.exists():
                    opinion.redacted_pdf.name = str(rp)
                up = unredacted_dir / fname if unredacted_dir.exists() else None
                if up and up.exists():
                    opinion.original_pdf.name = str(up)
                mp = masked_dir / fname if masked_dir.exists() else None
                if mp and mp.exists():
                    opinion.masked_pdf.name = str(mp)
                opinion.save()

            if opinion.masked_pdf.name:
                llm_scan = LLMScan.objects.create(
                    scan=scan, masked_pdf=opinion.masked_pdf.name,
                    status=LLMScan.Status.PENDING,
                )
                llm_scan.opinions.add(opinion)

    except Exception as exc:
        import traceback
        traceback.print_exc()
        Scan.objects.filter(pk=scan_pk).update(
            status=Status.ERROR, progress_message=str(exc)[:255],
        )


# ---------------------------------------------------------------------------
# Processing views
# ---------------------------------------------------------------------------


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
            s.redacted_filename = (
                os.path.basename(s.redacted_pdf.name) if s.redacted_pdf.name else ""
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
        from pathlib import Path as _P
        out = _P(scan.output_dir)
        for f in sorted(out.glob("*.pdf")):
            if f.name not in ("bitonal.pdf",) and not f.name.endswith(".redacted.pdf") and not f.name.endswith(".original.pdf"):
                return FileResponse(open(f, "rb"), content_type="application/pdf")
        bitonal = out / "bitonal.pdf"
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

    def _validate_with_bitonal(scan_pk):
        import django
        import traceback as _tb
        django.db.connections.close_all()
        from pathlib import Path

        scan = Scan.objects.get(pk=scan_pk)
        try:
            if not scan.output_dir:
                output_dir = Path(settings.MEDIA_ROOT) / "processed" / str(scan_pk)
                if scan.reporter and scan.volume:
                    output_dir = (
                        output_dir / scan.reporter.short_name
                        / str(scan.volume) / str(scan.start_page or 1)
                    )
                output_dir.mkdir(parents=True, exist_ok=True)
                Scan.objects.filter(pk=scan_pk).update(output_dir=str(output_dir))
            else:
                output_dir = Path(scan.output_dir)
                output_dir.mkdir(parents=True, exist_ok=True)

            bitonal_path = output_dir / "bitonal.pdf"
            if not bitonal_path.exists():
                from blackletter.api import bitonal as bl_bitonal

                def _bitonal_progress(current, total, message):
                    Scan.objects.filter(pk=scan_pk).update(
                        progress_message=message,
                        progress_current=current,
                        progress_total=total,
                    )

                bl_bitonal(scan.pdf_path, str(output_dir), progress_callback=_bitonal_progress)
                pdf = fitz.open(str(bitonal_path))
                count = pdf.page_count
                pdf.close()
                Scan.objects.filter(pk=scan_pk).update(page_count=count)

            _run_incremental_validation(scan_pk, str(bitonal_path))

        except Exception as exc:
            _tb.print_exc()
            print(f"[validate] ERROR: {exc}", flush=True)
            Scan.objects.filter(pk=scan_pk).update(
                status=Status.ERROR, progress_message=str(exc)[:255],
            )

    t = threading.Thread(target=_validate_with_bitonal, args=(scan.pk,), daemon=True)
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

    def _run_detect(scan_pk):
        import subprocess
        import sys
        import django
        import time as _time
        django.db.connections.close_all()
        from pathlib import Path as _P

        scan = Scan.objects.get(pk=scan_pk)
        try:
            output_dir = _P(scan.output_dir)
            bitonal = output_dir / "bitonal.pdf"

            ocr_exists = any(
                f.name not in ("bitonal.pdf",) and not f.name.endswith(".redacted.pdf") and not f.name.endswith(".original.pdf")
                for f in output_dir.glob("*.pdf")
            )
            if not ocr_exists and bitonal.exists():
                def _run_ocr_bg(scan_pk, bitonal_str, output_dir_str):
                    try:
                        import django as _dj
                        _dj.db.connections.close_all()
                        _scan = Scan.objects.get(pk=scan_pk)
                        from blackletter.api import ocr as _ocr
                        _ocr(
                            bitonal_str, output_dir_str,
                            reporter=_scan.reporter.short_name or "",
                            volume=str(_scan.volume) or "",
                            first_page=_scan.start_page or 1,
                        )
                    except Exception as _e:
                        print(f"  Background OCR failed: {_e}", flush=True)
                threading.Thread(
                    target=_run_ocr_bg,
                    args=(scan_pk, str(bitonal), str(output_dir)),
                    daemon=True,
                ).start()

            pdf_path = str(bitonal) if bitonal.exists() else scan.pdf_path

            # Run YOLO detection as a subprocess so stdout progress is captured
            script = f"""
import os
os.environ["PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK"] = "True"
from blackletter.api import detect as bl_detect
dets = bl_detect("{pdf_path}", "{output_dir}", models=["small", "medium", "large"])
print(f"\\nDetect complete: {{len(dets)}} detections", flush=True)
"""
            log_path = _P(settings.MEDIA_ROOT) / "processed" / str(scan_pk) / "detect.log"
            log_path.parent.mkdir(parents=True, exist_ok=True)
            Scan.objects.filter(pk=scan_pk).update(
                progress_message="Running YOLO detection...",
                progress_log="",
            )
            proc = subprocess.Popen(
                [sys.executable, "-c", script],
                stdout=open(log_path, "w"),
                stderr=subprocess.STDOUT,
                cwd=str(_P(settings.INSTALL_ROOT)),
            )
            while proc.poll() is None:
                _time.sleep(1)
                try:
                    log_text = log_path.read_text(errors="replace").replace("\x00", "")
                    lines = [l for l in log_text.strip().split("\n") if l.strip()]
                    msg = lines[-1].strip() if lines else "Running YOLO detection..."
                    Scan.objects.filter(pk=scan_pk).update(
                        progress_message=msg[:255],
                        progress_log=log_text[-5000:],
                    )
                except Exception:
                    pass

            log_text = log_path.read_text(errors="replace").replace("\x00", "")
            if proc.returncode != 0:
                raise RuntimeError(
                    f"YOLO detection failed (exit {proc.returncode}): {log_text[-500:]}"
                )

            # Load detections from detections.json written by subprocess
            det_path = output_dir / "detections.json"
            dets = json.loads(det_path.read_text())

            Detection.objects.filter(scan_id=scan_pk).delete()
            from blackletter.models import Label
            det_objects = []
            for d in dets:
                try:
                    label_name = Label(d["label_id"]).name
                except (ValueError, KeyError):
                    label_name = d.get("label", "UNKNOWN")
                det_objects.append(Detection(
                    scan_id=scan_pk, page_index=d["page_index"],
                    label=label_name, label_id=d["label_id"],
                    confidence=d["confidence"],
                    x0=d["bbox"][0], y0=d["bbox"][1],
                    x1=d["bbox"][2], y1=d["bbox"][3],
                    img_width=d.get("img_width", 0),
                    img_height=d.get("img_height", 0),
                    model_name=d.get("found_by", [{}])[0].get("model", ""),
                    model_count=d.get("model_count", 1),
                    found_by=json.dumps(d.get("found_by", [])),
                ))
            Detection.objects.bulk_create(det_objects)

            pair_log = log_text + f"\n{len(dets)} detections saved. Pairing opinions..."
            Scan.objects.filter(pk=scan_pk).update(
                progress_message=f"{len(dets)} detections. Pairing opinions...",
                progress_log=pair_log[-5000:],
            )

            from blackletter.api import pair as bl_pair
            opinions = bl_pair(
                str(det_path), pdf_path,
                reporter=scan.reporter.short_name or "",
                volume=str(scan.volume) or "",
                first_page=scan.start_page or 1,
            )

            done_log = pair_log + f"\nPairing complete: {len(opinions)} opinions."
            Scan.objects.filter(pk=scan_pk).update(
                opinions_json=json.dumps(opinions),
                status=Status.APPROVED,
                progress_message=f"Done — {len(dets)} detections, {len(opinions)} opinions",
                progress_log=done_log[-5000:],
            )

        except Exception as exc:
            import traceback
            traceback.print_exc()
            Scan.objects.filter(pk=scan_pk).update(
                status=Status.ERROR, progress_message=str(exc)[:255],
            )

    t = threading.Thread(target=_run_detect, args=(scan.pk,), daemon=True)
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
    _do_recalculate(scan)
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
    t = threading.Thread(target=_run_reprocess_background, args=(scan.pk,), daemon=True)
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
    if issue and issue.severity == "error":
        return JsonResponse(
            {"status": "error", "message": "Cannot dismiss errors -- fix the issue first."},
            status=400,
        )
    Issue.objects.filter(pk=issue_id, scan=scan).exclude(severity="error").delete()
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
    from pathlib import Path as _Path
    scan = get_object_or_404(Scan, pk=pk)
    output_base = _Path(scan.output_dir) if scan.output_dir else None
    if not output_base:
        output_base = _Path(settings.MEDIA_ROOT) / "processed" / str(pk)
    for candidate in [
        output_base / "opinions.json",
        output_base.parent / "opinions.json",
        output_base.parent.parent / "opinions.json",
    ]:
        if candidate.exists():
            return JsonResponse(json.loads(candidate.read_text()), safe=False)
    return JsonResponse([], safe=False)


@login_required
def serve_margin_rects(request, pk):
    from pathlib import Path as _Path
    scan = get_object_or_404(Scan, pk=pk)
    output_base = _Path(scan.output_dir) if scan.output_dir else None
    if not output_base:
        output_base = _Path(settings.MEDIA_ROOT) / "processed" / str(pk)
    for candidate in [
        output_base / "margin_rects.json",
        output_base.parent / "margin_rects.json",
        output_base.parent.parent / "margin_rects.json",
    ]:
        if candidate.exists():
            return JsonResponse(json.loads(candidate.read_text()), safe=False)
    ocr_pdf = None
    if output_base.is_dir():
        for f in sorted(output_base.rglob("*.pdf")):
            if f.is_file() and "redacted" not in f.name and "bitonal" not in f.name:
                ocr_pdf = f
                break
    if not ocr_pdf:
        return JsonResponse([], safe=False)
    from blackletter.margins import compute_margin_rects
    rects = compute_margin_rects(ocr_pdf)
    return JsonResponse(rects, safe=False)


@login_required
def serve_redaction_rects(request, pk):
    from pathlib import Path as _Path
    scan = get_object_or_404(Scan, pk=pk)
    output_base = _Path(scan.output_dir) if scan.output_dir else None
    if not output_base:
        output_base = _Path(settings.MEDIA_ROOT) / "processed" / str(pk)
    for candidate in [
        output_base / "redaction_rects.json",
        output_base.parent / "redaction_rects.json",
        output_base.parent.parent / "redaction_rects.json",
    ]:
        if candidate.exists():
            return JsonResponse(json.loads(candidate.read_text()), safe=False)
    return JsonResponse([], safe=False)


@login_required
@require_POST
def save_redaction_rect(request, pk):
    from pathlib import Path as _Path
    scan = get_object_or_404(Scan, pk=pk)
    data = json.loads(request.body)
    page_idx = data["page_index"]
    action = data.get("action", "update")
    original = data.get("original", {})
    adjusted = data.get("adjusted", {})
    rect_type = data.get("type", "")
    fill = data.get("fill", "black")
    output_base = _Path(scan.output_dir) if scan.output_dir else None
    if not output_base:
        output_base = _Path(settings.MEDIA_ROOT) / "processed" / str(pk)
    rects_path = None
    for candidate in [
        output_base / "redaction_rects.json",
        output_base.parent / "redaction_rects.json",
        output_base.parent.parent / "redaction_rects.json",
    ]:
        if candidate.exists():
            rects_path = candidate
            break
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
    from pathlib import Path as _Path
    scan = get_object_or_404(Scan, pk=pk)
    data = json.loads(request.body)
    page_idx = data["page_index"]
    original = data.get("original", {})
    action = data.get("action", "update")
    adjusted = data.get("adjusted", {})
    output_base = _Path(scan.output_dir) if scan.output_dir else None
    if not output_base:
        output_base = _Path(settings.MEDIA_ROOT) / "processed" / str(pk)
    rects_path = None
    for candidate in [output_base / "margin_rects.json", output_base.parent / "margin_rects.json"]:
        if candidate.exists():
            rects_path = candidate
            break
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
    from pathlib import Path as _P
    scan = get_object_or_404(Scan, pk=pk)
    if not scan.output_dir:
        return JsonResponse({"error": "No output directory"}, status=400)
    output_dir = _P(scan.output_dir)
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
        opinions_data = json.loads(scan.opinions_json)
        (output_dir / "opinions.json").write_text(json.dumps(opinions_data))
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
def refine_redactions_api(request, pk):
    """Run docTR refinement in background on the small OCR'd PDF, updating redaction_rects.json."""
    import subprocess
    import sys
    import threading
    from pathlib import Path as _P

    scan = get_object_or_404(Scan, pk=pk)
    if not scan.output_dir:
        return JsonResponse({"error": "No output directory"}, status=400)

    output_dir = _P(scan.output_dir)
    rects_path = output_dir / "redaction_rects.json"
    if not rects_path.exists():
        return JsonResponse({"error": "Run Compute Redactions first"}, status=400)

    # Pick the small OCR'd PDF (same logic as serve_scan_pdf)
    pdf_path = None
    for f in sorted(output_dir.glob("*.pdf")):
        if f.name not in ("bitonal.pdf",) and not f.name.endswith(".redacted.pdf") and not f.name.endswith(".original.pdf"):
            pdf_path = str(f)
            break
    if not pdf_path:
        return JsonResponse({"error": "No OCR'd PDF found"}, status=400)

    scan_pk = scan.pk

    def _run():
        import django
        django.db.connections.close_all()
        from pathlib import Path
        log_path = Path(settings.MEDIA_ROOT) / "processed" / str(scan_pk) / "refine.log"
        script = f"""
import os
os.environ["PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK"] = "True"
import json
from pathlib import Path
from blackletter.refine import refine_headnote_rects
from blackletter.models import Page, BBox, Detection as BLDetection, Label
import fitz

rects_path = Path("{rects_path}")
rects_data = json.loads(rects_path.read_text())

# Extract headnote rects (in pixel coords) per page
from blackletter.scanner import _pair_opinions
# Rebuild pages from detections json
dets_path = Path("{output_dir}") / "detections.json"
ops_path = Path("{output_dir}") / "opinions.json"
if not dets_path.exists() or not ops_path.exists():
    print("No detections.json/opinions.json — cannot refine", flush=True)
    raise SystemExit(0)

det_data = json.loads(dets_path.read_text())
src_pdf = fitz.open("{pdf_path}")
pages_data = {{}}
for entry in det_data:
    pi = entry["page_index"]
    if pi not in pages_data:
        pages_data[pi] = {{"img_width": entry.get("img_width", 1), "img_height": entry.get("img_height", 1), "detections": []}}
    pages_data[pi]["detections"].append(entry)
pages = []
for pi in sorted(pages_data.keys()):
    pd = pages_data[pi]
    pw, ph = (src_pdf[pi].rect.width, src_pdf[pi].rect.height) if pi < src_pdf.page_count else (612.0, 792.0)
    page = Page(index=pi, pdf_width=pw, pdf_height=ph, img_width=pd["img_width"], img_height=pd["img_height"])
    for d in pd["detections"]:
        b = d.get("bbox", [0,0,1,1])
        page.detections.append(BLDetection(bbox=BBox(x1=b[0],y1=b[1],x2=b[2],y2=b[3]), label=Label(d["label_id"]), confidence=d["confidence"], page_index=pi))
    pages.append(page)
pages_by_index = {{p.index: p for p in pages}}

from blackletter.models import Document as BLDoc
document = BLDoc(pdf_path="{pdf_path}", pages=pages, reporter="", volume="", first_page=1, ocr_applied=True)
opinions = _pair_opinions(document)

# Collect headnote rects (PDF point coords) same as compute_redaction_rects does
import fitz as _fitz
from blackletter.scanner import _find_redaction_end, _find_redaction_start
from blackletter.process import _redaction_rects, _headnote_fallback_rects
mid = pages[0].midpoint if pages else 0.5
all_headnote_rects = []
for caption, key in opinions:
    opinion_dets = []
    for p in pages:
        for d in p.detections:
            sk = d.sort_key(mid)
            if caption.sort_key(mid) <= sk <= key.sort_key(mid):
                opinion_dets.append(d)
    opinion_dets.sort(key=lambda d: d.sort_key(mid))
    end_marker = _find_redaction_end(opinion_dets, caption, key, mid, reporter="")
    if end_marker is not None:
        start = _find_redaction_start(opinion_dets, caption, mid)
        all_headnote_rects.extend(_redaction_rects(caption, end_marker, pages_by_index, start_marker=start if start is not caption else None))
    else:
        all_headnote_rects.extend(_headnote_fallback_rects(opinion_dets, caption, pages_by_index, mid))

print(f"Refining {{len(all_headnote_rects)}} headnote rects on {{len(set(pi for pi,_ in all_headnote_rects))}} pages...", flush=True)

# Group headnote rects by page
by_page = {{}}
for pi, rect in all_headnote_rects:
    by_page.setdefault(pi, []).append(rect)

# Start with existing non-headnote rects
rects_by_page = {{entry["page_index"]: [r for r in entry["rects"] if r.get("type") != "headnote"] for entry in rects_data}}

from blackletter.refine import _get_doctr_model, _refine_single_rect
import numpy as np
_SCALE = 200 / 72
det_model = _get_doctr_model()
mat = fitz.Matrix(_SCALE, _SCALE)

total_pages = len(by_page)
done = 0
for pi, rects in sorted(by_page.items()):
    done += 1
    fitz_page = src_pdf[pi]
    pix = fitz_page.get_pixmap(matrix=mat)
    page_img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, 3)
    p = pages_by_index.get(pi)
    sx = p.scale_x if p else 1.0
    sy = p.scale_y if p else 1.0
    for rect in rects:
        px_rect = (rect.x0 * _SCALE, rect.y0 * _SCALE, rect.x1 * _SCALE, rect.y1 * _SCALE)
        refined_px = _refine_single_rect(det_model, page_img, px_rect)
        for rpx in refined_px:
            rects_by_page.setdefault(pi, []).append({{
                "x0": round(rpx[0] / _SCALE / sx, 1), "y0": round(rpx[1] / _SCALE / sy, 1),
                "x1": round(rpx[2] / _SCALE / sx, 1), "y1": round(rpx[3] / _SCALE / sy, 1),
                "fill": "black", "type": "headnote"
            }})
    # Write incrementally after each page
    result = [{{"page_index": k, "rects": v}} for k, v in sorted(rects_by_page.items())]
    rects_path.write_text(json.dumps(result))
    print(f"docTR: {{done}}/{{total_pages}} pages", flush=True)

total = sum(len(v) for v in rects_by_page.values())
print(f"Refinement complete: {{total}} rects", flush=True)
"""
        proc = subprocess.Popen(
            [sys.executable, "-c", script],
            stdout=open(log_path, "w"),
            stderr=subprocess.STDOUT,
            cwd=str(_P(settings.INSTALL_ROOT)),
        )
        import time as _time
        while proc.poll() is None:
            _time.sleep(2)
            try:
                log_text = log_path.read_text(errors="replace").replace("\x00", "")
                lines = [l for l in log_text.strip().split("\n") if l.strip()]
                msg = lines[-1].strip() if lines else "Refining..."
                Scan.objects.filter(pk=scan_pk).update(progress_message=msg[:255])
            except Exception:
                pass
        Scan.objects.filter(pk=scan_pk).update(progress_message="")

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return JsonResponse({"status": "ok", "refining": True})


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
    t = threading.Thread(target=_run_generate_background, args=(scan.pk,), daemon=True)
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
def redetect_page(request, pk):
    from pathlib import Path as _P
    from PIL import Image
    from ultralytics import YOLO
    from blackletter.models import Label
    scan = get_object_or_404(Scan, pk=pk)
    data = json.loads(request.body)
    page_idx = data.get("page_index", 0)
    model_name = data.get("model", "large")
    action = data.get("action", "preview")
    output_base = _P(scan.output_dir) if scan.output_dir else _P(settings.MEDIA_ROOT) / "processed" / str(pk)
    ocr_pdf = None
    for f in sorted(output_base.rglob("*.pdf")):
        if "redacted" not in f.name and "bitonal" not in f.name:
            ocr_pdf = f
            break
    if not ocr_pdf:
        return JsonResponse({"error": "No PDF found"}, status=404)
    model_map = {"small": "small.pt", "medium": "medium.pt", "large": "large.pt"}
    import blackletter as _bl
    models_dir = _P(_bl.__file__).parent / "models"
    model_file = models_dir / model_map.get(model_name, "large.pt")
    model = YOLO(str(model_file))
    pdf = fitz.open(str(ocr_pdf))
    DPI = 200
    mat = fitz.Matrix(DPI / 72, DPI / 72)
    page = pdf[page_idx]
    pix = page.get_pixmap(matrix=mat)
    img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    results = model([img], conf=0.20, verbose=False)
    new_dets = []
    for box in results[0].boxes:
        class_id = int(box.cls[0].item())
        try:
            label_name = Label(class_id).name
        except ValueError:
            label_name = f"UNKNOWN_{class_id}"
        new_dets.append({
            "page_index": page_idx, "label": label_name, "label_id": class_id,
            "confidence": round(float(box.conf[0].item()), 3),
            "bbox": [round(v, 1) for v in box.xyxy[0].tolist()],
            "img_width": pix.width, "img_height": pix.height,
        })
    pdf.close()
    if action == "preview":
        return JsonResponse({"detections": new_dets, "model": model_name, "page_index": page_idx})
    det_path = None
    for candidate in [output_base / "detections.json", output_base.parent / "detections.json"]:
        if candidate.exists():
            det_path = candidate
            break
    if not det_path:
        return JsonResponse({"error": "No detections.json"}, status=404)
    existing = json.loads(det_path.read_text())
    if action == "accept":
        existing = [d for d in existing if d["page_index"] != page_idx]
        existing.extend(new_dets)
        det_path.write_text(json.dumps(existing))
        return JsonResponse({"status": "ok", "replaced": len(new_dets), "page_index": page_idx})
    elif action == "accept_new":
        page_existing = [d for d in existing if d["page_index"] == page_idx]
        added = 0
        for nd in new_dets:
            is_dup = False
            for e in page_existing:
                if e["label_id"] == nd["label_id"]:
                    if abs(e["bbox"][0] - nd["bbox"][0]) < 15 and abs(e["bbox"][1] - nd["bbox"][1]) < 15:
                        is_dup = True
                        break
            if not is_dup:
                existing.append(nd)
                added += 1
        det_path.write_text(json.dumps(existing))
        return JsonResponse({"status": "ok", "added": added, "found": len(new_dets), "page_index": page_idx})
    return JsonResponse({"error": "Unknown action"}, status=400)


@login_required
@require_POST
def add_single_detection(request, pk):
    from pathlib import Path as _P
    scan = get_object_or_404(Scan, pk=pk)
    det = json.loads(request.body)
    output_base = _P(scan.output_dir) if scan.output_dir else _P(settings.MEDIA_ROOT) / "processed" / str(pk)
    det_path = None
    for candidate in [output_base / "detections.json", output_base.parent / "detections.json"]:
        if candidate.exists():
            det_path = candidate
            break
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
def scan_for_label(request, pk):
    from pathlib import Path as _P
    from PIL import Image
    from ultralytics import YOLO
    from blackletter.models import Label
    scan = get_object_or_404(Scan, pk=pk)
    data = json.loads(request.body)
    label_name = data.get("label", "STATE_ABBREVIATION")
    model_name = data.get("model", "large")
    page_start = data.get("page_start", 0)
    page_end = data.get("page_end", None)
    output_base = _P(scan.output_dir) if scan.output_dir else _P(settings.MEDIA_ROOT) / "processed" / str(pk)
    ocr_pdf = None
    for f in sorted(output_base.rglob("*.pdf")):
        if "redacted" not in f.name and "bitonal" not in f.name:
            ocr_pdf = f
            break
    if not ocr_pdf:
        return JsonResponse({"error": "No PDF found"}, status=404)
    model_map = {"small": "small.pt", "medium": "medium.pt", "large": "large.pt"}
    import blackletter as _bl
    models_dir = _P(_bl.__file__).parent / "models"
    model_file = models_dir / model_map.get(model_name, "large.pt")
    model = YOLO(str(model_file))
    target_label = None
    for lbl in Label:
        if lbl.name == label_name:
            target_label = lbl
            break
    if target_label is None:
        return JsonResponse({"error": f"Unknown label: {label_name}"}, status=400)
    pdf = fitz.open(str(ocr_pdf))
    if page_end is None:
        page_end = len(pdf)
    page_end = min(page_end, len(pdf))
    DPI = 200
    mat = fitz.Matrix(DPI / 72, DPI / 72)
    new_detections = []
    for pi in range(page_start, page_end):
        page = pdf[pi]
        pix = page.get_pixmap(matrix=mat)
        img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        results = model([img], conf=0.20, verbose=False)
        for box in results[0].boxes:
            class_id = int(box.cls[0].item())
            if class_id == int(target_label):
                bbox = box.xyxy[0].tolist()
                conf = float(box.conf[0].item())
                new_detections.append({
                    "page_index": pi, "label": target_label.name,
                    "label_id": int(target_label), "confidence": round(conf, 3),
                    "bbox": [round(v, 1) for v in bbox],
                    "img_width": pix.width, "img_height": pix.height,
                })
    pdf.close()
    if not new_detections:
        return JsonResponse({"added": 0, "message": f"No {label_name} found"})
    det_path = None
    for candidate in [output_base / "detections.json", output_base.parent / "detections.json"]:
        if candidate.exists():
            det_path = candidate
            break
    if not det_path:
        return JsonResponse({"error": "No detections.json"}, status=404)
    existing = json.loads(det_path.read_text())
    added = 0
    for nd in new_detections:
        is_dup = False
        for e in existing:
            if e["page_index"] != nd["page_index"]:
                continue
            if e["label_id"] != nd["label_id"]:
                continue
            if abs(e["bbox"][0] - nd["bbox"][0]) < 15 and abs(e["bbox"][1] - nd["bbox"][1]) < 15:
                is_dup = True
                break
        if not is_dup:
            existing.append(nd)
            added += 1
    det_path.write_text(json.dumps(existing))
    return JsonResponse({
        "added": added, "scanned_pages": page_end - page_start,
        "found": len(new_detections),
        "message": f"Added {added} new {label_name} detections ({len(new_detections)} found, {len(new_detections) - added} duplicates skipped)",
    })


@login_required
@require_POST
def repare_opinions(request, pk):
    return JsonResponse({"error": "disabled"}, status=503)
    output_base = _P(scan.output_dir) if scan.output_dir else _P(settings.MEDIA_ROOT) / "processed" / str(pk)
    det_path = None
    for candidate in [output_base / "detections.json", output_base.parent / "detections.json",
                      output_base.parent.parent / "detections.json"]:
        if candidate.exists():
            det_path = candidate
            break
    if not det_path:
        return JsonResponse({"error": "No detections found"}, status=404)
    ocr_pdf = None
    search_dir = output_base if output_base.is_dir() else output_base.parent
    for f in sorted(search_dir.rglob("*.pdf")):
        if "redacted" not in f.name and "bitonal" not in f.name:
            ocr_pdf = f
            break
    if not ocr_pdf:
        return JsonResponse({"error": "No OCR PDF found"}, status=404)
    from blackletter.models import BBox, Detection as BLDetection, Document as BLDoc, Label, Page
    from blackletter.scanner import _pair_opinions
    raw = json.loads(det_path.read_text())
    for issue in scan.issues.filter(check_name="add_detection"):
        if issue.metadata:
            try:
                meta = json.loads(issue.metadata)
                raw.append({
                    "page_index": meta.get("page_index", 0),
                    "label": Label(meta["label_id"]).name, "label_id": meta["label_id"],
                    "confidence": 1.0, "bbox": meta.get("bbox", [0, 0, 1, 1]),
                    "img_width": meta.get("img_width", 1), "img_height": meta.get("img_height", 1),
                })
            except Exception:
                pass
    src_pdf = fitz.open(str(ocr_pdf))
    page_dims = {}
    for i in range(len(src_pdf)):
        r = src_pdf.load_page(i).rect
        page_dims[i] = (r.width, r.height)
    src_pdf.close()
    pages_data = {}
    for entry in raw:
        pi = entry["page_index"]
        if pi not in pages_data:
            pages_data[pi] = {"page_number": entry.get("page_number"),
                              "img_width": entry.get("img_width", 1),
                              "img_height": entry.get("img_height", 1), "detections": []}
        pages_data[pi]["detections"].append(entry)
    pages_meta = {}
    for candidate in [output_base / "pages_meta.json", output_base.parent / "pages_meta.json"]:
        if candidate.exists():
            pages_meta = json.loads(candidate.read_text())
            break
    pages = []
    for pi in sorted(pages_data.keys()):
        pd = pages_data[pi]
        pdf_w, pdf_h = page_dims.get(pi, (612.0, 792.0))
        meta = pages_meta.get(str(pi), pages_meta.get(pi, {}))
        page = Page(index=pi, pdf_width=pdf_w, pdf_height=pdf_h,
                    img_width=pd["img_width"], img_height=pd["img_height"],
                    page_number=pd["page_number"],
                    col_left_x1=meta.get("col_left_x1", 0), col_left_x2=meta.get("col_left_x2", 0),
                    col_right_x1=meta.get("col_right_x1", 0), col_right_x2=meta.get("col_right_x2", 0),
                    midpoint=meta.get("midpoint", 0))
        for d in pd["detections"]:
            bbox_raw = d.get("bbox", [0, 0, 1, 1])
            page.detections.append(BLDetection(
                bbox=BBox(x1=bbox_raw[0], y1=bbox_raw[1], x2=bbox_raw[2], y2=bbox_raw[3]),
                label=Label(d["label_id"]), confidence=d["confidence"], page_index=pi,
            ))
        pages.append(page)
    document = BLDoc(pdf_path=ocr_pdf, pages=pages,
                     reporter=scan.reporter.short_name or "", volume=str(scan.volume) or "",
                     first_page=scan.start_page or 1, ocr_applied=True)
    excluded = set()
    approved = set()
    for issue in scan.issues.filter(check_name__in=["suppress_detection", "approve_detection"]):
        if issue.metadata:
            try:
                meta = json.loads(issue.metadata)
                bbox = meta.get("bbox", [0, 0, 0, 0])
                tup = (meta.get("page_index", 0), meta.get("label_id", 0), round(bbox[0]), round(bbox[1]))
                if issue.check_name == "suppress_detection":
                    excluded.add(tup)
                else:
                    approved.add(tup)
            except Exception:
                pass
    opinions = _pair_opinions(document, excluded=excluded or None)
    from blackletter.scanner import _outside_opinion_rects
    _pages_by_index = {p.index: p for p in document.pages}
    _src = fitz.open(str(ocr_pdf))
    opinions_data = []
    for caption, key in opinions:
        outside_rects = []
        for pi in range(caption.page_index, key.page_index + 1):
            pg = _pages_by_index[pi]
            is_first = pi == caption.page_index
            is_last = pi == key.page_index
            pw = _src[pi].rect.width
            for rect in _outside_opinion_rects(pg, pw, caption, key, is_first, is_last):
                outside_rects.append({
                    "page_index": pi, "x0": round(rect.x0, 1), "y0": round(rect.y0, 1),
                    "x1": round(rect.x1, 1), "y1": round(rect.y1, 1),
                })
        opinions_data.append({
            "caption_page": caption.page_index,
            "caption_bbox": [round(caption.bbox.x1, 1), round(caption.bbox.y1, 1),
                             round(caption.bbox.x2, 1), round(caption.bbox.y2, 1)],
            "key_page": key.page_index,
            "key_bbox": [round(key.bbox.x1, 1), round(key.bbox.y1, 1),
                         round(key.bbox.x2, 1), round(key.bbox.y2, 1)],
            "page_count": key.page_index - caption.page_index + 1,
            "outside_rects": outside_rects,
        })
    _src.close()
    for candidate in [output_base / "opinions.json", output_base.parent / "opinions.json",
                      output_base.parent.parent / "opinions.json"]:
        if candidate.parent.exists():
            candidate.write_text(json.dumps(opinions_data))
            break
    from blackletter.process import compute_redaction_rects
    redaction_rects = compute_redaction_rects(document, opinions, excluded=excluded or None, approved=approved or None, skip_doctr=True)
    for candidate in [output_base / "redaction_rects.json", output_base.parent / "redaction_rects.json",
                      output_base.parent.parent / "redaction_rects.json"]:
        if candidate.parent.exists():
            candidate.write_text(json.dumps(redaction_rects))
            break
    return JsonResponse({"opinions": len(opinions), "message": f"Re-paired: {len(opinions)} opinions"})


@login_required
@require_POST
def reprocess_section_view(request, pk):
    from pathlib import Path as _P
    scan = get_object_or_404(Scan, pk=pk)
    data = json.loads(request.body)
    page_start = data["page_start"]
    page_end = data["page_end"]
    model_name = data.get("model", "medium")
    model_map = {
        "small": "/Users/Palin/Code/blackletter/blackletter/models/best.pt",
        "medium": "/Users/Palin/Code/blackletter/blackletter/models/medium.pt",
        "large": "/Users/Palin/Code/blackletter/blackletter/models/analyze.pt",
    }
    model_path = model_map.get(model_name, model_map["medium"])
    import glob
    import subprocess
    import sys
    ocr_pdfs = [f for f in glob.glob(f"{scan.output_dir}/*.pdf") if ".redacted." not in f]
    if not ocr_pdfs:
        return JsonResponse({"status": "error", "message": "No OCR'd PDF found"}, status=400)
    ocr_pdf = ocr_pdfs[0]
    first_page = scan.start_page or 1
    excluded = set()
    for issue in scan.issues.filter(check_name="suppress_detection"):
        if issue.metadata:
            meta = json.loads(issue.metadata)
            bbox = meta.get("bbox", [0, 0, 0, 0])
            excluded.add((meta.get("page_index", 0), meta.get("label_id", 0), round(bbox[0]), round(bbox[1])))
    injected = []
    for issue in scan.issues.filter(check_name="add_detection"):
        if issue.metadata:
            meta = json.loads(issue.metadata)
            pi = meta.get("page_index", -1)
            if page_start - first_page <= pi <= page_end - first_page:
                injected.append({
                    "page_index": pi, "label_id": meta.get("label_id", 0),
                    "bbox": meta.get("bbox", [0, 0, 1, 1]),
                    "img_width": meta.get("img_width", 1700), "img_height": meta.get("img_height", 2200),
                    "confidence": 1.0,
                })
    section_output = f"{scan.output_dir}/section_{page_start}_{page_end}"
    excluded_json = json.dumps(list(excluded)) if excluded else "[]"
    injected_json = json.dumps(injected) if injected else "[]"
    reporter = scan.reporter.short_name
    volume = str(scan.volume)
    script = f"""
import os, json
os.environ["PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK"] = "True"
from pathlib import Path
from blackletter.process import reprocess_section
excluded_raw = json.loads('{excluded_json}')
excluded = set(tuple(e) for e in excluded_raw)
injected = json.loads('{injected_json}')
result = reprocess_section(
    ocr_pdf_path="{ocr_pdf}", output_dir=Path("{section_output}"),
    page_start={page_start}, page_end={page_end}, first_page={first_page},
    reporter="{reporter}", volume="{volume}", model=Path("{model_path}"),
    excluded=excluded if excluded else None, injected=injected if injected else None,
)
print(json.dumps(result))
"""
    proc = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=600)
    if proc.returncode != 0:
        return JsonResponse({"status": "error", "message": f"Reprocess failed: {proc.stderr[-500:]}"}, status=500)
    result_line = [l for l in proc.stdout.strip().split("\n") if l.startswith("{")]
    if not result_line:
        return JsonResponse({"status": "error", "message": "No result from reprocess"}, status=500)
    result = json.loads(result_line[-1])
    opinions = json.loads(scan.opinions_json) if scan.opinions_json else []
    opinions = [op for op in opinions if not (op["first_page"] >= page_start and op["last_page"] <= page_end)]
    for subdir in ["redacted", "unredacted"]:
        src_dir = f"{section_output}/{subdir}"
        dst_dir = f"{scan.output_dir}/{subdir}"
        if os.path.isdir(src_dir):
            for f in os.listdir(src_dir):
                shutil.move(os.path.join(src_dir, f), os.path.join(dst_dir, f))
    opinions.extend(result["opinions"])
    opinions.sort(key=lambda o: o["first_page"])
    scan.opinions_json = json.dumps(opinions)
    scan.save()
    shutil.rmtree(section_output, ignore_errors=True)
    return JsonResponse({"status": "ok", "opinions": len(opinions)})


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
