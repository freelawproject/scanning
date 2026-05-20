from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import pdfplumber

if TYPE_CHECKING:
    from pdfplumber.page import Page as PdfPage

    from scanning.models import Page

DETECTION_IMG_W = 1700
DETECTION_IMG_H = 2200
_FOOTNOTE_CONF_FLOOR = 0.80


# ── geometry helpers ──────────────────────────────────────────────────


def _bbox_col(bbox: list[float]) -> str:
    """Classify a detection bbox into the left or right column.

    Splits at the horizontal midpoint of the detection canvas. The
    canvas is the YOLO image, not the PDF — detections are emitted in
    image-pixel coords (``DETECTION_IMG_W`` x ``DETECTION_IMG_H``).

    :param bbox: ``[x0, y0, x1, y1]`` in image-pixel coords.
    :return: ``"L"`` for left column, ``"R"`` for right column.
    """
    return "L" if (bbox[0] + bbox[2]) / 2 < DETECTION_IMG_W / 2 else "R"


def _bbox_zone(bbox: list[float]) -> str:
    """Classify a detection bbox into top / middle / bottom of the page.

    Three vertical thirds based on the bbox's vertical centerpoint, in
    image-pixel coords (``DETECTION_IMG_H``).

    :param bbox: ``[x0, y0, x1, y1]`` in image-pixel coords.
    :return: One of ``"top"``, ``"middle"``, ``"bottom"``.
    """
    cy = (bbox[1] + bbox[3]) / 2
    third = DETECTION_IMG_H / 3
    if cy < third:
        return "top"
    if cy < 2 * third:
        return "middle"
    return "bottom"


def _crop_text(
    pdf_page: PdfPage,
    bbox_img: list[float],
    max_chars: int = 120,
) -> str:
    """Crop a detection bbox out of the PDF and extract its text.

    The bbox is in image-pixel coords; we scale it to PDF points using
    ``DETECTION_IMG_W`` / ``DETECTION_IMG_H``. Detection bboxes
    sometimes overshoot the margin slightly (an annotator dragged past
    the edge); pdfplumber refuses to crop outside the page, so we
    clamp the scaled bbox to ``pdf_page.bbox`` before cropping. The
    extracted text is whitespace-collapsed and truncated to
    ``max_chars``.

    :param pdf_page: The pdfplumber ``Page`` to crop from.
    :param bbox_img: ``[x0, y0, x1, y1]`` in image-pixel coords.
    :param max_chars: Maximum returned-string length. Longer text is
        truncated with a trailing ellipsis.
    :return: The cropped text, whitespace-collapsed and truncated.
        Empty string if the clamped bbox is degenerate.
    """
    sx = pdf_page.width / DETECTION_IMG_W
    sy = pdf_page.height / DETECTION_IMG_H
    x0, y0, x1, y1 = bbox_img
    px0, py0, px1, py1 = pdf_page.bbox
    cx0 = max(px0, min(x0 * sx, px1))
    cy0 = max(py0, min(y0 * sy, py1))
    cx1 = max(px0, min(x1 * sx, px1))
    cy1 = max(py0, min(y1 * sy, py1))
    if cx1 <= cx0 or cy1 <= cy0:
        return ""
    text = pdf_page.crop((cx0, cy0, cx1, cy1)).extract_text() or ""
    text = " ".join(text.split())
    if len(text) > max_chars:
        text = text[:max_chars].rstrip() + "…"
    return text


def _first_line_of_caption(
    pdf_page: PdfPage, caption_bbox: list[float]
) -> str:
    """Return just the top line of a caption bbox.

    :param pdf_page: The pdfplumber ``Page`` to crop from.
    :param caption_bbox: ``[x0, y0, x1, y1]`` in image-pixel coords.
    :return: The first line of the caption, or empty string if the
        crop returned nothing.
    """
    bbox = list(caption_bbox)
    bbox[3] = min(bbox[3], bbox[1] + 80)
    txt = _crop_text(pdf_page, bbox, max_chars=120)
    return txt.split("\n", 1)[0] if "\n" in txt else txt


