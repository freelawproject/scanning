"""Repair the layout JSON of a dots.mocr page that broke on one character.

Issue #242. The worker asks dots.mocr for one JSON array per page: one
object per region with ``bbox``, ``category`` and ``text``. The model
writes that array correctly on hundreds of pages per shard and, on a
page dense with nested quotation marks, misplaces one escape. Upstream
``post_process_output`` then discards the whole array and keeps the
words, and the page reaches the reader with no cell and no page number.

Measured on four volumes (issue #242, comments of 2026-09-03), the
fault has three shapes, and each needs one edit:

- a lone ``"`` inside a string (``Expecting ',' delimiter``): put a
  backslash before it;
- a lone ``\\`` where ``\\"`` belongs (``Invalid \\escape``): put a
  quotation mark after it;
- a doubled closer, ``"}]"}]`` (``Extra data``): cut at the first
  complete parse.

The corpus survey of issue #268 measured a fourth shape on 22 pages: a
raw control character inside a string, where the model copied the line
break of the printed page in place of ``\n``. That one is answered by
a parse mode (``strict=False``) rather than by an arm -- see
:data:`_CONTROL_CHARACTER_MESSAGE` for why -- and it is recorded as the
edit ``relax_controls``.

:func:`repair` applies one edit per parser message, parses again, and
stops after :data:`MAX_EDITS`. It never writes over the answer as the
model wrote it: the callers keep ``raw`` and store the edits beside the
repaired cells.

The module owns one more rule, and for the same reason: the legality
of a box (:func:`legalize`, issue #297). Upstream checks none, and a
box in the wrong order failed a whole page in production. Both callers
run the rule, the worker on the answer and the glue on the stored
result.

Two callers share this module, and it must stay importable by both:
the worker image (``scanning/runpod-dotsmocr/handler.py``, which the
Dockerfile copies this file next to, so it imports it as a top-level
module) runs the repair before the retry ladder climbs, and the glue
(:mod:`scanning.dots_mocr`) runs it over every stored result that
carries ``raw``, which no new worker image reaches. So: no Django
import, standard library only.
"""

from __future__ import annotations

import json
from typing import NamedTuple

#: How many edits :func:`repair` makes before it gives up. Every
#: measured page needed one; three leaves room for a page with two
#: faults and still refuses to rewrite an answer that is not an array.
MAX_EDITS = 3

#: How many characters on each side of the fault the report shows.
EXCERPT_RADIUS = 40

#: The parser messages that mark a string closed too early: the value
#: ended at a quotation mark the model did not escape, and the parser
#: reads the rest of the text as syntax. Which message arrives depends
#: only on what follows the stray quotation mark, so all three name one
#: fault:
#:
#: - ``out[.]" and she felt`` -> ``Expecting ',' delimiter``
#: - ``out[.]", and she felt`` -> the parser takes the comma as the
#:   member separator and then wants a key, so
#:   ``Expecting property name enclosed in double quotes``
#: - a stray quotation mark inside a *key* -> ``Expecting ':' delimiter``
_EARLY_CLOSE_MESSAGES = (
    "Expecting ',' delimiter",
    "Expecting ':' delimiter",
    "Expecting property name enclosed in double quotes",
)

#: The one early-close message the parser reports **after** eating a
#: comma, so the arm has to step back over that comma to find the
#: quotation mark. A comma after a quotation is ordinary in an
#: opinion (``... out[.]", and she felt ...``), and without this the
#: arm reached only the pages whose stray quotation mark happened to
#: be followed by a space.
_AFTER_COMMA_MESSAGE = "Expecting property name enclosed in double quotes"

