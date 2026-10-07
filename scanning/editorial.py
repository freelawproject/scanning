"""The editor's notes the reader saw and no redaction covers (#450).

West prints an editor's note where its own image or table replaced the
text, ``[Editor's Note: The preceding image contains the reference for
footnotes 3, 4]``. The note is the publisher's text and must be
redacted; the model labels it ``EDITORIAL``, and a missed box leaves it
in the deliverable. No check asked whether a note had a box.

**dots.mocr is the witness, and the bracket is the filter.** The reader
writes the note as text. Measured on the local volumes in review 2: 3
readings, none covered (scan 15347 page 299, scan 15343 pages 631 and
1270), and every one opens with ``[``. An opinion that quotes a
statute's "editor's note" writes no bracket, so the bracket keeps it
out.

**The other ``EDITORIAL`` text is not read here.** The model's
``EDITORIAL`` boxes also hold "See publication Words and Phrases for
other judicial constructions and definitions", but that line is printed
inside a headnote: 41 of 41 on scan 15347 are under a black box. The
volumes where it is not are the volumes whose headnotes are not
redacted at all, which ``uncovered_headnote`` already reports, so
reading it would double every one of those cards.

The rule, in :func:`uncovered`, and nothing else writes the finding:

    An editor's note the reader saw is a finding when no black
    redaction, and no ``EDITORIAL`` box a curator drew, holds the
    centre of the note.

Two halves, the shape of ``brackets``. :func:`write_rows` runs in the
compute, which holds the OCR document, and stores one
``EditorialReading`` row per note. :func:`uncovered` runs in
``findings.rebuild``, reads rows only and no S3, and judges. So a
curator who draws the ``EDITORIAL`` box makes the card go at the next
rebuild, with no recompute.

**The box is an estimate.** dots.mocr measures a paragraph, and the
note is often the first lines of one (scan 15347 page 299: 78 of the
cell's 500 characters). No engine measures a line, so the note's band
is the cell's width and its share of the cell's height in proportion to
its share of the characters (:func:`note_band`). Each line of a column
holds about as many characters as the next, so the band lands on the
note's lines to within one, and it is stable between computes, which is
what a dismissal keyed by IoU needs.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterator
from dataclasses import dataclass

from blackletter.models import Label
from django.db import transaction

from scanning import boundaries, detections
from scanning.brackets import COVER_PAD
from scanning.models import (
    ApplyRun,
    CheckName,
    Detection,
    EditorialReading,
    Issue,
    Redaction,
    Scan,
)

logger = logging.getLogger(__name__)

#: An editor's note, from its opening bracket. The apostrophe is any of
#: the three the engines write, or none, on either side of the ``s``.
NOTE = re.compile(r"\[\s*editor[’'`]?s[’'`]?\s+note\b", re.IGNORECASE)

#: How far past the opening bracket the closing one is looked for. A
#: note is one sentence; a bracket further on is another one's.
MAX_NOTE_CHARS = 400

#: The label of the boxes this module compares itself with.
EDITORIAL_LABEL = Label.EDITORIAL.name


@dataclass(frozen=True)
class Reading:
    """One editor's note the OCR read, in the pixels of its own render.

    :param page_index: The 0-based page of the document read.
    :param bbox: The note's band, ``(x0, y0, x1, y1)``.
    :param width: The render width in pixels.
    :param height: The render height in pixels.
    :param text: The note as dots.mocr wrote it.
    """

    page_index: int
    bbox: tuple[float, float, float, float]
    width: float
    height: float
    text: str


def find_notes(text: str) -> list[tuple[int, int]]:
    """Return where the editor's notes of a cell's text are.

    :param text: One cell's text.
    :returns: ``[(start, end)]``, each from the opening bracket to the
        closing one included, or to :data:`MAX_NOTE_CHARS` past the
        opening one (the end of the text at most) when no closing
        bracket follows; empty when the text holds no note.
    :rtype: list[tuple[int, int]]
    """
    spans = []
    position = 0
    while (match := NOTE.search(text, position)) is not None:
        start = match.start()
        limit = min(len(text), start + MAX_NOTE_CHARS)
        close = text.find("]", match.end(), limit)
        end = close + 1 if close >= 0 else limit
        spans.append((start, end))
        position = end
    return spans


def note_band(
    bbox: tuple[float, float, float, float], start: int, end: int, length: int
) -> tuple[float, float, float, float]:
    """Return the part of a cell that a span of its text fills.

    The cell's width, and its height cut in the proportion of the
    characters before, inside and after the span.

    :param bbox: The cell box.
    :param start: The span's first character.
    :param end: The span's end.
    :param length: The length of the cell's text.
    :returns: The band, ``(x0, y0, x1, y1)``.
    """
    x0, y0, x1, y1 = bbox
    height = y1 - y0
    return (
        x0,
        y0 + height * start / length,
        x1,
        y0 + height * end / length,
    )


def read_document(document: dict | None) -> dict[int, list[Reading]]:
    """Read the editor's notes of a glued OCR document, by page.

    :param document: The document ``text_fit.load_document`` returns,
        or None.
    :returns: ``{page_index: [Reading]}``; a page with no note has no
        entry.
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
            text = cell.get("text") or ""
            box = cell.get("bbox") or []
            if len(box) != 4:
                continue
            for start, end in find_notes(text):
                out.setdefault(index, []).append(
                    Reading(
                        page_index=index,
                        bbox=note_band(
                            tuple(float(v) for v in box),
                            start,
                            end,
                            len(text),
                        ),
                        width=float(width),
                        height=float(height),
                        text=text[start:end],
                    )
                )
    return out