def _column_top_first_line(pdf_page: PdfPage, col: str) -> str:
    """Return the first line of text at the top of a column.

    Used for column-spanning opinions: when the caption is in one
    column and the closing key icon is in the other, the opinion's
    body wraps around the column break. This snippet points the LLM
    at where the body continues so it doesn't lose the text
    mid-opinion.

    :param pdf_page: The pdfplumber ``Page`` to crop from.
    :param col: ``"L"`` for left column, ``"R"`` for right column.
    :return: First line at the top of the requested column,
        whitespace-collapsed and capped at 120 chars.
    """
    page_w, page_h = pdf_page.width, pdf_page.height
    if col == "L":
        bbox = (0, 0, page_w / 2, min(page_h, 120))
    else:
        bbox = (page_w / 2, 0, page_w, min(page_h, 120))
    txt = pdf_page.crop(bbox).extract_text() or ""
    txt = " ".join(txt.split())
    if len(txt) > 120:
        txt = txt[:120].rstrip() + "…"
    return txt


# ── opinion roadmap (page-local) ──────────────────────────────────────


def _opinion_roadmap(
    detections: list[dict[str, Any]],
    opinions: list[dict[str, Any]],
    page_index: int,
    pdf_page: PdfPage,
) -> str | None:
    """Build the OPINION ROADMAP section of the prompt.

    Captions/keys come from this page's ``CASE_CAPTION`` + ``KEY_ICON``
    detections; pairing in reading order distinguishes "complete on
    this page" from "starts here" and "ends here from prior".

    Pass-through count comes from ``opinions`` (the scan's curated
    opinion list) — the one cross-page fact we can't derive from
    page-local detections. We also use ``opinions`` to drive the
    "continuation slots" filter that suppresses CASE_CAPTION phantoms
    when a long caption from a prior page spills onto this one.

    :param detections: This page's detection list (already filtered
        to ``page_index``).
    :param opinions: ``scan.opinions_json`` — the full curated opinion
        list for the scan, each entry with ``caption_page`` /
        ``key_page`` / etc.
    :param page_index: 0-based PDF page index for this page.
    :param pdf_page: The pdfplumber ``Page`` for caption / column-top
        text crops.
    :return: The roadmap section, or ``None`` if there's nothing to
        say about this page (no captions, no keys, no pass-through).
    """
    captions = [d for d in detections if d.get("label") == "CASE_CAPTION"]
    keys = [d for d in detections if d.get("label") == "KEY_ICON"]

    n_pass_through = sum(
        1
        for op in opinions
        if (
            isinstance(op.get("caption_page"), int)
            and isinstance(op.get("key_page"), int)
            and op["caption_page"] < page_index < op["key_page"]
        )
    )
    # Opinions whose caption was on a prior page AND that end on this
    # page. We use this to filter out caption-continuation phantoms —
    # the YOLO detector sometimes labels the tail of a long multi-page
    # caption as another CASE_CAPTION on this page. ``opinions`` is the
    # authoritative source of how many real captions start here.
    n_continuing_ends = sum(
        1
        for op in opinions
        if (
            isinstance(op.get("caption_page"), int)
            and isinstance(op.get("key_page"), int)
            and op["caption_page"] < page_index
            and op["key_page"] == page_index
        )
    )

    if not captions and not keys:
        if n_pass_through == 0:
            return None
        return (
            f"OPINION ROADMAP:\n- {n_pass_through} opinion(s) visible; "
            f"{n_pass_through} passes through this page.\n"
            "- An opinion that began on a previous page passes through "
            "this page entirely — body text only, no <parties>, no "
            "closing key."
        )

    # Reading order: L top→bottom, then R top→bottom.
    def _order(d: dict[str, Any]) -> tuple[int, float]:
        return (0 if _bbox_col(d["bbox"]) == "L" else 1, d["bbox"][1])

    # Pair captions to keys with a reading-order stack. ``opinions``
    # data drives two refinements:
    #
    #  * ``continuation_slots`` (= ``n_continuing_ends``) is the
    #    number of opinions whose captions started on a prior page
    #    AND that end on this page. While slots remain open,
    #    ``CASE_CAPTION`` detections are caption-continuation phantoms
    #    (tail of a long multi-page caption spilling onto this page)
    #    and are skipped; each ``KEY_ICON`` consumed counts toward
    #    ``n_ends_only`` and decrements ``continuation_slots``.
    #  * Once ``continuation_slots`` is zero, normal stack pairing
    #    resumes.
    events: list[tuple[str, dict[str, Any]]] = [
        ("cap", c) for c in captions
    ] + [("key", k) for k in keys]
    events.sort(key=lambda e: _order(e[1]))

    complete: list[tuple[dict[str, Any], dict[str, Any]]] = []
    n_ends_only = 0
    continuation_slots = n_continuing_ends
    stack: list[dict[str, Any]] = []
    for kind, d in events:
        if kind == "cap":
            if continuation_slots > 0:
                continue  # caption-continuation phantom from prior page
            stack.append(d)
            continue
        # key
        if continuation_slots > 0:
            n_ends_only += 1
            continuation_slots -= 1
        elif stack:
            complete.append((stack.pop(), d))
        else:
            n_ends_only += 1
    starts_only = stack  # unpaired captions
    n_complete = len(complete)
    n_starts = len(starts_only)

    visible = n_complete + n_starts + n_ends_only + n_pass_through
    summary_parts: list[str] = [f"{visible} opinion(s) visible"]
    if n_complete:
        summary_parts.append(f"{n_complete} complete on this page")
    if n_ends_only:
        summary_parts.append(
            f"{n_ends_only} continuing from previous page (ends here)"
        )
    if n_starts:
        summary_parts.append(f"{n_starts} starts here, continues to next page")
    if n_pass_through:
        summary_parts.append(f"{n_pass_through} passes through this page")

    lines: list[str] = ["; ".join(summary_parts) + "."]

    # Emit complete pairs first (reading order), then starts-only.
    complete.sort(key=lambda pair: _order(pair[0]))
    starts_only.sort(key=_order)

    idx = 0
    for cap, key in complete:
        idx += 1
        cap_col = _bbox_col(cap["bbox"])
        cap_zone = _bbox_zone(cap["bbox"])
        key_col = _bbox_col(key["bbox"])
        key_zone = _bbox_zone(key["bbox"])
        first_line = _first_line_of_caption(pdf_page, cap["bbox"])
        suffix = f' — caption begins: "{first_line}"' if first_line else ""
        if cap_col == key_col:
            lines.append(
                f"opinion {idx}: starts and ends in column {cap_col} "
                f"(caption {cap_zone}, ends {key_zone}){suffix}"
            )
        else:
            entry = (
                f"opinion {idx}: starts at the {cap_zone} of column "
                f"{cap_col} and ends in column {key_col} ({key_zone})"
                f" — column-spanning{suffix}"
            )
            cont = _column_top_first_line(pdf_page, key_col)
            if cont:
                entry += (
                    f"\n  opinion {idx} continues at the top of column "
                    f'{key_col} with: "{cont}"'
                )
            lines.append(entry)

    for cap in starts_only:
        idx += 1
        col = _bbox_col(cap["bbox"])
        zone = _bbox_zone(cap["bbox"])
        first_line = _first_line_of_caption(pdf_page, cap["bbox"])
        suffix = f' — caption begins: "{first_line}"' if first_line else ""
        lines.append(
            f"opinion {idx}: starts at the {zone} of column {col} "
            f"and continues onto the next page{suffix}"
        )

    if n_ends_only:
        lines.append(
            "Top of this page is the END of an opinion that began "
            "earlier — DO NOT emit a new <parties> for that section; "
            "it continues from the previous page."
        )
    if n_pass_through:
        lines.append(
            "An opinion that began on a previous page passes through "
            "this page entirely — body text only, no <parties>, no "
            "closing key."
        )

    return "OPINION ROADMAP:\n" + "\n".join(f"- {line}" for line in lines)


