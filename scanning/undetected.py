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
``opinion_ocr.undetected``. A page header, a page footer and a picture
cell are no text to keep; a cell with no word of three letters is a
stray mark the glue drops and nobody misses. The missed cells of a
page are clustered into blocks (:func:`clusters`), one card each,
because a dropped footnote is three cells and one answer.

A footnote cell makes no card (the curator's word, 2026-10-13: a
footnote the ``FOOTNOTES`` box stopped short of is a loss worth no
card), so a block is body text. The verdict's order is kept: it reads
the redactions before the frame, so a unit a visible redaction covers
is excluded as a redaction and never dropped as undetected, and a
block a curator blacked out by hand makes no card either
(``redactions.visible_by_page``, the glue's own list).

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
import re
from collections.abc import Iterator

from blackletter.models import Label
from django.db import transaction

from scanning import boundaries, detections, opinion_ocr, redactions
from scanning.models import (
    ApplyRun,
    CheckName,
    Detection,
    Issue,
    Scan,
    TextReading,
)

logger = logging.getLogger(__name__)

#: The dots.mocr categories whose cells make no card: the running head
#: and foot, which the text never holds; a picture, which has no text;
#: and a footnote, a loss the curator accepts.
SKIP_KINDS = frozenset({"Picture", "Page-header", "Page-footer", "Footnote"})

#: A cell with no run of three letters or digits is a stray mark.
WORD = re.compile(r"[A-Za-z0-9]{3,}")

#: How much of a cell's text a reading keeps, for the card.
MAX_TEXT_CHARS = 48

#: Two missed cells closer than this, in the pixels of the reader's
#: render (three quarters of an inch at 200 dpi), and side by side,
#: are one block.
CLUSTER_GAP_PX = 150.0

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


def is_text(cell: dict) -> bool:
    """Return whether a cell is text the opinion would keep."""
    if cell.get("kind") in SKIP_KINDS:
        return False
    return bool(WORD.search(cell.get("text") or ""))


def _inside(box: Box, rect: dict) -> bool:
    """Return whether ``box`` lies inside one frame rect: covered whole."""
    return (
        rect["x0"] <= box[0]
        and rect["y0"] <= box[1]
        and rect["x1"] >= box[2]
        and rect["y1"] >= box[3]
    )


def missed_cells(
    reading: TextReading,
    frame: list[dict],
    size: tuple[float, float],
    rects: list[dict] = (),
) -> list[dict]:
    """Return the text cells of a page the frame does not cover.

    :param reading: The page's reading.
    :param frame: ``opinion_ocr.page_frame`` of its live detections.
    :param size: The page size in points, the space the frame is in,
        so a reading rendered at another size than the detections
        lands on the same page.
    :param rects: The page's visible redactions, in points
        (``redactions.visible_by_page``): the verdict's first test.
    :returns: The cells, as the reading holds them.
    :rtype: list[dict]
    """
    missed = []
    for cell in reading.cells or []:
        box = _box(cell.get("box"))
        if box is None or not is_text(cell):
            continue
        x0, y0 = boundaries.to_points(
            box[0], box[1], reading.img_width, reading.img_height, *size
        )
        x1, y1 = boundaries.to_points(
            box[2], box[3], reading.img_width, reading.img_height, *size
        )
        box_pt = [x0, y0, x1, y1]
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
    its top and whose columns it shares (an overlap in x), so the two
    columns of a page make two blocks and a one-column page makes one.

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
            blocks.append({"box": list(box), "cells": [cell]})
    return blocks


def uncovered(scan: Scan, run: ApplyRun | None) -> Iterator[dict]:
    """Yield one finding per block of text no detection covers.

    Rows only: the readings of the measured run and the live detections
    of their pages. No S3 read and no render, so ``findings.rebuild``
    may call it in a request. The measure is the glue's
    (``opinion_ocr.page_frame``, ``opinion_ocr.undetected``), so a
    card here is a drop there.

    :param scan: The scan.
    :param run: The apply run the rows are measured against, or None.
    :yields: The finding dicts ``findings.rebuild`` writes.
    """
    readings = list(
        TextReading.objects.filter(scan=scan, apply_run=run).order_by(
            "page_index"
        )
    )
    if not readings:
        return
    # The run's space alone, every live row of it whatever its render
    # size, the glue's own selection (``opinion_ocr.load_inputs``): a
    # human row ``relocate_rows`` could not place keeps its old index
    # and its old run, and is a stale card, never cover on the page
    # its old index names.
    by_page: dict[int, list[Detection]] = {}
    for row in Detection.objects.live().filter(scan=scan, apply_run=run):
        by_page.setdefault(row.page_index, []).append(row)
    rects_by_page = {
        entry["page_index"]: entry["rects"]
        for entry in redactions.visible_by_page(scan)
    }
    for reading in readings:
        rows = by_page.get(reading.page_index)
        if not rows or not reading.img_width or not reading.img_height:
            continue
        # The page's render, off the first row that has one, as the
        # glue's ``inputs.renders`` reads it.
        sized = next((r for r in rows if r.img_width and r.img_height), None)
        if sized is None:
            continue
        width, height = sized.img_width, sized.img_height
        # The page size in points, as the rows' render gives it: the
        # space ``opinion_ocr.verdict`` measures in.
        size = boundaries.to_points(width, height, width, height)
        frame = opinion_ocr.page_frame(rows, size)
        if frame is None:
            continue
        missed = missed_cells(
            reading, frame, size, rects_by_page.get(reading.page_index, [])
        )
        for block in clusters(missed):
            yield _finding(reading, block, width, height)


def _finding(
    reading: TextReading, block: dict, width: int, height: int
) -> dict:
    """Build the finding dict of one block of text in no box.

    The box is in the pixels of the detections' render, the space every
    finding's box is in; ``guess`` repeats it, so the sidebar card and
    the card review flash the block. The metadata is what
    ``findings.address_of`` keys a dismissal by.
    """
    cells = block["cells"]
    label = Label.TEXT_COLUMN
    sx = width / reading.img_width
    sy = height / reading.img_height
    box = block["box"]
    bbox = [
        round(box[0] * sx, 1),
        round(box[1] * sy, 1),
        round(box[2] * sx, 1),
        round(box[3] * sy, 1),
    ]
    text = (cells[0].get("text") or "").strip()
    count = (
        "A block of text here is"
        if len(cells) == 1
        else f"{len(cells)} blocks of text here are"
    )
    return {
        "check_name": CheckName.UNDETECTED_TEXT,
        "target": Issue.Target.DETECTION,
        "page_number": reading.page_index + 1,
        "message": (
            f"{count} in no box (“{text}”). The opinion "
            "text leaves out a block the boxes miss. Pull the column box "
            "over it, or dismiss the card if it is not text."
        ),
        "metadata": {
            "page_index": reading.page_index,
            "label": label.name,
            "label_id": label.value,
            "bbox": bbox,
            "guess": bbox,
            "img_width": width,
            "img_height": height,
            "source_edit": reading.source_edit_id,
            "source_page": reading.source_page,
            "cells": len(cells),
        },
    }
