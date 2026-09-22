"""The page repair requests of review 1: the rows, and how a reader uses them.

One finding a reviewer cannot fix is one ``PageRepairRequest`` row
(issue #249). This module holds the query helpers over those rows,
and the one derivation the rows do not store: whether a request is
fulfilled.

Four rules run through it:

- **A request is dismissed, never deleted.** ``dismiss`` stamps the
  row. The row stays as the audit.
- **Fulfilled is derived.** A request is fulfilled when a standing
  ``INSERT_PAGE`` or ``REPLACE_PAGE`` edit exists at its address, or
  when a one-page missing-page request is answered by a replacement
  of either page beside its gap (#393). No stamp, so the upload
  cannot race one, and an undo of the upload reopens the request for
  free.
- **A stale request is marked, never dropped.** A request made
  against an earlier upload of the original names a page the
  reviewer saw then. A person judges it; nothing applies it.
- **A waiting request holds the review open** (#266). One term serves
  the badge of the scan list, the badge and the section of step 1, the
  header count and the gate of the review-1 approval: open, and no
  fulfilling edit. A second definition would let two of those readers
  disagree on one volume.
"""

from __future__ import annotations

from django.db.models import (
    BooleanField,
    Count,
    Exists,
    ExpressionWrapper,
    F,
    OuterRef,
    Q,
    QuerySet,
    Value,
)
from django.db.models.functions import Coalesce
from django.db.models.lookups import Exact
from django.utils import formats, timezone

from scanning.models import PageEdit, PageRepairRequest, Scan

#: The most characters a note keeps. A note is what the reviewer saw,
#: in one or two sentences, not a report.
NOTE_MAX_CHARS = 500

#: The query filters of the queue view, by name. ``waiting`` is the
#: default: the requests a scanner still has to act on.
QUEUE_STATES = ("waiting", "fulfilled", "dismissed", "all")


def _later_standing_edits():
    """Return the ``PageEdit`` rows that may fulfil the outer request.

    Three conditions, all needed, and shared by the two shapes below:

    - **The edit is later than the request.** A reviewer who finds the
      replacement blurry too asks again, and an edit that was already
      there when they asked answers nothing. Without the date, a
      request over an address that holds an edit is born fulfilled and
      no scanner ever sees it.
    - **A curator has not taken it back** (#232).
    - **It was made against the same upload of the original.** Every
      address is a page of the original as uploaded, and the original
      never changes: the apply (#206) writes another file and leaves
      it alone. So the fingerprint moves for one reason only, a
      re-upload, and an edit counted against the earlier upload names
      a leaf of another book. A blank fingerprint on either side
      matches anything, the rule of ``page_edits.is_stale``.

    ``applied_at`` is not read. It answers a different question, "is
    this decision built into an output?", and an applied edit against
    the current upload is done work that fulfils like a standing one.

    **The scan is read through the outer row, never through the edit.**
    The subquery already ties the edit to the request's scan, so the
    two reads land on one row of the table. The database cannot know
    that, and it joins the table a second time for the inner read: the
    join was 77% of the buffers this query touched, over 5000 waiting
    requests and 20000 edits. So the blank test on the scan is an
    ``Exact`` over the ``OuterRef``, which reads the join the outer
    query already carries. Do not write it as
    ``Q(scan__source_fingerprint="")`` again.

    :returns: A queryset the two shapes narrow by address.
    :rtype: QuerySet
    """
    same_original = (
        Q(source_fingerprint="")
        | Q(Exact(OuterRef("scan__source_fingerprint"), Value("")))
        | Q(source_fingerprint=OuterRef("scan__source_fingerprint"))
    )
    return PageEdit.objects.filter(
        same_original,
        scan=OuterRef("scan"),
        withdrawn_at__isnull=True,
        date_created__gt=OuterRef("date_created"),
    )


def _edits_at_the_address():
    """Return the edits of the request's own shape at its address.

    An insert in the gap fulfils an INSERT; a replacement of the page
    fulfils a REPLACE. A REPLACE request has no anchor and an INSERT
    request has no page, so each clause matches its own action alone.

    :returns: A queryset for an ``Exists`` annotation.
    :rtype: QuerySet
    """
    return _later_standing_edits().filter(
        Q(
            kind=PageEdit.Kind.REPLACE_PAGE,
            pdf_page=OuterRef("pdf_page"),
        )
        | Q(
            kind=PageEdit.Kind.INSERT_PAGE,
            anchor_pdf_page=OuterRef("anchor_pdf_page"),
        )
    )