def write_rows(scan: Scan, document: dict | None, run: ApplyRun | None) -> int:
    """Delete the scan's readings and write them again.

    The rule of ``brackets.write_rows``: a disposable row the compute
    owns, every reading of the scan replaced and not only the run's,
    and a document that did not load leaves the rows alone, because an
    empty set would withdraw every card of a volume whose S3 read
    failed.

    :param scan: The scan.
    :param document: The glued OCR document, or None.
    :param run: The apply run whose space the document's pages are in.
    :returns: How many readings were written.
    """
    if document is None:
        logger.warning(
            "scan %s: no OCR document, so the editor's note readings stay "
            "as they were",
            scan.pk,
        )
        return 0
    by_page = read_document(document)
    rows = []
    for index in sorted(by_page):
        edit_id, source_page = detections.source_for_index(scan, index, run)
        for reading in by_page[index]:
            rows.append(
                EditorialReading(
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
                    text=reading.text[:MAX_NOTE_CHARS],
                )
            )
    with transaction.atomic():
        EditorialReading.objects.filter(scan=scan).delete()
        EditorialReading.objects.bulk_create(rows)
    logger.info(
        "scan %s: %d editor's note reading(s) written for run %s",
        scan.pk,
        len(rows),
        run.pk if run else None,
    )
    return len(rows)


def _centre(reading: EditorialReading) -> tuple[float, float]:
    """Return the centre of a reading's band, as a fraction of its page."""
    return (
        (reading.x0 + reading.x1) / 2 / reading.img_width,
        (reading.y0 + reading.y1) / 2 / reading.img_height,
    )


def _in_detection(row: Detection, cx: float, cy: float) -> bool:
    """Return whether a detection box holds a point given as fractions.

    The box is grown by :data:`COVER_PAD` on each side, the slack of
    ``brackets._covers``: the band is an estimate, and its centre may
    sit a little outside a tight box over the note's lines.
    """
    if not row.img_width or not row.img_height:
        return False
    return (
        row.x0 / row.img_width - COVER_PAD
        <= cx
        <= row.x1 / row.img_width + COVER_PAD
        and row.y0 / row.img_height - COVER_PAD
        <= cy
        <= row.y1 / row.img_height + COVER_PAD
    )


def uncovered(scan: Scan, run: ApplyRun | None) -> Iterator[dict]:
    """Yield one finding per editor's note nothing redacts.

    Rows only: the readings of the measured run, the visible black
    redactions, and the hand-drawn ``EDITORIAL`` boxes. No S3 read and
    no render, so ``findings.rebuild`` may call it in a request.

    Cover is a black redaction of any type (a headnote box holds a note
    printed in a headnote, and a curator draws a black box over what
    must go), or a live ``EDITORIAL`` box a curator drew or approved
    (an approval is a hand-drawn row, #414): the next compute redacts
    it, and the curator sees the card go at once. **A model box is not
    cover.** The readings are written by the compute, after the model
    boxes have had their redactions, so a model box over a note with no
    black box is one whose redaction a curator dismissed or the gate
    refused, and the card is the only thing that says so. The test is
    the centre of the note's band, with :data:`COVER_PAD`, the test
    ``brackets._covers`` makes.

    :param scan: The scan.
    :param run: The apply run the rows are measured against, or None.
    :yields: The finding dicts ``findings.rebuild`` writes.
    """
    readings = [
        r
        for r in EditorialReading.objects.filter(scan=scan, apply_run=run)
        if r.img_width and r.img_height
    ]
    if not readings:
        return
    pages = {r.page_index for r in readings}
    blacks: dict[int, list[Redaction]] = {}
    for row in Redaction.objects.visible().filter(
        scan=scan, fill=Redaction.Fill.BLACK, page_index__in=pages
    ):
        blacks.setdefault(row.page_index, []).append(row)
    boxes: dict[int, list[Detection]] = {}
    for row in Detection.objects.live().filter(
        scan=scan,
        label=EDITORIAL_LABEL,
        page_index__in=pages,
        model_name=Detection.ModelName.MANUAL,
    ):
        boxes.setdefault(row.page_index, []).append(row)

    for reading in readings:
        cx, cy = _centre(reading)
        if any(
            _in_detection(row, cx, cy)
            for row in boxes.get(reading.page_index, [])
        ):
            continue
        px, py = boundaries.to_points(
            (reading.x0 + reading.x1) / 2,
            (reading.y0 + reading.y1) / 2,
            reading.img_width,
            reading.img_height,
        )
        pad_x, pad_y = boundaries.to_points(
            COVER_PAD * reading.img_width,
            COVER_PAD * reading.img_height,
            reading.img_width,
            reading.img_height,
        )
        if any(
            row.x0 - pad_x <= px <= row.x1 + pad_x
            and row.y0 - pad_y <= py <= row.y1 + pad_y
            for row in blacks.get(reading.page_index, [])
        ):
            continue
        yield _finding(reading)


def _finding(reading: EditorialReading) -> dict:
    """Build the finding dict of one uncovered note.

    The metadata is what ``findings.address_of`` keys a dismissal by,
    and what ``_getDetectionData`` in ``viewer_sidebar.js`` reads off
    the card to highlight the band. It names no detection, so the card
    offers no detection button.
    """
    return {
        "check_name": CheckName.UNCOVERED_EDITORS_NOTE,
        "target": Issue.Target.REDACTION,
        "page_number": reading.page_index + 1,
        "message": (
            f"The reader found an editor's note here ({reading.text}), "
            "and no redaction covers it. Draw an EDITORIAL box over it."
        ),
        "metadata": {
            "page_index": reading.page_index,
            "label": EDITORIAL_LABEL,
            "label_id": Label.EDITORIAL.value,
            "bbox": reading.bbox,
            "img_width": reading.img_width,
            "img_height": reading.img_height,
            "source_edit": reading.source_edit_id,
            "source_page": reading.source_page,
        },
    }
