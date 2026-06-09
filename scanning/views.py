"""Auth, scan CRUD, opinion, and queue views."""

import logging
from pathlib import Path

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import update_session_auth_hash
from django.contrib.auth import views as auth_views
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import PasswordChangeForm
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db.models import Count, Q
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from scanning.forms import (
    OpinionScanUploadForm,
    ProfileForm,
)
from scanning.models import (
    ExtractionStatus,
    OpinionScan,
    OpinionStatus,
    Page,
    Priority,
    QueuedAction,
    QueueStatus,
    Reporter,
    Scan,
    Source,
    Stage,
    Status,
    Volume,
)
from scanning.services import refresh_volume_queue_status_for_scan
from scanning.utils import get_volume, has_s3_credentials

logger = logging.getLogger(__name__)


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

    retry_cap_count = Scan.objects.filter(
        status=Status.ERROR_MAX_RETRIES,
    ).count()

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
            "retry_cap_count": retry_cap_count,
        },
    )


@login_required
def scan_detail(request: HttpRequest, pk: int) -> HttpResponse:
    """Redirect the legacy scan detail URL to the process view.

    The standalone detail page has been retired in favour of the unified
    process view, which displays the PDF and handles review/approval.
    This redirect keeps old/bookmarked ``/scans/<pk>/`` links working.

    :param request: The current HTTP request.
    :param pk: The primary key of the scan.
    :return: A redirect to the scan process view.
    """
    scan = get_object_or_404(Scan, pk=pk)
    return redirect("scan_process", pk=scan.pk)


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


@login_required
def profile(request):
    """Display and handle the user profile edit form.

    :param request: The current HTTP request.
    :type request: django.http.HttpRequest
    :returns: The rendered profile page or a redirect on success.
    :rtype: django.http.HttpResponse
    """
    if request.method == "POST":
        form = ProfileForm(request.POST, instance=request.user)
        if form.is_valid():
            form.save()
            messages.success(request, "Profile updated.")
            return redirect("profile")
    else:
        form = ProfileForm(instance=request.user)

    return render(request, "scanning/profile.html", {"form": form})


