"""The human page edits of review 1: the rows, and how a reader uses them.

One curator decision is one ``PageEdit`` row (issue #214). This module
holds the query helpers over those rows and the overlay that writes a
curator's page numbers over what the model read.

Two rules run through it:

- **The rows are the source; ``Scan.ocr_results`` is a cache.** The
  blob is rebuilt whole, from the glued dots.mocr run plus these rows,
  on every recompute. Nothing edits one entry of it in place any more,
  so two curators working on two pages of one volume cannot write each
  other's decision away.
- **A decision is closed, never deleted.** A curator who takes an
  edit back stamps ``withdrawn_at``, and a second file uploaded for
  one page withdraws the row before it (#232). So the rows carry
  every decision a person made about the volume, in order.
- **An edit whose original is gone is reported, never guessed at.**
  Every address names a page of the original as it was when the edit
  was made (``PageEdit.source_fingerprint``). An edit made against
  another original, or one naming a page the current volume does not
  have, is dropped from the overlay and raised as an issue the curator
  can see. Silently keeping it would put a number on the wrong page;
  silently dropping it is the defect this model exists to remove.
"""

from __future__ import annotations

import logging

from django.urls import reverse
from django.utils import timezone

from scanning.models import CheckName, Issue, PageEdit, Scan

logger = logging.getLogger(__name__)


def open_edits(scan: Scan, *kinds: str):
    """Return the scan's standing edits of the given kinds.

    A decision stands until one of two stamps closes it: the apply
    (#206) writes ``applied_at``, and a curator who takes it back
    writes ``withdrawn_at`` (#232). Neither stamp deletes anything, so
    the closed rows stay as the audit of what a person decided and
    when. Both are filtered here, once, because every reader of these
    rows comes through this function.

    :param scan: The scan whose edits are wanted.
    :param kinds: ``PageEdit.Kind`` values. All kinds when empty.
    :returns: A queryset of open rows, in address order.
    :rtype: django.db.models.QuerySet
    """
    rows = scan.page_edits.filter(
        applied_at__isnull=True, withdrawn_at__isnull=True
    )
    if kinds:
        rows = rows.filter(kind__in=kinds)
    return rows


def withdraw(rows, user) -> int:
    """Take back the decisions in ``rows``, without deleting them.

    A curator who undoes a deletion, removes an inserted page, or
    uploads a second file for one page takes a decision back. The row
    stays, stamped, so the audit shows what was decided and when it
    was undone (#232). The portal used to delete the row and, for an
    insert, the file with it.

    :param rows: A queryset of ``PageEdit`` rows to close.
    :param user: Who took them back. May be None.
    :returns: How many rows were stamped.
    :rtype: int
    """
    # ``update`` skips ``auto_now``, so the row's ``date_modified`` is
    # written by hand: the audit reads it as the last touch.
    now = timezone.now()
    return rows.update(withdrawn_at=now, withdrawn_by=user, date_modified=now)


def is_stale(edit: PageEdit, scan: Scan) -> bool:
    """Return whether an edit describes an original this scan no longer has.

    A blank fingerprint on either side is a legacy value -- the row or
    the scan predates the field -- and matches anything, the same
    tolerance ``jobs._still_describes`` gives a legacy job row.

    :param edit: The edit to judge.
    :param scan: The scan it belongs to.
    :returns: Whether the edit was made against another original.
    :rtype: bool
    """
    if not edit.source_fingerprint or not scan.source_fingerprint:
        return False
    return edit.source_fingerprint != scan.source_fingerprint


def current_edits(scan: Scan, *kinds: str) -> list[PageEdit]:
    """Return the unapplied edits that describe *this* original.

    What every consumer that **acts** on an edit must read. A row whose
    fingerprint does not match the scan's was made against another
    document, so its address names a page nobody chose: applying it
    would delete, or displace, a page the curator never saw.

    :param scan: The scan whose edits are wanted.
    :param kinds: ``PageEdit.Kind`` values. All kinds when empty.
    :returns: The open rows that still describe this original.
    :rtype: list[PageEdit]
    """
    return [
        edit for edit in open_edits(scan, *kinds) if not is_stale(edit, scan)
    ]


