"""Give the margin strips the text box of their page (issue #323).

A scanned volume has long vertical blots down the outer page edge, and
the margin strips miss them. blackletter measures the content box of a
page from its ink, and the ink is the union of the printed text *and*
the dirt, so one blot down the full height moves the box to the page
edge. Its second signal, the ``TEXT_COLUMN`` band, is gated on
``margins._ink_is_artifact_like``, which asks whether the ink it would
give up is near-solid or negligible. A blot is neither, so it reads as
text and the tightening is refused. Measured on scan 1828 (143 S.Ct.,
888 pages): refused on 442 of the 463 pages that wanted it, and 372
pages got no bottom strip at all, because a full-height blot puts the
ink box on the bottom edge.

**This app has a third signal: the dots.mocr cells.** Each cell is one
layout region with a box, and dirt gets no cell, so the union of the
cells of a page is where that page's printed text is. blackletter
takes it as ``Page.text_box`` and intersects the content box with it
(blackletter #78).

**The rule**, in :func:`text_box`, and nothing else measures it:

    A page's text box is the union of the dots.mocr cells of that page,
    in that page's pixels. A page whose cells were not read gets none,
    and blackletter then answers as it did before.

Every guard is blackletter's, on purpose. The box this states is
intersected with the one measured from the ink, which never leaves
``margins``: only that side can say whether a fit keeps enough of it
(``MARGIN_MIN_KEEP_RATIO``), whether it would cut inside a header-row
or column detection, or whether it describes some other page frame.
This module states a measurement; the library decides what to do with
it. That is the same split as ``ocr_applied``.

The cells are the measurement ``text_fit`` reads (#279) and
``columns`` reads (#308), and ``text_fit.load_document`` stays the one
rule for which OCR document they come from (#328).

**The boxes the margin measure reads are held inside that text box
too** (#370). The fit above arrives at the right content box on a
blotted page, and two model boxes then undo it: blackletter pulls every
strip back off any detection it would cover
(``margins._shrink_rects_for_detections``), and lets a ``TEXT_COLUMN``
hold the fit (``HOLD_LABELS``). On scan 1841 of this app (147 pages)
the model drew an ``IMAGE`` box over the whole text body of 93 pages,
and on some pages the ``TEXT_COLUMN`` box itself, out onto the blot and
up to the page edge; the strip on that side was then pinned at the
box's edge or dropped: 16 pages with no left strip, 14 with no right,
55 of 73 edge blots uncovered.

**The rule**, in :func:`clipped_pages`:

    A ``TEXT_COLUMN`` box (its x-bounds) and an ``IMAGE`` box (both
    axes) are read inside the page's text box, padded by the buffer the
    strips leave, on a copy of the page. A real picture is a cell, so it
    is inside the box; a text column is inside it by definition; the
    part of either box outside it is over dirt.

The column's y-bounds stay the model's own: they are what holds the fit
off a last line the reader missed. Two guards, the refusals visible from
this side of blackletter: a page whose text box is narrower than
``MIN_TEXT_WIDTH_FRACTION`` of the render (the one-column read of a
two-column page, where blackletter refuses the fit and the clip would be
the only effect) is left alone, and a box the clip would cut below
``CLIP_MIN_KEEP_RATIO`` of itself (a partial read, a plate with no cell)
is kept as it is, never dropped: a dropped box lets a strip cover what
it described. Under that rule scan 1841 lost no side strip, covered every
blot, and put no strip over a cell. The copies are for the margin
measure alone: the headnote rects and the outside-opinion masks read
the document's own column boxes, and must not change. When blackletter
takes the rule, :func:`clipped_pages` is a deletion.

**Every page gets its four strips** (:func:`ensure_strips`, #370). A
curator widens a strip by dragging it and draws one from nothing with
more work, so a strip the measure did not give a page is a
``MIN_STRIP_PT`` handle at its page edge. It is the same strip at every
compute, so a dismissal of it lands on the next.
"""

from __future__ import annotations

import logging
from dataclasses import replace

