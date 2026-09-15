"""The headnote brackets the reader saw and the model did not (#328).

A headnote bracket must be redacted. YOLO finds the box, and a missed
box leaves the bracket in the deliverable. ``uncovered_headnote`` asks
whether a redaction covers a box the model found; no check asked
whether a box is absent.

**dots.mocr is the second witness, and it is too noisy alone.** It
writes the bracket as text, so a bracket with no box shows up. Measured
on scan 2845 (387 So.3d, 1293 pages) the comparison gives 18
candidates, of which 5 are true: the other 13 are the star-pagination
mark, a bar with a small page number under it, which dots.mocr writes
as ``[5]``, ``[6]`` or ``[120]``.

**The bracket numbers are the third witness, and they remove the
noise.** West numbers the headnotes of an opinion from 1 upwards, and
the brackets in the body name every number, so the numbers of one
opinion have no hole. A candidate that fills a hole is a missed
bracket; a candidate whose number the opinion already names is the
star-pagination mark. Measured: 5 candidates on scan 2845, all 5 true,
and none on scan 1828, whose 61 brackets all have a box.

The rule, in :func:`missing`, and nothing else writes the finding:

    A bracket the reader saw and the model did not, whose number the
    opinion is missing, is a finding. No live ``HEADNOTE_BRACKET`` box
    covers it; at least one number it names is named by no covered
    bracket of its opinion; and its lowest number is not above that
    opinion's highest plus one.

Two halves, in two places. :func:`write_rows` runs in the compute,
which holds the OCR document, and stores one ``BracketReading`` row per
reading. :func:`missing` runs in ``findings.rebuild``, reads rows only
and no S3, and judges. So a curator who draws the missing box makes the
card go at the next rebuild, with no recompute.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterator
from dataclasses import dataclass

from django.db import transaction

from scanning import boundaries, detections
from scanning.models import (
    ApplyRun,
    BracketReading,
    CheckName,
    Detection,
    Issue,
    OpinionBoundary,
    Scan,
)

logger = logging.getLogger(__name__)

#: A headnote bracket, at the **start** of a cell. The offset is the
#: filter, not a nicety: a bracketed number inside a paragraph is the
#: star-pagination mark of a regional reporter, and matching those
#: turned 18 candidates into 93 on scan 2845. A bracket is always the
#: first characters of its cell: all 61 on scan 1828, 913 of 988
#: tokens on scan 2845.
TOKEN = re.compile(r"^\[\s*(\d{1,3})\s*(?:([,\-–—])\s*(\d{1,3})\s*)?\]")

#: The label of the boxes this module compares itself with.
BRACKET_LABEL = "HEADNOTE_BRACKET"

#: A number above this is not a headnote number. A West opinion with
#: more than 99 headnotes does not exist in the corpus, and the cap
#: keeps a printed page number out of the sequence.
MAX_NUMBER = 99

#: How far outside a cell the centre of a box may sit and still count
#: as covering it, as a fraction of the page. dots.mocr and the model
#: read two renders of one page, exact to about one pixel.
COVER_PAD = 0.005


@dataclass(frozen=True)
class Reading:
    """One bracket the OCR read, in the pixels of its own render.

    :param page_index: The 0-based page of the document read.
    :param bbox: The cell box, ``(x0, y0, x1, y1)``.
    :param width: The render width in pixels.
    :param height: The render height in pixels.
    :param numbers: The headnote numbers, expanded.
    :param raw: The token as dots.mocr wrote it.
    """

    page_index: int
    bbox: tuple[float, float, float, float]
    width: float
    height: float
    numbers: tuple[int, ...]
    raw: str


def expand(match: re.Match) -> tuple[int, ...]:
    """Return the headnote numbers one token names.

    ``[7]`` is one number, ``[1, 2]`` is two, and ``[16-19]`` is the
    four it spans: a curator reads a range as every number in it, and
    so does the sequence.

    :param match: A :data:`TOKEN` match.
    :returns: The numbers, in order; empty when they are not headnote
        numbers.
    """
    first = int(match.group(1))
    separator = match.group(2)
    second = int(match.group(3)) if match.group(3) else None
    if not 1 <= first <= MAX_NUMBER:
        return ()
    if second is None:
        return (first,)
    if not 1 <= second <= MAX_NUMBER or second < first:
        return ()
    if separator == ",":
        return (first, second)
    return tuple(range(first, second + 1))


def read_document(document: dict | None) -> dict[int, list[Reading]]:
    """Read the bracket readings of a glued OCR document, by page.

    The markdown and the cell text of a volume are large, so nothing is
    kept but the readings: a 1300-page document gives about 900 of
    them.

    :param document: The document ``text_fit.load_document`` returns,
        or None.
    :returns: ``{page_index: [Reading]}``; a page with no bracket has
        no entry.
    :rtype: dict[int, list[Reading]]
    """
    out: dict[int, list[Reading]] = {}
    for page in (document or {}).get("pages") or []:
        index = page.get("page_index")
        width = page.get("origin_width") or 0
        height = page.get("origin_height") or 0
        if index is None or not width or not height:
            continue
        for cell in page.get("cells") or []:
            match = TOKEN.match((cell.get("text") or "").lstrip())
            if match is None:
                continue
            numbers = expand(match)
            box = cell.get("bbox") or []
            if not numbers or len(box) != 4:
                continue
            out.setdefault(index, []).append(
                Reading(
                    page_index=index,
                    bbox=(
                        float(box[0]),
                        float(box[1]),
                        float(box[2]),
                        float(box[3]),
                    ),
                    width=float(width),
                    height=float(height),
                    numbers=numbers,
                    raw=match.group(0),
                )
            )
    return out


def write_rows(scan: Scan, document: dict | None, run: ApplyRun | None) -> int:
    """Delete the scan's readings of ``run`` and write them again.

    A disposable row, the rule of a model ``Detection``: the compute
    owns them, and a second compute of the same run replaces the set.
    A document that did not load leaves the rows alone, because an
    empty set would withdraw every card of a volume whose S3 read
    failed.

    :param scan: The scan.
    :param document: The glued OCR document, or None.
    :param run: The apply run whose space the document's pages are in.
    :returns: How many readings were written.
    """
    if document is None:
        logger.warning(
            "scan %s: no OCR document, so the bracket readings stay as "
            "they were",
            scan.pk,
        )
        return 0
    by_page = read_document(document)
    rows = []
    for index in sorted(by_page):
        edit_id, source_page = detections.source_for_index(scan, index, run)
        for reading in by_page[index]:
            rows.append(
                BracketReading(
                    scan=scan,
                    apply_run=run,
                    source_edit_id=edit_id,
                    source_page=source_page,
                    source_fingerprint=scan.source_fingerprint or "",
                    page_index=index,
                    x0=reading.bbox[0],
                    y0=reading.bbox[1],
                    x1=reading.bbox[2],
                    y1=reading.bbox[3],
                    img_width=int(reading.width),
                    img_height=int(reading.height),
                    numbers=list(reading.numbers),
                    raw=reading.raw[:32],
                )
            )
    with transaction.atomic():
        BracketReading.objects.filter(scan=scan, apply_run=run).delete()
        BracketReading.objects.bulk_create(rows)
    logger.info(
        "scan %s: %d bracket reading(s) written for run %s",
        scan.pk,
        len(rows),
        run.pk if run else None,
    )
    return len(rows)


def _covers(row: Detection, reading: BracketReading) -> bool:
    """Return whether a box sits in a reading's cell.

    Both boxes are pixels of a 200 dpi render of one page, but of two
    renders, so each is normalized to the fraction of its own render it
    covers. The test is the box's centre, the test
    ``findings._uncovered_headnote_findings`` uses: a cell is a
    paragraph, so a centre inside it is the whole answer.
    """
    if not row.img_width or not row.img_height:
        return False
    if not reading.img_width or not reading.img_height:
        return False
    cx = (row.x0 + row.x1) / 2 / row.img_width
    cy = (row.y0 + row.y1) / 2 / row.img_height
    x0 = reading.x0 / reading.img_width - COVER_PAD
    x1 = reading.x1 / reading.img_width + COVER_PAD
    y0 = reading.y0 / reading.img_height - COVER_PAD
    y1 = reading.y1 / reading.img_height + COVER_PAD
    return x0 <= cx <= x1 and y0 <= cy <= y1


def opinion_of(
    reading: BracketReading,
    ordered: list[tuple[tuple, OpinionBoundary]],
    columns: dict[int, float],
) -> OpinionBoundary | None:
    """Return the standing opinion that holds a reading.

    The last opinion that starts at or before the reading, in the
    reading order ``boundaries.reading_key`` defines, and only when the
    reading is not past that opinion's end page. The order, not the
    page span alone, is what settles a page two opinions share: one
    ends high in the left column and the next starts in the right one.

    :param reading: The reading.
    :param ordered: ``[(key, boundary)]``, sorted, from
        :func:`_ordered_opinions`.
    :param columns: ``boundaries.column_boundaries`` for the pages.
    :returns: The boundary, or None when no opinion holds the reading.
    """
    x, y = boundaries.to_points(
        reading.x0, reading.y0, reading.img_width, reading.img_height
    )
    key = boundaries.position_key(reading.page_index, x, y, columns)
    found = None
    for start, row in ordered:
        if start > key:
            break
        found = row
    if found is None or reading.page_index > found.end_page_index:
        return None
    return found


def _ordered_opinions(
    scan: Scan, rows: list[OpinionBoundary], pages: set[int]
) -> tuple[list[tuple[tuple, OpinionBoundary]], dict[int, float]]:
    """Return the standing opinions with their sort keys, and the columns.

    One query for the column boxes of the pages that matter: the
    opinion starts, plus the pages that hold a reading.
    """
    starts = {row.start_page_index for row in rows}
    columns = boundaries.column_boundaries(scan, starts | pages)
    ordered = sorted(
        (
            (boundaries.reading_key(row, columns), row)
            for row in rows
            if not row.is_dismissed
        ),
        key=lambda pair: pair[0],
    )
    return ordered, columns


def missing(
    scan: Scan, rows: list[OpinionBoundary], run: ApplyRun | None
) -> Iterator[dict]:
    """Yield one finding per bracket the model missed.

    Rows only: the readings of the measured run, the live
    ``HEADNOTE_BRACKET`` rows, and the standing boundaries. No S3 read
    and no render, so ``findings.rebuild`` may call it in a request.

    :param scan: The scan.
    :param rows: ``boundaries.standing(scan)``.
    :param run: The apply run the rows are measured against, or None.
    :yields: The finding dicts ``findings.rebuild`` writes.
    """
    readings = list(
        BracketReading.objects.filter(scan=scan, apply_run=run).order_by(
            "page_index", "y0", "x0"
        )
    )
    if not readings or not rows:
        return
    boxes: dict[int, list[Detection]] = {}
    for row in Detection.objects.live().filter(scan=scan, label=BRACKET_LABEL):
        boxes.setdefault(row.page_index, []).append(row)

    pages = {reading.page_index for reading in readings}
    ordered, columns = _ordered_opinions(scan, rows, pages)

    # Two passes over the readings. The first says which are covered,
    # and their numbers are the sequence of each opinion; the second
    # judges the uncovered ones against it. One pass cannot do it: a
    # candidate is judged against brackets that come after it.
    named: dict[int, set[int]] = {}
    uncovered: list[tuple[BracketReading, OpinionBoundary]] = []
    for reading in readings:
        opinion = opinion_of(reading, ordered, columns)
        if opinion is None:
            continue
        if any(
            _covers(row, reading) for row in boxes.get(reading.page_index, [])
        ):
            named.setdefault(opinion.pk, set()).update(reading.numbers)
        else:
            uncovered.append((reading, opinion))

    for reading, opinion in uncovered:
        covered = named.get(opinion.pk, set())
        # An opinion whose brackets the model found none of says
        # nothing, and the reading is written as a finding: the model
        # missing every bracket of an opinion is the fault this check
        # exists for. Measured over scans 1828 and 2845, no opinion is
        # in that state, so the branch costs nothing either way.
        holes = sorted(set(reading.numbers) - covered)
        if not holes:
            # The opinion already names every number this token names,
            # so the token is the star-pagination mark.
            continue
        if covered and min(reading.numbers) > max(covered) + 1:
            # Above the sequence: a page number, not a headnote.
            continue
        yield _finding(reading, holes)


def _finding(reading: BracketReading, holes: list[int]) -> dict:
    """Build the finding dict of one missed bracket.

    The metadata is what ``findings.address_of`` keys a dismissal by,
    and what ``_getDetectionData`` in ``viewer_sidebar.js`` reads off a
    detection card.
    """
    numbers = ", ".join(str(n) for n in holes)
    plural = "s" if len(holes) > 1 else ""
    return {
        "check_name": CheckName.MISSING_HEADNOTE_BRACKET,
        "target": Issue.Target.DETECTION,
        "page_number": reading.page_index + 1,
        "message": (
            f"The reader found headnote bracket{plural} {numbers} "
            f"here ({reading.raw}), and the model did not."
        ),
        "metadata": {
            "page_index": reading.page_index,
            "label": BRACKET_LABEL,
            "bbox": reading.bbox,
            "img_width": reading.img_width,
            "img_height": reading.img_height,
            "source_edit": reading.source_edit_id,
            "source_page": reading.source_page,
            "raw": reading.raw,
            "numbers": holes,
        },
    }
