"""The opinion boundaries of review 2: the rows, and the curator's decisions.

Issue #240, PR C. ``Scan.opinions_json`` held the output of the
pairing as a list of dicts, with no identity, no provenance and no
address. ``models.OpinionBoundary`` replaces it, one row per opinion,
under the rules PR A set for the detections:

- **Computed rows are disposable.** Every compute deletes the scan's
  computed rows and writes the pairing again (:func:`write_computed`).
- **Human rows are withdrawn, never deleted.** An ``ADD`` is a boundary
  the curator drew; a ``DISMISS`` is a decision about a computed one.
  It names its target by its **anchors** -- the start address and the
  start point -- because the computed row is gone at the next compute,
  and :func:`resolve` lands it on the new row with the same start.
- **A move of an anchor is a dismissal plus an addition** that names it
  in ``replaces`` (:func:`add`), so withdrawing the addition gives the
  computed boundary back.

Every consumer reads the rows through :func:`standing`, which orders
them in reading order, and :func:`viewer_payload`, which emits the
dict shape ``blackletter.api.pair`` produced, plus the ids. The anchors
are in PDF points; ``Detection`` keeps its pixels.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable
from typing import Any

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from scanning import detections
from scanning.models import ApplyRun, Detection, OpinionBoundary, Scan

logger = logging.getLogger(__name__)

#: How far, in PDF points, a rebuilt start anchor may sit from a
#: dismissal's copy and still be the same opinion. About one printed
#: line. The pairing is deterministic over the same rows, so the match
#: is exact in practice; the tolerance covers a column snap, which
#: moves the caption's left edge, and a re-import under a new run.
ANCHOR_TOLERANCE_PT = 12.0

POINTS_PER_INCH = 72.0


class UnaddressableBoundary(ValueError):
    """An anchor was asked on a page no address can be written for.

    The mirror of ``detections.UnaddressableDetection``: a boundary with
    no address could never follow the page space, so it is refused
    where it is asked.
    """


class MisorderedBoundary(ValueError):
    """The end anchor was placed on a page before the start anchor's."""


# ---------------------------------------------------------------------------
# Points and addresses
# ---------------------------------------------------------------------------


def to_points(
    x: float,
    y: float,
    img_width: int,
    img_height: int,
    page_width: float | None = None,
    page_height: float | None = None,
) -> tuple[float, float]:
    """Convert a pixel of a page render to PDF points.

    With the page size (the compute opens the PDF) the scale is exact,
    ``blackletter.models.Page.scale_x``. Without it the render is
    ``yolo.DPI`` dots per inch by a module constant (#194/#195), so the
    two answers differ by less than one point, from the rounding of the
    render's pixel count.

    :param x: The pixel column.
    :param y: The pixel row.
    :param img_width: The render width in pixels.
    :param img_height: The render height in pixels.
    :param page_width: The page width in points, when known.
    :param page_height: The page height in points, when known.
    :returns: ``(x, y)`` in points.
    """
    from scanning import yolo

    if page_width and img_width:
        sx = page_width / img_width
    else:
        sx = POINTS_PER_INCH / yolo.DPI
    if page_height and img_height:
        sy = page_height / img_height
    else:
        sy = POINTS_PER_INCH / yolo.DPI
    return x * sx, y * sy


def anchor_of_detection(row: Detection, which: str) -> tuple[float, float]:
    """Return the anchor a detection row gives, in points.

    :param row: A caption row (``which`` ``"start"``: its top-left) or a
        key icon row (``"end"``: its bottom-right).
    :param which: ``"start"`` or ``"end"``.
    :returns: ``(x, y)`` in points.
    """
    if which == "start":
        px, py = row.x0, row.y0
    else:
        px, py = row.x1, row.y1
    return to_points(px, py, row.img_width, row.img_height)


def placer(
    run: ApplyRun | None,
) -> Callable[[int | None, int | None], int | None]:
    """Return a function from a source address to a 0-based final index.

    ``apply.index_placer`` over the run's map, the arithmetic
    ``detections.relocate_manual_rows`` uses too. Without a run the
    space is the original's and the address is the index plus one.

    :param run: The standing apply run, or None for the original's space.
    :returns: A callable answering None for an address the map does not
        hold.
    """
    if run is None:

        def identity(edit_id, page):
            if edit_id is not None or not page:
                return None
            return page - 1

        return identity

    from scanning import apply

    return apply.index_placer(run.page_map or {})


