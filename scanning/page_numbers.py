"""Page numbers out of the glued dots.mocr volume JSON (issues #149/#204).

The legacy validate stage OCR'd a tight page-number crop, so its
``detected`` was essentially the number itself. dots.mocr instead
returns whole layout cells -- ``677 ATLANTIC REPORTER, 2d SERIES`` on
even pages, ``STATE v. SMITH -- Cite as 218 A.3d 677 -- 679`` on odd
ones -- so this adapter must pick the right cell and then the number
token inside it. Only the producer changes: the emitted entries keep
the ``ocr_results`` shape the sequence analysis
(``blackletter.validate``), the review-1 UI, and the overlay of the
curator's own page numbers (``page_edits``) already consume.

A page of a reporter carries several numbers in its head band, and only
one of them is the page number:

- the volume number, in the reporter title (``469 PACIFIC REPORTER, 3d
  SERIES``);
- the parallel citation page, alone in its own cell;
- the first page of the opinion, in the ``Cite as`` line;
- a headnote number, which dots also labels ``Page-header``;
- the last word of a case name that ends in a digit (``SCHOOL DIST.
  NO. 1``).

**Position is what tells them apart** (#228): the printed number sits
at the outer corner of the page, and every rival sits nearer the
middle. So the rank is geometric, not textual. Measured over one
1294-page volume, a true reading sits within 0.19 of the page width
from its edge and every rival at 0.25 or more, which is what
``CORNER_BAND`` records.

Cell selection intersects three redundant signals, degrading gracefully
when they disagree:

- the dots label is ``Page-header`` / ``Page-footer``;
- the bbox sits in the head or foot band. The band constants come from
  ai-research ``pipeline/core/order.py`` (branch ``extraction_align``):
  a head cell ends above ``0.085 * H``, a foot cell starts below
  ``0.95 * H``, with H the page's own render height;
- the text carries a plausible digit token at a line's outer end.

Known dots noise, handled here: superscript digits leak in beside the
number, the parallel-page-number icon is dropped or read as a stray
``L`` glued to the number, and the running head and the ``Cite as``
line arrive as two lines of **one** cell. Parallel page numbers
themselves are deferred.

A page the worker failed or filtered has no cells and gets
``detected=None``; the sequence analysis reports it as
``no_page_number`` and interpolates across it, and review 1's manual
assignment is the human backstop.
"""

from __future__ import annotations

import re

from scanning.services import DOTS_ZONE_PREFIX

#: Band fractions of the page render height, from ai-research
#: ``pipeline/core/order.py``: a cell entirely above HEAD_BAND is a
#: running head, one starting below FOOT_BAND is a footer.
HEAD_BAND = 0.085
FOOT_BAND = 0.95

#: How near its own edge of the page a token must sit to read as a
#: corner one, as a fraction of the page width. It grades the score and
#: names a trusted reading; it never gates the rank, because a volume
#: whose number is centred in the footer has no rival to lose to.
CORNER_BAND = 0.25

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


def _line_readings(line: str) -> list[tuple[str, str, str]]:
    """Read the page numbers one line of a cell offers.

    The running head puts the number at the page's outer corner, so it
    is the first or the last token of its line -- never a token buried
    in the middle, which is a year, a docket number or a citation.

    :param line: One cleaned line of a cell's text.
    :returns: ``(detected, type, side)`` per reading, where ``side`` is
        the end of the line the token was read at -- ``"left"`` or
        ``"right"`` -- or ``"both"`` when the reading is the whole
        line, which a bare number and a range (spaced or not) are.
    :rtype: list[tuple[str, str, str]]
    """
    tokens = line.split()
    if not tokens:
        return []
    range_match = _RANGE_RE.match(line)
    if range_match:
        return [
            (f"{range_match.group(1)}-{range_match.group(2)}", "range", "both")
        ]
    leading = _NUMBER_RE.match(tokens[0])
    if len(tokens) == 1:
        return [(leading.group(1), "single", "both")] if leading else []
    trailing = _NUMBER_RE.match(tokens[-1])
    readings = []
    if leading:
        readings.append((leading.group(1), "single", "left"))
    if trailing:
        readings.append((trailing.group(1), "single", "right"))
    return readings


