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

A page the book compresses prints a range in place of the number
(``913–925``, issue #233). The range is read at the corner of its
line, exactly as a single number is, and it enters ``ocr_results``
with ``type="range"``: the sequence analysis then breaks at that page
and counts every page the range covers as present.

A page the book adds between two numbered pages prints a number with a
trailing letter (``2094a``, issue #319). It is read at the corner too,
and it enters ``ocr_results`` with ``type="suffixed"``. It claims no
number: the sequence analysis skips it and keeps the page before it
and the page after it as neighbours, so 2094, 2094a, 2094b and 2095
are four pages and two numbers. :func:`_value` returns None for it, so
:func:`_resolve_by_neighbours` neither repairs such a page nor repairs
from one.

The stray ``L`` decides the order the token patterns are tried in: the
plain number first, the suffixed number second. The other order reads
``2094L``, which is the icon glued to page 2094, as a page named
``2094L``. The same icon is misread as a lower-case ``l`` and as an
``I``, which is why the reader trusts six letters (``SUFFIX_LETTERS``)
and not the alphabet: a letter it does not trust leaves the page with
no reading and a ``no_page_number`` card, which a curator answers,
while a wrong suffixed reading makes no card at all.

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

#: The three shapes a printed page number takes, as ``type`` in
#: ``Scan.ocr_results`` and in the apply's printed-page map.
SINGLE = "single"
RANGE = "range"
SUFFIXED = "suffixed"

#: A printed page number: 1 to 4 digits, possibly glued to the stray
#: ``L`` the parallel-page icon is misread as.
_NUMBER_RE = re.compile(r"^L?(\d{1,4})L?$")
#: A printed page number with a trailing letter (``2094a``, #319): the
#: page the book adds between two numbered pages. One ASCII letter,
#: upper case or lower case, and the case is kept -- the book prints
#: one of the two glyphs and no reader compares them. Tried after
#: :data:`_NUMBER_RE`, which owns the stray ``L``.
_SUFFIXED_RE = re.compile(r"^(\d{1,4}[A-Za-z])$")
#: A first-last range like ``677-685``, hyphen or en dash. The line
#: form allows the spaces the printer sets around the dash; the token
#: form is the range as one word of a longer line.
_RANGE_RE = re.compile(r"^(\d{1,4})\s*[–\-]\s*(\d{1,4})$")
_RANGE_TOKEN_RE = re.compile(r"^(\d{1,4})[–\-](\d{1,4})$")
#: How many digits a suffixed number the *reader* trusts carries. The
#: shape alone is two characters wide, which is what the ordinal of a
#: reporter series is: a head line that wraps can leave ``2d`` or
#: ``3d`` at a corner, and the reading would name the page 2. A
#: curator may still type ``9a``, because a person read the page.
MIN_SUFFIXED_DIGITS = 2

#: The letters the *reader* trusts at the end of a number. The book
#: labels the pages it adds in order from ``a``, and six is more than
#: a volume prints. Every other letter there is noise, and two of them
#: are noise the reader knows: the parallel-page icon is misread as an
#: ``l`` or an ``I`` (#228 strips the upper-case ``L`` alone), so
#: ``2094l`` would read as a page named ``2094l``. A curator may still
#: type any letter, because a person read the page.
SUFFIX_LETTERS = frozenset("abcdefABCDEF")

#: How many pages one printed range may cover. A compressed opinion
#: covers tens of pages; a docket number (``19-1234``) covers more
#: than any book page can, which is how the guard tells them apart.
MAX_RANGE_SPAN = 200
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


def _range_value(match: re.Match | None) -> str | None:
    """Return the range a match names, when the two numbers are pages.

    A page range runs forward and it is short: the compressed page of
    a withdrawn opinion covers tens of pages (issue #233). Two numbers
    joined by a dash are not always a range -- a docket number
    (``19-1234``) runs far past the end of any volume, and a split
    year (``1996-97``) runs backward -- so both shapes are refused
    here rather than read as a page.

    :param match: A match of one of the range patterns, or None.
    :returns: The range as ``"913-925"``, or None.
    :rtype: str | None
    """
    if match is None:
        return None
    first, last = int(match.group(1)), int(match.group(2))
    if first < 1 or last <= first or last - first > MAX_RANGE_SPAN:
        return None
    return f"{first}-{last}"


def number_type(value: str | None) -> str | None:
    """Name the shape of one stored printed page number.

    The one deriver of ``type`` beside ``detected``, for every writer
    of a curator's own number: ``page_edits.overlay_page_numbers``,
    ``apply.printed_pages`` and ``views_process.assign_page``, which
    answers it to the viewer. The reader has the shape already, from
    the pattern that matched.

    :param value: The stored number, as the curator typed it.
    :returns: ``"range"``, ``"suffixed"``, ``"single"``, or None for a
        blank value, which is the curator clearing the number.
    :rtype: str | None
    """
    if not value:
        return None
    if "-" in value:
        return RANGE
    if _SUFFIXED_RE.match(value):
        return SUFFIXED
    return SINGLE


def _token_reading(token: str) -> tuple[str, str] | None:
    """Read the page number one token of a line offers.

    The three shapes, in the one order they may be tried in: the plain
    number owns the stray ``L`` (#228), so it goes before the suffixed
    number (#319), which would otherwise read ``2094L`` as a page named
    ``2094L``. The range is last, because its dash makes it the one
    shape the other two cannot match.

    :param token: One whitespace-delimited word of a cleaned line.
    :returns: ``(detected, type)``, or None when the token is no page
        number.
    :rtype: tuple[str, str] | None
    """
    number = _NUMBER_RE.match(token)
    if number:
        return (number.group(1), SINGLE)
    suffixed = _suffixed_value(_SUFFIXED_RE.match(token))
    if suffixed:
        return (suffixed, SUFFIXED)
    spanned = _range_value(_RANGE_TOKEN_RE.match(token))
    if spanned:
        return (spanned, RANGE)
    return None


def _suffixed_value(match: re.Match | None) -> str | None:
    """Return the suffixed number a match names, when a page prints it.

    The guard of :func:`_range_value`, for the other shape whose
    pattern is wider than the printed thing (#319): the number carries
    at least :data:`MIN_SUFFIXED_DIGITS` digits, and the letter is one
    of :data:`SUFFIX_LETTERS`. A letter outside that set leaves the
    page with no reading, which is what it had before #319: a
    ``no_page_number`` card the curator answers.

    :param match: A match of the suffixed pattern, or None.
    :returns: The number as ``"2094a"``, or None.
    :rtype: str | None
    """
    if match is None:
        return None
    value = match.group(1)
    if len(value) - 1 < MIN_SUFFIXED_DIGITS:
        return None
    if value[-1] not in SUFFIX_LETTERS:
        return None
    return value


def suffixed_number(value: str | None) -> int | None:
    """Return the number a suffixed page shares its bucket with (#335).

    ``2094a`` gives ``2094``. The sequence gives such a page no span
    (``services.printed_page_span``), because the book adds it between
    two numbered pages; the key of an opinion that starts there is the
    number without the letter, so ``2094`` and ``2094a`` share one
    bucket and ``Opinion.index_in_page`` orders them.

    :param value: The stored number, as ``"2094a"``.
    :returns: The number, or None when the value is not that shape.
    :rtype: int | None
    """
    if not value or not _SUFFIXED_RE.match(str(value)):
        return None
    return int(str(value)[:-1])


def _line_readings(line: str) -> list[tuple[str, str, str]]:
    """Read the page numbers one line of a cell offers.

    The running head puts the number at the page's outer corner, so it
    is the first or the last token of its line -- never a token buried
    in the middle, which is a year, a docket number or a citation.

    A range is read at the corner too, not only as the whole line
    (#233): the head band of a compressed page prints
    ``913–925 ATLANTIC REPORTER, 2d SERIES``, and the whole-line rule
    left that page with no number at all. A number with a trailing
    letter is read at the corner in the same way (#319).

    :param line: One cleaned line of a cell's text.
    :returns: ``(detected, type, side)`` per reading, where ``side`` is
        the end of the line the token was read at -- ``"left"`` or
        ``"right"`` -- or ``"both"`` when the reading is the whole
        line, which a bare number, a suffixed number and a range
        (spaced or not) are.
    :rtype: list[tuple[str, str, str]]
    """
    tokens = line.split()
    if not tokens:
        return []
    whole_line_range = _range_value(_RANGE_RE.match(line))
    if whole_line_range:
        return [(whole_line_range, RANGE, "both")]
    leading = _token_reading(tokens[0])
    if len(tokens) == 1:
        return [(*leading, "both")] if leading else []
    trailing = _token_reading(tokens[-1])
    readings = []
    if leading:
        readings.append((*leading, "left"))
    if trailing:
        readings.append((*trailing, "right"))
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
        number, a suffixed number, or a range.
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
    if candidate is None or candidate["type"] != SINGLE:
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
        if current is None or current["type"] != SINGLE:
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


def pages_without_number(scan) -> list[int]:
    """Return the pages of this volume that carry no page number (#342).

    The gate of the review-1 approval, and the one rule for "a page has
    no number": the view refuses the approval while this list is not
    empty, and the step-1 bar shows a note in place of the button. The
    twin of ``repairs.has_waiting`` (#266), which refuses the same
    approval for the other reason.

    The list is derived from the data, never from the ``Issue`` rows.
    An ``Issue`` row is a copy of a card, and the copy is older than
    the last write on more than one path: ``views_process.assign_page``
    deletes the ``no_page_number`` row of the page it writes, and it
    deletes that row also when the curator **clears** the number. A
    gate over the rows would pass a page with no number at all.

    Four answers take a page off the list:

    - A number, from the reader or from the curator. The overlay comes
      first, so a number typed since the last rebuild counts at once.
    - A number the curator **cleared**. ``assign_page`` keeps the row
      with a blank value, so the row itself separates "a person
      cleared this" from "the model read nothing", and that is the
      gesture the page editor offers for a page with no number: the
      curator empties the field. The card of that page is deleted by
      the same endpoint, so a rule that ignored the row would name a
      page whose card a curator cannot reach until the next recompute.
    - A deletion. The volume loses the page (#255).
    - A dismissal of the page's ``no_page_number`` card. A cover, a
      blank leaf and a plate carry no printed number, and a refusal
      with no way out would strand the review. The dismissal is one
      click by a person who looked at the page.

    A page with a trailing letter (#319) carries a reading, so it is
    never named here, which is the rule of the issue. Its reading names
    no span (``services.printed_page_span``), so an opinion that starts
    on such a page is still named by its position. An inserted page is
    not in ``ocr_results``, and its number is read after the apply.

    :param scan: The scan a reviewer wants to approve.
    :returns: The 1-based pages of the original, in page order.
    :rtype: list[int]
    """
    from scanning import page_edits
    from scanning.models import CheckName, PageEdit

    if not scan.ocr_results:
        return []
    # A copy: ``overlay_page_numbers`` writes into the entries it is
    # given, and this function is a read. The cache belongs to the
    # rebuild (``services.recalculate_issues``), which writes it back.
    results = [dict(entry) for entry in scan.ocr_results]
    # The curator outranks the model (#214), so the overlay comes
    # first: a number typed after the last rebuild answers its page.
    results, _stale = page_edits.overlay_page_numbers(scan, results)
    without = {
        entry["pdf_page"] for entry in results if not entry.get("detected")
    }
    if not without:
        return []
    without -= page_edits.deleted_pages(scan)
    # ``current_edits`` reads the standing rows of *this* original, so
    # a withdrawn row and one made against another document answer
    # nothing. One query for the two kinds.
    answered = page_edits.current_edits(
        scan, PageEdit.Kind.DISMISS_ISSUE, PageEdit.Kind.SET_NUMBER
    )
    without -= {
        edit.pdf_page
        for edit in answered
        if edit.pdf_page
        and (
            edit.kind == PageEdit.Kind.SET_NUMBER
            or edit.value == CheckName.NO_PAGE_NUMBER
        )
    }
    return sorted(without)