def is_stale(row: OpinionBoundary, scan: Scan) -> bool:
    """Return whether a row was written against another original.

    Blank on either side is a legacy value and matches anything, the
    rule of ``page_edits.is_stale``.

    :param row: The row.
    :param scan: Its scan.
    :returns: Whether the fingerprints disagree.
    """
    if not row.source_fingerprint or not scan.source_fingerprint:
        return False
    return row.source_fingerprint != scan.source_fingerprint


def _distance(a: tuple[float, float], b: tuple[float, float]) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


# ---------------------------------------------------------------------------
# The compute
# ---------------------------------------------------------------------------


def write_computed(
    scan: Scan,
    document,
    row_ids: dict[int, int],
    run: ApplyRun | None,
    detect_run: int | None,
) -> list:
    """Pair the opinions of ``document`` and write them as computed rows.

    One pairing per compute: the caller hands the pairs to
    ``compute_redaction_rects`` afterwards. The document is the snapped
    one ``services._build_document_from_detections`` built from the
    live rows, and ``row_ids`` maps each of its detection objects (by
    ``id()``) to the ``Detection`` pk it came from, which is how the
    caption and the key FKs are exact and no match by rounded
    coordinates is needed.

    In one transaction: delete the scan's computed rows, write one per
    pair with the anchors in points (the document has the page size),
    the two indexes, the address of each page through the run's map,
    ``ordinal``, the two FKs, ``apply_run`` and ``detect_run``. Then
    :func:`resolve` lands the standing dismissals, and
    :func:`relocate_human_rows` moves the curator's additions into the
    run's space.

    :param scan: The scan.
    :param document: The snapped ``blackletter.models.Document``.
    :param row_ids: ``{id(bl_detection): Detection pk}``.
    :param run: The apply run whose space the document is in; None for
        the original's space.
    :param detect_run: The ``ExternalJob.run`` the rows came from.
    :returns: The ``(caption, key)`` pairs, as ``_pair_opinions`` gives
        them.
    """
    from blackletter.scanner import _pair_opinions

    pairs = _pair_opinions(document) if document.pages else []
    pages_by_index = {p.index: p for p in document.pages}
    scan.refresh_from_db(fields=["source_fingerprint"])
    rows = []
    unaddressed = 0
    for ordinal, (caption, key) in enumerate(pairs):
        start_page = pages_by_index[caption.page_index]
        end_page = pages_by_index[key.page_index]
        sx, sy = to_points(
            caption.bbox.x1,
            caption.bbox.y1,
            start_page.img_width,
            start_page.img_height,
            start_page.pdf_width,
            start_page.pdf_height,
        )
        ex, ey = to_points(
            key.bbox.x2,
            key.bbox.y2,
            end_page.img_width,
            end_page.img_height,
            end_page.pdf_width,
            end_page.pdf_height,
        )
        start_edit, start_src = detections.source_for_index(
            scan, caption.page_index, run
        )
        end_edit, end_src = detections.source_for_index(
            scan, key.page_index, run
        )
        if start_src is None or end_src is None:
            unaddressed += 1
        rows.append(
            OpinionBoundary(
                scan=scan,
                origin=OpinionBoundary.Origin.COMPUTED,
                start_source_edit_id=start_edit,
                start_source_page=start_src,
                start_page_index=caption.page_index,
                start_x=sx,
                start_y=sy,
                end_source_edit_id=end_edit,
                end_source_page=end_src,
                end_page_index=key.page_index,
                end_x=ex,
                end_y=ey,
                source_fingerprint=scan.source_fingerprint or "",
                apply_run=run,
                detect_run=detect_run,
                start_detection_id=row_ids.get(id(caption)),
                end_detection_id=row_ids.get(id(key)),
                ordinal=ordinal,
            )
        )
    with transaction.atomic():
        OpinionBoundary.objects.computed().filter(scan=scan).delete()
        OpinionBoundary.objects.bulk_create(rows, batch_size=500)
        landed, stale = resolve(scan)
        moved = 0
        if run is not None:
            moved, _ = relocate_human_rows(scan, run)
    if unaddressed:
        logger.warning(
            "scan %s: %d opinion boundary(ies) have a page the map does "
            "not address; they are drawn but cannot follow a new run",
            scan.pk,
            unaddressed,
        )
    logger.info(
        "scan %s: wrote %d opinion boundary(ies) (%d dismissal(s) landed, "
        "%d stale, %d addition(s) moved)",
        scan.pk,
        len(rows),
        landed,
        len(stale),
        moved,
    )
    return pairs


