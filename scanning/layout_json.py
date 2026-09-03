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

:func:`repair` applies one edit per parser message, parses again, and
stops after :data:`MAX_EDITS`. It never writes over the answer as the
model wrote it: the callers keep ``raw`` and store the edits beside the
repaired cells.

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
    while True:
        try:
            value = json.loads(text)
        except json.JSONDecodeError as exc:
            if len(edits) >= max_edits:
                return Repair(None, edits, _describe(text, exc, "edits spent"))
            repaired = _apply_arm(text, exc)
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


def excerpt(text: str, pos: int, radius: int = EXCERPT_RADIUS) -> str:
    """Return the text around ``pos``, marked with ``>>``.

    :param text: The answer.
    :param pos: The offset the parser reported.
    :param radius: Characters kept on each side.
    :returns: One line, safe to log.
    :rtype: str
    """
    start = max(0, pos - radius)
    end = min(len(text), pos + radius)
    window = text[start:pos] + ">>" + text[pos:end]
    return window.replace("\n", "\\n")


# ── the arms ──────────────────────────────────────────────────────────


def _apply_arm(text: str, exc: json.JSONDecodeError) -> tuple[str, str] | None:
    """Pick the arm for ``exc`` and apply it once.

    :param text: The text that failed to parse.
    :param exc: The parser's error.
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
        return _cut_extra(text)
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


def _cut_extra(text: str) -> tuple[str, str] | None:
    """Keep the first complete value and drop what follows it."""
    try:
        _, end = json.JSONDecoder().raw_decode(text)
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
    """Build the ``fault`` text for a parse nobody repaired."""
    return f"{exc.msg} at char {exc.pos} ({why}): {excerpt(text, exc.pos)}"