#: CPython's C scanner reports this exactly, and puts the offset **on**
#: the backslash. The pure-Python fallback says ``Invalid \escape:
#: ')'`` and points one character further. Both worker and daemon
#: images run CPython with the C scanner, and on the fallback this arm
#: simply does not fire: the message does not match, and
#: :func:`_restore_quote` checks the character under the offset anyway.
#: So a scanner change costs a repair, never a wrong edit.
_INVALID_ESCAPE_MESSAGE = "Invalid \\escape"
_EXTRA_DATA_MESSAGE = "Extra data"

#: The model copied the line break of the printed page into a ``text``
#: value, in place of ``\n``. Measured on 22 pages of the corpus survey
#: (issue #268): the break always falls at a quotation the page ends a
#: line with. CPython reports the message with the offset **on** the
#: control character, and the message ends in "at" -- the parser names
#: no character, because any of them is illegal there.
#:
#: This fault is answered by a parse mode and not by an arm, and that
#: is deliberate. An edit over the whole text would be wrong: a line
#: break **between** two tokens is legal whitespace, and only the
#: parser knows when it is inside a string. ``strict=False`` is that
#: knowledge, it keeps the character in the value, and it cannot reach
#: the structure -- a structural fault still raises, and the arms still
#: take their turn after it. One page in three of the 22 shows a
#: doubled break, which one edit each would have spent the budget on.
#:
#: CPython's C scanner reports this exactly, with the offset **on** the
#: control character, and names no character in the message: any of
#: them is illegal there. The pure-Python fallback says ``Invalid
#: control character '\n' at``, with the character in the message, so
#: the mode does not fire on that scanner -- the same limit
#: :data:`_INVALID_ESCAPE_MESSAGE` records, and the same outcome. A
#: scanner change costs a repair, never a wrong reading: matching a
#: message that carries a value would mean a prefix test on the branch
#: that reinterprets a whole page. Both worker and daemon images run
#: CPython with the C scanner.
_CONTROL_CHARACTER_MESSAGE = "Invalid control character at"


class Repair(NamedTuple):
    """What :func:`repair` answers.

    ``cells`` is the parsed array in the model's own pixel space, or
    ``None`` when no arm reached the fault. ``edits`` names each edit
    made, in order, as ``<arm>@<offset>``. ``fault`` is set only when
    ``cells`` is ``None``: the last parser message with an excerpt of
    the text around it, so a log line says what the next arm has to
    answer.
    """

    cells: list | None
    edits: list[str]
    fault: str | None


class Legal(NamedTuple):
    """What :func:`legalize` answers.

    ``cells`` is the list a reader can use: every box in order, and
    the cells whose box says nothing left out. ``edits`` names each
    change, in cell order, as ``<arm>@<cell index>`` -- the shape
    :class:`Repair` uses, so one counter reads both.
    """

    cells: list
    edits: list[str]


def repair(raw: str, max_edits: int = MAX_EDITS) -> Repair:
    """Parse ``raw`` as a layout array, moving one character per fault.

    :param raw: The model's answer as written.
    :param max_edits: How many edits to make before giving up.
    :returns: The :class:`Repair`. On a valid answer ``edits`` is
        empty.
    :rtype: Repair
    """
    text = raw
    edits: list[str] = []
    relaxed = False
    while True:
        try:
            value = json.loads(text, strict=not relaxed)
        except json.JSONDecodeError as exc:
            if len(edits) >= max_edits:
                return Repair(None, edits, _describe(text, exc, "edits spent"))
            if not relaxed and exc.msg == _CONTROL_CHARACTER_MESSAGE:
                # The one branch that changes the *mode* and not the
                # text. Setting the flag is what makes the loop
                # advance: with no edit to the text, a second strict
                # parse would raise this again forever. The recorded
                # edit is what ``max_edits`` counts, so a relaxed page
                # spends one of its three like any other repair.
                relaxed = True
                edits.append(f"relax_controls@{exc.pos}")
                continue
            repaired = _apply_arm(text, exc, strict=not relaxed)
            if repaired is None:
                return Repair(None, edits, _describe(text, exc, "no arm"))
            text, edit = repaired
            edits.append(edit)
            continue
        problem = _check_cells(value)
        if problem is not None:
            return Repair(None, edits, problem)
        return Repair(value, edits, None)