def resolve(scan: Scan) -> tuple[int, list[OpinionBoundary]]:
    """Land every standing dismissal on the computed rows just written.

    For each dismissal that is not withdrawn and not stale: the
    computed rows with the same start address and no decision yet, the
    one whose start anchor is nearest the dismissal's, and that within
    :data:`ANCHOR_TOLERANCE_PT`. Each row is taken once. An unresolved
    dismissal is logged and left standing; #240 PR D raises it as
    ``stale_boundary_edit``. Reads no computed row when no dismissal
    stands.

    :param scan: The scan.
    :returns: How many landed, and the ones that did not.
    """
    dismissals = list(
        OpinionBoundary.objects.standing_dismissals()
        .filter(scan=scan)
        .order_by("pk")
    )
    if not dismissals:
        return 0, []
    scan.refresh_from_db(fields=["source_fingerprint"])
    candidates: dict[tuple, list[OpinionBoundary]] = {}
    for row in OpinionBoundary.objects.computed().filter(
        scan=scan,
        decision__isnull=True,
        start_source_page__in={d.start_source_page for d in dismissals},
    ):
        candidates.setdefault(row.start_address, []).append(row)
    taken: set[int] = set()
    landed = 0
    stale: list[OpinionBoundary] = []
    for dismissal in dismissals:
        if is_stale(dismissal, scan):
            stale.append(dismissal)
            continue
        best, best_distance = None, None
        for row in candidates.get(dismissal.start_address, []):
            if row.pk in taken:
                continue
            distance = _distance(
                (row.start_x, row.start_y),
                (dismissal.start_x, dismissal.start_y),
            )
            if best_distance is None or distance < best_distance:
                best, best_distance = row, distance
        if (
            best is None
            or best_distance is None
            or best_distance > ANCHOR_TOLERANCE_PT
        ):
            stale.append(dismissal)
            continue
        taken.add(best.pk)
        OpinionBoundary.objects.filter(pk=best.pk).update(decision=dismissal)
        landed += 1
    if stale:
        logger.warning(
            "scan %s: %d opinion dismissal(s) found no boundary to land on: %s",
            scan.pk,
            len(stale),
            ", ".join(
                f"#{d.pk} src p.{d.start_source_page} ({d.start_x:.0f},"
                f"{d.start_y:.0f})"
                for d in stale[:20]
            ),
        )
    return landed, stale


def relocate_human_rows(
    scan: Scan, run: ApplyRun
) -> tuple[int, list[OpinionBoundary]]:
    """Put every standing addition at its pages in ``run``'s space.

    The compute writes the computed rows in the new run's space and
    keeps the curator's additions as they are, so after a reopen that
    deletes a page a boundary drawn under ``a1`` would sit one page out
    under ``a2``. The anchors' addresses say where they belong
    (:func:`placer`). A row with an unplaceable anchor, a row of another
    original, or a row with no address keeps its old indexes and is
    logged; #240 PR D raises it as the stale finding. Only the two
    indexes and ``apply_run`` are written.

    :param scan: The scan.
    :param run: The run whose space the computed rows were written in.
    :returns: How many rows were written, and the rows left unplaced.
    """
    place = placer(run)
    moved = 0
    unplaced: list[OpinionBoundary] = []
    for row in OpinionBoundary.objects.standing_additions().filter(scan=scan):
        if is_stale(row, scan):
            unplaced.append(row)
            continue
        start = place(*row.start_address)
        end = place(*row.end_address)
        if start is None or end is None:
            unplaced.append(row)
            continue
        if (
            row.start_page_index != start
            or row.end_page_index != end
            or row.apply_run_id != run.pk
        ):
            OpinionBoundary.objects.filter(pk=row.pk).update(
                start_page_index=start, end_page_index=end, apply_run=run
            )
            moved += 1
    if unplaced:
        logger.warning(
            "scan %s: %d curator opinion boundary(ies) have no page in %s "
            "and keep their old position: %s",
            scan.pk,
            len(unplaced),
            run.label,
            ", ".join(
                f"#{r.pk} src p.{r.start_source_page}-{r.end_source_page}"
                for r in unplaced[:20]
            ),
        )
    return moved, unplaced


