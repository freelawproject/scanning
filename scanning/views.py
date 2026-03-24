from django.contrib import messages
from django.contrib.auth import views as auth_views
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db.models import Count
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
    OpinionScan,
    OpinionStatus,
    Priority,
    QueueStatus,
    Reporter,
    Scan,
    Source,
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
            return redirect("scan_detail", pk=scan.pk)
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

    scans = Scan.objects.select_related(
        "reporter", "assigned_to"
    ).order_by("reporter__short_name", "volume")

    if selected_reporter:
        scans = scans.filter(reporter__short_name=selected_reporter)
    if status_filter:
        scans = scans.filter(queue_status=status_filter)
    if priority_filter:
        scans = scans.filter(priority=priority_filter)

    # Stats
    total = Scan.objects.count()
    by_status = dict(
        Scan.objects.values_list("queue_status")
        .annotate(c=Count("id"))
        .values_list("queue_status", "c")
    )

    return render(
        request,
        "scanning/queue.html",
        {
            "scans": scans,
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
def queue_detail_view(request, pk):
    """Detail page for a queue item -- view info, assignment, and PDF.

    :param request: The current HTTP request.
    :type request: django.http.HttpRequest
    :param pk: The primary key of the scan.
    :type pk: int
    :returns: The rendered queue detail page.
    :rtype: django.http.HttpResponse
    """
    scan = get_object_or_404(
        Scan.objects.select_related("reporter", "assigned_to", "uploaded_by"),
        pk=pk,
    )

    return render(
        request,
        "scanning/queue_detail.html",
        {
            "scan": scan,
            "queue_statuses": QueueStatus.choices,
        },
    )


@login_required
@require_POST
def claim_scan(request, pk):
    """Claim or unclaim a scan for scanning.

    If the POST body contains ``unclaim=1`` and the scan is currently
    assigned to the requesting user, the assignment is cleared and the
    queue status is reset to *needs_scanning*.

    :param request: The current HTTP request.
    :type request: django.http.HttpRequest
    :param pk: The primary key of the scan.
    :type pk: int
    :returns: A redirect to the queue or to the URL in ``next``.
    :rtype: django.http.HttpResponseRedirect
    """
    scan = get_object_or_404(Scan, pk=pk)

    if request.POST.get("unclaim") == "1":
        if scan.assigned_to == request.user:
            scan.queue_status = QueueStatus.NEEDS_SCANNING
            scan.assigned_to = None
            scan.assigned_at = None
            scan.save()
            messages.info(request, "Scan unclaimed.")
    elif scan.queue_status == QueueStatus.NEEDS_SCANNING:
        scan.queue_status = QueueStatus.ASSIGNED
        scan.assigned_to = request.user
        scan.assigned_at = timezone.now()
        scan.save()
        messages.success(request, "Scan claimed.")

    next_url = request.POST.get("next", "")
    if next_url:
        return redirect(next_url)
    return redirect("queue")


@login_required
@require_POST
def update_scan_status(request, pk):
    """Update a scan's queue status.

    :param request: The current HTTP request.
    :type request: django.http.HttpRequest
    :param pk: The primary key of the scan.
    :type pk: int
    :returns: A redirect to the queue or to the URL in ``next``.
    :rtype: django.http.HttpResponseRedirect
    """
    scan = get_object_or_404(Scan, pk=pk)
    new_status = request.POST.get("status")

    if new_status and new_status in dict(QueueStatus.choices):
        scan.queue_status = new_status
        if new_status == QueueStatus.NEEDS_SCANNING:
            scan.assigned_to = None
            scan.assigned_at = None
        scan.save()
        messages.success(request, f"Status updated to {scan.get_queue_status_display()}.")

    next_url = request.POST.get("next", "")
    if next_url:
        return redirect(next_url)
    return redirect("queue")