@login_required
def password_change(request):
    """Handle password change using Django's PasswordChangeForm.

    :param request: The current HTTP request.
    :type request: django.http.HttpRequest
    :returns: The rendered password change page or a redirect on success.
    :rtype: django.http.HttpResponse
    """
    if request.method == "POST":
        form = PasswordChangeForm(request.user, request.POST)
        if form.is_valid():
            user = form.save()
            update_session_auth_hash(request, user)
            messages.success(request, "Password changed.")
            return redirect("profile")
    else:
        form = PasswordChangeForm(request.user)

    return render(request, "scanning/password_change.html", {"form": form})


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
                "scanned": by_status.get("scanned", 0),
                "complete": by_status.get("complete", 0),
                "unavailable": by_status.get("unavailable", 0),
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

    # The two transitions handled here (NEEDS_SCANNING ↔ ASSIGNED) only
    # apply to scan-less volumes, which the filter conditions enforce.
    # In that state the inline values match what
    # ``refresh_volume_queue_status`` would compute, so we set them
    # directly and skip the extra query.
    if request.POST.get("unclaim") == "1":
        unclaimed = Volume.objects.filter(
            pk=volume.pk, assigned_to=request.user
        ).update(
            queue_status=QueueStatus.NEEDS_SCANNING,
            assigned_to=None,
            assigned_at=None,
        )
        if unclaimed:
            messages.info(request, "Volume unclaimed.")
    else:
        claimed = Volume.objects.filter(
            pk=volume.pk, queue_status=QueueStatus.NEEDS_SCANNING
        ).update(
            queue_status=QueueStatus.ASSIGNED,
            assigned_to=request.user,
            assigned_at=timezone.now(),
        )
        if claimed:
            messages.success(request, "Volume claimed.")
        else:
            messages.error(request, "Volume is not available to claim.")

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
    if first_page.isdigit():
        scan.start_page = int(first_page)
    if last_page.isdigit():
        scan.end_page = int(last_page)

    scan.has_state_abbrev = "has_state_abbrev" in request.POST
    scan.uploaded_by = request.user
    scan.status = Status.UPLOADED
    scan.save()  # Save first to get a PK

    # Create processing directory and save original PDF there
    output_dir = Path(scan.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    original_name = (
        f"{volume.reporter.short_name}.{volume.volume_number}"
        f".{scan.start_page or 1}.{scan.end_page or 0}"
        f".original.pdf"
    )

    # Keep a local copy in output_dir for the processing pipeline. In
    # prod this is /tmp/scanning/{pk}/...; in DEV it is under MEDIA_ROOT.
    local_path = output_dir / original_name
    with open(local_path, "wb") as f:
        for chunk in pdf.chunks():
            f.write(chunk)

    if settings.DEVELOPMENT:
        # DEV: store via Django FileField (MEDIA_ROOT/original_scans/...)
        # so the shared mount makes the file available to the daemon.
        pdf.seek(0)
        scan.original_pdf.save(original_name, pdf, save=False)
        scan.save(update_fields=["original_pdf"])
    else:
        # PROD: push to S3 under processing/{pk}/... so the daemon can
        # pull it (containers don't share /tmp/). Skip the FileField
        # write so MEDIA_ROOT stays empty.
        from scanning import s3_sync

        if not has_s3_credentials():
            logger.error(
                "Prod upload attempted without AWS credentials for scan %s",
                scan.pk,
            )
            scan.delete()
            messages.error(
                request,
                "Storage is not configured; contact an administrator.",
            )
            return redirect(
                "queue_detail",
                reporter_slug=reporter_slug,
                vol=vol,
            )

        try:
            uploaded = s3_sync.upload_file_to_s3(scan, original_name)
        except Exception:
            logger.exception(
                "Failed to upload original PDF to S3 for scan %s", scan.pk
            )
            uploaded = False
        if not uploaded:
            scan.delete()
            messages.error(
                request,
                "Upload to storage failed. Please try again in a moment.",
            )
            return redirect(
                "queue_detail",
                reporter_slug=reporter_slug,
                vol=vol,
            )
        scan.original_pdf.name = original_name
        scan.save(update_fields=["original_pdf"])

    action = request.POST.get("action", "upload_only")
    if action == "upload_validate":
        scan.status = Status.QUEUED
        scan.stage = Stage.VALIDATE
        scan.queued_action = QueuedAction.FULL_PIPELINE
        scan.progress_message = "Queued for processing..."
        scan.save()
        refresh_volume_queue_status_for_scan(scan)
        return redirect("scan_process", pk=scan.pk)

    refresh_volume_queue_status_for_scan(scan)
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

    # Curator manual-override path: intentionally bypasses
    # ``refresh_volume_queue_status`` so a reviewer can force any value
    # (e.g. UNAVAILABLE, or SCANNED before approval) regardless of
    # what the helper would otherwise derive from the scans.
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
def scan_pages_list(request: HttpRequest, pk: int) -> HttpResponse:
    """List every ``Page`` of a scan with extraction state.

    Phase 1: read-only. Filters by status / needs_review let the human
    surface the ~1-2 pages that need attention per volume without
    scrolling the whole list. Each PDF link opens the per-page file via
    ``serve_page_pdf``; each ``page_index`` cell links into the
    per-page detail view.

    :param request: The HTTP request.
    :param pk: Scan primary key.
    :return: Rendered pages-list page.
    """
    scan = get_object_or_404(Scan.objects.select_related("reporter"), pk=pk)
    pages = scan.pages.select_related("user_prompt").order_by("page_index")

    status_filter = request.GET.get("status") or ""
    review_filter = request.GET.get("needs_review") == "1"
    if status_filter:
        pages = pages.filter(status=status_filter)
    if review_filter:
        pages = pages.filter(needs_review=True)

    counts = scan.pages.aggregate(
        total=Count("id"),
        extracted=Count("id", filter=~Q(xml_content="")),
        with_prompt=Count("id", filter=Q(user_prompt__isnull=False)),
        needs_review=Count("id", filter=Q(needs_review=True)),
    )

    return render(
        request,
        "scanning/pages_list.html",
        {
            "scan": scan,
            "pages": pages,
            "counts": counts,
            "status_filter": status_filter,
            "review_filter": review_filter,
            "statuses": ExtractionStatus.choices,
        },
    )


@login_required
def page_detail(request: HttpRequest, pk: int) -> HttpResponse:
    """Per-page review pane.

    Phase 1: read-only — shows PDF link, current user prompt,
    extraction metadata, and prev/next navigation within the scan.
    Phase 2 will add edit / retry / OCR-fallback buttons.

    :param request: The HTTP request.
    :param pk: Page primary key.
    :return: Rendered page-detail page.
    """
    page = get_object_or_404(
        Page.objects.select_related("scan", "scan__reporter", "user_prompt"),
        pk=pk,
    )
    prev_page = (
        Page.objects.filter(scan=page.scan, page_index__lt=page.page_index)
        .order_by("-page_index")
        .first()
    )
    next_page = (
        Page.objects.filter(scan=page.scan, page_index__gt=page.page_index)
        .order_by("page_index")
        .first()
    )
    return render(
        request,
        "scanning/page_detail.html",
        {
            "page": page,
            "scan": page.scan,
            "prev_page": prev_page,
            "next_page": next_page,
        },
    )