# ---------------------------------------------------------------------------
# The curator's decisions
# ---------------------------------------------------------------------------


def withdraw(rows, user) -> int:
    """Take back human rows, and give back what they hid.

    Nothing is deleted (#232). A withdrawn dismissal releases the
    computed rows that pointed at it, so the boundary is drawn again
    without a compute.

    :param rows: A queryset of human rows.
    :param user: Who took them back. May be None.
    :returns: How many rows were stamped.
    """
    now = timezone.now()
    count = 0
    with transaction.atomic():
        for row in rows.filter(
            origin=OpinionBoundary.Origin.HUMAN, withdrawn_at__isnull=True
        ):
            OpinionBoundary.objects.filter(decision=row).update(decision=None)
            count += OpinionBoundary.objects.filter(
                pk=row.pk, withdrawn_at__isnull=True
            ).update(withdrawn_at=now, withdrawn_by=user, date_modified=now)
    return count


def dismiss(scan: Scan, row: OpinionBoundary, user) -> OpinionBoundary | None:
    """Take a boundary out of the volume.

    A computed row gets a ``DISMISS`` row that copies its anchors, and
    its ``decision`` at once, so the viewer needs no compute to hide it;
    a standing dismissal is returned as it is. A human addition is
    withdrawn, and the dismissal it ``replaces`` with it, so a moved
    boundary comes back where the pairing put it.

    :param scan: The scan.
    :param row: The row the curator pointed at.
    :param user: The curator. May be None.
    :returns: The standing dismissal for a computed row; None for a
        withdrawn addition.
    :raises UnaddressableBoundary: For a computed row with no address.
    """
    with transaction.atomic():
        row = (
            OpinionBoundary.objects.select_for_update(of=("self",))
            .select_related("decision")
            .get(pk=row.pk)
        )
        if not row.is_computed:
            if withdraw(OpinionBoundary.objects.filter(pk=row.pk), user):
                if row.replaces_id:
                    withdraw(
                        OpinionBoundary.objects.filter(pk=row.replaces_id),
                        user,
                    )
            return None
        if row.decision_id and row.decision.withdrawn_at is None:
            return row.decision
        if row.start_source_page is None or row.end_source_page is None:
            # The rule of ``detections.decide``: a dismissal with no
            # address could never land (``resolve`` matches by the
            # start address), and one written anyway stood for good and
            # was logged after every compute.
            logger.warning(
                "scan %s: opinion boundary %s has no address in the "
                "standing map; the dismissal is refused",
                scan.pk,
                row.pk,
            )
            raise UnaddressableBoundary(
                f"boundary {row.pk} of scan {scan.pk} has no address"
            )
        dismissal = OpinionBoundary.objects.create(
            scan=scan,
            origin=OpinionBoundary.Origin.HUMAN,
            kind=OpinionBoundary.Kind.DISMISS,
            start_source_edit_id=row.start_source_edit_id,
            start_source_page=row.start_source_page,
            start_page_index=row.start_page_index,
            start_x=row.start_x,
            start_y=row.start_y,
            end_source_edit_id=row.end_source_edit_id,
            end_source_page=row.end_source_page,
            end_page_index=row.end_page_index,
            end_x=row.end_x,
            end_y=row.end_y,
            source_fingerprint=scan.source_fingerprint or "",
            apply_run_id=row.apply_run_id,
            start_detection_id=row.start_detection_id,
            end_detection_id=row.end_detection_id,
            author=user,
        )
        OpinionBoundary.objects.filter(pk=row.pk).update(decision=dismissal)
    return dismissal


