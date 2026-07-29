"""Margin rect computation that does not require a PDF text layer.

blackletter's ``compute_margin_rects`` measures each page's content box
with ``page.get_text("blocks")``. That worked while the pipeline embedded
a Tesseract text layer before computing rects; it no longer does (the
ocrmypdf pass was dropped from ``run_full_pipeline``), and on a text-less
``bitonal.pdf`` the text-block bounds come back empty for every page,
which silently disables margin cleanup.

This module keeps the same output shape and the same rect geometry as
blackletter's version, but sources the content box from whichever signal
the page actually has:

1. the text layer, when the page has one (scans processed before the
   change, or any PDF with real text) so their margins do not move;
2. otherwise the rendered ink, i.e. the tightest box containing the dark
   pixels that make up the printed text.

The ink measurement lives in :mod:`scanning.ink`, which also has to
ignore scanner artifacts along the page edges (the very thing margin
cleanup exists to remove); see ``ink.content_box`` for how, and for the
tunable thresholds. It errs toward a larger content box, i.e. narrower
margin rects and less cleanup, never toward covering printed text.

Ink alone is not enough, though: it is the union of *everything* dark, so
a single bleed-through mark in a corner drags the content box out to the
page edge and the strips on that side shrink or vanish. Detections are the
second signal, and a sturdier one because they describe content rather
than marks: ``TEXT_COLUMN`` boxes bound the printed text horizontally, and
the header row (``PAGE_HEADER`` / ``PAGE_NUMBER`` / ``STATE_ABBREVIATION``)
bounds it vertically. The two estimates are intersected, so a bound is
only tightened when both signals support it.

The strips are laid out so the header row is never at risk: full-width
strips above and below the text body, and side strips that span the body
rows only. A page number sitting outside the column band therefore
survives by construction rather than by luck.
"""

from __future__ import annotations

from pathlib import Path

import fitz

from scanning.ink import content_box

# Buffer in PDF points around the content box (72 pts = 1 inch). Matches
# blackletter.margins.DEFAULT_BUFFER.
DEFAULT_BUFFER = 5.0

# Only clean margins when the content spans at least this fraction of the
# page width. Narrow content means an image page or appendix, where the
# bounds are not a reliable margin boundary. Matches blackletter. (The ink
# path applies the same rule in ``ink.content_box``.)
MIN_TEXT_WIDTH_FRACTION = 0.40

# Detections whose bboxes bound the printed text horizontally.
COLUMN_LABELS = frozenset({"TEXT_COLUMN"})

# Detections that make up the header row, which bounds the text vertically
# and must never be covered by a side strip.
HEADER_LABELS = frozenset({"PAGE_HEADER", "PAGE_NUMBER", "STATE_ABBREVIATION"})

# A "header" detection lying entirely within this many points of the top
# edge is bleed-through from the facing page, not this page's header. Those
# are exactly what a top strip is for, so they must not define the top
# bound. Real headers on letter-size reporter pages sit around 38-55 pt.
EDGE_BLEED_PT = 20.0


def _text_bounds(
    fitz_page: fitz.Page, page_width: float
) -> tuple[float, float, float, float] | None:
    """Find the content box from the page's text layer.

    Port of ``blackletter.margins._text_bounds`` so a page that has a
    text layer keeps the bounds it had before this module existed.

    :param fitz_page: A PyMuPDF page.
    :param page_width: Page width in PDF points.
    :return: ``(left, top, right, bottom)`` in PDF points, or None when
        the page has no text or the text is too narrow to trust.
    """
    blocks = fitz_page.get_text("blocks")
    text_blocks = [b for b in blocks if b[6] == 0 and b[4].strip()]
    if not text_blocks:
        return None

    left = min(b[0] for b in text_blocks)
    top = min(b[1] for b in text_blocks)
    right = max(b[2] for b in text_blocks)
    bottom = max(b[3] for b in text_blocks)

    if (right - left) < page_width * MIN_TEXT_WIDTH_FRACTION:
        return None

    # Extend vertically to cover image blocks inside the text column
    # (e.g. a key icon at the bottom of the page).
    img_blocks = [b for b in blocks if b[6] == 1]
    for b in img_blocks:
        if b[2] > left and b[0] < right:
            top = min(top, b[1])
            bottom = max(bottom, b[3])

    return left, top, right, bottom