from blackletter.margins import DEFAULT_BUFFER, MIN_TEXT_WIDTH_FRACTION
from blackletter.models import BBox, Label

from scanning.text_fit import PageCells

logger = logging.getLogger(__name__)

#: The labels whose boxes are read inside the text box (#370): the two
#: the model draws out onto a blot along the page edge.
CLIPPED_LABELS = frozenset({Label.TEXT_COLUMN, Label.IMAGE})

#: Of those, the labels clipped on the x axis alone. A column's y-bounds
#: are the one thing holding the fit off a last line the reader missed.
X_ONLY_LABELS = frozenset({Label.TEXT_COLUMN})

#: The least of itself a box may keep for the clip to stand. A blot
#: overrun is a sliver (the most any box of scan 1841 lost was a third);
#: a box that would lose more disagrees with the reader grossly, which
#: is a partial read or a picture the reader gave no cell, and it keeps
#: pinning the strip as it does today.
CLIP_MIN_KEEP_RATIO = 0.5

#: The width, in PDF points, of a strip :func:`ensure_strips` adds where
#: the measure gave none. About 2 mm: invisible on a trimmed reporter
#: page, and one resize handle wide in the step-2 viewer.
MIN_STRIP_PT = 6.0


def _held(value: float, limit: float) -> float:
    """Hold one coordinate inside the render it was measured in.

    :param value: The coordinate, in render pixels.
    :param limit: The render's extent on that axis, in pixels.
    :returns: The coordinate, inside ``[0, limit]``.
    :rtype: float
    """
    return min(max(float(value), 0.0), limit)


def text_box(
    cells: PageCells | None, img_width: float, img_height: float
) -> tuple[float, float, float, float] | None:
    """Return the union of a page's cells, in that page's pixels.

    Every coordinate goes through the fraction of the page it covers,
    as :func:`text_fit.fit_span` does, so the two renders meet without
    either knowing the other's resolution. A page the worker re-rendered
    (``PageCells.fallback``) needs no check for the same reason: the
    answer is a fraction of the same page either way.

    :param cells: The page's cells, in their own render pixels, or
        None for a page no reader answered.
    :param img_width: The page's render width, in pixels.
    :param img_height: The page's render height, in pixels.
    :returns: ``(x0, y0, x1, y1)`` in the page's pixels, or None when
        the page has no usable cell or no usable render size.
    :rtype: tuple[float, float, float, float] | None
    """
    if cells is None or not cells.boxes:
        return None
    if (
        cells.width <= 0
        or cells.height <= 0
        or img_width <= 0
        or img_height <= 0
    ):
        return None
    # Each cell is held inside the render it was measured in first.
    # ``layout_json.rescale`` divides and truncates without clamping,
    # and ``text_fit._cell_box`` reads the order of a box and not its
    # frame, so a cell can arrive outside the page. One would cost the
    # whole page its fit, because this is a union and blackletter
    # refuses a box past the page's pixels; the text fit is spared that,
    # because it measures a box against the cells that overlap it and
    # not against every cell of the page. A frame that is wrong, rather
    # than one cell that is stray, clamps to the whole page, which fits
    # nothing and changes nothing.
    x0 = min(_held(box[0], cells.width) for box in cells.boxes) / cells.width
    y0 = min(_held(box[1], cells.height) for box in cells.boxes) / cells.height
    x1 = max(_held(box[2], cells.width) for box in cells.boxes) / cells.width
    y1 = max(_held(box[3], cells.height) for box in cells.boxes) / cells.height
    if x1 <= x0 or y1 <= y0:
        return None
    # Rounded because the two frames are usually the same render, and a
    # fraction taken and put back costs a pixel its last bits.
    return (
        round(x0 * img_width, 2),
        round(y0 * img_height, 2),
        round(x1 * img_width, 2),
        round(y1 * img_height, 2),
    )


