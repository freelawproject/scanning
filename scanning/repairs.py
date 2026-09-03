"""The page repair requests of review 1: the rows, and how a reader uses them.

One finding a reviewer cannot fix is one ``PageRepairRequest`` row
(issue #249). This module holds the query helpers over those rows,
and the one derivation the rows do not store: whether a request is
fulfilled.

Three rules run through it:

- **A request is dismissed, never deleted.** ``dismiss`` stamps the
  row. The row stays as the audit.
- **Fulfilled is derived.** A request is fulfilled when a standing
  ``INSERT_PAGE`` or ``REPLACE_PAGE`` edit exists at its address.
  No stamp, so the upload cannot race one, and an undo of the upload
  reopens the request for free.
- **A stale request is marked, never dropped.** A request made
  against an earlier upload of the original names a page the
  reviewer saw then. A person judges it; nothing applies it.
"""

from __future__ import annotations

from django.db.models import Exists, F, OuterRef, Q, QuerySet
from django.db.models.functions import Coalesce
from django.utils import formats, timezone

from scanning.models import PageEdit, PageRepairRequest, Scan

#: The most characters a note keeps. A note is what the reviewer saw,
#: in one or two sentences, not a report.
NOTE_MAX_CHARS = 500

#: The query filters of the queue view, by name. ``waiting`` is the
#: default: the requests a scanner still has to act on.
QUEUE_STATES = ("waiting", "fulfilled", "dismissed", "all")


def _fulfilling_edits():
    """Return the ``PageEdit`` rows that fulfil the outer request.

    An insert in the gap fulfils an INSERT; a replacement of the page
    fulfils a REPLACE. Three conditions, all needed:

    - **The edit is later than the request.** A reviewer who finds the
      replacement blurry too asks again, and an edit that was already
      there when they asked answers nothing. Without the date, a
      request over an address that holds an edit is born fulfilled and
      no scanner ever sees it.
    - **A curator has not taken it back** (#232).
    - **The apply built it in, or it was made against the current
      original.** An applied edit (#206) is done work, whatever the
      fingerprint says: the apply changes the original, so its own
      edits never match the new one. A standing edit against another
      original is not applied and fulfils nothing. A blank fingerprint
      on either side matches anything, the rule of
      ``page_edits.is_stale``.

    :returns: A queryset for an ``Exists`` annotation.
    :rtype: QuerySet
    """
    same_original = (
        Q(source_fingerprint="")
        | Q(scan__source_fingerprint="")
        | Q(source_fingerprint=OuterRef("scan__source_fingerprint"))
    )
    done_or_current = Q(applied_at__isnull=False) | same_original
    at_the_address = Q(
        kind=PageEdit.Kind.REPLACE_PAGE,
        pdf_page=OuterRef("pdf_page"),
    ) | Q(
        kind=PageEdit.Kind.INSERT_PAGE,
        anchor_pdf_page=OuterRef("anchor_pdf_page"),
    )
    return PageEdit.objects.filter(
        done_or_current,
        at_the_address,
        scan=OuterRef("scan"),
        withdrawn_at__isnull=True,
        date_created__gt=OuterRef("date_created"),
    )


def annotate_fulfilled(rows: QuerySet) -> QuerySet:
    """Add the derived ``fulfilled`` flag, and order by address.

    The address is whichever column the action uses, so the order
    reads like the volume: page 1, then the gap after page 1, then
    page 2. A page sorts at twice its number and a gap one past the
    page it follows, so the gap comes after that page and before the
    next.

    :param rows: ``PageRepairRequest`` rows.
    :returns: The same rows, each with a boolean ``fulfilled`` and an
        integer ``sort_address``.
    :rtype: QuerySet
    """
    return rows.annotate(
        fulfilled=Exists(_fulfilling_edits()),
        sort_address=Coalesce(F("pdf_page") * 2, F("anchor_pdf_page") * 2 + 1),
    ).order_by("scan_id", "sort_address", "pk")


def open_requests(scan: Scan) -> QuerySet:
    """Return the scan's requests nobody has dismissed.

    :param scan: The scan whose requests are wanted.
    :returns: The open rows, with ``fulfilled``, in address order.
    :rtype: QuerySet
    """
    return annotate_fulfilled(
        scan.repair_requests.filter(dismissed_at__isnull=True)
    ).select_related("requested_by")


def waiting_requests(scan: Scan) -> list[PageRepairRequest]:
    """Return the requests a scanner still has to act on.

    :param scan: The scan whose requests are wanted.
    :returns: The open rows with no fulfilling edit.
    :rtype: list[PageRepairRequest]
    """
    return [row for row in open_requests(scan) if not row.fulfilled]