def _ink_bounds(
    fitz_page: fitz.Page, page_width: float
) -> tuple[float, float, float, float] | None:
    """Find the content box from the rendered page's dark pixels.

    Thin wrapper over :func:`scanning.ink.content_box`, which is shared
    with the redaction-rect geometry so both measure a page the same way.

    :param fitz_page: A PyMuPDF page.
    :param page_width: Page width in PDF points (unused; the shared
        helper reads it off the page).
    :return: ``(left, top, right, bottom)`` in PDF points, or None when
        the page has no usable ink or the ink is too narrow to trust.
    """
    del page_width
    return content_box(fitz_page)


def _detection_bounds(
    page_dets: list[dict], page_width: float, page_height: float
) -> tuple[float | None, float | None, float | None]:
    """Content bounds a page's detections support.

    :param page_dets: Detection dicts for one page (image-pixel bboxes,
        carrying ``img_width`` / ``img_height``).
    :param page_width: Page width in PDF points.
    :param page_height: Page height in PDF points.
    The horizontal band spans the text columns *and* the header row. A
    page number can sit outside the columns, and the side strips reach up
    into the header row, so leaving the header out of the band would let a
    strip cover it.

    :return: ``(band_left, band_right, header_top)`` in PDF points, each
        None when the page has no detection to derive it from.
    """
    band_left = band_right = header_top = None
    for d in page_dets:
        bbox = d.get("bbox")
        img_w = d.get("img_width") or 0
        img_h = d.get("img_height") or 0
        if not bbox or img_w <= 0 or img_h <= 0:
            continue
        sx = page_width / img_w
        sy = page_height / img_h
        label = d.get("label", "")
        if label in HEADER_LABELS and bbox[3] * sy <= EDGE_BLEED_PT:
            # Bleed-through from the facing page, not this page's header:
            # it defines no bound, and covering it is the whole point.
            continue
        if label in COLUMN_LABELS or label in HEADER_LABELS:
            left, right = bbox[0] * sx, bbox[2] * sx
            band_left = left if band_left is None else min(band_left, left)
            band_right = (
                right if band_right is None else max(band_right, right)
            )
        if label in HEADER_LABELS:
            top = bbox[1] * sy
            header_top = top if header_top is None else min(header_top, top)
    return band_left, band_right, header_top


def _tighten_bounds(
    bounds: tuple[float, float, float, float],
    page_dets: list[dict],
    page_width: float,
    page_height: float,
) -> tuple[float, float, float, float]:
    """Intersect measured content bounds with what detections support.

    Ink is the union of every mark on the page, so it only ever errs
    outward; detections describe content, so they only err inward. Taking
    the tighter of the two per side means a bound moves in only when both
    signals agree there is nothing there.

    Falls back to ``bounds`` if the result would be degenerate (a bogus
    ``TEXT_COLUMN`` box should not be able to collapse the content box).

    :param bounds: ``(left, top, right, bottom)`` from ink or text.
    :param page_dets: Detection dicts for this page.
    :param page_width: Page width in PDF points.
    :param page_height: Page height in PDF points.
    :return: The tightened ``(left, top, right, bottom)``.
    """
    left, top, right, bottom = bounds
    band_left, band_right, header_top = _detection_bounds(
        page_dets, page_width, page_height
    )
    if band_left is not None:
        left = max(left, band_left)
    if band_right is not None:
        right = min(right, band_right)
    if header_top is not None:
        top = max(top, header_top)
    if right - left < page_width * MIN_TEXT_WIDTH_FRACTION or bottom <= top:
        return bounds
    return left, top, right, bottom