def restore(scan: Scan, row: OpinionBoundary, user) -> bool:
    """Give a dismissed computed boundary back.

    Withdraws the standing dismissal, which clears ``decision``. A
    withdrawn addition is never restored: the curator adds again.

    :param scan: The scan.
    :param row: The computed row.
    :param user: The curator. May be None.
    :returns: Whether a dismissal stood before this call.
    """
    if not row.is_computed:
        return False
    # The caller's instance may predate the dismissal: read the FK.
    decision_id = (
        OpinionBoundary.objects.filter(pk=row.pk)
        .values_list("decision_id", flat=True)
        .first()
    )
    if not decision_id:
        return False
    return bool(withdraw(OpinionBoundary.objects.filter(pk=decision_id), user))


def _anchor(
    scan: Scan,
    spec: Detection | tuple[int, float, float],
    which: str,
    run: ApplyRun | None,
) -> dict[str, Any]:
    """Resolve one anchor of :func:`add` to its columns.

    :param scan: The scan.
    :param spec: A ``Detection`` row (its corner and its pk), or
        ``(page_index, x, y)`` with the point in PDF points.
    :param which: ``"start"`` or ``"end"``.
    :param run: The measured run.
    :returns: The field values, prefixed by ``which``.
    :raises UnaddressableBoundary: When the map holds no such page.
    """
    if isinstance(spec, Detection):
        page_index = spec.page_index
        x, y = anchor_of_detection(spec, which)
        detection_id = spec.pk
    else:
        page_index, x, y = spec
        detection_id = None
    edit_id, page = detections.source_for_index(scan, page_index, run)
    if not page:
        logger.warning(
            "scan %s: page_index %s has no address in the standing map; "
            "the opinion boundary is refused",
            scan.pk,
            page_index,
        )
        raise UnaddressableBoundary(
            f"page_index {page_index} of scan {scan.pk} has no address"
        )
    return {
        f"{which}_source_edit_id": edit_id,
        f"{which}_source_page": page,
        f"{which}_page_index": page_index,
        f"{which}_x": x,
        f"{which}_y": y,
        f"{which}_detection_id": detection_id,
    }


def add(
    scan: Scan,
    start: Detection | tuple[int, float, float],
    end: Detection | tuple[int, float, float],
    user,
    run: ApplyRun | None,
    replaces: OpinionBoundary | None = None,
) -> OpinionBoundary:
    """Write a boundary the curator drew.

    ``replaces`` names a computed row: the move dismisses it and writes
    the addition in one transaction, and the addition names the
    dismissal in ``replaces``, so withdrawing it gives the computed
    boundary back. An addition may span a merge or a split as well;
    only the viewer of this PR does not offer them (#287).

    :param scan: The scan.
    :param start: The caption row, or ``(page_index, x, y)`` in points.
    :param end: The key icon row, or ``(page_index, x, y)`` in points.
    :param user: The curator. May be None.
    :param run: ``detections.measured_run(scan)``.
    :param replaces: The row this boundary is made in place of: a
        computed boundary (dismissed here), or a standing curator
        addition (withdrawn here, its dismissal carried forward).
    :returns: The new row.
    :raises UnaddressableBoundary: When an anchor's page has no address.
    :raises MisorderedBoundary: When the end is before the start in
        reading order.
    """
    fields = _anchor(scan, start, "start", run)
    fields.update(_anchor(scan, end, "end", run))
    if fields["end_page_index"] < fields["start_page_index"]:
        raise MisorderedBoundary(
            f"scan {scan.pk}: the end page {fields['end_page_index']} is "
            f"before the start page {fields['start_page_index']}"
        )
    if fields["end_page_index"] == fields["start_page_index"]:
        # On one page the order is the reading order: the column from
        # the page's TEXT_COLUMN rows, then y. An end above the start in
        # the same column closes nothing; in the right column it may
        # sit higher than a start in the left one.
        page = fields["start_page_index"]
        divide = _column_boundaries(scan, {page}).get(page)

        def _key(x, y):
            column = 0 if divide is None or x < divide else 1
            return column, y

        if _key(fields["end_x"], fields["end_y"]) < _key(
            fields["start_x"], fields["start_y"]
        ):
            raise MisorderedBoundary(
                f"scan {scan.pk}: the end anchor is before the start "
                f"anchor on page {page}"
            )
    with transaction.atomic():
        dismissal = None
        if replaces is not None and replaces.is_computed:
            dismissal = dismiss(scan, replaces, user)
        elif replaces is not None:
            # A second move: withdraw the earlier addition alone, and
            # carry its dismissal forward. ``dismiss`` would withdraw the
            # dismissal too, and the computed boundary would stand
            # again beside the new addition (two boundaries for one
            # opinion). The one dismissal then stands through any number
            # of moves, and dismissing the last addition still gives the
            # computed boundary back.
            replaces = OpinionBoundary.objects.get(pk=replaces.pk)
            if replaces.kind != OpinionBoundary.Kind.ADD:
                raise ValueError(
                    "only a boundary or an addition can be replaced"
                )
            withdraw(OpinionBoundary.objects.filter(pk=replaces.pk), user)
            dismissal = replaces.replaces
        return OpinionBoundary.objects.create(
            scan=scan,
            origin=OpinionBoundary.Origin.HUMAN,
            kind=OpinionBoundary.Kind.ADD,
            source_fingerprint=scan.source_fingerprint or "",
            apply_run=run,
            author=user,
            replaces=dismissal,
            **fields,
        )


