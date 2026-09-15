"""Give the two column boxes of a page a gutter (issue #308).

A headnote redaction box takes its width from the ``TEXT_COLUMN`` box
of its column, and bl_warm does not measure two columns: it measures one
text block and cuts it at the centre. So the two boxes of a page share
an edge, on all 1291 two-column pages of one real volume, and the shared
edge is a midline rather than a column edge. Two faults follow from that
one input, and both end with a black box over the court's own text:

* ``blackletter.scanner._gutter_limits`` looks for the neighbour column
  with a strict ``b.x2 < holding.x1``. Two boxes that share an edge have
  no neighbour, so the limits come back infinite and
  ``clamp_to_gutters`` holds nothing back.
* The shared edge sits on the facing column's ink -- within one 100-dpi
  pixel of it on 285 pages of that volume. ``ink.grow_to_ink`` then
  starts its walk on an inked pixel column, one dark pixel keeps a short
  box walking, and the box runs up to its 20 pt budget into the facing
  text.

Measured over that volume, a redaction covered readable text of the
facing column on 57 pages, 192 boxes. With the columns this module
writes, none.

**The rule**, in :func:`gutter_span` and :func:`inner_edges`, and
nothing else measures a column edge:

    A page's two ``TEXT_COLUMN`` boxes are separated before any geometry
    reads them. The inner edges come from the dots.mocr cells: keep the
    cells the two boxes cover, drop a cell wider than
    :data:`WIDE_CELL_RATIO` of the page, project the rest on x, and take
    the widest blank band near the middle. An edge moves inwards only.
    A page with no band keeps its edges and gets a pixel and a half of
    gap on each side of the gutter centre, which does not move.

The vertical test is what removes the running head: one cell holds the
whole head and it spans the gutter. It finds a band on 1290 of the 1291
two-column pages of that volume, 10.1 pt wide at the median.

Two callers share the rule, because they hold the boxes in different
places: :func:`separate_rows` for the ``Detection`` rows, so the viewer
overlay and ``detection_entries`` show what the geometry used, and
:func:`separate_document` for a blackletter document, which runs after
``snap_document_columns`` because that pass grows each box to the ink
and caps it at the gutter centre, where two boxes could meet again.

The cells are the measurement ``text_fit`` reads (#279), and
``text_fit.load_cells`` stays the one rule for which OCR document that
is. Nothing here reads ``PageCells.fallback``: the rule compares two
fractions of the same page, as ``text_fit.fit_rects`` does, so a page
the worker re-rendered is measured correctly.
"""

from __future__ import annotations

import logging
from dataclasses import replace

from blackletter.models import Label

from scanning.text_fit import PageCells

logger = logging.getLogger(__name__)

#: A cell wider than this fraction of the page spans both columns: a
#: table, a picture or a full-width footnote. It is dropped before the
#: projection, because one such cell bridges the gutter and hides it.
WIDE_CELL_RATIO = 0.60

#: Where the centre of a real gutter lies, as a fraction of the page
#: width. A band outside this is the space beside a single column, or
#: the margin of a page whose cells were read badly.
BAND_LIMITS = (0.35, 0.65)

#: The narrowest a column may be left, as a fraction of its width. A
#: band that would take more of it is not this page's gutter, and the
#: pair falls back to the nudge.
MIN_KEEP_RATIO = 0.50

#: How far the fallback pulls each inner edge back, in pixels of the
#: render the boxes are in. Any gap at all is enough for
#: ``_gutter_limits`` to find a neighbour, and moving both edges by the
#: same amount keeps the gutter centre exactly where it was.
#:
#: Above :data:`MIN_MOVE_PX`, and that is the whole reason for the
#: half: the rule works in fractions of the page, so a move of exactly
#: one pixel comes back from the round trip as 0.999999... on most edge
#: values, the write is skipped as sub-pixel noise, and the pair stays
#: in contact with the fault it exists to remove.
NUDGE_PX = 1.5

#: The smallest move worth a write, in pixels. The same threshold
#: ``services._snap_text_columns_to_ink`` uses: under a pixel is under
#: the measurement.
MIN_MOVE_PX = 1.0