def is_stale(row: PageRepairRequest, scan: Scan) -> bool:
    """Return whether a request describes an earlier upload of the scan.

    The same rule as ``PageRepairRequest.is_stale``, for a caller that
    holds the scan already and does not want the row to load it.

    :param row: The request to judge.
    :param scan: The scan it belongs to.
    :returns: Whether the fingerprints differ. A blank on either side
        matches anything.
    :rtype: bool
    """
    if not row.source_fingerprint or not scan.source_fingerprint:
        return False
    return row.source_fingerprint != scan.source_fingerprint


def dismiss(rows: QuerySet, user) -> int:
    """Close the requests in ``rows`` without deleting them.

    A row already dismissed is left as it is: the first judgement
    stands, and a second click must not rewrite it.

    :param rows: ``PageRepairRequest`` rows.
    :param user: Who dismissed them.
    :returns: How many rows were stamped.
    :rtype: int
    """
    # ``update`` skips ``auto_now``; the audit reads ``date_modified``
    # as the last touch, so it is written by hand.
    now = timezone.now()
    return rows.filter(dismissed_at__isnull=True).update(
        dismissed_at=now, dismissed_by=user, date_modified=now
    )


def as_dict(row: PageRepairRequest, scan: Scan) -> dict:
    """Return the shape the viewer reads for one request.

    :param row: A request with the ``fulfilled`` annotation.
    :param scan: The scan it belongs to.
    :returns: The fields the viewer draws.
    :rtype: dict
    """
    return {
        "id": row.pk,
        "action": row.action,
        "action_label": row.get_action_display(),
        "pdf_page": row.pdf_page,
        "anchor_pdf_page": row.anchor_pdf_page,
        "logical_page": row.logical_page,
        "note": row.note,
        "requested_by": row.requested_by.username,
        # The queue template writes the same date with ``|date``, which
        # localizes; so does this, or one request shows two dates.
        "date_created": formats.date_format(
            timezone.localtime(row.date_created), "Y-m-d"
        ),
        "fulfilled": bool(getattr(row, "fulfilled", False)),
        "stale": is_stale(row, scan),
        "nav_pdf_index": row.nav_pdf_index,
    }


def viewer_payload(scan: Scan) -> list[dict]:
    """Return every open request of the scan, for the viewer.

    :param scan: The scan being rendered.
    :returns: One dict per open request, in address order.
    :rtype: list[dict]
    """
    return [as_dict(row, scan) for row in open_requests(scan)]


def queue(state: str = "waiting") -> QuerySet:
    """Return the requests of every scan, for the queue view.

    :param state: One of ``QUEUE_STATES``. An unknown state reads as
        ``waiting``.
    :returns: The rows, with ``fulfilled``, newest scan first.
    :rtype: QuerySet
    """
    rows = annotate_fulfilled(
        PageRepairRequest.objects.select_related(
            "scan",
            "scan__reporter",
            "scan__uploaded_by",
            "scan__volume_obj__assigned_to",
            "requested_by",
            "dismissed_by",
        )
    )
    if state == "dismissed":
        rows = rows.filter(dismissed_at__isnull=False)
    elif state == "fulfilled":
        rows = rows.filter(dismissed_at__isnull=True, fulfilled=True)
    elif state == "all":
        pass
    else:
        rows = rows.filter(dismissed_at__isnull=True, fulfilled=False)
    return rows.order_by("-scan_id", "sort_address", "pk")


def queue_scan_ids(rows: QuerySet) -> QuerySet:
    """Return the ids of the scans the rows belong to, newest first.

    The queue page is paginated by scan, not by row: one page of the
    queue is one trip to the shelf. The ids are what the paginator
    slices, so the database bounds the work by page size, and the rows
    of one page are fetched by these ids alone. A row is never
    deleted, so the ``all`` and ``dismissed`` states grow for good and
    a page that loaded every row would grow with them.

    :param rows: A queryset from :func:`queue`.
    :returns: Distinct scan ids, one per group, newest first.
    :rtype: QuerySet
    """
    return (
        rows.order_by("-scan_id").values_list("scan_id", flat=True).distinct()
    )


def group_by_scan(rows: QuerySet, scan_ids: list[int]) -> list[dict]:
    """Return the rows of the given scans, grouped and in id order.

    :param rows: A queryset from :func:`queue`.
    :param scan_ids: One page of :func:`queue_scan_ids`, in order.
    :returns: ``[{"scan": scan, "requests": [row, ...]}, ...]`` in the
        order of ``scan_ids``. A scan with no row on this page is left
        out, which cannot happen for ids the same queryset produced.
    :rtype: list[dict]
    """
    by_scan: dict[int, dict] = {}
    for row in rows.filter(scan_id__in=scan_ids):
        group = by_scan.setdefault(
            row.scan_id, {"scan": row.scan, "requests": []}
        )
        group["requests"].append(row)
    return [by_scan[pk] for pk in scan_ids if pk in by_scan]


def waiting_count() -> int:
    """Return how many requests wait, over every scan.

    One query. The header shows it beside the "Repairs" link.

    :returns: The count.
    :rtype: int
    """
    return queue("waiting").count()
