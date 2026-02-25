from django.contrib import messages
from django.contrib.auth import views as auth_views
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from scanning.forms import ScanReviewForm, ScanUploadForm
from scanning.models import Reporter, Scan, Status


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
    """List scans. Regular users see their own, staff see all.

    :param request: The current HTTP request.
    :type request: django.http.HttpRequest
    :returns: The rendered scan list page.
    :rtype: django.http.HttpResponse
    """
    scans = Scan.objects.select_related("reporter").all()

    # Filtering
    status_filter = request.GET.get("status")
    if status_filter:
        scans = scans.filter(status=status_filter)

    reporter_filter = request.GET.get("reporter")
    if reporter_filter:
        scans = scans.filter(reporter_id=reporter_filter)

    paginator = Paginator(scans, 25)
    page_number = request.GET.get("page")
    page_obj = paginator.get_page(page_number)

    return render(
        request,
        "scanning/scan_list.html",
        {
            "page_obj": page_obj,
            "status_choices": Status.choices,
            "reporter_choices": Reporter.objects.values_list(
                "pk", "full_name"
            ),
            "current_status": status_filter or "",
            "current_reporter": reporter_filter or "",
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
        },
    )