def gutter_span(
    cells: PageCells | None, top: float, bottom: float
) -> tuple[float, float] | None:
    """Return the blank band between the cells the two columns cover.

    Every coordinate is a fraction of the page: the caller divides its
    own boxes by its own page size, and the cells are divided by the
    render they were measured in, so the two spaces meet without either
    knowing the other.

    A cell counts when its vertical centre lies between ``top`` and
    ``bottom``. Without that the running head counts, and one cell holds
    the whole head and spans the gutter, so the page would look like one
    column.

    :param cells: The page's cells, or None when the page has none.
    :param top: The top of the two column boxes, as a fraction of the
        page height.
    :param bottom: Their bottom, the same way.
    :returns: ``(x0, x1)`` as fractions of the page width, or None when
        the page gives no usable band.
    :rtype: tuple | None
    """
    if cells is None or not cells.boxes:
        return None
    spans = []
    for x0, y0, x1, y1 in cells.boxes:
        middle = (y0 + y1) / 2 / cells.height
        if middle < top or middle > bottom:
            continue
        left, right = x0 / cells.width, x1 / cells.width
        if right - left >= WIDE_CELL_RATIO:
            continue
        spans.append((left, right))
    if len(spans) < 2:
        return None
    # The widest band *near the middle*, not the widest band: a cell
    # alone in a margin leaves a wider one beside it, and taking that
    # one would lose the gutter to the fallback.
    spans.sort()
    band, reach = None, spans[0][1]
    for left, right in spans[1:]:
        if (
            left > reach
            and BAND_LIMITS[0] < (reach + left) / 2 < BAND_LIMITS[1]
        ):
            if band is None or left - reach > band[1] - band[0]:
                band = (reach, left)
        reach = max(reach, right)
    return band


def inner_edges(
    left: tuple[float, float],
    right: tuple[float, float],
    band: tuple[float, float] | None,
    nudge: float,
) -> tuple[float, float] | None:
    """Return the inner edges the pair should have, or None to leave it.

    **The one rule.** ``left`` and ``right`` are ``(x0, x1)`` of the two
    boxes, and every coordinate is a fraction of the page width, as in
    :func:`gutter_span`.

    An edge moves inwards only. A box that grows takes in the facing
    column, which is the fault this exists to remove, and the ink snap
    that runs after this is the pass that may grow one, against the ink
    and no further than the gutter centre.

    :param left: The left box, ``(x0, x1)``.
    :param right: The right box, ``(x0, x1)``.
    :param band: :func:`gutter_span`, or None.
    :param nudge: How far the fallback pulls each edge back, as a
        fraction of the page width.
    :returns: The new ``(left inner, right inner)``, or None.
    :rtype: tuple | None
    """
    if left[1] <= left[0] or right[1] <= right[0]:
        return None
    if band is not None:
        # Inwards only: the band is the answer when it is inside the
        # boxes, and each box keeps whichever edge is already narrower.
        new_left = min(left[1], band[0])
        new_right = max(right[0], band[1])
        kept_left = new_left - left[0]
        kept_right = right[1] - new_right
        if (
            new_left < new_right
            and kept_left >= (left[1] - left[0]) * MIN_KEEP_RATIO
            and kept_right >= (right[1] - right[0]) * MIN_KEEP_RATIO
        ):
            if new_left == left[1] and new_right == right[0]:
                return None
            return new_left, new_right
    if left[1] < right[0]:
        # No usable band, and the pair already has a gutter for
        # ``_gutter_limits`` to measure. Nothing here can improve it.
        return None
    edge = (left[1] + right[0]) / 2
    return edge - nudge, edge + nudge


def separate_rows(scan, cells: dict[int, PageCells]) -> int:
    """Separate the ``TEXT_COLUMN`` rows of a scan, in place.

    The rows are what ``services.detection_entries`` hands every reader
    of the geometry, and what the viewer draws, so correcting them here
    keeps the two the same answer.

    **A pair a curator drew is not written.** A hand-drawn box reaches
    the database exactly as the reviewer drew it, and
    :func:`separate_document` corrects the geometry's copy of it without
    touching the row.

    :param scan: The scan.
    :param cells: The cells of each page, ``text_fit.page_cells``. An
        empty map still separates a touching pair, by the nudge.
    :returns: How many rows were written.
    :rtype: int
    """
    from scanning.models import Detection

    pages: dict[int, list] = {}
    for row in (
        Detection.objects.live()
        .filter(scan=scan, label="TEXT_COLUMN")
        .order_by("page_index", "x0")
    ):
        pages.setdefault(row.page_index, []).append(row)

    changed, pairs = [], 0
    for page_index, rows in sorted(pages.items()):
        if len(rows) != 2 or any(
            row.model_name == Detection.ModelName.MANUAL for row in rows
        ):
            continue
        pairs += 1
        left, right = rows
        width, height = float(left.img_width), float(left.img_height)
        if width <= 0 or height <= 0:
            continue
        edges = _edges_for(
            (left.x0, left.x1),
            (right.x0, right.x1),
            cells.get(page_index),
            width,
            height,
            min(left.y0, right.y0),
            max(left.y1, right.y1),
        )
        if edges is None:
            continue
        new_left, new_right = edges
        if abs(new_left - left.x1) >= MIN_MOVE_PX:
            left.x1 = round(new_left, 1)
            changed.append(left)
        if abs(new_right - right.x0) >= MIN_MOVE_PX:
            right.x0 = round(new_right, 1)
            changed.append(right)
    if changed:
        Detection.objects.bulk_update(changed, ["x0", "x1"], batch_size=500)
    _log(pairs, len(changed), "the rows")
    return len(changed)