def rescale(
    cells: list,
    input_width: int,
    input_height: int,
    origin_width: int,
    origin_height: int,
) -> list:
    """Move the cells from the model's pixel space to the render's.

    Mirrors upstream ``post_process_cells``: each coordinate is divided
    by the input-to-origin ratio of its axis and truncated to an
    integer. The page dict stores the four dimensions, so a caller
    with no page image (the glue) rescales exactly as the worker
    would have.

    :param cells: The parsed array, in model space.
    :param input_width: Width of the image the model saw.
    :param input_height: Height of the image the model saw.
    :param origin_width: Width of the page render.
    :param origin_height: Height of the page render.
    :returns: New cell dicts with rescaled ``bbox`` values.
    :rtype: list
    """
    scale_x = input_width / origin_width
    scale_y = input_height / origin_height
    out = []
    for cell in cells:
        x1, y1, x2, y2 = cell["bbox"]
        copy = dict(cell)
        copy["bbox"] = [
            int(float(x1) / scale_x),
            int(float(y1) / scale_y),
            int(float(x2) / scale_x),
            int(float(y2) / scale_y),
        ]
        out.append(copy)
    return out


def legalize(
    cells: list,
    origin_width: float | None = None,
    origin_height: float | None = None,
) -> Legal:
    """Order every box, and drop the cells no reader can use.

    Issue #297. The model writes the four coordinates of a box in the
    order it likes, and upstream checks none of them: its own
    ``is_legal_bbox`` is never called on the success path, and
    ``post_process_cells`` divides both ends of an axis by the same
    scale, so a box that arrives upside down stays upside down. One
    such box on a ``Picture`` cell failed a whole page in production:
    upstream crops the page image for the markdown, and Pillow refuses
    a box whose bottom is above its top.

    So the rule runs on the first-pass cells as well, in the render's
    pixel space, and both callers share it: the worker before it builds
    the markdown, and the glue over every stored result, which no new
    worker image reaches.

    :func:`_check_cells` holds the same rule for a repaired array and
    answers it differently: there the whole array is refused. The two
    questions are not the same. A repair has already moved one
    character of the answer, and an array that then holds a box in the
    wrong order is evidence the edit landed wrong, so it is refused. An
    answer the parser took as written is the model's own layout of the
    page, and the two corners describe the region it meant whichever
    order they arrive in.

    A cell is dropped when nothing can be read from its box: no box of
    four numbers, no area after the order, or no overlap with the page.
    A dropped cell is one region of the page, and the page keeps every
    other one; the caller decides what an empty answer means.

    :param cells: The cells, in the render's pixel space.
    :param origin_width: Width of the render, for the page test. The
        test is skipped when either dimension is missing.
    :param origin_height: Height of the render.
    :returns: The :class:`Legal`. On legal cells ``edits`` is empty and
        ``cells`` is the list as given.
    :rtype: Legal
    """
    out: list = []
    edits: list[str] = []
    on_page = (
        isinstance(origin_width, (int, float))
        and isinstance(origin_height, (int, float))
        and origin_width > 0
        and origin_height > 0
    )
    for index, cell in enumerate(cells):
        bbox = cell.get("bbox") if isinstance(cell, dict) else None
        if (
            not isinstance(bbox, list)
            or len(bbox) != 4
            or not all(_is_number(value) for value in bbox)
        ):
            edits.append(f"no_bbox@{index}")
            continue
        x1, y1, x2, y2 = bbox
        marks = []
        if x2 < x1:
            x1, x2 = x2, x1
            marks.append(f"swap_x@{index}")
        if y2 < y1:
            y1, y2 = y2, y1
            marks.append(f"swap_y@{index}")
        if x2 <= x1 or y2 <= y1:
            edits.append(f"flat_box@{index}")
            continue
        if on_page and (
            x1 >= origin_width or y1 >= origin_height or x2 <= 0 or y2 <= 0
        ):
            edits.append(f"off_page@{index}")
            continue
        if marks:
            edits.extend(marks)
            copy = dict(cell)
            copy["bbox"] = [x1, y1, x2, y2]
            out.append(copy)
            continue
        out.append(cell)
    if not edits:
        # The list as given, so a caller can tell "nothing to do" from
        # "every cell was rebuilt" by identity as well as by ``edits``.
        return Legal(cells, [])
    return Legal(out, edits)


