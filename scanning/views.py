"""Auth, scan CRUD, opinion, and queue views."""

import logging
import uuid
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
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from scanning import s3_sync
from scanning.forms import (
    OpinionScanUploadForm,
    ProfileForm,
)
from scanning.models import (
    ExtractionStatus,
    OpinionScan,
    OpinionStatus,
    Page,
    PendingUpload,
    Priority,
    QueueStatus,
    Reporter,
    Scan,
    Source,
    Status,
    UploadAction,
    Volume,
)
from scanning.services import apply_upload_action
from scanning.utils import get_volume, has_s3_credentials

# Max size for a direct-to-S3 original upload. Enforced by the presigned
# POST policy (see s3_sync.generate_presigned_post) so S3 rejects anything
# larger before it lands, and pre-checked in the presign view for a fast
# client-facing error. Configurable via the MAX_UPLOAD_SIZE_GB env var
# (default 3 GB); see settings.project.processing_storage.
MAX_ORIGINAL_UPLOAD_SIZE = settings.MAX_ORIGINAL_UPLOAD_SIZE

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
    interrupted_count = Scan.objects.filter(
        status=Status.ERROR_INTERRUPTED,
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
            "interrupted_count": interrupted_count,
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

    # Stats are aggregated over all volumes, independent of the page.
    total = Volume.objects.count()
    by_status = dict(
        Volume.objects.values_list("queue_status")
        .annotate(c=Count("id"))
        .values_list("queue_status", "c")
    )

    paginator = Paginator(volumes, 100)
    page_obj = paginator.get_page(request.GET.get("page"))

    return render(
        request,
        "scanning/queue.html",
        {
            "page_obj": page_obj,
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
            "direct_upload_enabled": s3_sync.direct_upload_enabled(),
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


def _upload_redirect(request: HttpRequest, url: str) -> HttpResponse:
    """Send the post-upload destination to the client.

    XHR uploads get the URL as JSON so the progress script can navigate
    after the response arrives (a plain redirect would consume any queued
    messages before the browser shows the page). Regular form posts get
    an ordinary redirect.

    :param request: The HTTP request.
    :param url: Destination URL.
    :return: A JSON payload for XHR requests, otherwise a redirect.
    """
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return JsonResponse({"redirect": url})
    return redirect(url)


def _prepare_scan_from_request(request, volume):
    """Create or fetch the Scan an upload targets and stamp its metadata.

    Shared by the classic through-Django upload (``queue_upload``) and
    the presigned direct-to-S3 flow (``presign_scan_upload``). A
    ``new_scan`` request builds a fresh row; otherwise ``scan_pk`` must
    name an existing scan in this volume. Page range, state-abbrev flag,
    uploader, and status are applied and the row is saved (so it has a
    PK for building the S3 key).

    :param request: The HTTP request carrying the upload form fields.
    :param volume: The Volume the scan belongs to.
    :returns: ``(scan, error_message)``. On success ``error_message`` is
        None; on a missing ``scan_pk`` the scan is None.
    :rtype: tuple[Scan | None, str | None]
    """
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
            return None, "No scan specified."
        scan = get_object_or_404(Scan, pk=scan_pk, volume_obj=volume)
        # Refuse to re-upload onto a scan that already has a confirmed
        # original. Its S3 key is deterministic on the scan's identity, so a
        # new upload would overwrite the live original in S3 before it's even
        # verified. The UI only ever creates new scans (new_scan=1); this
        # guards the crafted-POST path until re-upload gets a staged key.
        if scan.original_pdf.name:
            return None, "This scan already has an uploaded original."

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
    return scan, None


def _original_pdf_name(scan, volume):
    """Return the canonical ``*.original.pdf`` filename for a scan.

    Used as both the FileField name and the S3 key suffix so the
    daemon's ``download_processing_files`` finds the file at the
    expected key.

    :param scan: The scan the file belongs to.
    :param volume: The scan's volume (source of reporter/volume number).
    :return: e.g. ``a3d.214.1.95.original.pdf``.
    :rtype: str
    """
    return (
        f"{volume.reporter.short_name}.{volume.volume_number}"
        f".{scan.start_page or 1}.{scan.end_page or 0}"
        f".original.pdf"
    )


def _finalize_uploaded_scan(request, scan):
    """Apply the requested post-upload action and return the destination.

    Shared tail of both upload paths, run once the original PDF is
    stored. ``upload_validate`` queues the full pipeline and sends the
    user to the process viewer; ``upload_only`` just records success.

    :param request: The HTTP request (source of the ``action`` field).
    :param scan: The scan whose original PDF is now stored.
    :return: The URL to redirect the browser to.
    :rtype: str
    """
    action = request.POST.get("action", UploadAction.UPLOAD_ONLY)
    apply_upload_action(scan, action)
    if action == UploadAction.UPLOAD_VALIDATE:
        return reverse("scan_process", kwargs={"pk": scan.pk})

    messages.success(request, "PDF uploaded successfully.")
    return reverse(
        "queue_detail",
        kwargs={
            "reporter_slug": scan.reporter.short_name,
            "vol": scan.volume,
        },
    )


@login_required
@require_POST
def queue_upload(request, reporter_slug, vol):
    """Upload a PDF through Django (fallback when direct-to-S3 is off).

    Used when ``s3_sync`` direct upload is disabled (local dev without
    RunPod, or missing credentials). When direct upload is enabled the
    browser talks to ``presign_scan_upload``/``confirm_scan_upload``
    instead and the file never flows through this view.

    :param request: The HTTP request.
    :param reporter_slug: Short-name slug identifying the reporter.
    :param vol: Volume number within the reporter.
    :return: Redirect to queue detail or scan processing page.
    """
    volume = get_volume(reporter_slug, vol)
    queue_url = reverse(
        "queue_detail",
        kwargs={"reporter_slug": reporter_slug, "vol": vol},
    )

    pdf = request.FILES.get("original_pdf")
    if not pdf:
        messages.error(request, "No PDF file provided.")
        return _upload_redirect(request, queue_url)

    if not pdf.name.lower().endswith(".pdf"):
        messages.error(request, "Only PDF files are accepted.")
        return _upload_redirect(request, queue_url)

    header = pdf.read(5)
    pdf.seek(0)
    if header != b"%PDF-":
        messages.error(request, "The uploaded file is not a valid PDF.")
        return _upload_redirect(request, queue_url)

    scan, error = _prepare_scan_from_request(request, volume)
    if error:
        messages.error(request, error)
        return _upload_redirect(request, queue_url)

    output_dir = Path(scan.output_dir)
    original_name = _original_pdf_name(scan, volume)

    if settings.DEVELOPMENT:
        # DEV: keep a local copy in output_dir (under MEDIA_ROOT, shared
        # with the daemon) and store it via the Django FileField.
        output_dir.mkdir(parents=True, exist_ok=True)
        local_path = output_dir / original_name
        with open(local_path, "wb") as f:
            for chunk in pdf.chunks():
                f.write(chunk)
        pdf.seek(0)
        scan.original_pdf.save(original_name, pdf, save=False)
        scan.save(update_fields=["original_pdf"])
    else:
        # PROD: stream the uploaded file straight to S3 under
        # processing/{pk}/... in a single pass so the daemon can pull it
        # (containers don't share /tmp/). No local copy is written here;
        # the daemon recreates one when it downloads from S3.
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
            return _upload_redirect(request, queue_url)

        try:
            uploaded = s3_sync.upload_fileobj_to_s3(scan, pdf, original_name)
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
            return _upload_redirect(request, queue_url)
        scan.original_pdf.name = original_name
        scan.save(update_fields=["original_pdf"])

    return _upload_redirect(request, _finalize_uploaded_scan(request, scan))


@login_required
@require_POST
def presign_scan_upload(request, reporter_slug, vol):
    """Authorize a direct browser->S3 upload of a scan's original PDF.

    Creates/updates the target scan, then returns a presigned POST the
    browser uses to upload the (up to 3 GB) PDF straight to the scan's
    S3 processing prefix -- keeping those bytes off the Django request
    path. A ``PendingUpload`` row records the authorization until
    ``confirm_scan_upload`` verifies the object landed.

    :param request: The HTTP request with upload metadata (filename,
        content_type, size) plus the usual scan form fields.
    :param reporter_slug: Short-name slug identifying the reporter.
    :param vol: Volume number within the reporter.
    :return: JSON ``{"presigned": ..., "pending_id": ...}`` or an error.
    """
    volume = get_volume(reporter_slug, vol)

    filename = request.POST.get("filename", "")
    # This endpoint is PDF-only (filename must end in .pdf and the object is
    # PDF-verified on confirm), so pin the stored Content-Type rather than
    # trusting the browser -- a client-supplied MIME would otherwise be
    # baked into the S3 policy and stored on the object.
    content_type = "application/pdf"
    # Store the chosen action now so recovery can replay it if the browser
    # never reaches confirm_scan_upload (container died, tab closed, etc.).
    action = request.POST.get("action", UploadAction.UPLOAD_ONLY)
    if action not in UploadAction.values:
        action = UploadAction.UPLOAD_ONLY
    try:
        size = int(request.POST.get("size", "0"))
    except (TypeError, ValueError):
        size = 0

    if not filename.lower().endswith(".pdf"):
        return JsonResponse(
            {"error": "Only PDF files are accepted."}, status=400
        )
    if size <= 0 or size > MAX_ORIGINAL_UPLOAD_SIZE:
        limit_gb = MAX_ORIGINAL_UPLOAD_SIZE // 1024**3
        return JsonResponse(
            {"error": f"File is empty or exceeds the {limit_gb} GB limit."},
            status=400,
        )

    if not has_s3_credentials():
        logger.error(
            "Presign requested without AWS credentials for %s vol %s",
            reporter_slug,
            vol,
        )
        return JsonResponse(
            {"error": "Storage is not configured; contact an administrator."},
            status=503,
        )

    scan, error = _prepare_scan_from_request(request, volume)
    if error:
        return JsonResponse({"error": error}, status=400)

    original_name = _original_pdf_name(scan, volume)
    try:
        presigned = s3_sync.generate_presigned_post(
            scan, original_name, content_type, MAX_ORIGINAL_UPLOAD_SIZE
        )
        if not presigned:
            # S3 sync disabled despite credentials being present.
            raise RuntimeError("presign unavailable")
        pending = PendingUpload.objects.create(
            scan=scan,
            s3_key=f"{s3_sync.s3_processing_prefix(scan)}{original_name}",
            expected_size=size,
            content_type=content_type,
            action=action,
            created_by=request.user,
        )
    except Exception:
        # Presign disabled/errored, or the pending-row insert failed. Either
        # way the scan is fileless with no PendingUpload row, so the TTL sweep
        # can't reclaim it (it only follows stale pending rows) -- delete it
        # inline.
        logger.exception("Presign setup failed for scan %s", scan.pk)
        if not scan.original_pdf.name:
            scan.delete()
        return JsonResponse(
            {"error": "Could not initialize upload. Please try again."},
            status=503,
        )

    return JsonResponse(
        {"presigned": presigned, "pending_id": str(pending.id)}
    )


@login_required
@require_POST
def confirm_scan_upload(request, reporter_slug, vol):
    """Confirm a direct-to-S3 upload and queue the scan.

    Called by the browser once the presigned POST completes. Verifies
    the object actually landed in S3 (and is a real PDF), attaches it to
    the scan, applies the requested action, and deletes the
    ``PendingUpload``. On a failed verification the pending row is
    removed and a freshly-created (fileless) scan is cleaned up.

    :param request: The HTTP request carrying ``pending_id`` and the
        ``action`` field.
    :param reporter_slug: Short-name slug identifying the reporter.
    :param vol: Volume number within the reporter.
    :return: JSON ``{"redirect": ...}`` on success or ``{"error": ...}``.
    """
    volume = get_volume(reporter_slug, vol)  # 404s on a bad reporter/volume
    # Validate the UUID up front: a non-UUID id would make the UUIDField raise
    # ValidationError at query-prep time, which get_object_or_404 doesn't catch
    # (a 500 instead of a clean 400).
    try:
        pending_id = uuid.UUID(str(request.POST.get("pending_id") or ""))
    except ValueError:
        return JsonResponse({"error": "Invalid pending_id."}, status=400)
    # Scope the pending row to the URL's volume as well as its owner, so a
    # pending can only be confirmed through its own volume's endpoint.
    pending = get_object_or_404(
        PendingUpload,
        id=pending_id,
        created_by=request.user,
        scan__volume_obj=volume,
    )
    scan = pending.scan
    original_name = Path(pending.s3_key).name

    if not s3_sync.verify_uploaded_object(scan, original_name):
        if not scan.original_pdf.name:
            # Fresh, fileless scan: reclaim the rejected object (e.g. a
            # non-PDF that still landed via the presigned POST) and the scan.
            # For a re-upload the scan already has a confirmed original at
            # this same key, so don't delete it out from under a live scan.
            s3_sync.delete_uploaded_object(pending.s3_key)
            scan.delete()
        pending.delete()
        return JsonResponse(
            {"error": "Upload could not be verified. Please try again."},
            status=400,
        )

    scan.original_pdf.name = original_name
    scan.save(update_fields=["original_pdf"])
    pending.delete()

    return JsonResponse({"redirect": _finalize_uploaded_scan(request, scan)})


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
