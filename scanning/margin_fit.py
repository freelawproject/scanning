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
"""

from __future__ import annotations

import logging

from scanning.text_fit import PageCells

logger = logging.getLogger(__name__)


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