def stale_open_edits(scan: Scan, *kinds: str) -> list[PageEdit]:
    """Return the unapplied edits that describe another original.

    The other half of :func:`current_edits`, for the reader that has to
    *report* them. They are never applied and never silently dropped.

    :param scan: The scan whose edits are wanted.
    :param kinds: ``PageEdit.Kind`` values. All kinds when empty.
    :returns: The open rows made against another original.
    :rtype: list[PageEdit]
    """
    return [edit for edit in open_edits(scan, *kinds) if is_stale(edit, scan)]


def has_pending_changes(scan: Scan) -> bool:
    """Return whether the scan carries edits the apply has not built in.

    Only the structural kinds count. A page number and a dismissal need
    no apply: every recompute reads them off the rows.

    :param scan: The scan to check.
    :returns: Whether an unapplied structural edit exists.
    :rtype: bool
    """
    # The current ones only: a stale row can never be applied, so
    # counting it would hold the review open for good. It is reported
    # as an issue instead, which is the channel a person can act on.
    return bool(current_edits(scan, *PageEdit.STRUCTURAL_KINDS))


def pending_edit_flags(scan: Scan) -> dict:
    """Return the two pending-edit flags the step-1 bar reads.

    One read for both, because they answer one question about one set
    of rows: the outer condition is "are there unapplied edits", the
    inner one is "does applying them cost a GPU run". Two reads let
    the pair disagree, which is how one of them came to count a stale
    insert the other refused.

    Only ``has_pending_changes`` has a reader today: it raises the
    banner and the badge of step 1. ``has_pending_inserts`` is kept
    for the apply of #206, whose button is the one that must warn
    about a paid run; the button that used to read it applied nothing
    and was deleted with the bar branch that held it (#232).

    :param scan: The scan the bar is rendered for.
    :returns: ``has_pending_changes`` and ``has_pending_inserts``.
    :rtype: dict
    """
    pending = current_edits(scan, *PageEdit.STRUCTURAL_KINDS)
    with_an_image = (PageEdit.Kind.INSERT_PAGE, PageEdit.Kind.REPLACE_PAGE)
    return {
        "has_pending_changes": bool(pending),
        "has_pending_inserts": any(
            edit.kind in with_an_image for edit in pending
        ),
    }


def deleted_pages(scan: Scan) -> set[int]:
    """Return the 1-based original pages marked for deletion.

    :param scan: The scan to read.
    :returns: The pages a curator marked: unapplied, and against this
        original.
    :rtype: set[int]
    """
    return {
        edit.pdf_page
        for edit in current_edits(scan, PageEdit.Kind.DELETE_PAGE)
    }


def replacements_by_page(scan: Scan) -> dict[int, PageEdit]:
    """Return the standing replacements, keyed by the page they cover.

    What the viewer reads to show that a page was replaced (#232). The
    current rows only: a row made against another original names a
    page nobody chose, and ``stale_edit_issues`` reports it instead.

    :param scan: The scan to read.
    :returns: ``{pdf_page: edit}``, one row per page at most, which
        the partial unique key holds.
    :rtype: dict[int, PageEdit]
    """
    return {
        edit.pdf_page: edit
        for edit in current_edits(scan, PageEdit.Kind.REPLACE_PAGE)
    }


def inserts_by_gap(scan: Scan) -> dict[int, list[PageEdit]]:
    """Return the images to insert, keyed by the page they follow.

    What the apply and the export read, so it holds the current rows
    only -- an image anchored in another original has no gap here.

    :param scan: The scan to read.
    :returns: ``{anchor_pdf_page: [edit, ...]}``, each list in
        ``ordinal`` order. Key 0 holds the images that go before page 1.
    :rtype: dict[int, list[PageEdit]]
    """
    gaps: dict[int, list[PageEdit]] = {}
    rows = sorted(
        current_edits(scan, PageEdit.Kind.INSERT_PAGE),
        key=lambda edit: (edit.anchor_pdf_page, edit.ordinal),
    )
    for edit in rows:
        gaps.setdefault(edit.anchor_pdf_page, []).append(edit)
    return gaps


def next_ordinal(scan: Scan, anchor_pdf_page: int) -> int:
    """Return the next free position for an image in one gap.

    :param scan: The scan the insert belongs to.
    :param anchor_pdf_page: The original page the image follows.
    :returns: One past the highest ordinal in that gap, or 0.
    :rtype: int
    """
    taken = [
        edit.ordinal for edit in inserts_by_gap(scan).get(anchor_pdf_page, [])
    ]
    return max(taken) + 1 if taken else 0