# ── footnote band ─────────────────────────────────────────────────────


def _footnote_hint(
    detections: list[dict[str, Any]], pdf_page: PdfPage
) -> str | None:
    """Build the FOOTNOTES section of the prompt.

    Looks for ``FOOTNOTES`` label detections on this page. If at least
    one high-confidence detection is present we emit a "definitely
    emit a <footnotes> block" instruction plus a text snippet from
    each high-confidence band so the LLM knows where to look. If only
    low-confidence detections exist we emit a softer "may be present"
    note. If none are present, returns ``None``.

    :param detections: This page's detection list.
    :param pdf_page: The pdfplumber ``Page`` for footnote-band text
        crops.
    :return: The footnote section, or ``None`` if no FOOTNOTES were
        detected on this page.
    """
    fn_dets = [d for d in detections if d.get("label") == "FOOTNOTES"]
    if not fn_dets:
        return None

    high_conf = [
        d for d in fn_dets if d.get("confidence", 0) >= _FOOTNOTE_CONF_FLOOR
    ]
    if not high_conf:
        return (
            "FOOTNOTES may be present on this page (low-confidence detection)."
        )

    lines = [
        "FOOTNOTES detected at the bottom of this page — **emit** a "
        "<footnotes> block. Snippets below are the top of the footnote "
        "band; text may be interleaved across columns in reading order, "
        "use the page layout to assign each footnote to its correct "
        "column. Text begins with:"
    ]
    for fn in high_conf:
        txt = _crop_text(pdf_page, fn["bbox"])
        if txt:
            lines.append(f'  "{txt}"')
    return "\n".join(lines)


