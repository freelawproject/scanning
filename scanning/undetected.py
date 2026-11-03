"""The text the reader saw on each page, for the card of a block no box covers.

The opinion text drops a unit the detections of its page cover by less
than a quarter (``opinion_ocr.UNDETECTED``): the model boxed the page,
so a paragraph it left outside every box is read as the ghost of the
leaf behind, and the drop is the ``UNDETECTED_TEXT`` error card of
review 3, raised after the opinions are created, whose fix is a box
drawn in review 2 and a re-glue. On fifteen volumes the model left 113
such blocks outside every box: a footnote under a ``FOOTNOTES`` box
that stopped a line short, the last paragraph of a column, a figure's
caption, an "APPENDIX" heading. This module raises the card in review
2, where the box is drawn, so the drop never happens.

**One ``TextReading`` row per page**, written by the compute from the
glued OCR document the compute reads anyway (``write_rows``): the
cells as dots.mocr measured them, each with its box in the pixels of
that render, its kind and the first characters of its text. A
disposable row, the rule of ``BracketReading`` (#328): every compute
replaces the scan's set, a curator never writes one, and
``stamp_bracket_readings`` writes the set of a volume computed before
this rule. One row per page and not one per cell, because a volume
holds twenty thousand cells and the findings read every one of them at
every rebuild.

**The finding reads rows alone** (:func:`uncovered`), the rule of
``findings.rebuild``: the readings of the measured run and the live
detections of their pages. The measure is the glue's own, so the card
and the drop agree: the frame of a page is ``opinion_ocr.page_frame``
(every live detection, and no frame on a page with no ``TEXT_COLUMN``
box, which the glue does not judge either), and the test is
``opinion_ocr.undetected``. No cell is skipped for the reader's own
category (the curator's word, 2026-10-15): the model's boxes say what
a block is, so a footnote line the ``FOOTNOTES`` box stopped short of
is a card, where the reader's "footnote" or "page footer" label once
hid it. A cell with no word of three letters is a stray mark the glue
drops and nobody misses. The missed cells of a page are clustered
into blocks (:func:`clusters`), one card each, because a dropped
footnote is three cells and one answer.

The verdict's order is kept: it reads the redactions before the frame,
so a unit a visible redaction covers is excluded as a redaction and
never dropped as undetected, and a block a curator blacked out by hand
makes no card either (``redactions.visible_by_page``, the glue's own
list).

The card writes nothing of its own (the curator's word, 2026-10-14): a
block the boxes miss is nearly always the foot of a column the model
stopped short, so the answer is the page's column box pulled over it
(the card page's edit of another box, the viewer's own handles), and
the card goes at the next rebuild when the box covers the block. A
dismissal says the block is not text (the print of the leaf behind, a
stray mark). A third column box on a page is what the answer must not
add: the gutter passes read a page's column boxes as its two columns.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Collection, Iterator

from blackletter.models import Label
from django.db import transaction

from scanning import (
    boundaries,
    brackets,
    columns,
    detections,
    opinion_ocr,
    redactions,
)
from scanning.models import (
    ApplyRun,
    CheckName,
    Detection,
    Issue,
    Scan,
    TextReading,
)

logger = logging.getLogger(__name__)

#: The cells that are no part of a column box: the running head and
#: foot, and a picture. A footnote is, so the column the + adds on a
#: page with no column box covers it.
_NOT_COLUMN_KINDS = frozenset({"Picture", "Page-header", "Page-footer"})

#: How much of a cell's text a reading keeps, for the card.
MAX_TEXT_CHARS = 48

#: Two missed cells closer than this, in the pixels of the reader's
#: render (three quarters of an inch at 200 dpi), and side by side,
#: are one block.
CLUSTER_GAP_PX = 150.0

#: A cell wider than this share of the reader's page may be a line the
#: reader merged across the gutter, when the volume's columns say so
#: (``_by_column``): judged half by half, so each column asks for its
#: own column box. Body text only: a cell the page's footnote box would
#: grow over (``neighbour_box``) stays whole, because a page's
#: footnotes run across its columns and the grown box must too.
MERGED_CELL_RATIO = 0.5

Box = list[float]


def _box(value) -> Box | None:
    if not isinstance(value, list | tuple) or len(value) != 4:
        return None
    try:
        x0, y0, x1, y1 = (float(v) for v in value)
    except (TypeError, ValueError):
        return None
    if x1 <= x0 or y1 <= y0:
        return None
    return [x0, y0, x1, y1]


def _positive(value) -> bool:
    return isinstance(value, int | float) and value > 0


def read_document(document: dict | None) -> dict[int, tuple[int, int, list]]:
    """Read a glued OCR document into the text cells of each page.

    :param document: The document ``text_fit.load_document`` returns,
        or None.
    :returns: ``{page_index: (width, height, cells)}``, the render size
        and one ``{"box", "kind", "text"}`` per cell that has a box
        and some text.
    :rtype: dict[int, tuple[int, int, list]]
    """
    if not isinstance(document, dict):
        return {}
    pages: dict[int, tuple[int, int, list]] = {}
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
        cells = []
        for cell in page.get("cells") or []:
            if not isinstance(cell, dict):
                continue
            box = _box(cell.get("bbox"))
            text = cell.get("text") or cell.get("content") or ""
            if box is None or not isinstance(text, str) or not text.strip():
                continue
            cells.append(
                {
                    "box": [round(v, 1) for v in box],
                    "kind": str(cell.get("category") or ""),
                    "text": " ".join(text.split())[:MAX_TEXT_CHARS],
                }
            )
        pages[index] = (int(width), int(height), cells)
    return pages


def write_rows(scan: Scan, document: dict | None, run: ApplyRun | None) -> int:
    """Delete the scan's text readings and write them again.

    The rule of ``brackets.write_rows``: a disposable row the compute
    owns, every reading of the scan replaced and not only the run's,
    and a document that did not load leaves the rows alone, because an
    empty set would withdraw every card of a volume whose S3 read
    failed.

    :param scan: The scan.
    :param document: The glued OCR document, or None.
    :param run: The apply run whose space the document's pages are in.
    :returns: How many pages were written.
    """
    if document is None:
        logger.warning(
            "scan %s: no OCR document, so the text readings stay as they were",
            scan.pk,
        )
        return 0
    by_page = read_document(document)
    rows = []
    for index in sorted(by_page):
        width, height, cells = by_page[index]
        edit_id, source_page = detections.source_for_index(scan, index, run)
        rows.append(
            TextReading(
                scan=scan,
                apply_run=run,
                source_edit_id=edit_id,
                source_page=source_page,
                source_fingerprint=scan.source_fingerprint or "",
                page_index=index,
                img_width=width,
                img_height=height,
                cells=cells,
            )
        )
    with transaction.atomic():
        TextReading.objects.filter(scan=scan).delete()
        TextReading.objects.bulk_create(rows, batch_size=500)
    logger.info(
        "scan %s: the text cells of %d page(s) written for run %s",
        scan.pk,
        len(rows),
        run.pk if run else None,
    )
    return len(rows)


def _inside(box: Box, rect: dict) -> bool:
    """Return whether ``box`` lies inside one frame rect: covered whole."""
    return (
        rect["x0"] <= box[0]
        and rect["y0"] <= box[1]
        and rect["x1"] >= box[2]
        and rect["y1"] >= box[3]
    )


#: How far either way the guess looks for a page with two column boxes,
#: for the width of a column and the place of the gutter.
NEIGHBOUR_PAGES = 10


def _in_a_picture(cell: Box, pictures: list[Box]) -> bool:
    """Return whether the cell's centre lies in a live image box.

    Both in the reader's render. The centre and not the whole box, so
    a cell that overhangs the picture's edge by a little is the
    picture's.
    """
    cx = (cell[0] + cell[2]) / 2
    cy = (cell[1] + cell[3]) / 2
    return any(b[0] <= cx <= b[2] and b[1] <= cy <= b[3] for b in pictures)


def _across_a_picture(block: Box, cell: Box, pictures: list[Box]) -> bool:
    """Return whether a live image box lies between the block and the cell.

    A figure across the page with text above it and below it: the text
    on the far side is the next column box, never this one. The
    picture counts when it overlaps the two sideways and its vertical
    centre falls in the gap between them, so a box that touches either
    by a few pixels still separates them.
    """
    if cell[1] >= block[3]:
        top, bottom = block[3], cell[1]
    elif cell[3] <= block[1]:
        top, bottom = cell[3], block[1]
    else:
        return False
    left = min(block[0], cell[0])
    right = max(block[2], cell[2])
    for picture in pictures:
        if picture[2] <= left or picture[0] >= right:
            continue
        if top <= (picture[1] + picture[3]) / 2 <= bottom:
            return True
    return False


def _column_geometry(
    scan: Scan, run: ApplyRun | None, page_index: int, width: float
) -> tuple[tuple[float, float], tuple[float, float]] | None:
    """Return the x-ranges of the volume's two columns, or None.

    From the nearest page within :data:`NEIGHBOUR_PAGES` whose live
    ``TEXT_COLUMN`` boxes make two columns (``columns.column_groups``,
    so a picture that cuts a column into two boxes still reads as one
    column), in the render ``width`` of the page asked about. A page
    with one column, or three, says nothing, and a volume with no such
    page nearby gives None, where the guess falls back to the cells.
    """
    rows = (
        Detection.objects.live()
        .filter(
            scan=scan,
            apply_run=run,
            label=Label.TEXT_COLUMN.name,
            page_index__range=(
                page_index - NEIGHBOUR_PAGES,
                page_index + NEIGHBOUR_PAGES,
            ),
        )
        .exclude(page_index=page_index)
    )
    by_page: dict[int, list] = {}
    for row in rows:
        if row.img_width:
            by_page.setdefault(row.page_index, []).append(row)
    for index in sorted(by_page, key=lambda i: (abs(i - page_index), i)):
        spans = [
            (r.x0 * width / r.img_width, r.x1 * width / r.img_width)
            for r in by_page[index]
        ]
        groups = columns.column_groups(spans)
        if len(groups) != 2:
            continue
        left, right = (
            (min(spans[i][0] for i in g), max(spans[i][1] for i in g))
            for g in groups
        )
        return left, right
    return None


Columns = tuple[tuple[float, float], tuple[float, float]]


def _by_column(
    cells: list[dict],
    columns_px: Columns | None,
    keep_whole: Callable[[Box], bool] | None = None,
):
    """Yield the cells, each in one of the volume's columns.

    With the columns known, every cell is tagged ``column`` (0 the
    left, 1 the right): the column that holds more of it, so a centred
    heading that straddles the gutter goes to one side, and
    :func:`clusters` joins no cells of two columns, so no block and no
    box the card offers is wider than a column. A line the reader
    merged across the gutter (a page with a figure across it, an
    addendum's heading) covers both columns by more than
    ``columns.COLUMN_SHARE`` of each and is two cells here, cut at the
    column edges and tagged ``half`` too: judged whole, with one column
    boxed the cell was half covered, over the quarter the glue keeps a
    unit at, so the other column had no box and no card asked for one.
    The glue still keeps the unit whole; the card asks for the box the
    column lacks, not for text. A cell ``keep_whole`` answers for (one
    the page's footnote box would grow over) is yielded as it is,
    untagged: a footnote line runs across the columns, and so must the
    box that grows over it.
    """
    for cell in cells:
        box = _box(cell.get("box"))
        if box is None or columns_px is None:
            yield cell
            continue
        if keep_whole is not None and keep_whole(box):
            yield cell
            continue
        held = [min(box[2], x1) - max(box[0], x0) for x0, x1 in columns_px]
        if all(
            h > (x1 - x0) * columns.COLUMN_SHARE
            for h, (x0, x1) in zip(held, columns_px, strict=True)
        ):
            (lx0, lx1), (rx0, rx1) = columns_px
            yield {
                **cell,
                "box": [box[0], box[1], min(box[2], lx1), box[3]],
                "column": 0,
                "half": "left",
            }
            yield {
                **cell,
                "box": [max(box[0], rx0), box[1], box[2], box[3]],
                "column": 1,
                "half": "right",
            }
            continue
        if held[0] == held[1]:
            centre = (box[0] + box[2]) / 2
            column = min(
                range(2),
                key=lambda i: abs(sum(columns_px[i]) / 2 - centre),
            )
        else:
            column = 0 if held[0] > held[1] else 1
        yield {**cell, "column": column}


def missed_cells(
    reading: TextReading,
    frame: list[dict],
    size: tuple[float, float],
    rects: list[dict] = (),
    columns_px: Columns | None = None,
    keep_whole: Callable[[Box], bool] | None = None,
) -> list[dict]:
    """Return the text cells of a page the frame does not cover.

    :param reading: The page's reading.
    :param frame: ``opinion_ocr.page_frame`` of its live detections.
    :param size: The page size in points, the space the frame is in,
        so a reading rendered at another size than the detections
        lands on the same page.
    :param rects: The page's visible redactions, in points
        (``redactions.visible_by_page``): the verdict's first test.
    :param columns_px: The volume's two columns in the reader's pixels,
        or None: a cell across both is judged half by half
        (:func:`_by_column`).
    :param keep_whole: Says which cell is never cut, in the reader's
        pixels: the one a footnote box would grow over.
    :returns: The cells, as the reading holds them.
    :rtype: list[dict]
    """
    missed = []
    for cell in _by_column(reading.cells or [], columns_px, keep_whole):
        # Every cell with a box is text, whatever the reader called it
        # and however short: the cover tests below are the whole rule.
        box = _box(cell.get("box"))
        if box is None:
            continue
        x0, y0 = boundaries.to_points(
            box[0], box[1], reading.img_width, reading.img_height, *size
        )
        x1, y1 = boundaries.to_points(
            box[2], box[3], reading.img_width, reading.img_height, *size
        )
        # A box the reader ran past the page is cut at the detection
        # box that holds it (``opinion_ocr.fit_overrun``), the glue's
        # own reading of it, so the card judges, and names, the box
        # the text has.
        box_pt = opinion_ocr.fit_overrun(
            [x0, y0, x1, y1], size[0], size[1], frame
        )
        if box_pt is None:
            continue
        if box_pt != [x0, y0, x1, y1]:
            sx = reading.img_width / size[0]
            sy = reading.img_height / size[1]
            cell = {
                **cell,
                "box": [
                    round(box_pt[0] * sx, 1),
                    round(box_pt[1] * sy, 1),
                    round(box_pt[2] * sx, 1),
                    round(box_pt[3] * sy, 1),
                ],
            }
        # The verdict's first test: a unit a redaction covers is
        # excluded as a redaction, never dropped as undetected.
        if (
            rects
            and opinion_ocr.covered_share(box_pt, rects)[0]
            >= opinion_ocr.EXCLUDE_SHARE
        ):
            continue
        # The common case first: a cell inside one box is covered
        # whole, and the measure need not be run on it.
        if any(_inside(box_pt, rect) for rect in frame):
            continue
        if opinion_ocr.undetected(box_pt, frame):
            missed.append(cell)
    return missed


def clusters(cells: list[dict], gap: float = CLUSTER_GAP_PX) -> list[dict]:
    """Group the missed cells of a page into blocks.

    A cell joins the first block whose bottom is within ``gap`` above
    its top, whose columns it shares (an overlap in x), and whose
    ``column`` tag is its own (:func:`_by_column`, where the volume's
    columns are known), so the two columns of a page make two blocks
    and a one-column page makes one.

    :param cells: The cells, each with a ``box``.
    :param gap: The largest vertical gap inside a block, in the cells'
        own pixels.
    :returns: ``[{"box": union, "cells": [...]}, ...]``, by position.
    :rtype: list[dict]
    """
    blocks: list[dict] = []
    for cell in sorted(cells, key=lambda c: (c["box"][1], c["box"][0])):
        box = cell["box"]
        for block in blocks:
            if block.get("column") != cell.get("column"):
                continue
            union = block["box"]
            if box[1] - union[3] > gap:
                continue
            if min(box[2], union[2]) <= max(box[0], union[0]):
                continue
            block["cells"].append(cell)
            block["box"] = [
                min(union[0], box[0]),
                min(union[1], box[1]),
                max(union[2], box[2]),
                max(union[3], box[3]),
            ]
            break
        else:
            blocks.append(
                {
                    "box": list(box),
                    "cells": [cell],
                    "column": cell.get("column"),
                }
            )
    return blocks


#: How near the page's footnote box must be to a text card's block, in
#: render pixels, for the card to grow that box over the block instead
#: of adding a second one: a footnote line the box stopped short of
#: sits right under it, or beside it. Two footnote boxes with a seam
#: between them are a partial cover in review 3 and a hairline the glue
#: has to bridge; the footnotes of a page are one block, across both
#: columns, so the growth crosses the gutter on purpose. The reach is
#: this module's own gap between the cells of one block.
GROW_GAP_PX = 150.0

Box = list[float]


def neighbour_box(block: Box, others: list[dict], label: str) -> dict | None:
    """Return the live box of ``label`` a text card should grow over
    ``block``, with the box it would become, or None.

    A box in the block's column (the two share more than half of the
    narrower one's width, so a line the reader merged across the gutter
    still finds the footnote box under its left half) that ends within
    :data:`GROW_GAP_PX` above the block or starts within it below, or
    one beside the block (sharing half the block's height) within that
    gap sideways, gutter or no gutter: the caller grows a footnote box
    with this, and a page's footnotes run across its columns. The
    nearest when several. ``grown`` is the union, the box the resize
    writes; ``box`` is the box as it stands, the Undo. The one rule,
    read by the sidebar card (``metadata["grow"]``) and the cards page
    (``card_review.cards``).

    :param block: The card's block, ``[x0, y0, x1, y1]`` in render
        pixels.
    :param others: The page's live boxes, ``{"id", "label", "box"}``
        in the same pixels.
    :param label: The label of the box the answer would add.
    :returns: ``{"id", "box", "grown"}`` or None.
    :rtype: dict | None
    """
    bx0, by0, bx1, by1 = block
    best: tuple[float, dict] | None = None
    for other in others:
        if other.get("label") != label:
            continue
        ox0, oy0, ox1, oy1 = other["box"]
        # The two share more than half of the narrower one's width:
        # ``columns.column_groups`` reads the pair from the left, so a
        # line merged across the gutter found the footnote box under
        # its left half only when the line started first.
        shared = min(bx1, ox1) - max(bx0, ox0)
        same_column = shared > columns.COLUMN_SHARE * min(bx1 - bx0, ox1 - ox0)
        vertical = max(by0 - oy1, oy0 - by1, 0.0)
        sideways = max(bx0 - ox1, ox0 - bx1, 0.0)
        beside = (
            min(by1, oy1) - max(by0, oy0) >= (by1 - by0) / 2
            and sideways <= GROW_GAP_PX
        )
        if same_column and vertical <= GROW_GAP_PX:
            distance = vertical
        elif beside:
            distance = sideways
        else:
            continue
        if best is None or distance < best[0]:
            best = (
                distance,
                {
                    "id": other["id"],
                    "box": list(other["box"]),
                    "grown": [
                        min(ox0, bx0),
                        min(oy0, by0),
                        max(ox1, bx1),
                        max(oy1, by1),
                    ],
                },
            )
    return best[1] if best else None


def uncovered(
    scan: Scan,
    run: ApplyRun | None,
    pages: Collection[int] | None = None,
) -> Iterator[dict]:
    """Yield one finding per block of text no detection covers.

    Rows only: the readings of the measured run and the live detections
    of their pages. No S3 read and no render, so ``findings.rebuild``
    may call it in a request. The measure is the glue's
    (``opinion_ocr.page_frame``, ``opinion_ocr.undetected``), so a
    card here is a drop there. A page is judged by its own rows alone,
    so a write on one page rebuilds that page (``pages``).

    :param scan: The scan.
    :param run: The apply run the rows are measured against, or None.
    :param pages: The page indexes to judge, or None for every page.
    :yields: The finding dicts ``findings.rebuild`` writes.
    """
    readings = list(_readings(scan, run, pages))
    if not readings:
        return
    # The run's space alone, every live row of it whatever its render
    # size, the glue's own selection (``opinion_ocr.load_inputs``): a
    # human row ``relocate_rows`` could not place keeps its old index
    # and its old run, and is a stale card, never cover on the page
    # its old index names.
    by_page = _live_by_page(scan, run, pages)
    rects_by_page = {
        entry["page_index"]: entry["rects"]
        for entry in redactions.visible_by_page(scan, pages)
    }
    for reading in readings:
        rows = by_page.get(reading.page_index, [])
        if not reading.img_width or not reading.img_height:
            continue
        # The page's render, off the first row that has one, as the
        # glue's ``inputs.renders`` reads it; the reading's own on a
        # page with no row at all, the one space there is.
        sized = next((r for r in rows if r.img_width and r.img_height), None)
        if sized is not None:
            width, height = sized.img_width, sized.img_height
        elif rows:
            continue
        else:
            width, height = reading.img_width, reading.img_height
        # The page size in points, as the rows' render gives it: the
        # space ``opinion_ocr.verdict`` measures in.
        size = boundaries.to_points(width, height, width, height)
        frame = opinion_ocr.page_frame(rows, size)
        # The glue's frame is None on a page with no ``TEXT_COLUMN``
        # box, and its verdict keeps every unit there, so on such a
        # page this card is no drop in review 3. The page is judged
        # against its other boxes all the same: the model took the
        # page for a picture, the curator shrank the image box off the
        # text, and the text now sits in no box and in no column, which
        # nothing else would say. The card asks for the column box the
        # page lacks, and its message says the text is kept.
        no_column = frame is None
        if no_column:
            frame = [
                {"x0": box[0], "y0": box[1], "x1": box[2], "y1": box[3]}
                for box in opinion_ocr.zones_pt(rows, size)
            ]
        # The volume's columns (``_column_geometry``), read when the page
        # has no column box (the guess is as wide as a column) or holds
        # a cell wider than half the page (a line the reader merged
        # across the gutter is judged half by half), never otherwise:
        # it is a query per page.
        sx = width / reading.img_width
        geometry = None
        if no_column or any(
            b[2] - b[0] > reading.img_width * MERGED_CELL_RATIO
            for b in (_box(c.get("box")) for c in reading.cells or [])
            if b
        ):
            geometry = _column_geometry(scan, run, reading.page_index, width)
        columns_px = (
            tuple((x0 / sx, x1 / sx) for x0, x1 in geometry)
            if geometry
            else None
        )
        # The page's live boxes in the render the card's box is in, for
        # the box the card grows (:func:`neighbour_box`): a row of
        # another render size is scaled into it.
        others = [
            {
                "id": r.pk,
                "label": r.label,
                "box": [
                    r.x0 * width / r.img_width,
                    r.y0 * height / r.img_height,
                    r.x1 * width / r.img_width,
                    r.y1 * height / r.img_height,
                ],
                # The row's own render, the space a resize of it is
                # written in (``update_detection`` keeps the row's
                # size), so the card's answer goes back through it.
                "own_box": [r.x0, r.y0, r.x1, r.y1],
                "own_scale": (r.img_width / width, r.img_height / height),
            }
            for r in rows
            if r.img_width and r.img_height
        ]
        sy = height / reading.img_height

        def footnote_candidate(box: Box) -> bool:
            # A cell the footnote answer would take: never cut at the
            # gutter, so the grown box runs across the columns as the
            # footnotes do.
            scaled = [box[0] * sx, box[1] * sy, box[2] * sx, box[3] * sy]
            return (
                neighbour_box(scaled, others, Label.FOOTNOTES.name) is not None
            )

        missed = missed_cells(
            reading,
            frame,
            size,
            rects_by_page.get(reading.page_index, []),
            columns_px=columns_px,
            keep_whole=footnote_candidate,
        )
        for block in clusters(missed):
            yield _finding(
                reading,
                block,
                width,
                height,
                no_column=no_column,
                others=others,
                geometry=geometry,
            )


def _finding(
    reading: TextReading,
    block: dict,
    width: int,
    height: int,
    no_column: bool = False,
    others: list[dict] | None = None,
    geometry: tuple[tuple[float, float], tuple[float, float]] | None = None,
) -> dict:
    """Build the finding dict of one block of text in no box.

    The box is in the pixels of the detections' render, the space every
    finding's box is in; ``guess`` repeats it, so the sidebar card and
    the card review flash the block. The metadata is what
    ``findings.address_of`` keys a dismissal by. ``no_column`` says the
    page has no column box to pull: the message asks for one to be
    drawn and says the text is kept, and the metadata carries the flag.
    """
    cells = block["cells"]
    label = Label.TEXT_COLUMN
    sx = width / reading.img_width
    sy = height / reading.img_height
    box = block["box"]

    def scaled(b):
        return [
            round(b[0] * sx, 1),
            round(b[1] * sy, 1),
            round(b[2] * sx, 1),
            round(b[3] * sy, 1),
        ]

    bbox = scaled(box)
    # A block of the halves of a line the reader merged across the
    # gutter (``_split_merged``), all of one side.
    halves = {c.get("half") for c in cells}
    half = halves.pop() if len(halves) == 1 and None not in halves else None
    # The box the + adds. On a page with a column box it is the block,
    # and the curator pulls the column over it or adds the block; a
    # half takes its column's width, the box the column lacks. On a
    # page with none, the first column box turns the glue's judgement
    # on for the whole page, and a block-sized box would leave the
    # rest of its column, the footnotes under it included, outside
    # every box. So there the guess is the column the block sits in,
    # as wide as the volume's columns are (``_column_geometry``, off
    # the nearest page with two column boxes) and as tall as the cells
    # of that column beside the block make it: every cell of the page
    # but the running head, the foot and the pictures, the way a
    # double click fills a caption's column; a volume with no page of
    # two columns nearby gets the cells' own union. The cells a live
    # image box holds stay out (an addendum page is a table in an
    # image box under a heading in no box), and the union never
    # crosses one (a figure across the page with text above it and
    # below it): a column drawn over the picture as well was resized
    # by hand on every such page.
    guess = bbox
    columns_px = (
        [(c[0] / sx, c[1] / sx) for c in geometry] if geometry else None
    )
    column = cells[0].get("column") if columns_px else None
    side = columns_px[column] if column is not None else None
    if half and side:
        guess = scaled([side[0], box[1], side[1], box[3]])
    if no_column:
        pictures = [
            [
                o["box"][0] / sx,
                o["box"][1] / sy,
                o["box"][2] / sx,
                o["box"][3] / sy,
            ]
            for o in others or []
            if o["label"] == Label.IMAGE.name
        ]
        beside = []
        for cell in reading.cells or []:
            other = _box(cell.get("box"))
            if other is None or cell.get("kind") in _NOT_COLUMN_KINDS:
                continue
            if other[2] <= box[0] or other[0] >= box[2]:
                continue
            if _in_a_picture(other, pictures):
                continue
            if _across_a_picture(box, other, pictures):
                continue
            beside.append(other)
        if side is None:
            column_box = list(box)
            for other in beside:
                column_box = [
                    min(column_box[0], other[0]),
                    min(column_box[1], other[1]),
                    max(column_box[2], other[2]),
                    max(column_box[3], other[3]),
                ]
            guess = scaled(column_box)
        else:
            x0, x1 = side
            y0, y1 = box[1], box[3]
            for other in beside:
                held = min(other[2], x1) - max(other[0], x0)
                if held <= (other[2] - other[0]) * columns.COLUMN_SHARE:
                    continue
                y0, y1 = min(y0, other[1]), max(y1, other[3])
            guess = scaled([x0, y0, x1, y1])
    text = (cells[0].get("text") or "").strip()
    if half:
        count = (
            f"The {half} half of a line the reader merged across both "
            "columns is"
        )
    elif len(cells) == 1:
        count = "A block of text here is"
    else:
        count = f"{len(cells)} blocks of text here are"
    if no_column:
        message = (
            f"{count} in no box, and the page has no column box "
            f"(“{text}”). The opinion text keeps the "
            f"{'line' if half else 'block'} but has no column to place "
            "it in. Draw the column box over the text, or dismiss the "
            "card if it is not text."
        )
    elif half:
        message = (
            f"{count} in no box (“{text}”). The opinion text keeps the "
            "line, but that column has no box. Add the column box over "
            "it or pull the page's over it, or dismiss the card if it "
            "is not text."
        )
    else:
        message = (
            f"{count} in no box (“{text}”). The opinion "
            "text leaves out a block the boxes miss. Add a column box "
            "over it or pull the page's over it, or dismiss the card if "
            "it is not text."
        )
    return {
        "check_name": CheckName.UNDETECTED_TEXT,
        "target": Issue.Target.DETECTION,
        "page_number": reading.page_index + 1,
        "message": message,
        "metadata": {
            "page_index": reading.page_index,
            "label": label.name,
            "label_id": label.value,
            "bbox": bbox,
            "guess": guess,
            "img_width": width,
            "img_height": height,
            "source_edit": reading.source_edit_id,
            "source_page": reading.source_page,
            "cells": len(cells),
            "no_column": no_column,
            # Which half of a merged line the block is, or None.
            "half": half,
            # The page's footnote box beside the block, when there is
            # one: the card's footnote answer grows it over the block
            # instead of adding a second box (``neighbour_box``).
            "grow": _grow_in_own_space(
                neighbour_box(bbox, others or [], Label.FOOTNOTES.name),
                others or [],
            ),
        },
    }


def _grow_in_own_space(grow: dict | None, others: list[dict]) -> dict | None:
    """Return a grow answer in the grown row's own render.

    :func:`neighbour_box` measures in the card's render, where every
    box of the page was scaled to compare; a resize is written in the
    row's own (``update_detection`` keeps ``img_width`` and
    ``img_height``), and a row of another render size would land off
    the block if the card's pixels were written on it.

    :param grow: What :func:`neighbour_box` answered, or None.
    :param others: The page's boxes, with ``own_box`` and ``own_scale``.
    :returns: The answer with ``box`` and ``grown`` in the row's own
        pixels, or None.
    :rtype: dict | None
    """
    if grow is None:
        return None
    other = next((o for o in others if o["id"] == grow["id"]), None)
    if other is None or "own_scale" not in other:
        return grow
    sx, sy = other["own_scale"]
    x0, y0, x1, y1 = grow["grown"]
    return {
        "id": grow["id"],
        "box": list(other["own_box"]),
        "grown": [
            round(x0 * sx, 1),
            round(y0 * sy, 1),
            round(x1 * sx, 1),
            round(y1 * sy, 1),
        ],
    }


# ---------------------------------------------------------------------------
# A redaction that covers part of a line
# ---------------------------------------------------------------------------

#: The black boxes that cover a glyph and not words: a bracket, the key
#: icon, the divider rule, and the case number West prints over each
#: caption (``CASE_SEQUENCE``, a box blackletter clamps to the digit).
#: One of them over part of a line is its job: the reader merged the
#: glyph into the line beside it, and the line stays.
GLYPH_RECT_TYPES = frozenset(
    {"HEADNOTE_BRACKET", "KEY_ICON", "DIVIDER", "CASE_SEQUENCE"}
)

#: A cell with fewer letters and digits than this is a speck the reader
#: read as a character, and no card asks about it.
MIN_CUT_LETTERS = 3


#: How a card names the box that cuts the line.
_BOX_WORDS = {
    "headnote": "A headnote box",
    "EDITORIAL": "An editor's note box",
    "manual": "A hand-drawn box",
}


def _letters(text: str) -> int:
    """Return how many letters and digits ``text`` holds."""
    return sum(1 for char in text or "" if char.isalnum())


def cut_cells(
    reading: TextReading,
    size: tuple[float, float],
    rects: list[dict],
    head_spans: list[tuple[float, float]] = (),
    frame: list[dict] | None = None,
) -> list[tuple[dict, list[float], float, dict]]:
    """Return the cells of a page a black redaction covers in part.

    The share is the glue's own (``opinion_ocr.covered_share``, the core
    of the cell under the union of the boxes) over the boxes review 3's
    verdict measures (every visible box but a bracket's), and the band
    is the one that makes review 3 drop the block as a partial cover:
    at least ``EXCLUDE_SHARE`` and below ``FULL_SHARE``. A cell whose
    box of most cover is white (a margin, the running head) or a
    glyph's (:data:`GLYPH_RECT_TYPES`) is that box's job and no card.
    A cell on the running head's line
    (``opinion_ocr.head_lines``) is no text either way: review 3 takes it
    whole, and the head's whiteout covers it on the page.

    :param reading: The page's reading.
    :param size: The page size in points.
    :param rects: The page's visible redactions, in points.
    :param head_spans: The page's running-head lines, in points.
    :param frame: ``opinion_ocr.page_frame`` of the page's detections:
        a cell that runs off the page is cut at the box that holds it
        (``opinion_ocr.fit_overrun``), the line the glue measures.
    :returns: ``(cell, box in points, share, the box that covers most)``.
    :rtype: list[tuple]
    """
    out = []
    for cell in reading.cells or []:
        box = _box(cell.get("box"))
        if box is None or _letters(cell.get("text")) < MIN_CUT_LETTERS:
            continue
        x0, y0 = boundaries.to_points(
            box[0], box[1], reading.img_width, reading.img_height, *size
        )
        x1, y1 = boundaries.to_points(
            box[2], box[3], reading.img_width, reading.img_height, *size
        )
        # The glue's own reading of a cell that runs off the page, so
        # the card measures, names and covers the line review 3 does.
        box_pt = opinion_ocr.fit_overrun(
            [x0, y0, x1, y1], size[0], size[1], frame
        )
        if box_pt is None:
            continue
        if opinion_ocr.on_head_line(box_pt, list(head_spans)):
            continue
        # The boxes review 3's verdict measures: every visible box but
        # a bracket's (the glue deletes the token instead, #373), so a
        # white or glyph box that covers the rest of the line counts,
        # and a line the glue reads covered whole makes no card.
        measured = [
            rect
            for rect in rects
            if rect.get("rect_type") != brackets.BRACKET_LABEL
            and not (
                rect.get("rect_type") in opinion_ocr.TOKEN_RECT_TYPES
                and opinion_ocr.is_bracket_box(rect, box_pt)
            )
        ]
        share, hit = opinion_ocr.covered_share(box_pt, measured)
        if hit is None:
            continue
        if not opinion_ocr.EXCLUDE_SHARE <= share < opinion_ocr.FULL_SHARE:
            continue
        # The card is about a box of words: a line a white or a glyph's
        # box covers most of is its job, a margin, the running head, a
        # key beside its caption.
        if (
            hit.get("fill") != "black"
            or hit.get("rect_type") in GLYPH_RECT_TYPES
        ):
            continue
        out.append((cell, box_pt, share, hit))
    return out


def _readings(scan: Scan, run: ApplyRun | None, pages: Collection[int] | None):
    """Return the text readings of the run, of some pages or of every page."""
    rows = TextReading.objects.filter(scan=scan, apply_run=run)
    if pages is not None:
        rows = rows.filter(page_index__in=pages)
    return rows.order_by("page_index")


def _live_by_page(
    scan: Scan, run: ApplyRun | None, pages: Collection[int] | None
) -> dict[int, list[Detection]]:
    """Return the live rows of the run by page, of some pages or of every page.

    The run's space alone, every live row of it whatever its render
    size, the glue's own selection (``opinion_ocr.load_inputs``): a
    human row ``relocate_rows`` could not place keeps its old index
    and its old run, and is a stale card, never cover on the page its
    old index names.
    """
    rows = Detection.objects.live().filter(scan=scan, apply_run=run)
    if pages is not None:
        rows = rows.filter(page_index__in=pages)
    by_page: dict[int, list[Detection]] = {}
    for row in rows:
        by_page.setdefault(row.page_index, []).append(row)
    return by_page


def cut_lines(
    scan: Scan,
    run: ApplyRun | None,
    pages: Collection[int] | None = None,
) -> Iterator[dict]:
    """Yield one finding per line of text a redaction covers in part.

    The step-2 twin of review 3's ``PARTIAL_REDACTION`` card: a cell the
    glue would measure under part of a box (the same boxes and the same
    share), read on the reader's cells
    the compute stored, so the box is fixed before the approval. Two
    causes give one shape: a box the model drew short of its paragraph
    (the end of the line prints beside it), or a box that reaches into
    text that stays. Rows alone, the rule of ``findings.rebuild``.

    :param scan: The scan.
    :param run: The apply run the rows are measured against, or None.
    :param pages: The page indexes to judge, or None for every page.
    :yields: The finding dicts ``findings.rebuild`` writes.
    """
    readings = list(_readings(scan, run, pages))
    if not readings:
        return
    rects_by_page = {
        entry["page_index"]: entry["rects"]
        for entry in redactions.visible_by_page(scan, pages)
    }
    by_page = _live_by_page(scan, run, pages)
    for reading in readings:
        rects = rects_by_page.get(reading.page_index)
        if not rects or not reading.img_width or not reading.img_height:
            continue
        rows = by_page.get(reading.page_index, [])
        sized = next((r for r in rows if r.img_width and r.img_height), None)
        width, height = (
            (sized.img_width, sized.img_height)
            if sized is not None
            else (reading.img_width, reading.img_height)
        )
        size = boundaries.to_points(width, height, width, height)
        heads = opinion_ocr.head_lines(rows, size)
        frame = opinion_ocr.page_frame(rows, size)
        for cell, box_pt, share, hit in cut_cells(
            reading, size, rects, heads, frame
        ):
            yield _cut_finding(
                reading, cell, box_pt, share, hit, width, height, size
            )


def _cut_finding(
    reading: TextReading,
    cell: dict,
    box_pt: list[float],
    share: float,
    hit: dict,
    width: int,
    height: int,
    size: tuple[float, float],
) -> dict:
    """Build the finding dict of one line a redaction covers in part.

    ``bbox`` is the cell in the pixels of the detections' render, the
    space every finding's box is in and the address a dismissal keys
    on, with the box's type as its label. ``cover`` is the redaction
    grown over the cell, in points, the box "Cover the line" moves it
    to; ``redaction_id`` names the box.
    """
    sx, sy = width / size[0], height / size[1]
    bbox = [
        round(box_pt[0] * sx, 1),
        round(box_pt[1] * sy, 1),
        round(box_pt[2] * sx, 1),
        round(box_pt[3] * sy, 1),
    ]
    cover = [
        round(min(hit["x0"], box_pt[0]), 1),
        round(min(hit["y0"], box_pt[1]), 1),
        round(max(hit["x1"], box_pt[2]), 1),
        round(max(hit["y1"], box_pt[3]), 1),
    ]
    # The message quotes no word of the line: the line is partly under
    # the box, so its words may be the redacted ones.
    kind = _BOX_WORDS.get(hit.get("rect_type") or "", "A redaction box")
    return {
        "check_name": CheckName.REDACTION_CUTS_TEXT,
        "target": Issue.Target.REDACTION,
        "page_number": reading.page_index + 1,
        "message": (
            f"{kind} covers {round(share * 100)}% of a line of text. Either "
            "the box stops short and the rest of the line prints beside it, "
            "or it reaches into text that should stay. Cover the line, pull "
            "the box's edge back, or dismiss the card."
        ),
        "metadata": {
            "page_index": reading.page_index,
            "label": hit.get("rect_type") or "",
            "bbox": bbox,
            "img_width": width,
            "img_height": height,
            "source_edit": reading.source_edit_id,
            "source_page": reading.source_page,
            "redaction_id": hit.get("id"),
            "cover": cover,
            "share": round(share, 3),
        },
    }