def _corner_distance(bbox: list, width: int | float, side: str) -> float:
    """Measure how far a token sits from its own edge of the page.

    The bbox belongs to the whole cell, so the side the token was read
    at is what says which edge to measure against: a leading token
    starts where the cell starts, a trailing one ends where it ends.

    :param bbox: The cell's bbox, ``[x0, y0, x1, y1]``.
    :param width: The page's render width, the space the bbox lives in.
    :param side: Which end of its line the token was read at.
    :returns: The distance as a fraction of the page width, 1.0 when
        the page reports no geometry.
    :rtype: float
    """
    if len(bbox) < 4 or not width:
        return 1.0
    if side == "left":
        distance = bbox[0]
    elif side == "right":
        distance = width - bbox[2]
    else:
        distance = min(bbox[0], width - bbox[2])
    return max(distance, 0) / width


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


def _score(
    label_ok: bool, band_ok: bool, corner_ok: bool, whole_line: bool
) -> float:
    """Grade how many of the signals agreed.

    dots has no per-region confidence, so the score is synthetic:
    ``validate``'s auto-correct and the review UI only need a rough
    "how sure was the producer" ordering. Position carries it (#228):
    a bare digit anywhere on the page used to score full marks, which
    is exactly what a headnote number is.

    :param label_ok: The cell carried a Page-header/Page-footer label.
    :param band_ok: The cell's bbox sat in the head or foot band.
    :param corner_ok: The token sat within CORNER_BAND of its edge.
    :param whole_line: The reading was the whole line -- a bare
        number, or a range.
    :returns: 1.0 down to 0.5.
    :rtype: float
    """
    if corner_ok and label_ok and band_ok:
        return 1.0
    if (corner_ok and (label_ok or band_ok)) or (
        whole_line and label_ok and band_ok
    ):
        return 0.8
    return 0.5


def _rank_key(candidate: dict) -> tuple:
    """Order the candidates of one page, best first.

    A header outranks a footer: a section-opening page carries its
    number in the footer only, so the footer is the fallback, not a
    competitor. Then how many signals agreed, then the geometry,
    because every rival number of a reporter page sits nearer the
    middle than the printed one. The line index breaks the tie the two
    lines of one cell produce -- the running head is the top line, the
    ``Cite as`` line is below it and both share the one bbox.

    The score comes **before** the distance on purpose. dots labels a
    headnote number ``Page-header`` too, wherever on the page it sits,
    so a distance-first rank hands the page to a headnote digit printed
    in the margin of the body -- and to a whole column of them, which
    :func:`_resolve_by_neighbours` then reads as a sequence and
    approves. The band is what separates the two, and the score is
    where the band is counted.

    There is no printed-parity key. Even numbers do sit on left pages,
    but the rule reaches only two readings of one line at one exact
    distance, which needs a head cell centred to the pixel -- a
    synthetic page, not a render. A tie that deep keeps the order the
    cells arrived in, and :func:`_resolve_by_neighbours` is what
    resolves it.

    :param candidate: One candidate of :func:`page_candidates`.
    :returns: The sort key.
    :rtype: tuple
    """
    return (
        candidate["zone"] != "header",
        -candidate["score"],
        candidate["distance"],
        candidate["line"],
    )


def page_candidates(page: dict) -> list[dict]:
    """Read every page number one page's cells offer, best first.

    :param page: One ``pages[]`` entry of the glued volume document.
    :returns: The ranked candidates. Empty when the page was filtered,
        failed, or shows no number.
    :rtype: list[dict]
    """
    origin_height = page.get("origin_height") or 0
    origin_width = page.get("origin_width") or 0
    label_zone = {HEADER_CATEGORY: "header", FOOTER_CATEGORY: "footer"}

    candidates = []
    for cell in page.get("cells") or []:
        label = cell.get("category")
        band = _band(cell, origin_height)
        zone = label_zone.get(label) or band
        if zone is None:
            continue
        text = cell.get("text") or ""
        bbox = cell.get("bbox") or []
        for index, line in enumerate(_clean(text).splitlines()):
            line = line.strip()
            for detected, number_type, side in _line_readings(line):
                distance = _corner_distance(bbox, origin_width, side)
                candidates.append(
                    {
                        "zone": zone,
                        "detected": detected,
                        "type": number_type,
                        "line": index,
                        "distance": distance,
                        "score": _score(
                            label in label_zone,
                            band is not None,
                            distance <= CORNER_BAND,
                            side == "both",
                        ),
                        "ocr": text,
                    }
                )
    return sorted(candidates, key=_rank_key)