def uploaded_kind(edit: PageEdit) -> str:
    """Return whether a curator's file is a PDF or an image.

    The stored key keeps the extension the browser sent, narrowed by
    ``models.page_edit_image_path``, and the upload endpoint writes it
    from the sniffed bytes rather than from the name (#232). So the
    extension is the answer, and the viewer draws a PDF in a canvas
    where it draws an image in an ``<img>``.

    :param edit: An insert or a replacement row.
    :returns: ``"pdf"`` or ``"image"``. An empty file reads as an
        image, which is what the viewer's fallback draws.
    :rtype: str
    """
    name = (edit.image.name or "") if edit.image else ""
    return "pdf" if name.lower().endswith(".pdf") else "image"


def _inserted_entry(
    edit: PageEdit, entry: dict | None, unplaced: bool = False
) -> dict:
    """Return the page map entry that shows one uploaded image.

    :param edit: The ``INSERT_PAGE`` row.
    :param entry: The ``missing`` placeholder it fills, when there is
        one. Without it the entry is built from the row alone, which is
        what an insert whose placeholder a later OCR run removed needs.
    :param unplaced: Whether the volume has no position for this image.
        The viewer says so, and offers the same Remove button.
    :returns: An ``inserted`` page map entry.
    :rtype: dict
    """
    out = dict(entry or {})
    out["type"] = "inserted"
    out["insert_url"] = edit.image.url if edit.image else ""
    out["insert_edit_id"] = edit.pk
    out["insert_kind"] = uploaded_kind(edit)
    # The signed URL above expires within the hour, and a review page
    # stays open for longer. A link a curator may press later goes
    # through the view that signs one at the moment of the click.
    out["insert_file_url"] = reverse(
        "page_edit_file", kwargs={"pk": edit.scan_id, "edit_id": edit.pk}
    )
    out["unplaced"] = unplaced
    if not out.get("logical_number"):
        out["logical_number"] = edit.logical_page or ""
    return out


def project_inserts(scan: Scan, page_map: list[dict]) -> list[dict]:
    """Show each uploaded image at the gap its row names, and stamp the
    anchor on every placeholder.

    Two jobs, one walk, because both need the same running answer to
    "which original page does this position follow?".

    The stamp is the fix for the address the portal used to compute and
    throw away: the viewer resolved a placeholder's physical neighbour
    at render time, uploaded the image under the *printed* number, and
    left the apply with no way back to a position. Now the placeholder
    carries ``anchor_pdf_page``, the upload sends it back, and the row
    stores it once.

    An insert whose placeholder is gone -- a later OCR run read the
    number the placeholder stood for -- is still shown, right after its
    anchor page. Dropping it would hide a page a curator uploaded.

    **Every open insert reaches the viewer**, including the ones this
    volume has no position for: an image anchored past the last page, a
    page map that is empty or has lost the anchor page, and one made
    against another original. They go last, flagged ``unplaced``,
    because the viewer's Remove button is the only way to take an
    insert back. An image the walk dropped would strand its row where
    nothing in the portal could reach it.

    :param scan: The scan whose page map is being rendered.
    :param page_map: The stored page map. Not modified in place.
    :returns: The page map to render.
    :rtype: list[dict]
    """
    gaps = inserts_by_gap(scan)
    out: list[dict] = []
    placed: set[int] = set()
    anchor = 0
    queue = list(gaps.get(0, []))

    def _flush(queued):
        """Emit the images of a gap the walk is leaving.

        :param queued: The rows still queued for that gap.
        :returns: Their page map entries.
        """
        placed.update(edit.pk for edit in queued)
        return [_inserted_entry(edit, None) for edit in queued]

    for entry in page_map:
        if entry.get("type") == "pdf_page":
            out.extend(_flush(queue))
            out.append(entry)
            anchor = entry["pdf_index"] + 1
            queue = list(gaps.get(anchor, []))
            continue
        if entry.get("type") == "missing":
            entry = dict(entry, anchor_pdf_page=anchor)
            if queue:
                edit = queue.pop(0)
                placed.add(edit.pk)
                out.append(_inserted_entry(edit, entry))
                continue
        out.append(entry)
    out.extend(_flush(queue))

    out.extend(
        _inserted_entry(edit, None, unplaced=True)
        for edit in open_edits(scan, PageEdit.Kind.INSERT_PAGE)
        if edit.pk not in placed
    )
    return out


