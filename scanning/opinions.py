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
(#350, #336): the glue prefix belongs to one row at a time, and the
boxes and the boundary under the key may have moved, so every derived
artifact of the row -- its OCR documents and its redacted PDF -- is due
again and the old revision is never written over. An approved row keeps
its revision and its glues. A row no
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

import logging
from dataclasses import dataclass

from django.db import transaction
from django.db.models import Case, Count, F, Q, Value, When

from scanning import apply, boundaries, review_states
from scanning.models import (
    STALE_OPINION_CHECKS,
    Issue,
    Opinion,
    OpinionBoundary,
    OpinionCheck,
    OpinionFinding,
    OpinionReviewStatus,
    QueuedAction,
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
    stale: int = 0
    orphaned: int = 0

    @property
    def message(self) -> str:
        """The park message a curator reads under the progress bar."""
        total = self.created + self.updated
        parts = [
            f"{total} opinion(s): {self.created} new, {self.updated} updated"
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
    with transaction.atomic():
        existing = {
            (o.first_printed_page, o.index_in_page): o
            for o in Opinion.objects.filter(scan=scan)
        }
        matched: set[int] = set()
        new_rows = []
        for row, first, index, last in plan:
            fields = _fields(scan, run, row, last)
            opinion = existing.get((first, index))
            if opinion is None:
                new_rows.append(
                    Opinion(
                        scan=scan,
                        first_printed_page=first,
                        index_in_page=index,
                        **fields,
                    )
                )
                continue
            matched.add(opinion.pk)
            # One statement per matched row, with three rules folded in.
            # The way back from ERROR is this run of the work (#335).
            # The inputs of every glue may have moved under the key, so
            # the revision moves too, and with it the attempt count of
            # each glue (#350, #336): a redaction a curator moved after
            # a send-back changes the ink of the PDF and no field of
            # this row, so the revision is what says the set is new. An
            # approved opinion keeps its revision and its glues.
            approved = Q(status=OpinionReviewStatus.TEXT_REVIEW_DONE)
            Opinion.objects.filter(pk=opinion.pk).update(
                **fields,
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
                pdf_attempted_at=Case(
                    When(approved, then=F("pdf_attempted_at")),
                    default=Value(None),
                ),
            )
            summary.updated += 1
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
        "scan %s: opinions under %s: %d created, %d updated, %d stale, "
        "%d orphaned",
        scan.pk,
        run.label,
        summary.created,
        summary.updated,
        summary.stale,
        summary.orphaned,
    )
    return summary


# ---------------------------------------------------------------------------
# The queue and the worker
# ---------------------------------------------------------------------------


def queue_create_opinions(scan: Scan) -> bool:
    """Close review 2 and ask the daemon for the opinions.

    The write of the approve button (``views_process
    .approve_redaction_review``): one compare-and-swap from
    ``READY_FOR_REDACTION_REVIEW`` to ``QUEUED``. The worker writes
    ``REDACTION_REVIEW_DONE`` when it is done, and parks the scan back
    in the review on a failure, so the next press is the retry.

    :param scan: The scan the curator approved.
    :returns: Whether the write won.
    :rtype: bool
    """
    return bool(
        Scan.objects.filter(
            pk=scan.pk, status=Status.READY_FOR_REDACTION_REVIEW
        ).update(
            status=Status.QUEUED,
            queued_action=QueuedAction.CREATE_OPINIONS,
            progress_message="The opinions are queued for creation.",
            progress_current=0,
            progress_total=0,
        )
    )


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
    scan = Scan.objects.get(pk=scan_pk)
    status = Status.READY_FOR_REDACTION_REVIEW
    try:
        run_ = review_states.final_run(scan)
        if run_ is None:
            message = "The corrected volume is not built."
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


def finding_counts(opinion_ids) -> dict[int, tuple[int, int]]:
    """Return the open findings, and the stale ones, per opinion (#334).

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
    :returns: ``{opinion id: (open, stale)}``. An opinion with no open
        finding is absent.
    :rtype: dict[int, tuple[int, int]]
    """
    rows = (
        OpinionFinding.objects.filter(
            opinion_id__in=opinion_ids, dismissal__isnull=True
        )
        .order_by()
        .values("opinion_id")
        .annotate(
            open=Count("pk"),
            stale=Count("pk", filter=Q(check_name__in=STALE_OPINION_CHECKS)),
        )
    )
    return {row["opinion_id"]: (row["open"], row["stale"]) for row in rows}