def _replacements_beside_the_gap():
    """Return the replacements of either page beside an INSERT request's gap.

    The obvious case of #393: a blurry page with no number gives the
    reviewer a Replace button and a missing-page placeholder after
    it. The reviewer asks at the placeholder, the scanner scans the
    blurry page again, and the new page is the one asked for. The
    scanner did the work at the gap's edge, so the request is
    fulfilled. Both neighbours count, because the analysis may place
    the placeholder on either side of the page it could not read.

    The anchor is null on a REPLACE request, so this matches nothing
    there. :func:`annotate_fulfilled` adds the other half of the rule,
    which lives on the request row: a replacement is one page exactly
    (``REPLACEMENT_IS_ONE_PAGE_MESSAGE``), so it never answers a
    request for a range of pages.

    :returns: A queryset for an ``Exists`` annotation.
    :rtype: QuerySet
    """
    return _later_standing_edits().filter(
        Q(kind=PageEdit.Kind.REPLACE_PAGE)
        & (
            Q(pdf_page=OuterRef("anchor_pdf_page"))
            | Q(pdf_page=OuterRef("anchor_pdf_page") + 1)
        )
    )


#: The label of a request for a range of pages holds one hyphen
#: (#233). A replacement is one page, so it answers no such request.
RANGE_LABEL_MARK = "-"


def annotate_fulfilled(rows: QuerySet) -> QuerySet:
    """Add the derived ``fulfilled`` flag, and order by address.

    The address is whichever column the action uses, so the order
    reads like the volume: page 1, then the gap after page 1, then
    page 2. A page sorts at twice its number and a gap one past the
    page it follows, so the gap comes after that page and before the
    next.

    ``fulfilled`` is one of two shapes (#393): an edit of the request's
    own kind at its address (``fulfilled_at_address``), or a
    replacement of either page beside the gap of a one-page INSERT
    request (``fulfilled_beside``). The two are kept as their own
    annotations so :func:`as_dict` can say which shape answered, and
    the viewer can tell the reviewer what to check.

    :param rows: ``PageRepairRequest`` rows.
    :returns: The same rows, each with the booleans ``fulfilled``,
        ``fulfilled_at_address`` and ``fulfilled_beside``, and an
        integer ``sort_address``.
    :rtype: QuerySet
    """
    return (
        rows.annotate(
            fulfilled_at_address=Exists(_edits_at_the_address()),
            fulfilled_beside=Exists(_replacements_beside_the_gap()),
        )
        .annotate(
            fulfilled=ExpressionWrapper(
                Q(fulfilled_at_address=True)
                | (
                    Q(fulfilled_beside=True)
                    & ~Q(logical_page__contains=RANGE_LABEL_MARK)
                ),
                output_field=BooleanField(),
            ),
            sort_address=Coalesce(
                F("pdf_page") * 2, F("anchor_pdf_page") * 2 + 1
            ),
        )
        .order_by("scan_id", "sort_address", "pk")
    )


def fulfilled_by(row: PageRepairRequest) -> str | None:
    """Return the kind of edit that answered the request, or ``None``.

    ``"insert"`` is an insert in the gap of an INSERT request;
    ``"replace"`` is a replacement of the page of a REPLACE request, or
    of a page beside the gap of an INSERT request (#393). The viewer
    reads it with ``action``: an INSERT request answered by
    ``"replace"`` is the one whose new page the reviewer must check
    against the page asked for.

    :param row: A request with the annotations of
        :func:`annotate_fulfilled`.
    :returns: ``"insert"``, ``"replace"`` or ``None``.
    :rtype: str | None
    """
    if not getattr(row, "fulfilled", False):
        return None
    if getattr(row, "fulfilled_at_address", False):
        return (
            "insert"
            if row.action == PageRepairRequest.Action.INSERT
            else "replace"
        )
    return "replace"


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


def has_waiting(scan: Scan) -> bool:
    """Return whether a scanner still has to act on this scan (#266).

    The gate of the review-1 approval, and the same term as every
    other reader: open, and no fulfilling edit. A **stale** request
    waits too, unlike a stale ``PageEdit``, which does not hold the
    review open (#214). The two rows differ: an apply cannot place a
    stale edit, but a request is work for a person, and a person
    judges a stale request and dismisses it with one click. A
    **fulfilled** request never waits, because the scanner did the
    work; the row stays open so the reviewer can judge the new page
    and ask again (#249).

    No row crosses into Python: the database evaluates the ``Exists``
    of the derivation. The ordering of :func:`annotate_fulfilled` is
    cleared, because an ``EXISTS`` needs none.

    :param scan: The scan the reviewer wants to approve.
    :returns: Whether one open request has no fulfilling edit.
    :rtype: bool
    """
    return (
        annotate_fulfilled(
            scan.repair_requests.filter(dismissed_at__isnull=True)
        )
        .filter(fulfilled=False)
        .order_by()
        .exists()
    )


