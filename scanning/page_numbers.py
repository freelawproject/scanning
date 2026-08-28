"""Page numbers out of the glued dots.mocr volume JSON (issues #149/#204).

The legacy validate stage OCR'd a tight page-number crop, so its
``detected`` was essentially the number itself. dots.mocr instead
returns whole layout cells -- ``677 ATLANTIC REPORTER, 2d SERIES`` on
even pages, ``STATE v. SMITH -- Cite as 218 A.3d 677 -- 679`` on odd
ones -- so this adapter must pick the right cell and then the number
token inside it. Only the producer changes: the emitted entries keep
the ``ocr_results`` shape the sequence analysis
(``blackletter.validate``), the review-1 UI, and manual page
assignment (``assign_page``) already consume.

Cell selection intersects three redundant signals, degrading gracefully
when they disagree:

- the dots label is ``Page-header`` / ``Page-footer``;
- the bbox sits in the head or foot band. The band constants come from
  ai-research ``pipeline/core/order.py`` (branch ``extraction_align``):
  a head cell ends above ``0.085 * H``, a foot cell starts below
  ``0.95 * H``, with H the page's own render height;
- the text carries a plausible digit token.

Known dots noise, handled here: superscript digits leak in beside the
number, and the parallel-page-number icon is dropped or read as a stray
``L`` glued to the number. Parallel page numbers themselves are
deferred.

A page the worker failed or filtered has no cells and gets
``detected=None``; the sequence analysis reports it as
``no_page_number`` and interpolates across it, and review 1's manual
assignment is the human backstop.
"""

from __future__ import annotations

import re

from scanning.services import DOTS_ZONE_PREFIX, _is_manual_read

#: Band fractions of the page render height, from ai-research
#: ``pipeline/core/order.py``: a cell entirely above HEAD_BAND is a
#: running head, one starting below FOOT_BAND is a footer.
HEAD_BAND = 0.085
FOOT_BAND = 0.95

HEADER_CATEGORY = "Page-header"
FOOTER_CATEGORY = "Page-footer"

#: A printed page number: 1 to 4 digits, possibly glued to the stray
#: ``L`` the parallel-page icon is misread as.
_NUMBER_RE = re.compile(r"^L?(\d{1,4})L?$")
#: A first-last range like ``677-685``, hyphen or en dash.
_RANGE_RE = re.compile(r"^(\d{1,4})\s*[–\-]\s*(\d{1,4})$")
#: Superscript digits are neighbouring-footnote noise, never part of
#: the page number; drop them before any token is read.
_SUPERSCRIPTS = str.maketrans("", "", "⁰¹²³⁴⁵⁶⁷⁸⁹")

_EMPTY = {
    "detected": None,
    "type": None,
    "score": None,
    "zone": None,
    "ocr": None,
}


def _clean(text: str) -> str:
    """Return ``text`` with the known dots noise removed.

    :param text: A cell's raw text.
    :returns: The text without superscript digits, whitespace-trimmed.
    :rtype: str
    """
    return text.translate(_SUPERSCRIPTS).strip()


def _outer_token(text: str) -> str | None:
    """Return the page-number token at the outer corner of ``text``.

    The running head puts the number at the page's outer corner, so it
    is the first or the last whitespace token. When both ends carry a
    number (rare), printed parity breaks the tie: even numbers sit on
    left pages (leading), odd numbers on right pages (trailing).

    :param text: A cleaned cell text.
    :returns: The digits, or None when neither end holds a number.
    :rtype: str | None
    """
    tokens = text.split()
    if not tokens:
        return None
    leading = _NUMBER_RE.match(tokens[0])
    trailing = _NUMBER_RE.match(tokens[-1])
    if leading and trailing and len(tokens) > 1:
        if int(trailing.group(1)) % 2 == 1:
            return trailing.group(1)
        return leading.group(1)
    match = leading or trailing
    return match.group(1) if match else None


def _read_cell(text: str) -> tuple[str, str] | None:
    """Read a page number (or range) out of one cell's text.

    :param text: The cell's raw text.
    :returns: ``(detected, type)``, or None when the text holds no
        number.
    :rtype: tuple[str, str] | None
    """
    cleaned = _clean(text)
    range_match = _RANGE_RE.match(cleaned)
    if range_match:
        return f"{range_match.group(1)}-{range_match.group(2)}", "range"
    number = _outer_token(cleaned)
    if number:
        return number, "single"
    return None


