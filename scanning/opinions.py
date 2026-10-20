"""The creation of the ``Opinion`` rows (#336, part 1).

The review-2 approval queues ``QueuedAction.CREATE_OPINIONS``, and the
daemon runs :func:`run` for the scan. The pass reads the standing
boundaries (``boundaries.standing``) and the printed numbers of the
corrected volume (``apply.load_printed_pages``), and writes one
``Opinion`` row per live boundary.

**The key is the printed page, never a position.** A row is keyed by
``(scan, first_printed_page, index_in_page)`` (#335). The first printed
page comes from the start page of the boundary; the index is the rank
of the boundary among those that start on that printed page, in the
reading order ``standing`` gives. A start page with no printed number
refuses the whole volume (:class:`PrintedNumberError`). The viewer's
file names fall back to a position (``boundaries._page_bounds``), and
that is right for a file name and wrong for a key, so the two rules are
two functions.

**A second run keeps every human field.** A key that exists is updated
in place: the addresses, the indexes, the run, the page count, the last
printed page and the boundary link. ``status``, the approval and the
notes are not touched, except that an ``ERROR`` row goes back to
``PROCESSING``, because the work that failed runs again. The
``glue_revision`` of a matched row that is not approved is raised
(#350, #336) when the digest of its inputs moved (:func:`input_digest`:
the boxes on its pages, its anchors, its printed numbers, the run): the
glue prefix belongs to one row at a time, and every derived artifact of
the row -- its OCR documents and its redacted PDF -- is due again and
the old revision is never written over. A row whose digest did not
move keeps its revision, its glues and its status, so a fix on one page
of the volume rebuilds the opinions of that page alone. An approved
row keeps its revision and its glues whatever moved. A row no
boundary matched keeps its data and gets one of the two
``STALE_OPINION_CHECKS`` cards: ``STALE_PAGE_NUMBER`` when a live
boundary shares its start address under another number, and
``ORPHANED_OPINION`` otherwise. The pass writes those two checks alone,
and deletes them from a row that matches again; the page checks and
``PAGE_GAP`` belong to the rebuild of #334. Nothing here is deleted but
those model rows, and no human row is written.

**The pass is small.** It reads one JSON document and the rows, pulls
no PDF and renders nothing. The per-opinion PDF (part 3) is its own
daemon action over the ``Opinion`` rows, capped at one PDF per tick,
and this pass writes no chain, no stamp and no queue for it.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections import defaultdict
from dataclasses import dataclass, field

from django.db import transaction
from django.db.models import Case, Count, F, Q, Value, When

from scanning import apply, boundaries, review_states, s3_sync
from scanning.models import (
    STALE_OPINION_CHECKS,
    Detection,
    Issue,
    Opinion,
    OpinionBoundary,
    OpinionCheck,
    OpinionFinding,
    OpinionReviewStatus,
    QueuedAction,
    Redaction,
    Scan,
    Status,
)
from scanning.services import _park_after_redactions, printed_page_span

logger = logging.getLogger(__name__)


class PrintedNumberError(ValueError):
    """A boundary's printed span cannot be read off the volume.

    The key of an opinion is its first printed page, so a start page
    with no number has no key, and a key is never guessed from a
    position. The message names the page, 1-based, in the corrected
    volume.
    """


class BoundaryOutOfSpace(ValueError):
    """A standing boundary is not measured against the corrected volume.

    The rows are written in the page space of the final apply run, so
    every boundary must carry that run. A boundary of another run, or
    of another original, would put an opinion on pages the volume no
    longer shows.
    """


@dataclass
class Summary:
    """What one pass wrote, for the log line and the park message."""

    created: int = 0
    updated: int = 0
    #: Of the updated rows, those whose inputs moved and whose revision
    #: rose; the rest keep their text and their PDF.
    rebuilt: int = 0
    stale: int = 0
    orphaned: int = 0

    @property
    def message(self) -> str:
        """The park message a curator reads under the progress bar."""
        total = self.created + self.updated
        parts = [
            f"{total} opinion(s): {self.created} new, {self.updated} updated, "
            f"{self.rebuilt} rebuilt"
        ]
        without = self.stale + self.orphaned
        if without:
            parts.append(f"{without} without a boundary")
        return ", ".join(parts) + "."


# ---------------------------------------------------------------------------
# The printed numbers
# ---------------------------------------------------------------------------


def printed_lookup(printed: dict) -> dict[int, tuple[int, int | None]]:
    """Return ``{final page index: (start, end)}`` for the key.

    The shape of ``apply.page_number_lookup``, plus one arm: a
    ``suffixed`` page (``2094a``, #319) maps to its number, so ``2094``
    and ``2094a`` share one bucket and ``index_in_page`` orders them,
    the rule of #335. The sequence check drops such a page on purpose
    (``services.printed_page_span``), and this lookup is not the
    sequence check.

    :param printed: The document of ``apply.load_printed_pages``.
    :returns: Mapping of 0-based final page index to a span.
    :rtype: dict[int, tuple[int, int | None]]
    """
    from scanning import page_numbers

    lookup: dict[int, tuple[int, int | None]] = {}
    for entry in printed.get("pages", []):
        value, kind = entry.get("printed"), entry.get("type")
        span = printed_page_span(value, kind)
        if span is None and kind == page_numbers.SUFFIXED:
            number = page_numbers.suffixed_number(value)
            if number is not None:
                span = (number, None)
        if span is not None:
            lookup[int(entry["final_page"]) - 1] = span
    return lookup


def printed_span(
    row: OpinionBoundary, lookup: dict[int, tuple[int, int | None]]
) -> tuple[int, int]:
    """Return the ``(first, last)`` printed pages of a boundary.

    Strict, unlike ``boundaries._page_bounds``: the start page must be
    in the lookup, or the boundary has no key. The last printed page is
    read off the last page of the opinion that carries a number, so a
    blank last leaf is legal. The arithmetic of blackletter's rule
    stays: a page that prints a range gives its end to an opinion that
    starts there and its start to one that ends there, and an opinion
    inside one range page covers the whole range.

    :param row: The boundary.
    :param lookup: :func:`printed_lookup`.
    :returns: The span.
    :rtype: tuple[int, int]
    :raises PrintedNumberError: When the start page has no number, or
        the numbers run backwards.
    """
    start = lookup.get(row.start_page_index)
    if start is None:
        raise PrintedNumberError(
            f"Page {row.start_page_index + 1} of the corrected volume has "
            "no printed number, and an opinion starts there. Review 1 is "
            "closed for this volume: ask an admin to re-queue it, then "
            "give that page a number before the approval."
        )
    if row.end_page_index <= row.start_page_index:
        return start[0], start[1] or start[0]
    first = start[1] or start[0]
    last = None
    for index in range(row.end_page_index, row.start_page_index, -1):
        entry = lookup.get(index)
        if entry is not None:
            last = entry[0]
            break
    if last is None:
        # Every later page is blank: the opinion ends on its start page
        # as far as the numbers go.
        last = first
    if last < first:
        raise PrintedNumberError(
            f"The printed numbers of the opinion that starts on page "
            f"{row.start_page_index + 1} run backwards ({first} to "
            f"{last}). Check the page numbers in review 1."
        )
    return first, last


# ---------------------------------------------------------------------------
# The rows
# ---------------------------------------------------------------------------


def live_boundaries(scan: Scan) -> list[OpinionBoundary]:
    """Return the boundaries an opinion is cut from, in reading order.

    ``boundaries.standing`` less the dismissed rows: the set step 3
    reads through ``viewer_payload(live_only=True)``.

    :param scan: The scan.
    :returns: The rows.
    :rtype: list[OpinionBoundary]
    """
    return [r for r in boundaries.standing(scan) if not r.is_dismissed]


def check_space(scan: Scan, run, rows: list[OpinionBoundary]) -> None:
    """Refuse a boundary that is not in the corrected volume's space.

    :param scan: The scan.
    :param run: The final apply run.
    :param rows: :func:`live_boundaries`.
    :raises BoundaryOutOfSpace: On the first row of another run or
        another original.
    """
    for row in rows:
        if row.apply_run_id != run.pk or boundaries.is_stale(row, scan):
            raise BoundaryOutOfSpace(
                "A boundary is not measured against the corrected volume. "
                "Recompute the redactions, then approve again."
            )


def keyed(
    rows: list[OpinionBoundary], lookup: dict[int, tuple[int, int | None]]
) -> list[tuple[OpinionBoundary, int, int, int]]:
    """Return ``(row, first, index_in_page, last)`` for every boundary.

    The rows come in reading order, so the rank inside one printed page
    is the order of arrival.

    :param rows: :func:`live_boundaries`.
    :param lookup: :func:`printed_lookup`.
    :returns: The keyed rows, in the order given.
    :raises PrintedNumberError: From :func:`printed_span`.
    """
    seen: dict[int, int] = {}
    out = []
    for row in rows:
        first, last = printed_span(row, lookup)
        index = seen.get(first, 0)
        seen[first] = index + 1
        out.append((row, first, index, last))
    return out


# ---------------------------------------------------------------------------
# The digest of an opinion's inputs
# ---------------------------------------------------------------------------

#: The version of :func:`input_digest`. Raise it when a code change alters
#: what the OCR glue or the redacted PDF reads off the rows, and every
#: opinion is rebuilt at its volume's next approval, as every approval
#: did before the digest.
DIGEST_RULES = 1


@dataclass
class ScanRows:
    """The rows of one scan the digests read, grouped by page.

    Read once per creation (:func:`read_rows`), the shape
    ``opinion_figures.ledger_digest`` keeps: a creation over a volume of
    five hundred opinions runs two queries and not a thousand.

    :param redactions: ``{page_index: [(rect_type, fill, x0, y0, x1,
        y1)]}``, the boxes a reader paints, rounded to a tenth of a
        point.
    :param detections: ``{page_index: [(label, x0, y0, x1, y1,
        confidence, img_width, img_height)]}``, every live detection of
        the page, whatever its run.
    """

    redactions: dict[int, list[tuple]] = field(default_factory=dict)
    detections: dict[int, list[tuple]] = field(default_factory=dict)


def read_rows(scan: Scan, run) -> ScanRows:
    """Read the rows every opinion of one scan is built from.

    The same selections the glue and the PDF make: the visible
    redactions of the scan (``redactions.visible_by_page``,
    ``opinion_pdf._redaction_pages``) and every live detection of the
    scan, whatever its run: the glue reads the run's
    (``opinion_ocr.load_inputs``), but the masks and the picture rects
    of the PDF read them all (``boundaries.outside_rects``,
    ``opinion_pdf._image_rects``), so the union is what a digest must
    cover. A digest over anything else, or anything less, would miss a
    move a reader sees. The run's engine documents are no input
    (:func:`input_digest`): they are the re-glue's to apply.

    :param scan: The scan.
    :param run: The final apply run the rows are measured in.
    :returns: The rows, by page.
    :rtype: ScanRows
    """
    rows = ScanRows()
    redactions: dict[int, list[tuple]] = defaultdict(list)
    for page, kind, fill, x0, y0, x1, y1 in (
        Redaction.objects.visible()
        .filter(scan=scan)
        .values_list("page_index", "rect_type", "fill", "x0", "y0", "x1", "y1")
    ):
        redactions[page].append(
            (
                kind or "",
                fill or "",
                *(round(float(v), 1) for v in (x0, y0, x1, y1)),
            )
        )
    detections: dict[int, list[tuple]] = defaultdict(list)
    for page, label, x0, y0, x1, y1, confidence, width, height in (
        Detection.objects.live()
        .filter(scan=scan)
        .values_list(
            "page_index",
            "label",
            "x0",
            "y0",
            "x1",
            "y1",
            "confidence",
            "img_width",
            "img_height",
        )
    ):
        detections[page].append(
            (
                label or "",
                *(round(float(v), 1) for v in (x0, y0, x1, y1)),
                round(float(confidence or 0), 2),
                int(width or 0),
                int(height or 0),
            )
        )
    rows.redactions = dict(redactions)
    rows.detections = dict(detections)
    return rows


def input_digest(
    row: OpinionBoundary,
    run,
    rows: ScanRows,
    lookup: dict[int, tuple[int, int | None]],
) -> str:
    """Return the digest of everything one opinion is built from.

    **The one rule** of ``Opinion.input_digest``. The OCR glue and the
    redacted PDF read, and read only: the visible redactions and the
    live detections of the opinion's pages; its own boundary, the two
    page indexes and the two anchor points (``boundaries.outside_rects``
    builds the neighbour masks from the opinion's own anchors and the
    page's detections, never from a neighbour's); the printed numbers
    of its pages; and the run, whose bitonal copy and page map are
    fixed for its lifetime. Human text edits have their own
    ``edit_revision`` and the pictures their own ``figure_digest``.

    The run's engine documents are **no input**, by choice. They move
    the text and never the PDF, and the approval never carries a PDF,
    so a digest that read them would cut every PDF of the volume again
    for a late engine or a document a re-glue rewrote in place.
    ``reglue_opinion_ocr`` is the one way an engine reaches the
    opinions: it writes the text again and carries the PDF (#452). So
    a late engine moves no digest, and the first approval after one
    rebuilds only what the boxes moved.

    The rows are sorted, so the order a query returns them in is no
    input. :data:`DIGEST_RULES` is, so a change of what the glue reads
    moves every digest at once.

    :param row: The opinion's boundary.
    :param run: The final apply run.
    :param rows: :func:`read_rows` of the scan.
    :param lookup: :func:`printed_lookup` of the run's printed pages.
    :returns: A hex digest.
    :rtype: str
    """
    pages = range(row.start_page_index, row.end_page_index + 1)
    raw = json.dumps(
        {
            "rules": DIGEST_RULES,
            "run": run.pk,
            "anchors": [
                row.start_page_index,
                row.end_page_index,
                *(
                    round(float(v or 0), 1)
                    for v in (row.start_x, row.start_y, row.end_x, row.end_y)
                ),
            ],
            "printed": [
                list(lookup[page]) if page in lookup else None
                for page in pages
            ],
            "redactions": [
                [page, sorted(rows.redactions.get(page, []))] for page in pages
            ],
            "detections": [
                [page, sorted(rows.detections.get(page, []))] for page in pages
            ],
        },
        sort_keys=True,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _unchanged(opinion: Opinion, digest: str) -> bool:
    """Return whether a matched row may keep its revision.

    Its digest is the one just computed, and it is not in ``ERROR``: the
    way back from ``ERROR`` is the work running again, which takes a
    new revision. A blank digest (a row from before the rule) never
    matches, so such a row is rebuilt once and stamped.
    """
    return (
        bool(digest)
        and opinion.input_digest == digest
        and opinion.status != OpinionReviewStatus.ERROR
    )


def _fields(scan: Scan, run, row: OpinionBoundary, last: int) -> dict:
    """The columns the pass owns on an ``Opinion`` row."""
    return {
        "last_printed_page": last,
        "page_count": row.end_page_index - row.start_page_index + 1,
        "start_source_edit_id": row.start_source_edit_id,
        "start_source_page": row.start_source_page,
        "end_source_edit_id": row.end_source_edit_id,
        "end_source_page": row.end_source_page,
        "start_page_index": row.start_page_index,
        "end_page_index": row.end_page_index,
        "apply_run": run,
        "source_fingerprint": scan.source_fingerprint or "",
        "boundary": row,
    }


def _write_stale_cards(
    unmatched: list[Opinion],
    by_address: dict[tuple, int],
) -> tuple[int, int]:
    """Give every unmatched row its one card, and return the two counts.

    :param unmatched: The rows no boundary matched.
    :param by_address: ``{(start edit id, start source page): first}``
        of the live boundaries, for the stale-number test.
    :returns: ``(stale, orphaned)``.
    """
    stale = orphaned = 0
    cards = []
    for opinion in unmatched:
        address = (opinion.start_source_edit_id, opinion.start_source_page)
        now = by_address.get(address) if opinion.start_source_page else None
        if now is not None and now != opinion.first_printed_page:
            stale += 1
            check = OpinionCheck.STALE_PAGE_NUMBER
            message = (
                f"The opinion was stamped on printed page "
                f"{opinion.first_printed_page}; its start page now reads "
                f"{now}."
            )
        else:
            orphaned += 1
            check = OpinionCheck.ORPHANED_OPINION
            message = "No standing boundary starts where this opinion does."
        cards.append(
            OpinionFinding(
                opinion=opinion,
                page_in_opinion=None,
                check_name=check,
                severity=Issue.Severity.WARNING,
                message=message,
            )
        )
    OpinionFinding.objects.bulk_create(cards)
    return stale, orphaned


def create_rows(
    scan: Scan, run, rows: list[OpinionBoundary], printed: dict
) -> Summary:
    """Write one ``Opinion`` row per live boundary, and the stale cards.

    The one writer of an ``Opinion`` row. In one transaction: the rows
    of the scan are read by key, every boundary updates or creates its
    row, the two stale checks are deleted from every row of the scan
    and written again on the rows no boundary matched.

    The match is by key, so a boundary added before another on the
    same printed page takes that page's index 0 and, with it, the row
    and the human fields the other opinion held; the other opinion's
    old row becomes the orphan. That is the accepted cost of the #335
    key: the index moves at most the opinions of one printed page.

    :param scan: The scan.
    :param run: The final apply run the boundaries are measured in.
    :param rows: :func:`live_boundaries`, already checked by
        :func:`check_space`.
    :param printed: The document of ``apply.load_printed_pages``.
    :returns: The counts.
    :rtype: Summary
    :raises PrintedNumberError: From :func:`printed_span`; nothing is
        written then.
    """
    lookup = printed_lookup(printed)
    plan = keyed(rows, lookup)
    summary = Summary()
    scan_rows = read_rows(scan, run)
    with transaction.atomic():
        existing = {
            (o.first_printed_page, o.index_in_page): o
            for o in Opinion.objects.filter(scan=scan)
        }
        matched: set[int] = set()
        new_rows = []
        for row, first, index, last in plan:
            fields = _fields(scan, run, row, last)
            digest = input_digest(row, run, scan_rows, lookup)
            opinion = existing.get((first, index))
            if opinion is None:
                new_rows.append(
                    Opinion(
                        scan=scan,
                        first_printed_page=first,
                        index_in_page=index,
                        input_digest=digest,
                        **fields,
                    )
                )
                continue
            matched.add(opinion.pk)
            if _unchanged(opinion, digest):
                # Nothing the row is built from moved (the one rule,
                # :func:`input_digest`): the revision stays, and with it
                # the OCR documents, the text, the PDF and the status.
                # A fix on another page of the volume rebuilds nothing
                # here, which is what a reopen for one box costs.
                Opinion.objects.filter(pk=opinion.pk).update(
                    **fields, input_digest=digest
                )
                summary.updated += 1
                continue
            # One statement per matched row, with three rules folded in.
            # The way back from ERROR is this run of the work (#335).
            # The inputs of the glue moved under the key, so the
            # revision moves too, and with it the attempt count of each
            # glue (#350, #336): a redaction a curator moved after a
            # send-back changes the ink of the PDF and no field of this
            # row, so the revision is what says the set is new. An
            # approved opinion keeps its revision and its glues, and
            # its digest with them: the digest names the inputs its
            # glues were built from, so the first creation after a
            # text reopen sees the move and rebuilds it then.
            approved = Q(status=OpinionReviewStatus.TEXT_REVIEW_DONE)
            Opinion.objects.filter(pk=opinion.pk).update(
                **fields,
                input_digest=Case(
                    When(approved, then=F("input_digest")),
                    default=Value(digest),
                ),
                status=Case(
                    When(
                        status=OpinionReviewStatus.ERROR,
                        then=Value(OpinionReviewStatus.PROCESSING),
                    ),
                    default=F("status"),
                ),
                glue_revision=Case(
                    When(approved, then=F("glue_revision")),
                    default=F("glue_revision") + 1,
                ),
                ocr_glue_attempts=Case(
                    When(approved, then=F("ocr_glue_attempts")),
                    default=Value(0),
                ),
                pdf_attempts=Case(
                    When(approved, then=F("pdf_attempts")), default=Value(0)
                ),
                ensemble_attempts=Case(
                    When(approved, then=F("ensemble_attempts")),
                    default=Value(0),
                ),
                pdf_attempted_at=Case(
                    When(approved, then=F("pdf_attempted_at")),
                    default=Value(None),
                ),
            )
            summary.updated += 1
            if opinion.status != OpinionReviewStatus.TEXT_REVIEW_DONE:
                summary.rebuilt += 1
        Opinion.objects.bulk_create(new_rows, batch_size=500)
        summary.created = len(new_rows)

        # The two row-fact cards are model rows: written again here,
        # fresh, and never dismissed (``STALE_OPINION_CHECKS``).
        OpinionFinding.objects.filter(
            opinion__scan=scan, check_name__in=STALE_OPINION_CHECKS
        ).delete()
        unmatched = [o for o in existing.values() if o.pk not in matched]
        by_address = {
            (row.start_source_edit_id, row.start_source_page): first
            for row, first, _, _ in plan
            if row.start_source_page is not None
        }
        summary.stale, summary.orphaned = _write_stale_cards(
            unmatched, by_address
        )
    logger.info(
        "scan %s: opinions under %s: %d created, %d updated (%d rebuilt), "
        "%d stale, %d orphaned",
        scan.pk,
        run.label,
        summary.created,
        summary.updated,
        summary.rebuilt,
        summary.stale,
        summary.orphaned,
    )
    return summary


# ---------------------------------------------------------------------------
# The queue and the worker
# ---------------------------------------------------------------------------


#: The queued actions of a review-2 approval in flight (#240). From the
#: approval's read of the rows to the creation of the opinions, a box a
#: curator writes reaches no opinion, so the writes of review 2 refuse
#: while a scan holds one of them (``views_api._refuse_closed_review``).
APPROVAL_ACTIONS = (
    QueuedAction.CREATE_OPINIONS,
    QueuedAction.COMPUTE_THEN_CREATE_OPINIONS,
)


def queue_create_opinions(scan: Scan) -> str:
    """Close review 2 and ask the daemon for the opinions.

    The write of the approve button (``views_process
    .approve_redaction_review``): one compare-and-swap from
    ``READY_FOR_REDACTION_REVIEW`` to ``QUEUED``. The worker writes
    ``REDACTION_REVIEW_DONE`` when it is done, and parks the scan back
    in the review on a failure, so the next press is the retry.

    The action is ``CREATE_OPINIONS``, or ``COMPUTE_THEN_CREATE_OPINIONS``
    when a curator changed the detections after the last compute
    (``detections.changed_since_compute``, #240): the opinions are cut
    from the redaction rows, and a detection reaches those rows through
    the compute alone. The compute then queues the creation itself.

    :param scan: The scan the curator approved.
    :returns: The action queued, or an empty string when the write lost.
    :rtype: str
    """
    from scanning import detections

    action = QueuedAction.CREATE_OPINIONS
    message = "The opinions are queued for creation."
    if detections.changed_since_compute(scan):
        action = QueuedAction.COMPUTE_THEN_CREATE_OPINIONS
        message = (
            "The detections changed after the last computation. The "
            "redactions are queued for computation, then the opinions "
            "for creation."
        )
    moved = Scan.objects.filter(
        pk=scan.pk, status=Status.READY_FOR_REDACTION_REVIEW
    ).update(
        status=Status.QUEUED,
        queued_action=action,
        progress_message=message,
        progress_current=0,
        progress_total=0,
    )
    return action if moved else ""


def run(scan_pk: int) -> None:
    """Create the opinions of a claimed scan, and park it.

    The body of ``services.run_create_opinions``. It raises nothing: a
    success parks the scan in ``REDACTION_REVIEW_DONE`` with the counts,
    and every failure parks it back in ``READY_FOR_REDACTION_REVIEW``
    with the reason, where the recompute button and the approve button
    are live. No ``ERROR``, no counter: a person is the bound.

    :param scan_pk: Primary key of the claimed scan.
    :return: None.
    """
    from scanning import detections
    from scanning.services import APPROVAL_NOT_DONE_NOTE

    scan = Scan.objects.get(pk=scan_pk)
    status = Status.READY_FOR_REDACTION_REVIEW
    try:
        run_ = review_states.final_run(scan)
        if run_ is None:
            message = "The corrected volume is not built."
        elif detections.changed_since_compute(scan):
            # A box written after the approval read the rows and before
            # the writes refused (#240): the swap of the approval, or a
            # tab that posted as it landed. It is in no redaction, so
            # the opinions wait for the next approval, which computes.
            message = (
                "The detections changed after the approval. "
                f"{APPROVAL_NOT_DONE_NOTE}"
            )
        else:
            printed = apply.load_printed_pages(scan, run_)
            rows = live_boundaries(scan)
            check_space(scan, run_, rows)
            summary = create_rows(scan, run_, rows, printed)
            status = Status.REDACTION_REVIEW_DONE
            message = summary.message
    except (PrintedNumberError, BoundaryOutOfSpace, apply.ApplyError) as exc:
        logger.warning("scan %s: opinions not created: %s", scan_pk, exc)
        message = str(exc)
    except Exception:
        logger.exception(
            "scan %s: the creation of the opinions failed", scan_pk
        )
        message = (
            "The creation of the opinions failed. Approve again to retry."
        )
    _park_after_redactions(scan_pk, message, status)


# ---------------------------------------------------------------------------
# The list page
# ---------------------------------------------------------------------------


def picture_counts(rows) -> dict[int, int]:
    """Return how many pictures each opinion's text holds.

    A picture is a curated box of review 2 on a page of the opinion's
    span, read by ``opinion_ocr.picture_pages``, the glue's own rule:
    a live ``IMAGE`` detection of the opinion's run less the junk the
    viewer drops (#463). A page two opinions share counts for both.
    The caller passes the rows of one page of the list, so two queries
    answer it whatever its length, one when no page holds a box.

    :param rows: The ``Opinion`` rows, usually one page of the list.
    :returns: ``{opinion id: count}``; an opinion with none is absent.
    :rtype: dict[int, int]
    """
    from scanning import opinion_ocr

    rows = [row for row in rows if row.apply_run_id]
    if not rows:
        return {}
    pages = opinion_ocr.picture_pages({row.apply_run_id for row in rows})
    counts = {}
    for row in rows:
        count = sum(
            1
            for page in pages.get(row.apply_run_id, ())
            if row.start_page_index <= page <= row.end_page_index
        )
        if count:
            counts[row.pk] = count
    return counts


def finding_counts(opinion_ids) -> dict[int, tuple[int, int, int]]:
    """Return the open findings, the stale ones and the blocking ones (#334).

    The warning badge of the opinions list, the twin of
    ``findings.open_count`` for one scan and of
    ``repairs.waiting_counts`` for one page of a list. The caller passes
    the ids of one page alone, so one grouped query answers the whole
    page and the size of the corpus never reaches it.

    **The ordering is cleared before the grouping.** Django puts the
    ordering columns into ``GROUP BY``, and ``OpinionFinding.Meta``
    orders by ``page_in_opinion``, so without the clear an opinion with
    two findings on two pages reads 1 twice instead of 2 once.

    :param opinion_ids: The opinions to count, usually one page of the
        list.
    The blocking count is the gate of the approval
    (``opinion_review.blocking_filter``, #375), so the badge says what
    the button waits on and holds no copy of the rule.

    :returns: ``{opinion id: (open, stale, blocking)}``. An opinion with
        no open finding is absent.
    :rtype: dict[int, tuple[int, int, int]]
    """
    from scanning.opinion_review import blocking_filter

    rows = (
        OpinionFinding.objects.filter(
            opinion_id__in=opinion_ids, dismissal__isnull=True
        )
        .order_by()
        .values("opinion_id")
        .annotate(
            open=Count("pk"),
            stale=Count("pk", filter=Q(check_name__in=STALE_OPINION_CHECKS)),
            blocking=Count("pk", filter=blocking_filter()),
        )
    )
    return {
        row["opinion_id"]: (row["open"], row["stale"], row["blocking"])
        for row in rows
    }


# ---------------------------------------------------------------------------
# The review state
# ---------------------------------------------------------------------------


def text_review_ready(opinion: Opinion) -> bool:
    """Return whether an opinion has everything the text review needs.

    **The one rule**, read off the row and never off the bucket: the
    redacted PDF of the live revision (``opinion_pdf.is_written``) and
    the text of that revision (``ensemble.is_written``, which holds the
    OCR glue's own stamp inside it). The review page draws those two
    objects and nothing else, so a row that has them is a row a person
    can work on. The pictures of the text are cut too
    (``opinion_figures.is_written``, #463): the page shows them in the
    text, and the approval embeds them.

    :param opinion: The row.
    :returns: Whether the review can start.
    :rtype: bool
    """
    from scanning import ensemble, opinion_figures, opinion_pdf

    return (
        opinion_pdf.is_written(opinion)
        and ensemble.is_written(opinion)
        and opinion_figures.is_written(opinion)
    )


#: How many rows one tick moves in each direction. The twin of
#: ``opinion_ocr.OPINIONS_PER_TICK`` and ``opinion_pdf.PDFS_PER_TICK``:
#: the collect tick is serial, and the first tick after a deploy would
#: else carry the whole corpus.
PROMOTIONS_PER_TICK = 50


def _live_stamps() -> Q:
    """Return the rows whose three ledgers name the live revision.

    The candidate filter of the pass, in both directions. It is the
    shape of :func:`text_review_ready` and not a second copy of it:
    that function decides, row by row, and this only keeps the tick
    from reading every opinion of the corpus.

    :returns: The filter.
    :rtype: Q
    """
    return Q(
        glue_revision__isnull=False,
        redacted_pdf_revision=F("glue_revision"),
        ocr_glue_revision=F("glue_revision"),
        ensemble_revision=F("glue_revision"),
    ) & (Q(figure_digest="") | Q(figures_cut_digest=F("figure_digest")))


def promote_ready(opinion: Opinion) -> bool:
    """Take one opinion to ``READY_FOR_TEXT_REVIEW``.

    A compare-and-swap over ``PROCESSING`` and the revision the caller
    read: a re-glue that landed between the read and the write wins,
    and the row is a candidate again when its two objects are written
    at the new revision. ``TEXT_REVIEW_DONE`` and ``ERROR`` are not
    touched; the way out of ``ERROR`` is the work that writes the row
    again.

    :param opinion: The row.
    :returns: Whether this call moved the row.
    :rtype: bool
    """
    if not text_review_ready(opinion):
        return False
    moved = Opinion.objects.filter(
        pk=opinion.pk,
        status=OpinionReviewStatus.PROCESSING,
        glue_revision=opinion.glue_revision,
    ).update(status=OpinionReviewStatus.READY_FOR_TEXT_REVIEW)
    if moved:
        logger.info(
            "%s of scan %s is ready for the text review",
            opinion,
            opinion.scan_id,
        )
        if opinion.glue_revision:
            # Revision 0 is the first: no folder can be older, so the
            # tick spends no listing on it.
            prune_glues(opinion)
    return bool(moved)


#: A glue revision folder under ``Opinion.object_prefix``, and its number.
#: ``approved/`` and ``tag/`` never match it.
_REVISION_FOLDER = re.compile(r"r(\d+)/")


def prune_glues(opinion: Opinion, dry_run: bool = False) -> list[str] | None:
    """Delete the glues of every revision below the opinion's live one.

    The glues of a revision (the engine documents, the manifest, the
    redacted PDF, the ensemble documents) are derived, and a re-glue
    writes each of them again with no paid read, so nothing reads a
    revision once the live one is complete (#452). The caller says it
    is: :func:`promote_ready` after its swap, and the command
    ``prune_opinion_glues`` over the rows :func:`text_review_ready`
    accepts. Only ``r{k}/`` with ``k`` below ``glue_revision`` goes:
    the approved texts (``approved/``), the tagger input (``tag/``) and
    every paid result outside the opinion's prefix stay. The revision
    only goes up, so a re-glue during the walk makes no live folder
    old.

    Best effort, the rule of ``s3_sync.delete_objects``: an orphan
    costs storage and nothing else, so a failed listing deletes nothing
    and nothing raises into the tick. The command is the retry.

    :param opinion: The row, with ``scan`` and its reporter.
    :param dry_run: List the keys and delete nothing.
    :returns: The keys of the older revisions, or None when the listing
        failed.
    :rtype: list[str] | None
    """
    root = (
        f"{s3_sync.s3_processing_prefix(opinion.scan)}{opinion.object_prefix}"
    )
    keys = s3_sync.list_keys(root)
    if keys is None:
        return None
    old = []
    for key in keys:
        match = _REVISION_FOLDER.match(key[len(root) :])
        if match and int(match.group(1)) < opinion.glue_revision:
            old.append(key)
    if old and not dry_run:
        deleted = s3_sync.delete_objects(old)
        logger.info(
            "%s of scan %s: deleted %d of %d object(s) of the revisions "
            "below r%d",
            opinion,
            opinion.scan_id,
            deleted,
            len(old),
            opinion.glue_revision,
        )
    return old


def demote_stale(opinion: Opinion) -> bool:
    """Take one opinion back to ``PROCESSING``.

    The other half of :func:`promote_ready`, and the reason both live
    in one pass. ``create_rows`` and the two re-glue commands raise
    ``glue_revision`` on every row a person has not approved, and they
    keep the status, so a row that was ready now points at a PDF and a
    text nobody has written yet. The list would call it ready and the
    review page would show two empty columns.

    A compare-and-swap over ``READY_FOR_TEXT_REVIEW`` and the revision
    the caller read. An approved row is not touched: a person read that
    text, and the way a new revision reaches it is a new approval.

    :param opinion: The row.
    :returns: Whether this call moved the row.
    :rtype: bool
    """
    if text_review_ready(opinion):
        return False
    moved = Opinion.objects.filter(
        pk=opinion.pk,
        status=OpinionReviewStatus.READY_FOR_TEXT_REVIEW,
        glue_revision=opinion.glue_revision,
    ).update(status=OpinionReviewStatus.PROCESSING)
    if moved:
        logger.info(
            "%s of scan %s went back to processing: its PDF or its text "
            "is not written at r%s",
            opinion,
            opinion.scan_id,
            opinion.glue_revision,
        )
    return bool(moved)


def promote_ready_opinions(limit: int = PROMOTIONS_PER_TICK) -> int:
    """Move every qualifying opinion in or out of the text review.

    The twin of ``review_states.promote_ready_scans`` for one opinion,
    and the one writer of ``READY_FOR_TEXT_REVIEW`` (#365). The two
    objects are written by two passes that know nothing of each other,
    and either can be last, so the state is a pass of its own rather
    than a line in both.

    **Both directions, because a revision goes up as well as down.**
    A row is promoted when its two objects are written and demoted when
    a re-glue takes them away (:func:`demote_stale`). The promotion
    reads the scan status and the demotion does not: a volume an admin
    sent back invites nobody new, and a row that keeps its objects
    keeps its state until a re-glue moves the revision.

    ``opinion_pdf.OPINION_PDF_STATUSES`` is the one table of the scan
    statuses the opinion passes read, so #334 extends it once.

    :param limit: How many rows to move in each direction.
    :returns: How many opinions were moved.
    :rtype: int
    """
    from scanning.opinion_pdf import OPINION_PDF_STATUSES

    ready = Opinion.objects.filter(
        _live_stamps(),
        scan__status__in=OPINION_PDF_STATUSES,
        status=OpinionReviewStatus.PROCESSING,
    ).select_related("scan", "scan__reporter")[:limit]
    moved = sum(1 for row in ready if promote_ready(row))
    stale = Opinion.objects.filter(
        status=OpinionReviewStatus.READY_FOR_TEXT_REVIEW
    ).exclude(_live_stamps())[:limit]
    return moved + sum(1 for row in stale if demote_stale(row))