# ---------------------------------------------------------------------------
# The readers
# ---------------------------------------------------------------------------


def _column_boundaries(scan: Scan, page_indexes: set[int]) -> dict[int, float]:
    """Return the x, in points, that divides the two columns of each page.

    From the live ``TEXT_COLUMN`` rows of the page, the rule of
    blackletter's ``_column_bounds_pdf``: the middle of the gap between
    the leftmost and the rightmost box. A page with fewer than two boxes
    has one column and is absent.

    :param scan: The scan.
    :param page_indexes: The pages to answer for.
    :returns: ``{page_index: x}``.
    """
    if not page_indexes:
        return {}
    columns: dict[int, list[Detection]] = {}
    for row in Detection.objects.live().filter(
        scan=scan, label="TEXT_COLUMN", page_index__in=page_indexes
    ):
        columns.setdefault(row.page_index, []).append(row)
    boundaries: dict[int, float] = {}
    for page_index, rows in columns.items():
        if len(rows) < 2:
            continue
        rows.sort(key=lambda r: (r.x0 + r.x1) / 2)
        left, right = rows[0], rows[-1]
        px = (left.x1 + right.x0) / 2
        boundaries[page_index] = to_points(
            px, 0, left.img_width, left.img_height
        )[0]
    return boundaries


def reading_key(
    row: OpinionBoundary, columns: dict[int, float]
) -> tuple[int, int, float, float]:
    """Return the sort key of a boundary: page, column, then y, then x.

    Most reporters print two columns, and blackletter reads the left one
    top to bottom and then the right one, so an opinion can end low in
    the left column while the next starts high in the right one. A y
    alone would order them wrong (plan section 3.3).

    :param row: The boundary.
    :param columns: :func:`_column_boundaries` for the start pages.
    :returns: The key.
    """
    divide = columns.get(row.start_page_index)
    column = 0 if divide is None or row.start_x < divide else 1
    return row.start_page_index, column, row.start_y, row.start_x


def standing(scan: Scan) -> list[OpinionBoundary]:
    """Return the boundaries a reader may draw, in reading order.

    The computed rows, dismissed or not (``is_dismissed`` says which,
    so the sidebar can show a dismissed one muted with an undo), plus
    the curator's additions that are not withdrawn, less the computed
    rows a move replaced. One query for the rows, one for the moves,
    and one for the column boxes of their start pages. Every
    consumer -- the viewer JSON, the sidebar, the paused step 3 -- goes
    through here.

    :param scan: The scan.
    :returns: The rows, ordered.
    """
    rows = list(
        OpinionBoundary.objects.filter(scan=scan).filter(
            Q(origin=OpinionBoundary.Origin.COMPUTED)
            | Q(
                origin=OpinionBoundary.Origin.HUMAN,
                kind=OpinionBoundary.Kind.ADD,
                withdrawn_at__isnull=True,
            )
        )
    )
    # A computed boundary a move replaced is left out: the curator's
    # addition stands in its place, and its Dismiss is the undo of the
    # move. A muted card with its own Undo would be a second path to
    # the same act, and one that leaves the replacement standing. A
    # move is a dismissal with a standing row in ``replacements``.
    moved = set(
        OpinionBoundary.objects.standing_additions()
        .filter(replaces_id__in={r.decision_id for r in rows if r.decision_id})
        .values_list("replaces_id", flat=True)
    )
    rows = [r for r in rows if r.decision_id not in moved]
    columns = _column_boundaries(scan, {r.start_page_index for r in rows})
    rows.sort(key=lambda r: reading_key(r, columns))
    return rows