def excerpt(text: str, pos: int, radius: int = EXCERPT_RADIUS) -> str:
    """Return the text around ``pos``, marked with ``>>``.

    Every control character is written as its escape, not the line
    break alone. The fault of #268 **is** a control character, so this
    line is where one is read, and a raw carriage return or tab in a
    log line hides the very text a person came to read. Characters
    above the control range are kept as they are, so a paragraph mark
    or an accent survives.

    :param text: The answer.
    :param pos: The offset the parser reported.
    :param radius: Characters kept on each side.
    :returns: One line, safe to log.
    :rtype: str
    """
    start = max(0, pos - radius)
    end = min(len(text), pos + radius)
    window = text[start:pos] + ">>" + text[pos:end]
    return "".join(
        character
        if character >= " "
        else character.encode("unicode_escape").decode("ascii")
        for character in window
    )


# ── the arms ──────────────────────────────────────────────────────────


def _apply_arm(
    text: str, exc: json.JSONDecodeError, strict: bool = True
) -> tuple[str, str] | None:
    """Pick the arm for ``exc`` and apply it once.

    :param text: The text that failed to parse.
    :param exc: The parser's error.
    :param strict: The parse mode the caller is in. The two text arms
        read the text and need no parser; the extra-data arm parses
        again, and it must parse the way the caller did, or a relaxed
        page with a doubled closer fails on the control character the
        mode had already read.
    :returns: ``(repaired text, edit name)``, or ``None`` when no arm
        fits the message and the text at the offset.
    :rtype: tuple[str, str] | None
    """
    if exc.msg in _EARLY_CLOSE_MESSAGES:
        return _escape_quote(
            text, exc.pos, after_comma=exc.msg == _AFTER_COMMA_MESSAGE
        )
    if exc.msg == _INVALID_ESCAPE_MESSAGE:
        return _restore_quote(text, exc.pos)
    if exc.msg == _EXTRA_DATA_MESSAGE:
        return _cut_extra(text, strict=strict)
    return None


def _escape_quote(
    text: str, pos: int, after_comma: bool = False
) -> tuple[str, str] | None:
    """Put a backslash before the quotation mark that closed a string
    too early.

    The parser reports the first character it could not use, so the
    quotation mark is the nearest non-space character before ``pos``.
    With ``after_comma`` the parser had already taken one comma as a
    member separator, so the walk steps over that comma too --
    ``_AFTER_COMMA_MESSAGE`` says when.

    A quotation mark that a backslash already escapes cannot have
    closed the string, so the arm does not fit there. Nor does it fit
    a genuinely missing comma (``} {``), an unquoted key
    (``, category:``) or a doubled comma: the walk then lands on
    something that is not a quotation mark, and the page stays
    filtered.

    :param text: The text that failed to parse.
    :param pos: The offset the parser reported.
    :param after_comma: Whether to step over one member separator.
    :returns: ``(repaired text, edit name)``, or ``None``.
    :rtype: tuple[str, str] | None
    """
    i = _back_over_space(text, pos - 1)
    if after_comma and i >= 0 and text[i] == ",":
        i = _back_over_space(text, i - 1)
    if i < 0 or text[i] != '"' or _is_escaped(text, i):
        return None
    return text[:i] + "\\" + text[i:], f"escape_quote@{i}"


