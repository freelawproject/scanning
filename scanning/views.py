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
from django.db.models import Count
from django.http import Http404, HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import urlencode
from django.views.decorators.http import require_POST

from scanning import (
    ensemble,
    opinion_ocr,
    opinion_pdf,
    opinions,
    repairs,
    s3_sync,
    stats,
)
from scanning.forms import (
    OpinionScanUploadForm,
    ProfileForm,
)
from scanning.models import (
    Issue,
    Opinion,
    OpinionReviewStatus,
    OpinionScan,
    OpinionStatus,
    PageRepairRequest,
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


def _whole_number(value: str | None) -> int | None:
    """Return a query-string value as an integer, or ``None``.

    The guard of every integer filter of every list page. ``str.isdigit``
    is not that test: it answers true for ``"\u00b2"``, which ``int``
    refuses, and Django's ``IntegerField.get_prep_value`` re-raises that
    ``ValueError`` rather than a ``ValidationError``. A superscript in
    ``?volume=`` was an unhandled 500.

    :param value: The raw query-string value, or ``None``.
    :returns: The number, or ``None`` when there is none to read.
    :rtype: int | None
    """
    if not value:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


@login_required
def scan_list(request: HttpRequest) -> HttpResponse:
    """List scans with opinion count annotation.

    Each row of the page carries ``waiting_repairs``, the number of
    pages a scanner must still scan (#266). A volume with one cannot
    pass the page completeness review, so the badge keeps a reviewer
    out of it.

    :param request: The current HTTP request.
    :return: The rendered scan list page.
    """
    scans = (
        # ``uploaded_by`` is joined because every row prints the
        # username: without it the page cost one query per scan.
        Scan.objects.select_related("reporter", "uploaded_by")
        # The legacy rows, on purpose: ``opinions`` is the new
        # ``Opinion`` table since #335, and this column counts what the
        # legacy pipeline generated. #334 gives the new rows their own
        # page.
        .annotate(opinion_count=Count("legacy_opinions"))
        .order_by("-date_created")
    )

    # Filtering
    status_filter = request.GET.get("status")
    if status_filter:
        scans = scans.filter(status=status_filter)

    reporter_filter = request.GET.get("reporter")
    if _whole_number(reporter_filter) is not None:
        scans = scans.filter(reporter_id=reporter_filter)
    else:
        reporter_filter = ""

    source_filter = request.GET.get("source")
    if source_filter:
        scans = scans.filter(source=source_filter)

    volume_filter = request.GET.get("volume")
    if volume_filter:
        if _whole_number(volume_filter) is not None:
            scans = scans.filter(volume=volume_filter)
        else:
            messages.error(request, "Volume must be a number.")
            volume_filter = ""

    paginator = Paginator(scans, 25)
    page_number = request.GET.get("page")
    page_obj = paginator.get_page(page_number)

    # The repair badge (#266): a volume whose pages a scanner must
    # scan cannot pass the page completeness review, so a reviewer
    # must see that before they open it. The count is stamped after
    # the pagination, so one grouped query answers the 25 rows of
    # this page and the size of the corpus never reaches it.
    waiting = repairs.waiting_counts([scan.pk for scan in page_obj])
    for scan in page_obj:
        scan.waiting_repairs = waiting.get(scan.pk, 0)

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
    """List the opinions of the third review (#334).

    One flat row per :class:`Opinion`, the rows ``opinions.create_rows``
    writes after the review-2 approval. The four filters are the shape
    of the scan list, and the step-3 tab of a volume sends ``scan``.

    The warning count is stamped after the pagination, so one grouped
    query answers the 50 rows of this page and the size of the corpus
    never reaches it. This is the badge rule of the scan list (#266).

    The opinions of the legacy pipeline are on their own page
    (:func:`legacy_opinion_list`), linked from this one.

    :param request: The current HTTP request.
    :return: The rendered opinion list page.
    """
    opinions_qs = Opinion.objects.select_related(
        "scan", "scan__reporter"
    ).order_by(
        "scan__reporter__short_name",
        "scan__volume",
        "first_printed_page",
        "index_in_page",
        # The last key makes the ordering total. ``Scan`` has no unique
        # key over (reporter, volume) -- a volume comes in parts -- so
        # two scans tie on the four keys above, and Postgres may rank
        # tied rows differently for each page of a LIMIT/OFFSET walk.
        # An opinion would then show on two pages, or on none.
        "pk",
    )

    scan_filter = request.GET.get("scan")
    if _whole_number(scan_filter) is not None:
        opinions_qs = opinions_qs.filter(scan_id=scan_filter)
    else:
        scan_filter = ""

    reporter_filter = request.GET.get("reporter")
    if _whole_number(reporter_filter) is not None:
        opinions_qs = opinions_qs.filter(scan__reporter_id=reporter_filter)
    else:
        reporter_filter = ""

    status_filter = request.GET.get("status")
    if status_filter:
        opinions_qs = opinions_qs.filter(status=status_filter)

    volume_filter = request.GET.get("volume")
    if volume_filter:
        if _whole_number(volume_filter) is not None:
            opinions_qs = opinions_qs.filter(scan__volume=volume_filter)
        else:
            messages.error(request, "Volume must be a number.")
            volume_filter = ""

    paginator = Paginator(opinions_qs, 50)
    page_obj = paginator.get_page(request.GET.get("page"))

    counts = opinions.finding_counts([row.pk for row in page_obj])
    for row in page_obj:
        row.open_findings, row.stale_findings = counts.get(row.pk, (0, 0))

    return render(
        request,
        "scanning/opinion_list.html",
        {
            "page_obj": page_obj,
            "status_choices": OpinionReviewStatus.choices,
            "reporter_choices": [
                (str(r.pk), f"{r.full_name} ({r.short_name})")
                for r in Reporter.objects.all()
            ],
            "current_scan": scan_filter,
            "current_reporter": reporter_filter,
            "current_status": status_filter or "",
            "current_volume": volume_filter or "",
        },
    )


@login_required
def legacy_opinion_list(request: HttpRequest) -> HttpResponse:
    """List the opinions of the legacy pipeline (#334).

    ``OpinionScan`` is frozen (#173, #206): nothing writes it. The new
    pipeline writes ``Opinion`` rows, which :func:`opinion_list` lists
    at ``/opinions/``. This page keeps what the legacy pipeline made.

    :param request: The current HTTP request.
    :return: The rendered legacy opinion list page.
    """
    # Not ``opinions``: that name holds the module this view's twin
    # calls for its warning badge, and a local would hide it.
    opinions_qs = OpinionScan.objects.select_related(
        "reporter", "scan"
    ).order_by("reporter__short_name", "volume", "page_start", "pk")

    # Filtering
    scan_filter = request.GET.get("scan")
    if _whole_number(scan_filter) is not None:
        opinions_qs = opinions_qs.filter(scan_id=scan_filter)
    else:
        scan_filter = ""

    reporter_filter = request.GET.get("reporter")
    if _whole_number(reporter_filter) is not None:
        opinions_qs = opinions_qs.filter(reporter_id=reporter_filter)
    else:
        reporter_filter = ""

    status_filter = request.GET.get("status")
    if status_filter:
        opinions_qs = opinions_qs.filter(status=status_filter)

    volume_filter = request.GET.get("volume")
    if volume_filter:
        if _whole_number(volume_filter) is not None:
            opinions_qs = opinions_qs.filter(volume=volume_filter)
        else:
            messages.error(request, "Volume must be a number.")
            volume_filter = ""

    paginator = Paginator(opinions_qs, 50)
    page_number = request.GET.get("page")
    page_obj = paginator.get_page(page_number)

    return render(
        request,
        "scanning/legacy_opinion_list.html",
        {
            "page_obj": page_obj,
            "status_choices": OpinionStatus.choices,
            "reporter_choices": [
                (str(r.pk), f"{r.full_name} ({r.short_name})")
                for r in Reporter.objects.all()
            ],
            # The scan filter is kept, as on the new page: without it
            # one page turn widened the list to the whole corpus.
            "current_scan": scan_filter,
            "current_reporter": reporter_filter,
            "current_status": status_filter or "",
            "current_volume": volume_filter or "",
        },
    )


@login_required
def opinion_review(request: HttpRequest, pk: int) -> HttpResponse:
    """Show one opinion of the third review (#334/#365).

    The text review of an opinion. It shows what the rows hold -- the
    citation, the printed range, the status, the links to the volume
    and the boundary, and the findings -- and the text of the OCR
    ensemble beside the pages it was read from.

    The page reads the redacted PDF of the opinion (#336) beside the
    text of the OCR ensemble (#365), page by page. The browser draws
    both: it asks ``opinion_pdf_url`` and ``opinion_ensemble_url`` for
    a presigned GET of each object and reads them straight from the
    bucket, so **this view makes no S3 call**. Every address is written
    on the container, and the script spells none (#334).

    The three ledgers are read off the row and never off the bucket:
    ``opinion_pdf.is_written``, ``opinion_ocr.is_written`` and
    ``ensemble.is_written``. Each one decides what the page shows in
    place of a column it cannot draw, and the OCR one decides whether
    the button appears: a control an endpoint would refuse is the one
    thing a viewer must never offer. A staff reader gets the ``files``
    link (``opinion_file_index``), the rule of the volume's own glued
    outputs (#243).

    The one write control is "Read the OCR documents again", which
    posts to ``rerun_opinion_ensemble``. The approval, the dismissal
    and the typing of a page come with the review that closes an
    opinion.

    :param request: The current HTTP request.
    :param pk: The primary key of the opinion.
    :return: The rendered opinion review page.
    """
    opinion = get_object_or_404(
        Opinion.objects.select_related(
            "scan", "scan__reporter", "apply_run", "boundary", "approved_by"
        ),
        pk=pk,
    )
    findings = list(
        opinion.findings.select_related("dismissal").order_by(
            "page_in_opinion", "check_name"
        )
    )
    # The label is stamped here, not derived in the template. Page 0 is
    # a real page and a falsy value, and ``None`` is not a name a
    # Django template holds, so the test belongs in Python.
    for row in findings:
        row.page_label = (
            "the whole opinion"
            if row.page_in_opinion is None
            else f"page {row.page_in_opinion + 1} of the opinion"
        )
    open_findings = sum(1 for row in findings if row.dismissal_id is None)
    ocr_written = opinion_ocr.is_written(opinion)
    # The dismissal answers a card while the opinion is ready for the
    # text review alone, the gate of the endpoint (#419). A stale card
    # is a fact about the row and takes none.
    can_dismiss = opinion.status == OpinionReviewStatus.READY_FOR_TEXT_REVIEW
    for row in findings:
        if can_dismiss and not row.is_undismissable:
            kwargs = {
                "pk": opinion.scan_id,
                "opinion_pk": opinion.pk,
                "finding_pk": row.pk,
            }
            row.dismiss_url = reverse("dismiss_opinion_finding", kwargs=kwargs)
            row.restore_url = reverse("restore_opinion_finding", kwargs=kwargs)
    # Two lists (#419): the ERROR cards the approval waits on, with the
    # stale ones, and the warnings. A dismissed card goes to the end of
    # its list; the sort is stable, so the page order stays.
    blocking = [
        row.severity == Issue.Severity.ERROR or row.is_stale
        for row in findings
    ]
    to_check = sorted(
        (row for row, block in zip(findings, blocking) if block),
        key=lambda row: row.dismissal_id is not None,
    )
    warnings = sorted(
        (row for row, block in zip(findings, blocking) if not block),
        key=lambda row: row.dismissal_id is not None,
    )

    def address(name: str) -> str:
        """Return one route of this opinion, for a ``data-`` attribute."""
        return reverse(
            name, kwargs={"pk": opinion.scan_id, "opinion_pk": opinion.pk}
        )

    return render(
        request,
        "scanning/opinion_review.html",
        {
            "opinion": opinion,
            "findings": findings,
            "open_findings": open_findings,
            "to_check": to_check,
            "warnings": warnings,
            "open_to_check": sum(
                1 for r in to_check if r.dismissal_id is None
            ),
            "open_warnings": sum(
                1 for r in warnings if r.dismissal_id is None
            ),
            # The three addresses the script reads (#365). They are
            # routes and not presigned URLs: each one is minted per
            # request, and a URL signed at render time would die in an
            # open tab.
            "pdf_url_endpoint": address("opinion_pdf_url"),
            "ensemble_url_endpoint": address("opinion_ensemble_url"),
            "rerun_url": address("rerun_opinion_ensemble"),
            # What the page draws, and what it says instead.
            "ensemble_written": ensemble.is_written(opinion),
            "ocr_written": ocr_written,
            # The button reads the OCR documents, so it appears only
            # where they exist and the text is not approved.
            "can_rerun": (
                ocr_written
                and opinion.status != OpinionReviewStatus.TEXT_REVIEW_DONE
            ),
            # The human edits (#376): the toolbar of a locked block,
            # offered where the endpoints take a write, the gate of
            # ``views_api._edit_context``.
            "can_edit": (
                opinion.status == OpinionReviewStatus.READY_FOR_TEXT_REVIEW
                and ensemble.is_written(opinion)
            ),
            "edit_text_url": address("edit_opinion_text"),
            "edit_section_url": address("edit_opinion_section"),
            "edit_move_url": address("move_opinion_block"),
            "edit_withdraw_url": address("withdraw_opinion_edit"),
            # Absent when the PDF pass has not written the file at
            # the live revision: the template shows the reason instead
            # of the two links and the column of the pages.
            "redacted_pdf_url": (
                reverse(
                    "serve_opinion_pdf",
                    kwargs={
                        "pk": opinion.scan_id,
                        "opinion_pk": opinion.pk,
                    },
                )
                if opinion_pdf.is_written(opinion)
                else ""
            ),
            "files_url": reverse(
                "opinion_file_index",
                kwargs={"pk": opinion.scan_id, "opinion_pk": opinion.pk},
            ),
            # The list the reviewer came from, so the back link keeps
            # their filters. Never fed to a redirect: it is a query
            # string on one known route, not a ``next``.
            "list_query": request.GET.urlencode(),
        },
    )


@login_required
def legacy_opinion_detail(request: HttpRequest, pk: int) -> HttpResponse:
    """Show one legacy opinion, with side-by-side PDF frames (#334).

    :param request: The current HTTP request.
    :param pk: The primary key of the opinion scan.
    :return: The rendered legacy opinion detail page.
    """
    opinion = get_object_or_404(
        OpinionScan.objects.select_related("reporter", "scan", "uploaded_by"),
        pk=pk,
    )

    return render(
        request,
        "scanning/legacy_opinion_detail.html",
        {"opinion": opinion},
    )


@login_required
def legacy_opinion_upload(request: HttpRequest) -> HttpResponse:
    """Upload one legacy opinion, for a superuser alone (#334).

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
            return redirect("legacy_opinion_detail", pk=opinion.pk)
    else:
        form = OpinionScanUploadForm()

    return render(
        request,
        "scanning/legacy_opinion_upload.html",
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
def repair_queue(request: HttpRequest) -> HttpResponse:
    """The queue of pages a scanner must scan again or scan anew (#249).

    Every user sees it. The rows are grouped by scan, so a scanner
    with the book fixes every page of a volume in one trip. Each row
    links to the page in step 1. The ``state`` filter reads
    ``repairs.QUEUE_STATES``; the default shows the requests that
    wait, which is the work.

    :param request: The current HTTP request.
    :return: The rendered repair queue page.
    """
    state = request.GET.get("state", "waiting")
    if state not in repairs.QUEUE_STATES:
        state = "waiting"
    reporter_filter = request.GET.get("reporter", "")

    rows = repairs.queue(state)
    if reporter_filter:
        rows = rows.filter(scan__reporter__short_name=reporter_filter)

    # Paginate the scans, then fetch the rows of the scans on the page.
    # A row is never deleted, so a page that loaded every row first
    # would grow with the history of the corpus.
    paginator = Paginator(repairs.queue_scan_ids(rows), 50)
    page_obj = paginator.get_page(request.GET.get("page"))
    groups = repairs.group_by_scan(rows, list(page_obj.object_list))

    return render(
        request,
        "scanning/repair_queue.html",
        {
            "page_obj": page_obj,
            "groups": groups,
            "state": state,
            "states": repairs.QUEUE_STATES,
            "reporters": Reporter.objects.all(),
            "selected_reporter": reporter_filter,
            # The waiting total is ``waiting_repairs_count``, from the
            # context processor that feeds the header badge: one query.
        },
    )


#: The query keys the queue reads. The dismissal returns the user to
#: the list they pressed the button on, and carries no other key.
REPAIR_QUEUE_KEYS = ("state", "reporter", "page")


@login_required
@require_POST
def dismiss_repair_from_queue(request: HttpRequest, pk: int) -> HttpResponse:
    """Close a repair request from the queue page (#393).

    The same rule as ``dismiss_page_repair``, the button on the page
    card: any logged-in user may dismiss, the row is stamped and never
    deleted, and a second press is a no-op. The card is not always
    there. A missing-page request draws its button on the placeholder
    of its gap, and the placeholder goes when the printed sequence
    stops showing the gap; the request then waits with no button
    anywhere, and holds the review-1 approval (#266). This view is the
    button that never goes.

    The redirect returns to the queue with the same filters. The keys
    are ``REPAIR_QUEUE_KEYS`` and the target is the queue itself, so
    no ``next`` string reaches a template.

    :param request: The HTTP request. Its query string is the queue's.
    :param pk: The request to dismiss.
    :return: A redirect to the queue.
    """
    rows = PageRepairRequest.objects.filter(pk=pk)
    if not rows.exists():
        raise Http404("Unknown request.")
    if repairs.dismiss(rows, request.user):
        messages.success(request, "Request dismissed.")
    else:
        messages.info(request, "This request was already dismissed.")
    query = {
        key: request.GET[key]
        for key in REPAIR_QUEUE_KEYS
        if request.GET.get(key)
    }
    url = reverse("repair_queue")
    if query:
        url += "?" + urlencode(query)
    return redirect(url)


@login_required
def stats_view(request: HttpRequest) -> HttpResponse:
    """How much work is done, and where the rest of it waits (#260).

    Every user sees it, as they see ``/repairs/``: the counts are not
    sensitive, and a scanner reads the same report as a member of the
    staff. The queries live in ``scanning.stats``.

    :param request: The current HTTP request.
    :return: The rendered stats page.
    """
    return render(request, "scanning/stats.html", stats.collect())


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
    stored. Both actions queue the pipeline (see
    ``services.apply_upload_action``); what differs is where the user
    lands -- ``upload_validate`` opens the process viewer,
    ``upload_only`` goes back to the queue.

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
    if size <= 0 or size > settings.MAX_ORIGINAL_UPLOAD_SIZE:
        limit_gb = settings.MAX_ORIGINAL_UPLOAD_SIZE // 1024**3
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
            scan,
            original_name,
            content_type,
            settings.MAX_ORIGINAL_UPLOAD_SIZE,
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

    # Marks the start of an upload. The bytes go straight from the browser
    # to S3, so this line is the only record on our side that a transfer
    # began: pair it with the matching "upload finished" line in
    # ``confirm_scan_upload`` to measure a volunteer's real throughput
    # (issue #181). Note the pairing survives a lost confirm only in the
    # log -- the ``PendingUpload`` row is deleted once the upload lands.
    logger.info(
        "Upload started for scan %s: %.1f MB, action=%s, pending %s, key %s",
        scan.pk,
        size / 1024 / 1024,
        action,
        pending.id,
        pending.s3_key,
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

    # Counterpart to the "Upload started" line in ``presign_scan_upload``.
    # Measured from the row's creation, so it includes the seconds between
    # presign and the browser's first byte; the size is the one the browser
    # declared at presign time, which the policy's content-length-range
    # condition caps but does not confirm. Close enough to answer "how fast
    # are our uploaders?" (issue #181) without a HEAD per upload.
    elapsed = (timezone.now() - pending.date_created).total_seconds()
    size_mb = pending.expected_size / 1024 / 1024
    logger.info(
        "Upload finished for scan %s: %.1f MB in %.1fs (%.2f MB/s), "
        "pending %s",
        scan.pk,
        size_mb,
        elapsed,
        size_mb / elapsed if elapsed > 0 else 0.0,
        pending.id,
    )
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
