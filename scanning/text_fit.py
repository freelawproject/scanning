"""Fit a text redaction box to the text under it (issue #279).

A text redaction box is wider than the text column below it, and the
two boxes of a page almost touch: a reader sees one black band across
the two columns.

**The cause is here, not in blackletter.**
``services._build_document_with_ids`` builds every
``blackletter.models.Page`` with no column bounds, so
``Page.__post_init__`` applies the fallback split: the left column is
3 % to 48 % of the page width and the right column 52 % to 97 %.
``scanner._redaction_rects`` and ``scanner._headnote_fallback_rects``
read exactly those bounds, so each box overruns its text column on the
outer side and the gap between the two is 4 % of the page width.
blackletter cannot correct it afterwards: ``scanner._text_x_bounds``
refuses to move the horizontal limits when the text layer came from our
own OCR, and its reason is right -- a box that is too narrow leaves
headnote text in the deliverable, and blackletter has no better
measurement of the column.

**This app has one: the dots.mocr cells.** Each cell is one layout
region with a bounding box, and a cell of a two-column page belongs to
one column. The union of the cells a box covers gives the true
horizontal limits of the text under it.

The rule, in :func:`fit_span`, and nothing else measures a text box:

    A text redaction box takes the horizontal limits of the dots.mocr
    cells it covers. The box gets narrower, never wider. The vertical
    limits do not move.

The vertical limits stay where blackletter measured them, from the page
ink (``_tighten_to_text``, ``_text_bottom``). Ink is a finer
measurement than a layout cell, and a cell that is one line short would
leave that line of a headnote in the deliverable.

Two callers share the rule, because they hold the boxes in different
spaces: :func:`fit_rects` for the compute, which holds blackletter's
rects in the pixels of the detection render, and :func:`fit_rows` for
the backfill, which holds ``Redaction`` rows in PDF points. Both
normalize to the fraction of the page they cover before they ask.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import NamedTuple

from scanning import dots_mocr

logger = logging.getLogger(__name__)

#: The redaction types a fit may move: the two that are blocks of body
#: text. Every other type stays as it is, and each for its own reason.
#: ``DIVIDER`` is a printed rule, not text. ``HEADNOTE_BRACKET`` and
#: ``STATE_ABBREVIATION`` are single glyphs blackletter keeps
#: untightened on purpose. ``PAGE_HEADER`` sits in the head band, where
#: one cell holds the whole running head, so a fit there would measure
#: the box against itself. ``margin`` is a strip of the page edge and
#: ``manual`` is a box a curator drew.
TEXT_RECT_TYPES = frozenset({"headnote", "EDITORIAL"})

#: Slack left around the cells, in PDF points. blackletter's own
#: padding in ``_tighten_to_text`` and ``_text_x_bounds``.
PAD_PT = 2.0

#: The narrowest a fit may leave a box, as a fraction of its width. A
#: page whose cells missed most of the text would otherwise leave that
#: text in the deliverable, which is the one direction that costs more
#: than a wide box. A refused fit is counted and logged.
MIN_KEEP_RATIO = 0.40

#: Points per inch of a PDF page. With :data:`dots_mocr.DPI` it turns a
#: cell's render pixels into the page width in points, which is what
#: :func:`fit_rows` normalizes a stored row with. Both renders come
#: from the same page at the same resolution, so the answer is exact to
#: one pixel (0.36 pt) and :data:`PAD_PT` covers that.
POINTS_PER_INCH = 72.0


@dataclass(frozen=True)
class PageCells:
    """The cell boxes of one page, with the render they were measured in.

    :param width: The render width in pixels (``origin_width``).
    :param height: The render height in pixels (``origin_height``).
    :param boxes: The cell boxes, ``(x0, y0, x1, y1)`` in those pixels.
    """

    width: float
    height: float
    boxes: tuple[tuple[float, float, float, float], ...]


def page_cells(document: dict | None) -> dict[int, PageCells]:
    """Read a glued OCR document into the cell boxes of each page.

    The text and the markdown are dropped: a volume document carries
    both for 1300 pages, and the fit reads neither, so only the parse
    is expensive and not what is held.

    A page with no usable cell gives no entry, and a caller leaves such
    a page alone. That covers a failed page, a filtered page (#242)
    and the new page of an insert, which no reader answered.

    :param document: The document ``dots_mocr.glue_run`` or
        ``apply._glue_ocr`` wrote, or None.
    :returns: ``{page_index: PageCells}``.
    :rtype: dict[int, PageCells]
    """
    if not isinstance(document, dict):
        return {}
    pages: dict[int, PageCells] = {}
    for page in document.get("pages") or []:
        if not isinstance(page, dict):
            continue
        index = page.get("page_index")
        width = page.get("origin_width")
        height = page.get("origin_height")
        if not isinstance(index, int) or isinstance(index, bool):
            continue
        if not _positive(width) or not _positive(height):
            continue
        boxes = []
        for cell in page.get("cells") or []:
            box = _cell_box(cell)
            if box is not None:
                boxes.append(box)
        if boxes:
            pages[index] = PageCells(
                float(width or 0), float(height or 0), tuple(boxes)
            )
    return pages


def load_cells(scan, run) -> dict[int, PageCells]:
    """Read the cells of the space ``run`` names, and never raise.

    **The one rule for which OCR document the fit reads**, the twin of
    ``services.geometry_pdf_path``. With a standing apply run it is the
    run's glued OCR volume (``ApplyRun.ocr_key``), whose pages are the
    corrected volume's; without one it is the volume's own glued
    document (``dots_mocr.glued_volume_key``), whose pages are the
    original's. ``views_process.ocr_text_url`` chooses between the two
    the same way (#262), and both key their pages by ``page_index``.

    A read that fails costs nothing but the fit: the boxes stay as
    blackletter measured them, which is correct and only wide. So the
    fault is logged and an empty map comes back.

    :param scan: The scan.
    :param run: The standing apply run, or None for the original's
        space.
    :returns: ``{page_index: PageCells}``, empty when nothing was read.
    :rtype: dict[int, PageCells]
    """
    from scanning import apply, s3_sync

    try:
        if run is not None:
            if not run.ocr_key:
                return {}
            document = apply.load_ocr_document(scan, run)
        else:
            key = dots_mocr.glued_volume_key(scan)
            if not key:
                return {}
            document = s3_sync.download_json_object(key)
    except Exception:
        logger.exception(
            "scan %s: the OCR volume did not load; the text redaction "
            "boxes keep the width blackletter measured",
            scan.pk,
        )
        return {}
    return page_cells(document)


class Fit(NamedTuple):
    """What :func:`fit_span` answered about one box.

    :param span: The new horizontal limits, or None when the box does
        not move.
    :param reason: Empty when the box moves, ``"unreached"`` when no
        cell of the page covers it, ``"refused"`` when a guard held the
        answer back.
    """

    span: tuple[float, float] | None
    reason: str


def fit_span(
    box: tuple[float, float, float, float],
    cells: PageCells,
    pad: float,
) -> Fit:
    """Return the horizontal limits the cells under ``box`` give it.

    **The one rule.** Every coordinate is a fraction of the page: the
    caller divides its own box by its own page size, and the cells are
    divided by the render they were measured in, so the two spaces meet
    without either knowing the other. The answer comes back in the same
    fractions.

    A cell counts when it overlaps ``box`` vertically. Without that a
    box over the top of a column would take its width from a cell far
    below it. The union of those cells, padded, bounds the box on each
    side, and the box never grows: a fit can only stop painting over
    something the model never read as text.

    :param box: ``(x0, y0, x1, y1)``, each a fraction of the page.
    :param cells: The page's cells, in their own render pixels.
    :param pad: The slack to leave, as a fraction of the page width.
    :returns: The answer, with the reason when the box does not move.
    :rtype: Fit
    """
    x0, y0, x1, y1 = box
    width = x1 - x0
    if width <= 0:
        return Fit(None, "refused")
    low, high = None, None
    for cx0, cy0, cx1, cy1 in cells.boxes:
        if cy1 / cells.height <= y0 or cy0 / cells.height >= y1:
            continue
        left, right = cx0 / cells.width, cx1 / cells.width
        if right <= x0 or left >= x1:
            # A cell of the facing column. The box is measured against
            # what it covers, never against what stands beside it.
            continue
        low = left if low is None else min(low, left)
        high = right if high is None else max(high, right)
    if low is None or high is None:
        return Fit(None, "unreached")
    new_x0 = max(x0, low - pad)
    new_x1 = min(x1, high + pad)
    if new_x0 >= new_x1 or new_x1 - new_x0 < width * MIN_KEEP_RATIO:
        return Fit(None, "refused")
    return Fit((new_x0, new_x1), "")


@dataclass
class FitCounts:
    """What one pass over a set of boxes did.

    :param read: The text boxes the pass looked at.
    :param fitted: The boxes it made narrower.
    :param unreached: The boxes no cell of their page reached.
    :param refused: The boxes a guard held back.
    :param removed: The width each fit took off, in points.
    """

    read: int = 0
    fitted: int = 0
    unreached: int = 0
    refused: int = 0
    removed: list[float] = field(default_factory=list)

    def count(self, reason: str) -> None:
        """Count one box the fit did not move.

        :param reason: The ``Fit.reason``.
        :return: None.
        """
        if reason == "refused":
            self.refused += 1
        else:
            self.unreached += 1

    def add(self, other: FitCounts) -> None:
        """Fold another pass's counts into this one.

        :param other: The counts to add.
        :return: None.
        """
        self.read += other.read
        self.fitted += other.fitted
        self.unreached += other.unreached
        self.refused += other.refused
        self.removed.extend(other.removed)


def fit_rects(rects: list[dict], cells: dict[int, PageCells], pages) -> int:
    """Fit the text rects of a compute, in place.

    The compute's caller. ``rects`` is what
    ``services._measure_redaction_rects`` returned: one entry per page,
    the boxes in the pixels of the detection render, which
    ``redactions.write_computed`` converts to points afterwards. The
    two renders are of the same source page at the same resolution but
    their pixel counts need not agree, so each box is normalized by its
    own page's ``img_width``/``img_height``.

    :param rects: blackletter's rects, mutated in place.
    :param cells: The cells of each page, :func:`page_cells`.
    :param pages: The blackletter ``Page`` objects, for the render size.
    :returns: How many boxes were made narrower.
    :rtype: int
    """
    if not cells:
        return 0
    sizes = {
        page.index: (
            float(page.img_width),
            float(page.img_height),
            float(page.pdf_width),
        )
        for page in pages
    }
    counts = FitCounts()
    for entry in rects:
        index = entry.get("page_index")
        if not isinstance(index, int):
            continue
        page_cell = cells.get(index)
        size = sizes.get(index)
        if page_cell is None or size is None:
            continue
        width, height, points = size
        if width <= 0 or height <= 0 or points <= 0:
            continue
        pad = PAD_PT / points
        for rect in entry.get("rects") or []:
            if rect.get("type") not in TEXT_RECT_TYPES:
                continue
            counts.read += 1
            box = (
                rect["x0"] / width,
                rect["y0"] / height,
                rect["x1"] / width,
                rect["y1"] / height,
            )
            span, reason = fit_span(box, page_cell, pad)
            if span is None:
                counts.count(reason)
                continue
            new_x0, new_x1 = span[0] * width, span[1] * width
            if new_x0 - rect["x0"] < 1 and rect["x1"] - new_x1 < 1:
                continue
            counts.removed.append(
                ((new_x0 - rect["x0"]) + (rect["x1"] - new_x1))
                * points
                / width
            )
            rect["x0"], rect["x1"] = round(new_x0, 1), round(new_x1, 1)
            counts.fitted += 1
    _log(counts, "the compute")
    return counts.fitted


def fit_rows(scan, cells: dict[int, PageCells]) -> FitCounts:
    """Fit the standing computed text rows of a scan, in place.

    The backfill's caller (``refit_text_redactions``). The rows are in
    PDF points, and the page width in points is the cell render's own
    width at :data:`dots_mocr.DPI`, so the command needs no PDF and no
    render.

    **A row a standing dismiss points at is left alone.** A dismiss
    names its target by a copy of the box and ``redactions.resolve``
    lands it by an IoU of at least ``detections.IOU_THRESHOLD``, so a
    box that loses half its width would drop that decision. A dismissed
    box is not painted either, so a narrower one is worth nothing.

    :param scan: The scan.
    :param cells: The cells of each page, :func:`page_cells`.
    :returns: What the pass did.
    :rtype: FitCounts
    """
    from scanning.models import Redaction

    counts = FitCounts()
    if not cells:
        return counts
    rows = list(
        Redaction.objects.computed().filter(
            scan=scan,
            decision__isnull=True,
            rect_type__in=TEXT_RECT_TYPES,
            page_index__in=cells.keys(),
        )
    )
    changed = []
    for row in rows:
        page_cell = cells[row.page_index]
        if row.bbox is None:
            continue
        counts.read += 1
        width = page_cell.width * POINTS_PER_INCH / dots_mocr.DPI
        height = page_cell.height * POINTS_PER_INCH / dots_mocr.DPI
        box = (
            row.x0 / width,
            row.y0 / height,
            row.x1 / width,
            row.y1 / height,
        )
        span, reason = fit_span(box, page_cell, PAD_PT / width)
        if span is None:
            counts.count(reason)
            continue
        new_x0, new_x1 = span[0] * width, span[1] * width
        if new_x0 - row.x0 < 0.1 and row.x1 - new_x1 < 0.1:
            continue
        counts.removed.append((new_x0 - row.x0) + (row.x1 - new_x1))
        row.x0, row.x1 = round(new_x0, 1), round(new_x1, 1)
        changed.append(row)
        counts.fitted += 1
    if changed:
        Redaction.objects.bulk_update(changed, ["x0", "x1"], batch_size=500)
    _log(counts, f"scan {scan.pk}")
    return counts


def _cell_box(cell) -> tuple[float, float, float, float] | None:
    """Return one cell's box, when it has a usable one.

    :param cell: One ``cells[]`` entry of a page.
    :returns: ``(x0, y0, x1, y1)``, or None.
    :rtype: tuple | None
    """
    if not isinstance(cell, dict):
        return None
    bbox = cell.get("bbox")
    if not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
        return None
    if not all(_number(value) for value in bbox):
        return None
    x0, y0, x1, y1 = (float(value) for value in bbox)
    if x1 <= x0 or y1 <= y0:
        return None
    return x0, y0, x1, y1


def _number(value) -> bool:
    """Return whether ``value`` is a real number.

    :param value: The value.
    :returns: Whether it is an int or a float, and not a bool.
    :rtype: bool
    """
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _positive(value) -> bool:
    """Return whether ``value`` is a number above zero.

    :param value: The value.
    :returns: Whether it can divide a coordinate.
    :rtype: bool
    """
    return _number(value) and value > 0


def _log(counts: FitCounts, who: str) -> None:
    """Log one pass, when it looked at anything.

    :param counts: The pass's counts.
    :param who: What to name in the line.
    :return: None.
    """
    if not counts.read:
        return
    logger.info(
        "text fit (%s): %d text box(es) read, %d fitted, %d reached by no "
        "cell, %d refused by a guard",
        who,
        counts.read,
        counts.fitted,
        counts.unreached,
        counts.refused,
    )