def _entry(page: dict, candidate: dict | None) -> dict:
    """Build one ``ocr_results`` entry from a page and its reading.

    :param page: One ``pages[]`` entry of the glued volume document.
    :param candidate: The chosen candidate, or None for no reading.
    :returns: ``{pdf_page, detected, type, score, zone, ocr,
        img_width, img_height}``.
    :rtype: dict
    """
    entry = {
        "pdf_page": page["pdf_page"],
        **_EMPTY,
        "img_width": page.get("origin_width"),
        "img_height": page.get("origin_height"),
    }
    if candidate is None:
        return entry
    entry.update(
        detected=candidate["detected"],
        type=candidate["type"],
        score=candidate["score"],
        zone=f"{DOTS_ZONE_PREFIX}{candidate['zone']}",
        ocr=candidate["ocr"],
    )
    return entry


def extract_page_number(page: dict) -> dict:
    """Build one ``ocr_results`` entry from one glued page dict.

    Geometry alone, one page at a time. The volume-wide reading
    (:func:`ocr_results_from_volume`) adds the neighbour pass on top.

    :param page: One ``pages[]`` entry of the glued volume document.
    :returns: The entry; ``detected`` is None when the page was
        filtered, failed, or shows no number.
    :rtype: dict
    """
    candidates = page_candidates(page)
    return _entry(page, candidates[0] if candidates else None)


def _value(candidate: dict | None) -> int | None:
    """Return a candidate's number, when it is a single page number.

    :param candidate: One candidate, or None.
    :returns: The number, or None for a range or no reading.
    :rtype: int | None
    """
    if candidate is None or candidate["type"] != "single":
        return None
    return int(candidate["detected"])


def _resolve_by_neighbours(
    chosen: list[dict | None], candidates: list[list[dict]]
) -> list[dict | None]:
    """Prefer the reading both neighbours ask for (#228).

    The second net under the geometry, for the volume whose head cell
    holds the citation page and the page number at one distance.

    **Both** neighbours must name the same number, and the page must
    offer it. One neighbour is not enough: the rivals of a page number
    run in sequence themselves -- a parallel citation page, a headnote
    column -- so a single misread page would hand its own sequence to
    the page beside it, which is the one page the geometry may have
    read correctly. Asking two independent readings for one value
    costs the pass nothing measurable: over a real 1294-page volume it
    fires on no page at all, because the geometry already answers them.

    It reads the neighbours off the *geometric* picks throughout, never
    off its own repairs, so no repair can cascade. It never invents a
    number. It never touches a page that offers one value -- a reading
    no rival contests is the geometry's to keep -- and it never touches
    a range, which names two pages and answers no sequence.

    :param chosen: The geometric pick per page, in page order.
    :param candidates: The ranked candidates per page, in page order.
    :returns: The picks, with the contested ones resolved.
    :rtype: list[dict | None]
    """
    values = [_value(candidate) for candidate in chosen]
    resolved = list(chosen)
    for index, options in enumerate(candidates):
        current = chosen[index]
        if current is None or current["type"] != "single":
            continue
        if len({o["detected"] for o in options}) < 2:
            continue
        previous = values[index - 1] if index else None
        following = values[index + 1] if index + 1 < len(values) else None
        if previous is None or following is None:
            continue
        wanted = previous + 1
        if wanted != following - 1 or _value(current) == wanted:
            continue
        agreeing = [o for o in options if _value(o) == wanted]
        if agreeing:
            resolved[index] = agreeing[0]
    return resolved


def ocr_results_from_volume(document: dict) -> list[dict]:
    """Convert one glued volume document into ``Scan.ocr_results``.

    One entry per page, in ``pdf_page`` order, and pure machine output:
    a curator's own numbers are ``PageEdit`` rows since #214, and
    ``page_edits.overlay_page_numbers`` writes them over this on every
    recompute. This function used to carry them over from the previous
    blob, by the ``"manual"`` stamp on two of its fields -- a
    convention any new writer could forget, and one that dropped an
    entry whose page the new run did not report, in silence.

    :param document: The glued volume JSON (issue #202).
    :returns: The new ``ocr_results`` list.
    :rtype: list[dict]
    """
    pages = sorted(document["pages"], key=lambda p: p["pdf_page"])
    candidates = [page_candidates(page) for page in pages]
    chosen = [options[0] if options else None for options in candidates]
    chosen = _resolve_by_neighbours(chosen, candidates)
    return [_entry(page, candidate) for page, candidate in zip(pages, chosen)]