def fit_pages(document, cells: dict[int, PageCells]) -> int:
    """Set ``text_box`` on every page of a document whose cells were read.

    The last word on the box the margin measure reads. It runs after
    ``columns.separate_document`` for no reason of its own: the two
    touch different fields, and the order keeps every cell-derived
    correction of a document in one place.

    A page with no entry keeps None. That covers a failed page, a
    filtered page (#242), the new page of an insert that no reader
    answered, and every page of a volume with no OCR document at all
    (``text_fit.load_cells`` answers ``{}`` for that, over a
    ``load_document`` that never raises).
    Such a page keeps the strips blackletter measures from its ink,
    which is what it gets today, so a read that is missing costs a page
    the fit and never more than the fit.

    :param document: The blackletter document, mutated in place.
    :param cells: The cells of each page, ``text_fit.page_cells``.
    :returns: How many pages were given a text box.
    :rtype: int
    """
    if not cells:
        return 0
    fitted = 0
    pages = 0
    for page in getattr(document, "pages", []):
        pages += 1
        box = text_box(
            cells.get(page.index),
            float(page.img_width),
            float(page.img_height),
        )
        if box is None:
            continue
        page.text_box = box
        fitted += 1
    if pages:
        logger.info(
            "Margin text box: %d of %d page(s) carry one; %d page(s) keep the "
            "box blackletter measures from the ink",
            fitted,
            pages,
            pages - fitted,
        )
    return fitted


def _clipped_box(bbox: BBox, frame: BBox, x_only: bool) -> BBox | None:
    """Return ``bbox`` held inside ``frame``, or None when the clip is refused.

    :param bbox: The detection's box, in the page's pixels.
    :param frame: The padded text box, in the same pixels.
    :param x_only: Whether to leave the y-bounds as they are.
    :returns: The clipped box, or None when it would be empty or keep
        less than :data:`CLIP_MIN_KEEP_RATIO` of the box's area.
    :rtype: BBox | None
    """
    x1 = max(bbox.x1, frame.x1)
    x2 = min(bbox.x2, frame.x2)
    y1, y2 = (
        (bbox.y1, bbox.y2)
        if x_only
        else (max(bbox.y1, frame.y1), min(bbox.y2, frame.y2))
    )
    if x2 <= x1 or y2 <= y1:
        return None
    area = (bbox.x2 - bbox.x1) * (bbox.y2 - bbox.y1)
    if area <= 0:
        return None
    if (x2 - x1) * (y2 - y1) / area < CLIP_MIN_KEEP_RATIO:
        return None
    return BBox(x1=x1, y1=y1, x2=x2, y2=y2)


def clipped_pages(pages) -> list:
    """Return the pages with their column and image boxes held inside the text box.

    A page is returned as the same object when nothing is clipped: it
    has no text box, its text box is narrower than
    ``MIN_TEXT_WIDTH_FRACTION`` of the render, or no box of
    :data:`CLIPPED_LABELS` reaches past the frame. Otherwise a copy is
    returned whose changed detections are copies too; the page given and
    its detections are never mutated.

    The frame is the text box padded by ``margins.DEFAULT_BUFFER`` on
    every side, the buffer ``compute_margin_rects`` defaults to and the
    one ``services._measure_margin_rects`` lets it default to. The two
    move together: a strip stands that buffer off the content box, so a
    box held at the same distance pins nothing the fit did not already
    leave.

    :param pages: The blackletter pages of the document.
    :returns: The pages, in order, for the margin measure.
    :rtype: list
    """
    out = []
    read = clipped = kept = gated = 0
    for page in pages:
        box = page.text_box
        if box is None:
            out.append(page)
            continue
        read += 1
        tx1, ty1, tx2, ty2 = (float(v) for v in box)
        if tx2 - tx1 < MIN_TEXT_WIDTH_FRACTION * float(page.img_width):
            gated += 1
            out.append(page)
            continue
        pad_x = DEFAULT_BUFFER / page.scale_x
        pad_y = DEFAULT_BUFFER / page.scale_y
        frame = BBox(
            x1=tx1 - pad_x, y1=ty1 - pad_y, x2=tx2 + pad_x, y2=ty2 + pad_y
        )
        detections = []
        changed = False
        for det in page.detections:
            if det.label not in CLIPPED_LABELS:
                detections.append(det)
                continue
            held = _clipped_box(det.bbox, frame, det.label in X_ONLY_LABELS)
            if held is None:
                kept += 1
                detections.append(det)
                continue
            if held == det.bbox:
                detections.append(det)
                continue
            clipped += 1
            changed = True
            detections.append(replace(det, bbox=held))
        out.append(replace(page, detections=detections) if changed else page)
    if read:
        logger.info(
            "Margin boxes: %d page(s) with a text box, %d column/image box(es) "
            "held inside it, %d kept whole below the keep ratio, %d page(s) "
            "skipped for a narrow text box",
            read,
            clipped,
            kept,
            gated,
        )
    return out