# ── public entry ──────────────────────────────────────────────────────


_BLANK_PAGE_INSTRUCTION = (
    "PAGE IS BLANK / REDACTED — the body of this page is entirely "
    "covered by headnote redactions and there are no footnotes. Emit "
    "just the <pagenumber> and nothing else."
)


def build_user_prompt(page: Page) -> str | None:
    """Build the per-page user prompt text from a ``Page`` row.

    Inputs come from the ``Page`` row plus its parent ``Scan``:

      * ``page.is_blank`` short-circuits to a fixed instruction (the
        body is entirely covered by headnote redactions, so we tell
        the LLM to emit just the pagenumber).
      * ``page.detections`` drives caption/key pairing and the
        footnote hint.
      * ``page.scan.opinions_json`` supplies the cross-page facts the
        page can't know on its own: pass-through count, and the
        continuation-slot filter for phantom CASE_CAPTIONs that are
        actually tails of a long caption spilling in from a prior
        page.
      * ``page.pdf_path`` (resolved against ``page.scan.output_dir``)
        is opened only for caption / footnote text crops.

    :param page: The ``scanning.models.Page`` row to build a prompt
        for. The Page must have ``detections``, ``is_blank``,
        ``page_index``, ``pdf_path``, and a ``scan`` with
        ``output_dir`` + ``opinions_json``.
    :return: The prompt text, or ``None`` if there's nothing useful
        to say about the page (no detections, no opinions touching
        it, and it isn't blank).
    """
    if page.is_blank:
        return _BLANK_PAGE_INSTRUCTION

    pdf_path = Path(page.scan.output_dir) / page.pdf_path
    detections = page.detections or []
    opinions = (page.scan.opinions_json or []) if page.scan else []

    with pdfplumber.open(pdf_path) as pdf:
        pdf_page = pdf.pages[0]
        sections: list[str] = []
        roadmap = _opinion_roadmap(
            detections, opinions, page.page_index, pdf_page
        )
        if roadmap:
            sections.append(roadmap)
        fn = _footnote_hint(detections, pdf_page)
        if fn:
            sections.append(fn)

    return "\n\n".join(sections) if sections else None