def _band(cell: dict, origin_height: int | float) -> str | None:
    """Name the band a cell sits in, if any.

    :param cell: One dots layout cell (``bbox``, ``category``, ``text``).
    :param origin_height: The page's render height, the space the bbox
        lives in.
    :returns: ``"header"``, ``"footer"``, or None for the body.
    :rtype: str | None
    """
    bbox = cell.get("bbox") or []
    if len(bbox) < 4 or not origin_height:
        return None
    if bbox[3] < HEAD_BAND * origin_height:
        return "header"
    if bbox[1] > FOOT_BAND * origin_height:
        return "footer"
    return None


def _score(label_ok: bool, band_ok: bool, digits_only: bool) -> float:
    """Grade how many of the three signals agreed.

    dots has no per-region confidence, so the score is synthetic:
    ``validate``'s auto-correct and the review UI only need a rough
    "how sure was the producer" ordering.

    :param label_ok: The cell carried a Page-header/Page-footer label.
    :param band_ok: The cell's bbox sat in the head or foot band.
    :param digits_only: The cell's text was the number and nothing else.
    :returns: 1.0 down to 0.5.
    :rtype: float
    """
    if digits_only and label_ok and band_ok:
        return 1.0
    if (label_ok and band_ok) or digits_only:
        return 0.8
    return 0.5


def extract_page_number(page: dict) -> dict:
    """Build one ``ocr_results`` entry from one glued page dict.

    Header candidates outrank footer ones -- a section-opening page
    carries its number in the footer only, so the footer is the
    fallback, not a competitor.

    :param page: One ``pages[]`` entry of the glued volume document.
    :returns: ``{pdf_page, detected, type, score, zone, ocr,
        img_width, img_height}``; ``detected`` is None when the page
        was filtered, failed, or shows no number.
    :rtype: dict
    """
    entry = {
        "pdf_page": page["pdf_page"],
        **_EMPTY,
        "img_width": page.get("origin_width"),
        "img_height": page.get("origin_height"),
    }
    origin_height = page.get("origin_height") or 0

    label_zone = {HEADER_CATEGORY: "header", FOOTER_CATEGORY: "footer"}
    candidates = []
    for cell in page.get("cells") or []:
        label = cell.get("category")
        band = _band(cell, origin_height)
        zone = label_zone.get(label) or band
        if zone is None:
            continue
        text = cell.get("text") or ""
        read = _read_cell(text)
        if read is None:
            continue
        detected, number_type = read
        cleaned = _clean(text)
        digits_only = bool(
            _NUMBER_RE.match(cleaned) or _RANGE_RE.match(cleaned)
        )
        candidates.append(
            {
                "zone": zone,
                "detected": detected,
                "type": number_type,
                "score": _score(
                    label in label_zone, band is not None, digits_only
                ),
                "ocr": text,
            }
        )

    if not candidates:
        return entry
    best = sorted(
        candidates,
        key=lambda c: (c["zone"] != "header", -c["score"]),
    )[0]
    entry.update(
        detected=best["detected"],
        type=best["type"],
        score=best["score"],
        zone=f"{DOTS_ZONE_PREFIX}{best['zone']}",
        ocr=best["ocr"],
    )
    return entry


def ocr_results_from_volume(
    document: dict, existing: list[dict] | None
) -> list[dict]:
    """Convert one glued volume document into ``Scan.ocr_results``.

    One entry per page, in ``pdf_page`` order. An entry a curator typed
    by hand (``assign_page`` stamps ``zone`` and ``ocr`` with
    ``"manual"``) outranks anything a model read, so it is carried over
    verbatim from ``existing``.

    :param document: The glued volume JSON (issue #202).
    :param existing: The scan's current ``ocr_results``, for the manual
        carry-over. None or empty when the stage never ran.
    :returns: The new ``ocr_results`` list.
    :rtype: list[dict]
    """
    manual = {
        entry["pdf_page"]: entry
        for entry in (existing or [])
        if _is_manual_read(entry)
    }
    results = []
    for page in sorted(document["pages"], key=lambda p: p["pdf_page"]):
        kept = manual.get(page["pdf_page"])
        results.append(kept or extract_page_number(page))
    return results