def _page_bounds(
    start_index: int,
    end_index: int,
    page_numbers: dict[int, tuple],
    first_page: int,
) -> tuple[int, int]:
    """Return the printed numbers an opinion's file is named by.

    The rule of blackletter's ``_opinion_page_bounds``: a page that
    prints a range gives its end to an opinion that starts there and its
    start to one that ends there; a page with no number falls back to
    its index plus the volume's first page.

    :param start_index: The start page, 0-based.
    :param end_index: The end page, 0-based.
    :param page_numbers: ``{page_index: (number, end or None)}``.
    :param first_page: The volume's first printed page.
    :returns: ``(first, last)``.
    """
    start = page_numbers.get(start_index) or (None, None)
    end = page_numbers.get(end_index) or (None, None)
    first = start[1] or start[0] or start_index + first_page
    last = end[0] or end_index + first_page
    return first, last


def outside_rects(
    scan: Scan, rows: list[OpinionBoundary]
) -> dict[int, list[dict]]:
    """Return the masks over the neighbours' text on a shared page.

    Derived from the anchors and the live rows, unwidened (blackletter's
    ``_outside_opinion_rects`` with no page: the ink growth needs the
    PDF, which step 3 has and the viewer does not). On an opinion's
    first page everything before the caption in reading order is
    masked, on its last page everything after the key. The page
    geometry comes from the rows' render size at ``yolo.DPI``.

    :param scan: The scan.
    :param rows: The boundaries to answer for.
    :returns: ``{boundary pk: [{"page_index", "x0", "y0", "x1", "y1"}]}``
        in PDF points.
    """
    from blackletter.models import BBox, Label, Page
    from blackletter.models import Detection as BLDetection
    from blackletter.scanner import _outside_opinion_rects

    pages_wanted: set[int] = set()
    for row in rows:
        pages_wanted.add(row.start_page_index)
        pages_wanted.add(row.end_page_index)
    if not pages_wanted:
        return {}
    by_page: dict[int, list[Detection]] = {}
    for det in Detection.objects.live().filter(
        scan=scan, page_index__in=pages_wanted
    ):
        by_page.setdefault(det.page_index, []).append(det)
    pages: dict[int, Page] = {}
    for page_index, dets in by_page.items():
        img_w = dets[0].img_width or 1
        img_h = dets[0].img_height or 1
        pw, ph = to_points(img_w, img_h, img_w, img_h)
        page = Page(
            index=page_index,
            pdf_width=pw,
            pdf_height=ph,
            img_width=img_w,
            img_height=img_h,
        )
        for det in dets:
            try:
                label = Label(det.label_id)
            except ValueError:
                continue
            page.detections.append(
                BLDetection(
                    bbox=BBox(x1=det.x0, y1=det.y0, x2=det.x1, y2=det.y1),
                    label=label,
                    confidence=det.confidence,
                    page_index=page_index,
                )
            )
        pages[page_index] = page

    def _marker(page: Page, x: float, y: float, which: str) -> BLDetection:
        """A one-pixel detection at the anchor, in the page's pixels."""
        px = x / page.scale_x
        py = y / page.scale_y
        if which == "start":
            box = BBox(x1=px, y1=py, x2=px + 1, y2=py + 1)
            label = Label.CASE_CAPTION
        else:
            box = BBox(x1=px - 1, y1=py - 1, x2=px, y2=py)
            label = Label.KEY_ICON
        return BLDetection(
            bbox=box, label=label, confidence=1.0, page_index=page.index
        )

    result: dict[int, list[dict]] = {}
    for row in rows:
        rects: list[dict] = []
        # The first and the last page, once each when they are one.
        for page_index in sorted({row.start_page_index, row.end_page_index}):
            page = pages.get(page_index)
            if page is None:
                continue
            is_first = page_index == row.start_page_index
            is_last = page_index == row.end_page_index
            caption = _marker(page, row.start_x, row.start_y, "start")
            key = _marker(page, row.end_x, row.end_y, "end")
            for rect in _outside_opinion_rects(
                page, page.pdf_width, caption, key, is_first, is_last
            ):
                rects.append(
                    {
                        "page_index": page_index,
                        "x0": round(rect.x0, 1),
                        "y0": round(rect.y0, 1),
                        "x1": round(rect.x1, 1),
                        "y1": round(rect.y1, 1),
                    }
                )
        result[row.pk] = rects
    return result