def _rects_for_bounds(
    bounds: tuple[float, float, float, float],
    page_width: float,
    page_height: float,
    buffer: float,
) -> list[dict]:
    """Build the margin strips around a content box.

    Full-width strips above and below the content, then side strips that
    span only the rows between them. Keeping the side strips out of the
    header and footer rows is what lets the x-bounds be tightened to the
    text columns without ever reaching a page number in a corner; the
    corners themselves are still covered, by the full-width strips.

    :param bounds: ``(left, top, right, bottom)`` content box.
    :param page_width: Page width in PDF points.
    :param page_height: Page height in PDF points.
    :param buffer: Safety buffer in PDF points around the content.
    :return: List of ``{x0, y0, x1, y1}`` rect dicts, ordered left, right,
        top, bottom.
    """
    left, top, right, bottom = bounds
    safe_left = max(0, left - buffer)
    safe_top = max(0, top - buffer)
    safe_right = min(page_width, right + buffer)
    safe_bottom = min(page_height, bottom + buffer)

    rects: list[dict] = []
    if safe_left > 1:
        rects.append(
            {
                "x0": 0,
                "y0": round(safe_top, 1),
                "x1": round(safe_left, 1),
                "y1": round(safe_bottom, 1),
            }
        )
    if page_width - safe_right > 1:
        rects.append(
            {
                "x0": round(safe_right, 1),
                "y0": round(safe_top, 1),
                "x1": round(page_width, 1),
                "y1": round(safe_bottom, 1),
            }
        )
    if safe_top > 1:
        rects.append(
            {
                "x0": 0,
                "y0": 0,
                "x1": round(page_width, 1),
                "y1": round(safe_top, 1),
            }
        )
    if page_height - safe_bottom > 1:
        rects.append(
            {
                "x0": 0,
                "y0": round(safe_bottom, 1),
                "x1": round(page_width, 1),
                "y1": round(page_height, 1),
            }
        )
    return rects


def compute_margin_rects(
    pdf_path: str | Path,
    buffer: float = DEFAULT_BUFFER,
    detections: list[dict] | None = None,
) -> list[dict]:
    """Compute margin rects for every page of a PDF.

    Drop-in replacement for ``blackletter.margins.compute_margin_rects``
    that works with or without a text layer. Pages whose content box
    cannot be established get an empty rect list, meaning "leave this
    page alone".

    :param pdf_path: Path to the PDF to measure.
    :param buffer: Safety buffer in PDF points around the content box.
    :param detections: Optional detection dicts (as in ``detections.json``)
        used to tighten the bounds. Without them the bounds come from the
        page's marks alone, which is what happens on a page with no
        ``TEXT_COLUMN`` detection.
    :return: List of ``{"page_index", "rects", "page_width",
        "page_height"}`` dicts, one per page, coordinates in PDF points.
    """
    dets_by_page: dict[int, list[dict]] = {}
    for d in detections or []:
        page_idx = d.get("page_index")
        if page_idx is not None:
            dets_by_page.setdefault(page_idx, []).append(d)

    result: list[dict] = []
    with fitz.open(str(pdf_path)) as doc:
        for page_idx in range(len(doc)):
            page = doc[page_idx]
            pw = page.rect.width
            ph = page.rect.height
            entry = {
                "page_index": page_idx,
                "rects": [],
                # Consumers that adjust these rects need the page size and
                # cannot always infer it from the rects themselves.
                "page_width": round(pw, 1),
                "page_height": round(ph, 1),
            }
            bounds = _text_bounds(page, pw) or _ink_bounds(page, pw)
            if bounds is None:
                result.append(entry)
                continue
            bounds = _tighten_bounds(
                bounds, dets_by_page.get(page_idx, []), pw, ph
            )
            entry["rects"] = _rects_for_bounds(bounds, pw, ph, buffer)
            result.append(entry)
    return result