def waiting_counts(scan_ids) -> dict[int, int]:
    """Return how many requests wait, per scan, in one query (#266).

    The badge of the scan list. The list paginates 25 scans, and the
    caller asks for the ids of one page only, so the work is bound by
    the page size and not by the size of the corpus. One grouped
    count, never a subquery per row.

    **The ordering is cleared before the grouping.** Django puts the
    ordering columns of the queryset into ``GROUP BY``, and
    :func:`annotate_fulfilled` orders by ``sort_address``, which is a
    per-row expression. Without the clear the database groups by the
    address as well, and a scan with two waiting requests reads 1
    twice instead of 2 once.

    :param scan_ids: The scans to count, usually one page of a list.
    :returns: The number of waiting requests, by scan id. A scan with
        none is absent.
    :rtype: dict[int, int]
    """
    rows = (
        queue("waiting")
        .filter(scan_id__in=scan_ids)
        .order_by()
        .values("scan_id")
        .annotate(waiting=Count("pk"))
    )
    return {row["scan_id"]: row["waiting"] for row in rows}


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
        "fulfilled_by": fulfilled_by(row),
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


def project_requests(page_map: list[dict], requests: list[dict]) -> list[dict]:
    """Give every open INSERT request a placeholder in the viewer (#393).

    The viewer draws an INSERT request on the placeholder of its gap,
    and a placeholder is a ``missing`` entry of the page map, which
    the sequence analysis alone writes. When the sequence stops
    showing the gap -- the blurry page was scanned again and read, or
    a curator typed its number -- the placeholder goes, and with it
    the request's note, its Dismiss button and its insert form. The
    request then waits with no control on the page, and holds the
    review-1 approval (#266).

    So a request whose gap has no placeholder gets one here, flagged
    ``from_request``, right after the page it follows and after the
    images already uploaded into that gap. The viewer draws it as a
    placeholder whose heading says why it stands on a closed
    sequence. A request whose anchor page is not in the map goes
    last, like an unplaced insert (``page_edits.project_inserts``).

    This is a projection for the viewer and never a write to the
    stored map: a ``missing`` entry stored for a request would tell
    the sequence checks that a page is missing when the sequence says
    it is not.

    :param page_map: The page map to render, after
        ``page_edits.project_inserts`` stamped the anchors.
    :param requests: :func:`viewer_payload`'s dicts.
    :returns: The page map with one placeholder per uncovered request.
        Not modified in place.
    :rtype: list[dict]
    """
    covered = {
        entry.get("anchor_pdf_page")
        for entry in page_map
        if entry.get("type") == "missing"
    }
    uncovered = [
        r
        for r in requests
        if r["action"] == PageRepairRequest.Action.INSERT
        and r["anchor_pdf_page"] not in covered
    ]
    if not uncovered:
        return page_map

    def _placeholder(r: dict) -> dict:
        label = str(r["logical_page"] or "")
        entry = {
            "type": "missing",
            "logical_number": label,
            "anchor_pdf_page": r["anchor_pdf_page"],
            "from_request": True,
        }
        first, mark, last = label.partition(RANGE_LABEL_MARK)
        if mark and first.isdigit() and last.isdigit():
            entry["missing_range"] = [int(first), int(last)]
        return entry

    out = list(page_map)
    unplaced: list[dict] = []
    for r in sorted(uncovered, key=lambda r: r["anchor_pdf_page"]):
        anchor = r["anchor_pdf_page"]
        if anchor == 0:
            at = 0
        else:
            at = next(
                (
                    position + 1
                    for position, entry in enumerate(out)
                    if entry.get("type") == "pdf_page"
                    and entry.get("pdf_index") == anchor - 1
                ),
                None,
            )
            if at is None:
                unplaced.append(_placeholder(r))
                continue
        # The images already uploaded into this gap come first, so the
        # card reads "and this one is still asked for".
        while at < len(out) and out[at].get("type") == "inserted":
            at += 1
        out.insert(at, _placeholder(r))
    out.extend(unplaced)
    return out


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


def waiting_totals() -> tuple[int, int]:
    """Return how many requests wait, and over how many scans (#260).

    The stats page prints "X repairs over Y scans", and the two
    numbers come from one aggregate: two queries could disagree,
    because a reviewer may add a request between them, and no row
    crosses into Python for a count the database gives.

    :returns: The number of requests, then the number of scans.
    :rtype: tuple[int, int]
    """
    totals = queue("waiting").aggregate(
        requests=Count("pk"), scans=Count("scan_id", distinct=True)
    )
    return totals["requests"], totals["scans"]