def _back_over_space(text: str, i: int) -> int:
    """Return the offset of the nearest non-space character at or before
    ``i``, or ``-1``.

    :param text: The text to walk.
    :param i: Where to start, walking backwards.
    :returns: The offset, or ``-1`` when only spaces precede it.
    :rtype: int
    """
    while i >= 0 and text[i].isspace():
        i -= 1
    return i


def _restore_quote(text: str, pos: int) -> tuple[str, str] | None:
    """Put a quotation mark after a backslash that escapes nothing.

    The parser reports the offset of the backslash. The page printed
    a quotation mark there (issue #242, scan 2702), so ``\\"`` is what
    the model meant to write.
    """
    if pos >= len(text) or text[pos] != "\\":
        return None
    return text[: pos + 1] + '"' + text[pos + 1 :], f"restore_quote@{pos}"


def _cut_extra(text: str, strict: bool = True) -> tuple[str, str] | None:
    """Keep the first complete value and drop what follows it.

    :param text: The text that failed to parse.
    :param strict: The caller's parse mode, passed to the decoder.
    :returns: ``(repaired text, edit name)``, or ``None``.
    :rtype: tuple[str, str] | None
    """
    try:
        _, end = json.JSONDecoder(strict=strict).raw_decode(text)
    except json.JSONDecodeError:
        return None
    return text[:end], f"cut_extra@{end}"


def _is_escaped(text: str, i: int) -> bool:
    """Return whether an odd run of backslashes precedes ``text[i]``."""
    count = 0
    j = i - 1
    while j >= 0 and text[j] == "\\":
        count += 1
        j -= 1
    return count % 2 == 1


# ── the checks ────────────────────────────────────────────────────────


def _check_cells(value) -> str | None:
    """Say what is wrong with a parsed value as a layout array.

    The shape upstream's success path needs: a list of dicts, each
    with a ``bbox`` of four numbers and a ``category`` string. A bbox
    is checked for legality here, in model space (``x2 > x1`` and
    ``y2 > y1``, upstream's ``is_legal_bbox``), so both callers share
    one rule and a repair that produced a degenerate box is refused.

    :param value: What ``json.loads`` returned.
    :returns: A short reason, or ``None`` when the value passes.
    :rtype: str | None
    """
    if not isinstance(value, list):
        return f"the answer is a {type(value).__name__}, not an array"
    if not value:
        # Upstream refuses an empty array too (``post_process_cells``
        # asserts a first cell), so a page whose repair produced one is
        # filtered on both sides. Without this the glue would call such
        # a page repaired, drop it out of ``filtered_pages`` and hand
        # the reader a page with no cell to read a number from.
        return "the repaired array holds no cell"
    for index, cell in enumerate(value):
        if not isinstance(cell, dict):
            return f"cell {index} is a {type(cell).__name__}, not an object"
        bbox = cell.get("bbox")
        if (
            not isinstance(bbox, list)
            or len(bbox) != 4
            or not all(_is_number(v) for v in bbox)
        ):
            return f"cell {index} has no bbox of four numbers: {bbox!r}"
        x1, y1, x2, y2 = bbox
        if x2 <= x1 or y2 <= y1:
            return f"cell {index} has an illegal bbox: {bbox!r}"
        if not isinstance(cell.get("category"), str):
            return f"cell {index} has no category"
    return None


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _describe(text: str, exc: json.JSONDecodeError, why: str) -> str:
    """Build the ``fault`` text for a parse nobody repaired.

    The message keeps the parser's own words, and one of them already
    ends in "at" (``Invalid control character at``), so the offset is
    joined without repeating it. That line is the whole triage path
    for a shape no arm reaches, and it is read by a person.
    """
    message = exc.msg.removesuffix(" at")
    return f"{message} at char {exc.pos} ({why}): {excerpt(text, exc.pos)}"