def overlay_page_numbers(
    scan: Scan, results: list[dict]
) -> tuple[list[dict], list[PageEdit]]:
    """Write the curator's page numbers over the model's readings.

    The curator outranks the model: a typed number is the one thing in
    ``Scan.ocr_results`` a rerun of the OCR stage must not overwrite.
    Each overlaid entry keeps the ``"manual"`` stamp the rest of the
    portal reads (``services._is_manual_read``, the sidebar), so the
    stamp stays a derived marker and the row stays the source.

    :param scan: The scan the results belong to.
    :param results: Per-page entries, straight from the OCR stage.
    :returns: The overlaid entries, and the edits that could not be
        placed on a page of this original.
    :rtype: tuple[list[dict], list[PageEdit]]
    """
    by_page = {entry["pdf_page"]: entry for entry in results}
    stale: list[PageEdit] = []
    for edit in open_edits(scan, PageEdit.Kind.SET_NUMBER):
        entry = by_page.get(edit.pdf_page)
        if entry is None or is_stale(edit, scan):
            logger.warning(
                "page_edits: scan %s: page number %r for PDF page %s was "
                "not applied (%s)",
                scan.pk,
                edit.value,
                edit.pdf_page,
                "the original changed"
                if entry is not None
                else "the volume has no such page",
            )
            stale.append(edit)
            continue
        if edit.value:
            entry["detected"] = edit.value
            entry["type"] = "range" if "-" in edit.value else "single"
            entry["score"] = 1.0
        else:
            entry["detected"] = None
            entry["type"] = None
            entry["score"] = None
        entry["zone"] = "manual"
        entry["ocr"] = "manual"
    return results, stale


def _dismissal_matches(edit: PageEdit, issue: dict) -> bool:
    """Return whether one dismissal covers one rebuilt issue.

    An issue names a page in one of two spaces, and the check decides
    which: a physical PDF page for the checks in
    ``models.PHYSICAL_PAGE_CHECKS``, and the printed number for the
    rest, which is why the dismissal keeps both columns.

    :param edit: An open ``DISMISS_ISSUE`` row.
    :param issue: One rebuilt issue dict, before it becomes a row.
    :returns: Whether the curator already dismissed this issue.
    :rtype: bool
    """
    from scanning.models import PHYSICAL_PAGE_CHECKS

    if edit.value != issue["check_name"]:
        return False
    page = issue.get("page_number")
    if page is None:
        return edit.pdf_page is None and not edit.logical_page
    if issue["check_name"] in PHYSICAL_PAGE_CHECKS:
        return edit.pdf_page == page
    return edit.pdf_page is None and edit.logical_page == str(page)


def drop_dismissed(scan: Scan, issues: list[dict]) -> list[dict]:
    """Remove the issues a curator has already dismissed.

    A rebuild writes new ``Issue`` rows with new primary keys, so a
    dismissal cannot be a row that was deleted: it is a decision, and
    it lives with the other decisions. The match is on the check's name
    plus its page, for that reason.

    :param scan: The scan being rebuilt.
    :param issues: The rebuilt issue dicts.
    :returns: The dicts the curator has not dismissed.
    :rtype: list[dict]
    """
    dismissals = [
        edit
        for edit in open_edits(scan, PageEdit.Kind.DISMISS_ISSUE)
        if not is_stale(edit, scan)
    ]
    if not dismissals:
        return issues
    return [
        issue
        for issue in issues
        if not any(_dismissal_matches(e, issue) for e in dismissals)
    ]


def stale_edit_issues(stale: list[PageEdit]) -> list[dict]:
    """Describe edits that were not applied, as issues for the curator.

    The portal used to drop a curator's number in silence when the page
    it named was absent from a new OCR run. A person who typed a number
    must be told it did not land, and the issue list is where review 1
    already looks.

    :param stale: The edits the overlay could not place.
    :returns: Issue dicts, ready for ``Issue`` rows.
    :rtype: list[dict]
    """
    issues = []
    for edit in stale:
        page = edit.pdf_page or edit.anchor_pdf_page
        issues.append(
            {
                "page_number": page,
                "check_name": CheckName.STALE_PAGE_EDIT,
                "severity": Issue.Severity.WARNING,
                "message": (
                    f"PDF page {page}: your edit "
                    f"({edit.get_kind_display().lower()}"
                    f"{f', {edit.value!r}' if edit.value else ''}) was "
                    f"made against a different version of this scan, or "
                    f"names a page it no longer has, so it was not "
                    f"applied. Make it again on the page as it is now."
                ),
            }
        )
    return issues