def has_live(scan: Scan) -> bool:
    """Return whether the volume has a boundary a reader would draw.

    A computed row under no dismissal, or a curator's addition not
    withdrawn. The one answer for the action bar's "Next: Generate"
    (``views_process._review_flags``), so its two renders agree (#151).

    :param scan: The scan.
    :returns: Whether one exists.
    """
    return (
        OpinionBoundary.objects.filter(scan=scan)
        .filter(
            Q(origin=OpinionBoundary.Origin.COMPUTED, decision__isnull=True)
            | Q(
                origin=OpinionBoundary.Origin.HUMAN,
                kind=OpinionBoundary.Kind.ADD,
                withdrawn_at__isnull=True,
            )
        )
        .exists()
    )


def viewer_payload(
    scan: Scan,
    page_numbers: dict[int, tuple] | None = None,
    live_only: bool = False,
) -> list[dict]:
    """Return the standing boundaries in the shape the viewer reads.

    The dict shape of ``blackletter.api.pair`` -- ``caption_page``,
    ``key_page``, ``end_page`` (the stored indexes), ``page_count``,
    ``has_image``, ``first_page_number``, ``last_page_number``,
    ``outside_rects`` -- plus ``id``, ``origin``, ``kind``,
    ``dismissed``, ``dismissal_id``, ``caption_detection_id``,
    ``key_detection_id``, ``start`` and ``end``. ``caption_bbox`` and
    ``key_bbox`` are gone: their one reader matched the paired rows by
    rounded pixels, and the ids replace that match.

    :param scan: The scan.
    :param page_numbers: ``{page_index: (number, end or None)}`` in the
        rows' space, when the caller holds it;
        ``services._page_number_lookup`` otherwise.
    :param live_only: Leave the dismissed rows out. Step 3 wants the
        opinions it cuts, not the cards the sidebar shows.
    :returns: The dicts, in reading order.
    """
    rows = standing(scan)
    if live_only:
        rows = [r for r in rows if not r.is_dismissed]
    if not rows:
        return []
    if page_numbers is None:
        from scanning.services import _page_number_lookup

        page_numbers = _page_number_lookup(scan)
    image_pages = set(
        Detection.objects.live()
        .filter(scan=scan, label="IMAGE")
        .values_list("page_index", flat=True)
    )
    masks = outside_rects(scan, rows)
    first_page = scan.start_page or 1
    payload = []
    for row in rows:
        first, last = _page_bounds(
            row.start_page_index, row.end_page_index, page_numbers, first_page
        )
        payload.append(
            {
                "id": row.pk,
                "origin": row.origin,
                "kind": row.kind,
                "dismissed": row.is_dismissed,
                "dismissal_id": row.decision_id,
                "caption_page": row.start_page_index,
                "key_page": row.end_page_index,
                "end_page": row.end_page_index,
                "page_count": row.end_page_index - row.start_page_index + 1,
                "caption_detection_id": row.start_detection_id,
                "key_detection_id": row.end_detection_id,
                "start": {"x": row.start_x, "y": row.start_y},
                "end": {"x": row.end_x, "y": row.end_y},
                "has_image": any(
                    p in image_pages
                    for p in range(
                        row.start_page_index, row.end_page_index + 1
                    )
                ),
                "first_page_number": first,
                "last_page_number": last,
                "outside_rects": masks.get(row.pk, []),
            }
        )
    return payload