def _sides(entry: dict) -> dict:
    """Name the strips of one page as ``margins._rects_for_bounds`` lays them out.

    A full-width strip at the top edge is the top, one at the bottom
    edge the bottom; a strip at the left edge is the left, one at the
    right edge the right.

    :param entry: One ``compute_margin_rects`` entry.
    :returns: ``{"top", "bottom", "left", "right"}``, each a rect or None.
    :rtype: dict
    """
    pw = float(entry["page_width"])
    ph = float(entry["page_height"])
    sides = {"top": None, "bottom": None, "left": None, "right": None}
    for rect in entry.get("rects") or []:
        full_width = rect["x0"] <= 1 and rect["x1"] >= pw - 1
        if full_width and rect["y0"] <= 1:
            sides["top"] = rect
        elif full_width and rect["y1"] >= ph - 1:
            sides["bottom"] = rect
        elif rect["x0"] <= 1:
            sides["left"] = rect
        elif rect["x1"] >= pw - 1:
            sides["right"] = rect
    return sides


def ensure_strips(entries: list, min_pt: float = MIN_STRIP_PT) -> int:
    """Give every page the four strips, adding a thin one where the measure gave none.

    The top and bottom are added first, full width and ``min_pt`` tall;
    a side is added ``min_pt`` wide, spanning the rows between the top
    and bottom strips, as blackletter lays the sides out. Coordinates
    are rounded to a tenth of a point, as blackletter's are. A page too
    small to hold two strips is left alone.

    :param entries: The ``compute_margin_rects`` entries, mutated in place.
    :param min_pt: The width of an added strip, in PDF points.
    :returns: How many strips were added.
    :rtype: int
    """
    added = 0
    for entry in entries:
        pw = float(entry.get("page_width") or 0)
        ph = float(entry.get("page_height") or 0)
        if pw <= 2 * min_pt or ph <= 2 * min_pt:
            continue
        rects = entry.setdefault("rects", [])
        sides = _sides(entry)
        if sides["top"] is None:
            sides["top"] = {
                "x0": 0,
                "y0": 0,
                "x1": round(pw, 1),
                "y1": round(min_pt, 1),
            }
            rects.append(sides["top"])
            added += 1
        if sides["bottom"] is None:
            sides["bottom"] = {
                "x0": 0,
                "y0": round(ph - min_pt, 1),
                "x1": round(pw, 1),
                "y1": round(ph, 1),
            }
            rects.append(sides["bottom"])
            added += 1
        y0 = sides["top"]["y1"]
        y1 = sides["bottom"]["y0"]
        if sides["left"] is None:
            rects.append(
                {
                    "x0": 0,
                    "y0": round(y0, 1),
                    "x1": round(min_pt, 1),
                    "y1": round(y1, 1),
                }
            )
            added += 1
        if sides["right"] is None:
            rects.append(
                {
                    "x0": round(pw - min_pt, 1),
                    "y0": round(y0, 1),
                    "x1": round(pw, 1),
                    "y1": round(y1, 1),
                }
            )
            added += 1
    if added:
        logger.info(
            "Margin strips: %d thin strip(s) added where the measure gave none",
            added,
        )
    return added