def separate_document(document, cells: dict[int, PageCells]) -> int:
    """Separate the column boxes of a blackletter document, in place.

    The last word on the boxes the geometry reads.
    ``snap_text_columns_to_ink`` grows each box onto its ink and caps it
    at the gutter centre, so two boxes that started apart can meet
    there; this runs after it and puts the gap back.

    Positions are kept, like ``snap_text_columns_to_ink`` keeps them:
    ``Detection`` compares by value, so two identical boxes on one page
    would otherwise be indistinguishable.

    :param document: The document, mutated in place.
    :param cells: The cells of each page, ``text_fit.page_cells``.
    :returns: How many boxes were moved.
    :rtype: int
    """
    moved = 0
    pages = 0
    for page in getattr(document, "pages", []):
        indexed = sorted(
            (
                (position, det)
                for position, det in enumerate(page.detections)
                if det.label == Label.TEXT_COLUMN
            ),
            key=lambda pair: pair[1].bbox.x1,
        )
        if len(indexed) != 2:
            continue
        pages += 1
        (left_at, left), (right_at, right) = indexed
        width, height = float(page.img_width), float(page.img_height)
        if width <= 0 or height <= 0:
            continue
        edges = _edges_for(
            (left.bbox.x1, left.bbox.x2),
            (right.bbox.x1, right.bbox.x2),
            cells.get(page.index),
            width,
            height,
            min(left.bbox.y1, right.bbox.y1),
            max(left.bbox.y2, right.bbox.y2),
        )
        if edges is None:
            continue
        new_left, new_right = edges
        if abs(new_left - left.bbox.x2) >= MIN_MOVE_PX:
            page.detections[left_at] = replace(
                left, bbox=replace(left.bbox, x2=round(new_left, 1))
            )
            moved += 1
        if abs(new_right - right.bbox.x1) >= MIN_MOVE_PX:
            page.detections[right_at] = replace(
                right, bbox=replace(right.bbox, x1=round(new_right, 1))
            )
            moved += 1
    _log(pages, moved, "the document")
    return moved


def _edges_for(
    left: tuple[float, float],
    right: tuple[float, float],
    cells: PageCells | None,
    width: float,
    height: float,
    top: float,
    bottom: float,
) -> tuple[float, float] | None:
    """Answer one pair, in the pixels the boxes are in.

    The two callers hold their boxes in the same render but read them
    off different objects, so the conversion to fractions of the page
    and back lives here, once.

    :param left: The left box's ``(x0, x1)``, in render pixels.
    :param right: The right box's ``(x0, x1)``, the same way.
    :param cells: The page's cells, or None.
    :param width: The render width, in pixels.
    :param height: The render height, in pixels.
    :param top: The top of the two boxes, in render pixels.
    :param bottom: Their bottom, in render pixels.
    :returns: The new inner edges in render pixels, or None.
    :rtype: tuple | None
    """
    band = gutter_span(cells, top / height, bottom / height)
    edges = inner_edges(
        (left[0] / width, left[1] / width),
        (right[0] / width, right[1] / width),
        band,
        NUDGE_PX / width,
    )
    if edges is None:
        return None
    return edges[0] * width, edges[1] * width


def _log(pages: int, moved: int, who: str) -> None:
    """Log one pass, when it looked at anything.

    :param pages: The pairs the pass read.
    :param moved: The boxes it moved.
    :param who: What to name in the line.
    :return: None.
    """
    if not pages:
        return
    logger.info(
        "column gutter (%s): %d page(s) with two column boxes, %d box(es) "
        "moved",
        who,
        pages,
        moved,
    )
