"""The OCR ensemble over one opinion's engine documents (#365).

Every engine reads the whole volume, and ``opinion_ocr`` (#350) cuts
each read to one opinion and marks the text under a redaction or
outside the boundary. This module is the next step: it puts the
engines' units on the same pieces of the page, puts those pieces in
reading order, resolves each one to the read the engines agree on, and
writes the result to the ``OpinionText`` rows and to one document on
S3.

**The input is the opinion documents alone.** No volume document, no
PDF, no render and no row of review 2. So the work is a few small
reads and a geometry over about four pages, which is why it runs both
on the collect tick and in the request of the "Re run OCR ensemble"
button.

**One space.** Every unit of an opinion document carries ``box_pt``,
its box in the points of the volume page. The alignment, the order and
the boxes the viewer draws all use that space, and the redacted PDF of
the opinion is cut with ``insert_pdf``, which keeps the page size. So a
box of this document addresses the same place on the page of that PDF.

**Four steps, from the prototype** (the ``ai-research`` repository,
branch ``extraction_align``, ``pipeline/core/{align,order,consensus}``):

1. **Align.** Two units of different engines link when the
   intersection covers :data:`OVERLAP` of the smaller box:
   containment, not IoU, because a small box inside a big one is the
   same content and scores badly on IoU. A group is a connected
   component of those links, so one engine's three boxes and another's
   one box resolve in one pass. **A unit that reads nothing does not
   link**: a picture box over the body of a page would otherwise take
   every paragraph of the other engine into one group and read them
   across the gutter. Such a unit is attached to the group it covers
   most, as a silent engine, so a reader still learns that one engine
   read nothing there. The size of a unit decides nothing: a block
   over the whole body **is** the text of that body, and held apart it
   would write the page a second time.
2. **Order.** Three bands: the running heads, the body, the foot. The
   body splits at a column boundary taken from the **left edges** of
   the body boxes, which survives what a hunt for the gutter does not.
   A box that crosses the boundary is full width and restarts the
   order below it. One rule (:func:`reading_order`) orders the groups
   of a page **and** the members of one engine inside a group: a group
   can span the gutter, and members ordered by line alone read left
   one, right one, left two, right two.
3. **Resolve.** The engines' texts vote. A group they all read the
   same way is ``unanimous``; a group a majority reads the same way is
   ``majority``; a group with no majority is voted word by word; a
   group one engine saw is ``single``.
4. **Exclude.** A group any of whose units carries an ``exclusion`` is
   dropped, **after** the alignment and never before it. The ensemble
   experiment of #317 measured that: the engines' boxes differ, so a
   drop before the alignment leaves one-engine regions behind. The
   group goes whole, so a reading inside it that no exclusion covers
   is text the reader loses, and that is what the
   ``PARTIAL_REDACTION`` card counts.

**Three deviations from the prototype.**

- Its constants are pixels of a 1700 by 2200 render. Here they are
  fractions of the page frame, because a page of a volume is measured
  in points and no two volumes share a render.
- It stores HTML with ``<mark>`` elements. This module stores no
  markup: a voted group stores its words as tokens with a
  ``low_confidence`` flag, and the viewer builds the nodes. The portal
  escapes every label it draws, and stored markup would break that.
- It ranks the engines with a ``PRIORITY`` tuple. Here the order of
  ``opinion_ocr.ENGINES`` is that rank, so there is no second list.
- It compares the engines' text as they wrote it. Here the vote
  compares a key (:func:`compare_text`) and shows the winner's own
  text. Over one real opinion of seven pages, the quotes, the dashes,
  the ellipsis and the markdown marks alone made 16 of 77 groups vote
  word by word and marked 26 words low; with the key, 4 groups vote
  and 5 words are marked, and every one of those is a difference of
  reading.

**The ledger is on the row.** ``ensemble_revision ==
ocr_glue_revision`` is the one rule for "the ensemble describes the
documents that exist now" (:func:`is_written`), a query and never an S3
read. A fault of the row spends ``ensemble_attempts``, and at
:data:`MAX_ATTEMPTS` the row is ``ERROR``: loud, then quiet. The way
back is a run that works: the button, the command, or the next
approval, which raises the revision.

**The gate is the engine count.** ``settings.
OPINION_ENSEMBLE_MIN_ENGINES`` (3) is how many engine documents a row
must hold before the pass takes it, and ``Opinion.ocr_engine_count``
is that number, stamped by the OCR glue. A vote of two engines settles
nothing: every place they differ has no majority. Surya (#368) is the
third engine, so a volume whose run carries all three keys opens the
pass by itself. A volume read by two engines waits, and the command
``rerun_opinion_ensemble`` and the endpoint ``ensemble/rerun/`` waive
the gate for it. No page posts to the endpoint yet: the viewer of #365
puts the button on it.
"""

from __future__ import annotations

import logging
import re
import time
import unicodedata
from collections import Counter
from difflib import SequenceMatcher
from itertools import combinations

from django.conf import settings
from django.db import transaction
from django.db.models import F, Q
from django.utils import timezone

from scanning import detections, opinion_ocr, s3_sync
from scanning.models import (
    Issue,
    Opinion,
    OpinionCheck,
    OpinionFinding,
    OpinionFindingDismissal,
    OpinionReviewStatus,
    OpinionText,
    Status,
)

logger = logging.getLogger(__name__)

#: Version of the document this module writes.
SCHEMA_VERSION = 1

#: The file, beside the ``{engine}.json`` files of the OCR glue.
DOCUMENT = "ensemble.json"

#: The least share of the smaller box two units must share to link.
OVERLAP = 0.5

#: A group of this share of the page or more is reported as
#: ``page_scale``. It is a report and not a rule: the prototype holds
#: a unit of half the page out of the link graph, and half a page is a
#: size real text reaches (one Mistral block over the body of a
#: single-column page is about 0.68 of it). What must not link is a
#: unit that reads nothing, whatever its size.
MAX_AREA = 0.9

#: Below this worst pair IoU of the merged boxes a group is weak: the
#: link rule is permissive, and a text difference of a weak group may
#: be the alignment and not the engines.
WEAK_IOU = 0.3

#: The bands, as fractions of the page height. A box wholly above the
#: first is a running head; a box that starts below the second is a
#: footer.
TOP_BAND = 0.085
BOTTOM_BAND = 0.95

#: The least gap between two left edges, as a fraction of the page
#: width, that says a second column starts.
COLUMN_GAP = 0.1176

#: The boundary sits this fraction of the width left of the right
#: column's first edge.
EDGE_PAD = 0.0118

#: How far past the boundary a box must reach on both sides to be full
#: width rather than a column member that overshoots.
STRADDLE_L = 0.0235
STRADDLE_R = 0.0353

#: Boxes within this fraction of the height of each other are on one
#: line and read left to right: two running heads sit level, and a
#: point of noise must not decide which comes first.
LINE_BAND = 0.0091

#: The column boundary must lie between these fractions of the width.
MIN_BOUNDARY = 0.25
MAX_BOUNDARY = 0.85

#: A page with fewer body boxes reads as one column.
MIN_BODY_BOXES = 4

#: What joins two groups in a page's text.
PARAGRAPH_GAP = "\n\n"

#: How the engines agreed on one group.
UNANIMOUS = "unanimous"
MAJORITY = "majority"
VOTED = "voted"
SINGLE = "single"

#: Why a group is not in the text.
DROP_EXCLUDED = "excluded"
DROP_EMPTY = "empty"

#: The ``error`` of a page no engine measured. A page nobody read
#: carries the engine's own reason instead, and both write the one
#: ``PAGE_NOT_READ`` card: the page has no text at all.
UNMEASURED = "no engine measured this page"

#: The checks this module writes. One rebuild is their only writer,
#: and ``opinions.create_rows`` keeps the two stale ones.
ENSEMBLE_CHECKS = frozenset(
    {
        OpinionCheck.ENGINES_DISAGREE,
        OpinionCheck.NO_MAJORITY,
        OpinionCheck.PARTIAL_REDACTION,
        OpinionCheck.PAGE_NOT_READ,
    }
)

#: How many rows one tick writes. One row is three small reads and a
#: geometry over about four pages.
ENSEMBLE_PER_TICK = 10

#: Failed ticks on one row at one revision before the row is ERROR.
MAX_ATTEMPTS = 3

#: The start of every ``Opinion.error_message`` this module writes, so
#: a success clears its own message and nobody else's.
MESSAGE_PREFIX = "OCR ensemble: "

#: What the comparison reads as the same character. The two engines
#: differ here on almost every page of a real volume, and none of it is
#: a difference of reading: Mistral writes the curly quotes of the
#: page, dots.mocr writes the straight ones.
_SAME_CHARACTER = str.maketrans(
    {
        "\u2018": "'",
        "\u2019": "'",
        "\u201a": "'",
        "\u201b": "'",
        "\u2032": "'",
        "\u201c": '"',
        "\u201d": '"',
        "\u201e": '"',
        "\u2033": '"',
        "\u2013": "-",
        "\u2014": "-",
        "\u2212": "-",
        "\u00a0": " ",
    }
)

#: The markdown marks a comparison drops: a heading, an emphasis, a
#: code span. One engine writes ``## FACTS`` where the other writes
#: ``### FACTS``, and both read the same words.
_MARKUP = re.compile(r"[*_`~#]+")

#: A run of two periods or more, and a run of spaced periods: one
#: engine writes ``....`` where the other writes ``. . . .``.
_DOT_RUN = re.compile(r"\.(?:\s*\.)+")

_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")
#: Mistral writes an image placeholder as a block's whole text where
#: another engine emits an empty figure box: a placeholder, not content.
_MD_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")


#: What an :class:`EnsembleError` is about. The message of the error
#: names the object it read, which belongs in the log and on the row;
#: the button answers the line ``views_api`` keeps for the code, so a
#: key of the bucket never reaches a browser.
UNREADABLE = "unreadable"
NO_ENGINE = "no_engine"
SHORT_DOCUMENT = "short_document"


class EnsembleError(Exception):
    """A fact about one row: its text cannot be written.

    Spends an attempt on the row. The message goes to
    ``Opinion.error_message``.

    :param message: What failed, for the log and for the row.
    :param code: Which of :data:`UNREADABLE`, :data:`NO_ENGINE` and
        :data:`SHORT_DOCUMENT` it is, for the line the button shows.
    """

    def __init__(self, message: str, code: str = UNREADABLE) -> None:
        super().__init__(message)
        self.code = code


class TransientFault(Exception):
    """A fault that passes: a read or a write of the bucket failed.

    Spends nothing, the rule of ``opinion_pdf.TransientFault``. The
    collect tick runs every fifteen seconds, so a bucket that is away
    for a minute would otherwise spend every attempt of every due row
    and end them all in ERROR, which only a person can undo.

    A missing object is not this: the row says its OCR documents are
    written, so an object that is not there is a fact about the row.
    The retry costs three small reads and no page, so the row waits no
    cooldown: that is the one place this differs from #336, whose
    retry re-pulls a volume.
    """


class RevisionMoved(TransientFault):
    """The OCR glue wrote again while the text was written (#365).

    A fault that passes, and the caller treats it as one: the rows,
    the cards and the stamp of :func:`write` are taken back inside its
    transaction, the row keeps its old stamp, and the ensemble is due
    again. Nothing was written, so no caller may report a success.
    """


# ---------------------------------------------------------------------------
# The text of one unit
# ---------------------------------------------------------------------------


def plain(fragment: str | None) -> str:
    """Return one unit's content with none of its markup.

    A fragment that carries no reading is empty here, whatever its
    marks: a lone ``###`` is a heading mark with no heading, and a
    group of one such unit would otherwise write it into the page.
    :func:`compare_text` is the one rule for "this fragment reads
    nothing", and :func:`align_page` reads it too.

    :param fragment: The engine's text.
    :returns: The text, with the tags and the image placeholders gone
        and the whitespace collapsed; empty when it reads nothing.
    :rtype: str
    """
    if not fragment:
        return ""
    stripped = _TAG.sub(" ", _MD_IMAGE.sub(" ", fragment))
    shown = _WS.sub(" ", stripped).strip()
    return shown if compare_text(shown) else ""


def compare_text(text: str) -> str:
    """Return the text as the vote compares it, never as it is shown.

    **The comparison is not the text.** The engines agree about the
    words and differ about the typography: the quotes, the dashes, the
    ellipsis and the markdown marks. Over one real opinion of seven
    pages those differences alone made 16 of 77 groups vote word by
    word and marked 26 words low, and not one of them was a difference
    of reading. So the vote compares this key, and the winner's own
    text is what a reader sees.

    :param text: One engine's read.
    :returns: The key.
    :rtype: str
    """
    folded = unicodedata.normalize("NFKC", text).translate(_SAME_CHARACTER)
    folded = _DOT_RUN.sub("...", _MARKUP.sub("", folded))
    return _WS.sub(" ", folded).strip()


def compare_word(word: str) -> str:
    """Return one word as the vote compares it.

    The word-by-word twin of :func:`compare_text`. One key per word of
    the read, so the key list and the shown list stay side by side: a
    mark of its own becomes an empty key, and the vote drops it.

    :param word: One word of one engine's read.
    :returns: The key, which may be empty.
    :rtype: str
    """
    folded = unicodedata.normalize("NFKC", word).translate(_SAME_CHARACTER)
    return _DOT_RUN.sub("...", _MARKUP.sub("", folded)).strip()


def _pairs(text: str) -> list[tuple[str, str]]:
    """Return ``[(key, the word as it is shown)]`` for one read."""
    return [(compare_word(word), word) for word in text.split()]


# ---------------------------------------------------------------------------
# The geometry
# ---------------------------------------------------------------------------


def area(box: list[float]) -> float:
    """Return the area of one box.

    :param box: ``[x0, y0, x1, y1]``.
    :returns: The area.
    :rtype: float
    """
    return max(0.0, box[2] - box[0]) * max(0.0, box[3] - box[1])


def contained(a: list[float], b: list[float]) -> float:
    """Return the intersection as a share of the smaller box.

    Containment and not IoU: a small box inside a big one is the same
    content, and IoU calls it a stranger.

    :param a: One box.
    :param b: The other box.
    :returns: The share, 0 when either box has no area.
    :rtype: float
    """
    smaller = min(area(a), area(b))
    if smaller <= 0:
        return 0.0
    return opinion_ocr.intersection(a, b) / smaller


def iou(a: list[float], b: list[float]) -> float:
    """Return the plain IoU of two boxes.

    Read on the **merged** boxes alone, to score an alignment.

    :param a: One box.
    :param b: The other box.
    :returns: The IoU, 0 when the union has no area.
    :rtype: float
    """
    shared = opinion_ocr.intersection(a, b)
    union = area(a) + area(b) - shared
    return shared / union if union > 0 else 0.0


def _union_box(boxes: list[list[float]]) -> list[float]:
    """Return the box that holds every box of ``boxes``."""
    return [
        min(b[0] for b in boxes),
        min(b[1] for b in boxes),
        max(b[2] for b in boxes),
        max(b[3] for b in boxes),
    ]


class _Union:
    """Union-find over the units of one page."""

    def __init__(self, size: int) -> None:
        self.parent = list(range(size))

    def find(self, index: int) -> int:
        """Return the root of ``index``."""
        while self.parent[index] != index:
            self.parent[index] = self.parent[self.parent[index]]
            index = self.parent[index]
        return index

    def join(self, left: int, right: int) -> None:
        """Put two members in one group."""
        a, b = self.find(left), self.find(right)
        if a != b:
            self.parent[a] = b


# ---------------------------------------------------------------------------
# The alignment
# ---------------------------------------------------------------------------


def _merge(
    members: list[dict],
    line_band: float,
    boundary: float | None,
    width: float,
) -> dict:
    """Merge one engine's members of a group into one unit.

    **The members read in the order of the page**, columns included
    (:func:`reading_order`). One engine can read a page as one block
    while the other reads every paragraph of both columns, and those
    paragraphs are then the members of one group: ordered by line
    band alone they read across the gutter, left one, right one, left
    two, right two, and the vote turns that into a text no page has.

    :param members: The engine's units of the group.
    :param line_band: The height of one line band, in points.
    :param boundary: The column boundary of the page, or None.
    :param width: The page width, in points.
    :returns: The merged unit.
    :rtype: dict
    """
    ordered = reading_order(members, line_band, boundary, width)
    texts = [t for t in (plain(m["text"]) for m in ordered) if t]
    excluded = [m for m in ordered if m["exclusion"]]
    partial = [
        m
        for m in excluded
        if (m["exclusion"] or {}).get("reason") == "redaction"
        and m["share"] < opinion_ocr.FULL_SHARE
    ]
    return {
        "ids": [m["id"] for m in ordered],
        "types": [m["type"] for m in ordered],
        "box_pt": _round_box(_union_box([m["box_pt"] for m in ordered])),
        "text": " ".join(texts),
        "excluded": bool(excluded),
        "reason": (
            (excluded[0]["exclusion"] or {}).get("reason") if excluded else ""
        ),
        "partial": bool(partial),
        # Whether this engine read a word here that no exclusion
        # covers. A group is dropped whole, so a clean reading beside
        # an excluded one is text the reader loses, and that is what
        # the ``PARTIAL_REDACTION`` card counts (#365).
        "clean": any(plain(m["text"]) for m in ordered if not m["exclusion"]),
    }


def _round_box(box: list[float]) -> list[float]:
    """Return a box rounded the way the viewer draws it."""
    return [round(value, 2) for value in box]


def _group(
    by_engine: dict[str, list[dict]],
    line_band: float,
    page_area: float,
    boundary: float | None,
    width: float,
) -> dict:
    """Build one group from the units of each engine in it.

    :param by_engine: ``{engine: its units of this group}``.
    :param line_band: The height of one line band, in points.
    :param page_area: The area of the page, in square points.
    :param boundary: The column boundary of the page, or None.
    :param width: The page width, in points.
    :returns: The group.
    :rtype: dict
    """
    merged = {
        engine: _merge(members, line_band, boundary, width)
        for engine, members in by_engine.items()
    }
    boxes = [unit["box_pt"] for unit in merged.values()]
    worst = round(
        min((iou(a, b) for a, b in combinations(boxes, 2)), default=1.0), 3
    )
    excluded = [unit for unit in merged.values() if unit["excluded"]]
    box = _round_box(_union_box(boxes))
    return {
        "engines": merged,
        "present": _ranked(merged),
        "box_pt": box,
        "page_scale": area(box) >= MAX_AREA * page_area,
        "alignment_iou": worst,
        "weak": len(boxes) > 1 and worst < WEAK_IOU,
        "excluded": bool(excluded),
        "reason": excluded[0]["reason"] if excluded else "",
        # A group is dropped whole, so a reading no exclusion covers
        # beside an excluded one is text the reader loses, and the
        # card must say so (#365). The redaction over one cell of a
        # page-wide block is the daily shape of it: the cell is
        # covered whole, so the unit's own ``partial`` is false, and
        # the page would lose its body with no card at all.
        "partial": any(unit["partial"] for unit in merged.values())
        or bool(excluded)
        and any(unit["clean"] for unit in merged.values()),
    }


def _attach_quiet(
    quiet: list[dict],
    groups: list[dict],
    line_band: float,
    page_area: float,
    boundary: float | None,
    width: float,
) -> None:
    """Put each unit that reads nothing beside the group it covers.

    A unit with no reading never links, so it cannot chain a page. It
    is still a fact: one engine drew a box where another read the
    text, and ``resolve`` reports it in ``silent``. Each such unit goes
    to the one group it covers most, so a picture box over eight
    paragraphs is one mark and not eight. A unit that covers no group
    becomes a group of its own, which carries no text and is dropped.

    :param quiet: The units whose reading is empty.
    :param groups: The groups of the speaking units; changed in place.
    :param line_band: The height of one line band, in points.
    :param page_area: The area of the page, in square points.
    :param boundary: The column boundary of the page, or None.
    :param width: The page width, in points.
    :return: None.
    """
    attached: dict[int, dict[str, list[dict]]] = {}
    for unit in quiet:
        best, share = None, 0.0
        for index, group in enumerate(groups):
            if unit["engine"] in group["engines"]:
                continue
            covered = contained(unit["box_pt"], group["box_pt"])
            if covered > share:
                best, share = index, covered
        if best is None or share < OVERLAP:
            groups.append(
                _group(
                    {unit["engine"]: [unit]},
                    line_band,
                    page_area,
                    boundary,
                    width,
                )
            )
            continue
        attached.setdefault(best, {}).setdefault(unit["engine"], []).append(
            unit
        )
    for index, by_engine in attached.items():
        for engine, members in by_engine.items():
            # The box and the exclusion of the group stay the ones the
            # engines that read gave it: a silent box must not grow the
            # group, and a redaction over an empty box must not take
            # away text another engine read under its own verdict.
            groups[index]["engines"][engine] = _merge(
                members, line_band, boundary, width
            )
        groups[index]["present"] = _ranked(groups[index]["engines"])


def align_page(units: list[dict], width: float, height: float) -> list[dict]:
    """Return the aligned groups of one page, unordered.

    **A unit that reads nothing does not link.** The guard exists so
    that a box covering many others cannot chain the page into one
    group, and that is a picture box, which reads nothing. A big unit
    that **does** read is the text of what it covers, and it must link
    or the page is written twice. So the rule reads the presence of a
    reading and never its content: what the engines wrote still
    decides nothing about what merges. :func:`plain` is the one rule
    for "this unit reads nothing", because a picture box of Mistral
    carries an image placeholder and not an empty string.

    :param units: Every engine's units of the page, each with
        ``engine``, ``id``, ``box_pt``, ``text``, ``type``,
        ``exclusion`` and ``share``.
    :param width: The page width, in points.
    :param height: The page height, in points.
    :returns: The groups, each with ``engines``, ``present``,
        ``box_pt``, ``page_scale``, ``alignment_iou``, ``weak`` and the
        exclusion of its members.
    :rtype: list[dict]
    """
    line_band = LINE_BAND * height
    page_area = width * height
    # The columns of the page, off the units and not off the groups: a
    # group can span the gutter, and the members inside it read by
    # column like everything else (:func:`reading_order`).
    boundary = column_boundary(units, width, height)
    speaking, quiet = [], []
    for unit in units:
        # :func:`plain` and never ``compare_text``: Mistral writes a
        # picture box as an image placeholder and a break as a tag,
        # and both are text to a comparison. A box that reads nothing
        # must not link, whatever it wrote in place of the reading.
        (speaking if plain(unit["text"]) else quiet).append(unit)

    union = _Union(len(speaking))
    for left, right in combinations(range(len(speaking)), 2):
        if speaking[left]["engine"] == speaking[right]["engine"]:
            continue
        share = contained(speaking[left]["box_pt"], speaking[right]["box_pt"])
        if share >= OVERLAP:
            union.join(left, right)

    buckets: dict[int, list[int]] = {}
    for index in range(len(speaking)):
        buckets.setdefault(union.find(index), []).append(index)

    groups = []
    for indices in buckets.values():
        by_engine: dict[str, list[dict]] = {}
        for index in indices:
            by_engine.setdefault(speaking[index]["engine"], []).append(
                speaking[index]
            )
        groups.append(_group(by_engine, line_band, page_area, boundary, width))
    _attach_quiet(quiet, groups, line_band, page_area, boundary, width)
    return groups


def _ranked(engines) -> list[str]:
    """Return the engine names in the order the ensemble votes."""
    order = list(opinion_ocr.ENGINES)
    return sorted(engines, key=lambda name: _rank(name, order))


def _rank(name: str, order: list[str]) -> int:
    """Return one engine's place in the vote."""
    return order.index(name) if name in order else len(order)


# ---------------------------------------------------------------------------
# The reading order
# ---------------------------------------------------------------------------


def line_sort(boxes: list[dict], line_band: float) -> list[dict]:
    """Return boxes top to bottom, left to right inside a line band.

    Used for the head and the foot bands, and for the members of one
    group, where there are no columns to reason about.

    :param boxes: Dicts with ``box_pt``.
    :param line_band: The height of one band, in points.
    :returns: The boxes, ordered.
    :rtype: list[dict]
    """
    band = line_band if line_band > 0 else 1.0
    return sorted(
        boxes,
        key=lambda b: (round(b["box_pt"][1] / band), b["box_pt"][0]),
    )


def reading_order(
    boxes: list[dict], line_band: float, boundary: float | None, width: float
) -> list[dict]:
    """Return boxes in the reading order of one page's body.

    **The one rule**, and both levels call it: :func:`place` orders
    the groups of a page, and :func:`_merge` orders the members of one
    engine inside a group. A group can span the gutter, so the members
    inside it need the columns as much as the groups do.

    The left column reads before the right one, and a box that
    straddles the boundary is full width: it reads where it sits and
    the order restarts below it.

    :param boxes: Dicts with ``box_pt``.
    :param line_band: The height of one band, in points.
    :param boundary: The column boundary, or None for one column.
    :param width: The page width, in points.
    :returns: The boxes, ordered.
    :rtype: list[dict]
    """
    if boundary is None:
        return line_sort(boxes, line_band)
    band = line_band if line_band > 0 else 1.0
    separators = sorted(
        (b for b in boxes if _straddles(b["box_pt"], boundary, width)),
        key=lambda b: b["box_pt"][1],
    )
    rest = [b for b in boxes if not _straddles(b["box_pt"], boundary, width)]
    ordered: list[dict] = []
    top = -1.0
    for separator in [*separators, None]:
        cut = separator["box_pt"][1] if separator is not None else float("inf")
        segment = [b for b in rest if top <= b["box_pt"][1] < cut]
        segment.sort(
            key=lambda b: (
                _side(b["box_pt"], boundary),
                round(b["box_pt"][1] / band),
                b["box_pt"][0],
            )
        )
        ordered += segment
        if separator is not None:
            ordered.append(separator)
        top = cut
    return ordered


def _split_bands(
    groups: list[dict], height: float
) -> tuple[list[dict], list[dict], list[dict]]:
    """Return the head, the body and the foot of one page."""
    head, body, foot = [], [], []
    for group in groups:
        if group["box_pt"][3] < TOP_BAND * height:
            head.append(group)
        elif group["box_pt"][1] > BOTTOM_BAND * height:
            foot.append(group)
        else:
            body.append(group)
    return head, body, foot


def column_boundary(
    boxes: list[dict], width: float, height: float
) -> float | None:
    """Return the x that separates the two columns, or None.

    Only the body boxes vote: a running head and a footer straddle the
    gutter and would hide it. The boundary is the right cluster's first
    edge less a pad, not the middle of the gap, because the left
    column's text runs up to the gutter.

    The caller decides which boxes it asks about: :func:`align_page`
    asks about the units, which is every edge the page has, and
    :func:`place` about the groups it is placing.

    :param boxes: Dicts with ``box_pt``: the units, or the groups.
    :param width: The page width, in points.
    :param height: The page height, in points.
    :returns: The boundary, or None for one column.
    :rtype: float | None
    """
    _, body, _ = _split_bands(boxes, height)
    edges = sorted(box["box_pt"][0] for box in body)
    if len(edges) < MIN_BODY_BOXES:
        return None
    gap, right_edge = 0.0, None
    for left, right in zip(edges, edges[1:]):
        if right - left > gap:
            gap, right_edge = right - left, right
    if right_edge is None or gap < COLUMN_GAP * width:
        return None
    boundary = right_edge - EDGE_PAD * width
    if not MIN_BOUNDARY * width < boundary < MAX_BOUNDARY * width:
        return None
    if sum(1 for edge in edges if edge < boundary) < 2:
        return None
    if sum(1 for edge in edges if edge >= boundary) < 2:
        return None
    return boundary


def _straddles(box: list[float], boundary: float, width: float) -> bool:
    """Return whether a box crosses the boundary on both sides."""
    return (
        box[0] < boundary - STRADDLE_L * width
        and box[2] > boundary + STRADDLE_R * width
    )


def _side(box: list[float], boundary: float) -> str:
    """Return which column a box belongs to."""
    return "L" if (box[0] + box[2]) / 2 < boundary else "R"


def place(groups: list[dict], width: float, height: float) -> list[dict]:
    """Return the groups in reading order, each stamped.

    Every group is placed, the dropped ones included, because the
    column boundary is read off the boxes of the page and a page whose
    redacted blocks were taken out first would lose it.

    :param groups: The groups of one page.
    :param width: The page width, in points.
    :param height: The page height, in points.
    :returns: New dicts, with ``band`` and ``column``.
    :rtype: list[dict]
    """
    head, body, foot = _split_bands(groups, height)
    boundary = column_boundary(groups, width, height)
    line_band = LINE_BAND * height

    ordered = [
        {
            **group,
            "band": "body",
            "column": (
                None
                if boundary is None
                or _straddles(group["box_pt"], boundary, width)
                else _side(group["box_pt"], boundary)
            ),
        }
        for group in reading_order(body, line_band, boundary, width)
    ]

    return (
        [
            {**group, "band": "head", "column": None}
            for group in line_sort(head, line_band)
        ]
        + ordered
        + [
            {**group, "band": "foot", "column": None}
            for group in line_sort(foot, line_band)
        ]
    )


# ---------------------------------------------------------------------------
# The vote
# ---------------------------------------------------------------------------


def _candidates(
    base: list[tuple[str, str]], other: list[tuple[str, str]]
) -> tuple[dict[int, list[tuple[str, str]]], dict[int, list[list]]]:
    """Return one engine's reading of each word of the base read.

    Both sides are ``(key, the word as it is shown)`` pairs, and the
    alignment runs over the keys alone (:func:`compare_word`): the
    engines must not vote about a quote mark.

    A substitution maps word for word when the two spans have the same
    length; else the whole span is that engine's reading of the base's
    first position, which keeps a two-against-one word split from
    dropping words in silence.

    :param base: The base engine's pairs.
    :param other: The other engine's pairs.
    :returns: ``({position: readings}, {position: inserted runs})``.
    :rtype: tuple[dict, dict]
    """
    at: dict[int, list[tuple[str, str]]] = {}
    inserted: dict[int, list[list]] = {}
    base_keys = [key for key, _ in base]
    other_keys = [key for key, _ in other]
    matcher = SequenceMatcher(None, base_keys, other_keys, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            for index in range(i1, i2):
                at.setdefault(index, []).append(base[index])
        elif tag == "replace":
            if i2 - i1 == j2 - j1:
                for offset in range(i2 - i1):
                    at.setdefault(i1 + offset, []).append(other[j1 + offset])
            else:
                span = other[j1:j2]
                joined = (
                    " ".join(key for key, _ in span),
                    " ".join(word for _, word in span),
                )
                at.setdefault(i1, []).append(joined)
                for index in range(i1 + 1, i2):
                    at.setdefault(index, []).append(("", ""))
        elif tag == "delete":
            for index in range(i1, i2):
                at.setdefault(index, []).append(("", ""))
        elif tag == "insert":
            inserted.setdefault(i1, []).append(other[j1:j2])
    return at, inserted


def vote_words(
    base: list[tuple[str, str]], others: list[list[tuple[str, str]]]
) -> tuple[list[dict], int]:
    """Return the winning word at each position, as tokens.

    The vote runs over the keys and the answer carries the word as its
    engine wrote it. No markup: a token is ``{"text": word}``, and a
    word no majority settled carries ``"low_confidence": True``. The
    viewer builds the nodes from that.

    A word the base engine did not read is put in, marked and counted
    when the engines that read it are a majority or a tie: the base is
    the engine this module believes, so a word it did not read is a
    question for a person, and with two engines every difference is a
    tie. A word only a minority read is dropped, which is the reading
    of the majority.

    :param base: The base engine's ``(key, word)`` pairs.
    :param others: The other engines' pairs.
    :returns: ``(tokens, how many positions had no majority)``.
    :rtype: tuple[list[dict], int]
    """
    votes: list[dict[int, list[tuple[str, str]]]] = []
    inserts: list[dict[int, list[list]]] = []
    for other in others:
        at, inserted = _candidates(base, other)
        votes.append(at)
        inserts.append(inserted)

    # A strict majority of every reading, the base's own included, so
    # two engines never settle a word the third disputes.
    total = len(others) + 1
    quorum = (len(others) + 3) // 2
    tokens: list[dict] = []
    disputed = 0
    for position in range(len(base) + 1):
        runs: list[tuple[tuple[str, ...], tuple[str, ...]]] = [
            (
                tuple(key for key, _ in run),
                tuple(word for _, word in run),
            )
            for inserted in inserts
            for run in inserted.get(position, [])
        ]
        counted = Counter(keys for keys, _ in runs)
        for keys, count in counted.items():
            # A run the base does not have: ``count`` engines read it
            # and ``total - count`` did not. A majority of them puts it
            # in; a tie puts it in too, because the same tie on a word
            # the base **does** have is marked rather than dropped, and
            # two engines make every difference a tie. A minority is
            # dropped, the reading of the majority.
            if count < quorum and count * 2 != total:
                continue
            words = next(words for other, words in runs if other == keys)
            tokens += [
                {"text": word, "low_confidence": True} for word in words
            ]
            # A word the base did not read is marked, so it is counted
            # too: the mark the viewer draws and the card the findings
            # write read the same number.
            disputed += len(words)
        if position == len(base):
            break
        readings = [base[position]] + [
            reading for vote in votes for reading in vote.get(position, [])
        ]
        winner, count = Counter(key for key, _ in readings).most_common(1)[0]
        if count >= quorum:
            if winner:
                # "" is the reading of a majority that dropped the word,
                # and of a mark that carries no reading at all.
                tokens.append(
                    {
                        "text": next(
                            word for key, word in readings if key == winner
                        )
                    }
                )
        else:
            disputed += 1
            tokens.append({"text": base[position][1], "low_confidence": True})
    return tokens, disputed


def resolve(group: dict) -> dict:
    """Return the read of one aligned group.

    The engines vote over :func:`compare_text`, and the winner's own
    text is the answer: the vote must not turn a curly quote into a
    difference, and a reader must see the page as its engine read it.

    **An engine that read nothing does not vote.** One engine calls a
    region a picture and reads no word of it while another reads the
    paragraph under it. An empty read is not a reading of the text, so
    it is not a candidate and it cannot carry the vote; the engines
    that did read decide, and the silent ones are named in ``silent``,
    which makes the group a place the engines differ.

    :param group: One group of :func:`align_page`.
    :returns: ``{agreement, source, agreeing, silent, text, tokens,
        n_low_confidence}``. ``tokens`` is empty unless the group was
        voted word by word.
    :rtype: dict
    """
    engines = group["engines"]
    every = _ranked(engines)
    keys = {name: compare_text(engines[name]["text"]) for name in every}
    present = [name for name in every if keys[name]]
    silent = [name for name in every if not keys[name]]
    if not present:
        # Nobody read a word here. The group carries no text and
        # ``build_page`` drops it as empty.
        return {
            "agreement": SINGLE,
            "source": every[0],
            "agreeing": every,
            "silent": [],
            "text": engines[every[0]]["text"],
            "tokens": [],
            "n_low_confidence": 0,
        }

    base = present[0]
    if len(present) == 1:
        return {
            "agreement": SINGLE,
            "source": base,
            "agreeing": [base],
            "silent": silent,
            "text": engines[base]["text"],
            "tokens": [],
            "n_low_confidence": 0,
        }

    winner, votes = Counter(keys[name] for name in present).most_common(1)[0]
    quorum = (len(present) + 2) // 2
    if votes >= quorum:
        agreeing = [name for name in present if keys[name] == winner]
        return {
            "agreement": UNANIMOUS if votes == len(present) else MAJORITY,
            "source": agreeing[0],
            "agreeing": agreeing,
            "silent": silent,
            "text": engines[agreeing[0]]["text"],
            "tokens": [],
            "n_low_confidence": 0,
        }

    tokens, disputed = vote_words(
        _pairs(engines[base]["text"]),
        [_pairs(engines[name]["text"]) for name in present if name != base],
    )
    return {
        "agreement": VOTED,
        "source": base,
        "agreeing": [],
        "silent": silent,
        "text": " ".join(token["text"] for token in tokens if token["text"]),
        "tokens": tokens,
        "n_low_confidence": disputed,
    }


# ---------------------------------------------------------------------------
# The document
# ---------------------------------------------------------------------------


def _units_of(page: dict, engine: str) -> tuple[list[dict], list[dict]]:
    """Return one engine's units of one page, placed and unplaced.

    A unit with no ``box_pt`` cannot be aligned. It is ``unjudged``
    already, so it never reaches the text; it is reported as dropped.

    :param page: One page of an opinion document.
    :param engine: The engine's name.
    :returns: ``(the units with a box, the units without one)``.
    :rtype: tuple[list[dict], list[dict]]
    """
    placed, unplaced = [], []
    for unit in page.get("units") or []:
        if not isinstance(unit, dict):
            continue
        box = opinion_ocr.as_box(unit.get("box_pt"))
        entry = {
            "engine": engine,
            "id": unit.get("id"),
            "box_pt": box,
            "text": unit.get("text") or "",
            "type": unit.get("type") or "",
            "exclusion": unit.get("exclusion"),
            "share": unit.get("share") or 0.0,
        }
        (placed if box else unplaced).append(entry)
    return placed, unplaced


def _frame(pages: dict[str, dict]) -> tuple[float, float] | None:
    """Return the page size in points, off the first engine that has it.

    Every engine's page carries the same size: ``opinion_ocr`` measures
    it from the detections or the dots.mocr render, and the engine only
    decides the render the box came from.

    :param pages: ``{engine: the page}``.
    :returns: ``(width, height)``, or None when no engine measured it.
    :rtype: tuple[float, float] | None
    """
    for page in pages.values():
        frame = page.get("frame") or {}
        width, height = frame.get("width_pt"), frame.get("height_pt")
        if width and height:
            return float(width), float(height)
    return None


def _counts() -> dict:
    """Return an empty count of one page or of one document.

    ``differing`` is one per group the engines did not read alike, and
    it is the number the ``ENGINES_DISAGREE`` card reads: a group can
    be voted and hold a silent engine at once, and it is one place,
    not two.

    :returns: Every count at zero.
    :rtype: dict
    """
    return {
        "groups": 0,
        "dropped": 0,
        UNANIMOUS: 0,
        MAJORITY: 0,
        VOTED: 0,
        SINGLE: 0,
        "silent": 0,
        "differing": 0,
        "low_confidence": 0,
        "partial": 0,
    }


def build_page(pages: dict[str, dict], page_in_opinion: int) -> dict:
    """Return the ensemble of one page of one opinion.

    :param pages: ``{engine: the page of that engine's document}``.
    :param page_in_opinion: The 0-based page of the opinion.
    :returns: The page entry of the document.
    :rtype: dict
    """
    first = next(iter(pages.values()))
    read = {
        engine: page for engine, page in pages.items() if "error" not in page
    }
    entry = {
        "page_in_opinion": page_in_opinion,
        "page_index": first.get("page_index"),
        "pdf_page": first.get("pdf_page"),
        "source": first.get("source"),
        "frame": None,
        "text": "",
        # Every engine of the document, and the ones whose read of
        # this page failed (#238 does fail single pages). A group of
        # fewer engines than this is a place they did not read alike,
        # the rule of :func:`_differs`, so a page one engine did not
        # read says so on every group of it.
        "engines": list(pages),
        "missing": [engine for engine in pages if engine not in read],
        "groups": [],
        "dropped": [],
        "counts": _counts(),
    }
    if not read:
        entry["error"] = first.get("error") or "no engine read this page"
        return entry

    size = _frame(read)
    units: list[dict] = []
    for engine, page in read.items():
        placed, unplaced = _units_of(page, engine)
        units += placed
        for unit in unplaced:
            entry["dropped"].append(
                {
                    "engines": {engine: [unit["id"]]},
                    "box_pt": None,
                    "reason": (unit["exclusion"] or {}).get("reason")
                    or DROP_EXCLUDED,
                    "partial": False,
                }
            )
    if size is None:
        # No detection and no render measured this page, so no box of
        # it is in points and nothing can be aligned. Every unit is
        # unjudged already, the rule of ``opinion_ocr.verdict``. The
        # page carries a reason like a page nobody read, because a
        # page with no text and no card is a page lost in silence.
        entry["error"] = UNMEASURED
        entry["counts"]["dropped"] = len(entry["dropped"])
        return entry

    width, height = size
    entry["frame"] = {
        "width_pt": round(width, 2),
        "height_pt": round(height, 2),
    }
    ordered = place(align_page(units, width, height), width, height)

    parts: list[str] = []
    offset = 0
    for group in ordered:
        read_back = resolve(group)
        if group["excluded"] or not read_back["text"]:
            entry["dropped"].append(
                {
                    "engines": {
                        name: unit["ids"]
                        for name, unit in group["engines"].items()
                    },
                    "box_pt": group["box_pt"],
                    "reason": (
                        group["reason"] or DROP_EXCLUDED
                        if group["excluded"]
                        else DROP_EMPTY
                    ),
                    "partial": group["partial"],
                }
            )
            continue
        start = offset
        end = start + len(read_back["text"])
        offset = end + len(PARAGRAPH_GAP)
        parts.append(read_back["text"])
        entry["groups"].append(
            {
                "id": len(entry["groups"]),
                "band": group["band"],
                "column": group["column"],
                "box_pt": group["box_pt"],
                "start": start,
                "end": end,
                "agreement": read_back["agreement"],
                "source": read_back["source"],
                "agreeing": read_back["agreeing"],
                "alignment_iou": group["alignment_iou"],
                "weak": group["weak"],
                "page_scale": group["page_scale"],
                "silent": read_back["silent"],
                "n_low_confidence": read_back["n_low_confidence"],
                "tokens": read_back["tokens"],
                "text": read_back["text"],
                "engines": {
                    name: {
                        "ids": unit["ids"],
                        "box_pt": unit["box_pt"],
                        "text": unit["text"],
                    }
                    for name, unit in group["engines"].items()
                },
            }
        )
        entry["counts"][read_back["agreement"]] += 1
        entry["counts"]["low_confidence"] += read_back["n_low_confidence"]
        if read_back["silent"]:
            entry["counts"]["silent"] += 1
        if _differs(entry["groups"][-1], len(pages)):
            entry["counts"]["differing"] += 1

    entry["text"] = PARAGRAPH_GAP.join(parts)
    entry["counts"]["groups"] = len(entry["groups"])
    entry["counts"]["dropped"] = len(entry["dropped"])
    entry["counts"]["partial"] = sum(
        1 for drop in entry["dropped"] if drop["partial"]
    )
    return entry


def _differs(group: dict, engines: int) -> bool:
    """Return whether the engines did not read one group alike.

    One rule for the card of the findings and for the entry of
    ``OpinionText.disagreements``, which must count the same places.

    A group **no** other engine has a box for counts too: one engine
    read a block and the other drew nothing there, which is a place a
    person must look at, and it is the shape a page one engine did not
    read takes on every group of it.

    :param group: One group of the document.
    :param engines: How many engines the document holds.
    :returns: Whether it is a disagreement.
    :rtype: bool
    """
    if group["agreement"] in (MAJORITY, VOTED) or group.get("silent"):
        return True
    return len(group.get("engines") or {}) < engines


def build_document(opinion: Opinion, documents: dict[str, dict]) -> dict:
    """Return the ensemble document of one opinion.

    :param opinion: The row.
    :param documents: ``{engine: the opinion document of that engine}``.
    :returns: The document.
    :rtype: dict
    :raises EnsembleError: When an engine document lacks a page of the
        opinion.
    """
    engines = [name for name in opinion_ocr.ENGINES if name in documents]
    if not engines:
        raise EnsembleError("this opinion has no engine document", NO_ENGINE)
    by_page: dict[str, dict[int, dict]] = {}
    for engine in engines:
        by_page[engine] = {
            page.get("page_in_opinion"): page
            for page in documents[engine].get("pages") or []
            if isinstance(page, dict)
        }

    pages = []
    counts = _counts()
    for page_in_opinion in range(opinion.page_count):
        of_page = {}
        for engine in engines:
            page = by_page[engine].get(page_in_opinion)
            if page is None:
                raise EnsembleError(
                    f"the {engine} document has no page "
                    f"{page_in_opinion + 1} of this opinion",
                    SHORT_DOCUMENT,
                )
            of_page[engine] = page
        entry = build_page(of_page, page_in_opinion)
        for key, value in entry["counts"].items():
            counts[key] += value
        pages.append(entry)

    return {
        "schema_version": SCHEMA_VERSION,
        "scan_pk": opinion.scan_id,
        "opinion": {
            "first_printed_page": opinion.first_printed_page,
            "index_in_page": opinion.index_in_page,
            "last_printed_page": opinion.last_printed_page,
            "page_count": opinion.page_count,
            "glue_revision": opinion.glue_revision,
        },
        "apply_run": (opinion.apply_run.label if opinion.apply_run_id else ""),
        "engines": engines,
        "params": {
            "overlap": OVERLAP,
            "max_area": MAX_AREA,
            "weak_iou": WEAK_IOU,
        },
        "generated_at": timezone.now().isoformat(),
        "pages": pages,
        "counts": counts,
    }


# ---------------------------------------------------------------------------
# The keys and the ledger
# ---------------------------------------------------------------------------


def document_key(opinion: Opinion) -> str:
    """Return the S3 key of one opinion's ensemble document.

    :param opinion: The row.
    :returns: The key.
    :rtype: str
    """
    prefix = s3_sync.s3_processing_prefix(opinion.scan)
    return f"{prefix}{opinion.glue_prefix}{DOCUMENT}"


def is_written(opinion: Opinion) -> bool:
    """Return whether the ensemble describes the documents that exist.

    **The one rule**, read off the row and never off the bucket. Both
    stamps must name the live revision: an ensemble of the documents of
    an older revision is not the text of this opinion.

    :param opinion: The row.
    :returns: Whether the text is current.
    :rtype: bool
    """
    return (
        opinion_ocr.is_written(opinion)
        and opinion.ensemble_revision is not None
        and opinion.ensemble_revision == opinion.ocr_glue_revision
    )


def min_engines() -> int:
    """Return how many engine documents the pass waits for.

    :returns: ``settings.OPINION_ENSEMBLE_MIN_ENGINES``.
    :rtype: int
    """
    return int(getattr(settings, "OPINION_ENSEMBLE_MIN_ENGINES", 3))


def due(limit: int | None = None):
    """Return the rows that owe their ensemble, as a queryset.

    A row whose OCR glue is written at the live revision, that holds
    at least :func:`min_engines` engine documents, that is neither
    ``ERROR`` nor ``TEXT_REVIEW_DONE``, that has attempts left, and
    whose ensemble stamp is not the OCR glue's. The scan must be in
    ``REDACTION_REVIEW_DONE``: a volume an admin sent back writes no
    text while it is back.

    An approved row is out for the reason ``opinion_ocr.reglue`` and
    ``opinions.create_rows`` leave it alone: a person read its text
    and said it is right, and nothing derived writes over that.

    :param limit: How many rows to take, newest scan first.
    :returns: The queryset.
    """
    rows = (
        Opinion.objects.filter(
            scan__status=Status.REDACTION_REVIEW_DONE,
            ensemble_attempts__lt=MAX_ATTEMPTS,
            ocr_glue_revision__isnull=False,
            ocr_glue_revision=F("glue_revision"),
            ocr_engine_count__gte=min_engines(),
        )
        .exclude(
            status__in=(
                OpinionReviewStatus.ERROR,
                OpinionReviewStatus.TEXT_REVIEW_DONE,
            )
        )
        .filter(
            Q(ensemble_revision__isnull=True)
            | ~Q(ensemble_revision=F("ocr_glue_revision"))
        )
        .order_by("-scan_id", "first_printed_page", "index_in_page")
    )
    return rows[:limit] if limit else rows


def record_failure(opinion: Opinion, message: str) -> None:
    """Spend one attempt on the row, and end it at the cap.

    :param opinion: The row.
    :param message: What failed, for ``error_message``.
    :return: None.
    """
    Opinion.objects.filter(pk=opinion.pk).update(
        ensemble_attempts=F("ensemble_attempts") + 1
    )
    # The message is shared with every work that prepares the review,
    # so this one never writes over another work's ERROR: a row the
    # PDF pass ended must keep the reason it ended, which is the only
    # record an operator has.
    Opinion.objects.filter(pk=opinion.pk).exclude(
        Q(status=OpinionReviewStatus.ERROR)
        & ~Q(error_message__startswith=MESSAGE_PREFIX)
    ).update(error_message=f"{MESSAGE_PREFIX}{message}")
    ended = (
        Opinion.objects.filter(
            pk=opinion.pk, ensemble_attempts__gte=MAX_ATTEMPTS
        )
        .exclude(status=OpinionReviewStatus.TEXT_REVIEW_DONE)
        .update(status=OpinionReviewStatus.ERROR)
    )
    if ended:
        logger.error(
            "%s of scan %s: the ensemble failed %d times; the row is ERROR "
            "until the next approval. Last: %s",
            opinion,
            opinion.scan_id,
            MAX_ATTEMPTS,
            message,
        )
    else:
        logger.warning(
            "%s of scan %s: the ensemble failed: %s",
            opinion,
            opinion.scan_id,
            message,
        )


# ---------------------------------------------------------------------------
# The rows
# ---------------------------------------------------------------------------


def _address(page: dict) -> tuple[int | None, int | None]:
    """Return the durable address of one page of the corrected volume.

    ``detections.source_of_entry`` is the one rule for this shape, and
    it is called and never copied: the apply writes the page of an
    edit 0-based inside that edit's shard (a rotation writes 0), and
    every stored address is 1-based. A copy of the rule here would put
    the ``OpinionText`` row one page below the ``Detection``, the
    boundary and the opinion at the same address.

    :param page: One page of the document.
    :returns: ``(the page edit's pk or None, the 1-based page of its
        source)``.
    :rtype: tuple[int | None, int | None]
    """
    return detections.source_of_entry(page)


def write_rows(opinion: Opinion, document: dict) -> int:
    """Write the ``OpinionText`` rows of one opinion.

    One row per page. ``text`` and ``disagreements`` are written again
    at every run, because they are a cache of the documents; nothing
    here reads or writes ``human_text``, which is the truth.

    :param opinion: The row.
    :param document: :func:`build_document`.
    :returns: How many rows were written.
    :rtype: int
    """
    written = 0
    for page in document["pages"]:
        source_edit_id, source_page = _address(page)
        OpinionText.objects.update_or_create(
            opinion=opinion,
            page_in_opinion=page["page_in_opinion"],
            defaults={
                "text": page["text"],
                "disagreements": _disagreements(page),
                "source_edit_id": source_edit_id,
                "source_page": source_page,
                "page_index": page["page_index"],
                "apply_run_id": opinion.apply_run_id,
            },
        )
        written += 1
    # A row of a page the opinion no longer has. An opinion does grow
    # shorter: ``opinions.create_rows`` writes ``page_count`` again on
    # a matched row, so a boundary a curator moves up takes a page off
    # the end. The derived text of that page goes, and a row a curator
    # typed stays: ``human_text`` is the truth and nothing discards it
    # (#335).
    OpinionText.objects.filter(
        opinion=opinion,
        page_in_opinion__gte=len(document["pages"]),
        human_text="",
    ).delete()
    return written


def _disagreements(page: dict) -> list[dict]:
    """Return one entry per place the engines did not all agree.

    :param page: One page of the document.
    :returns: ``[{start, end, agreement, variants}]``.
    :rtype: list[dict]
    """
    engines = len(page.get("engines") or [])
    return [
        {
            "start": group["start"],
            "end": group["end"],
            "agreement": group["agreement"],
            "variants": {
                name: unit["text"] for name, unit in group["engines"].items()
            },
        }
        for group in page["groups"]
        if _differs(group, engines)
    ]


# ---------------------------------------------------------------------------
# The findings
# ---------------------------------------------------------------------------


def rebuild_findings(opinion: Opinion, document: dict) -> int:
    """Write the findings of the ensemble again, from the document.

    **The one writer** of :data:`ENSEMBLE_CHECKS`. It deletes and
    writes those three checks alone, so the two stale checks of
    ``opinions.create_rows`` stay where they are. A standing dismissal
    of the same page and check mutes the new card, the rule of
    ``findings.resolve``; nothing deletes a dismissal.

    :param opinion: The row.
    :param document: :func:`build_document`.
    :returns: How many findings were written.
    :rtype: int
    """
    OpinionFinding.objects.filter(
        opinion=opinion, check_name__in=ENSEMBLE_CHECKS
    ).delete()
    standing = {
        (row.page_in_opinion, row.check_name): row
        for row in OpinionFindingDismissal.objects.filter(
            opinion=opinion, withdrawn_at__isnull=True
        )
    }
    cards = []
    for page in document["pages"]:
        page_number = page["page_in_opinion"]
        counts = page["counts"]
        # Every group the engines did not read alike (``_differs``):
        # the ones a majority settled, the ones the word vote settled,
        # and the ones an engine read nothing of. It is the count of
        # ``OpinionText.disagreements`` for this page, and a card and
        # an entry must not disagree. With two engines no group can
        # hold a majority, so a card that read ``counts[MAJORITY]``
        # alone would never be written.
        if page.get("error"):
            # The page has no text at all, so it has no group to
            # count and no other card to write.
            cards.append(
                _card(
                    opinion,
                    page_number,
                    OpinionCheck.PAGE_NOT_READ,
                    Issue.Severity.ERROR,
                    _unread_message(page),
                    standing,
                )
            )
            continue
        differing = counts["differing"]
        missing = page.get("missing") or []
        if differing or missing:
            cards.append(
                _card(
                    opinion,
                    page_number,
                    OpinionCheck.ENGINES_DISAGREE,
                    Issue.Severity.WARNING,
                    _disagree_message(differing, missing),
                    standing,
                )
            )
        if counts["low_confidence"]:
            cards.append(
                _card(
                    opinion,
                    page_number,
                    OpinionCheck.NO_MAJORITY,
                    Issue.Severity.ERROR,
                    f"{counts['low_confidence']} word(s) on this page have "
                    "no majority. Read them against the PDF.",
                    standing,
                )
            )
        if counts["partial"]:
            cards.append(
                _card(
                    opinion,
                    page_number,
                    OpinionCheck.PARTIAL_REDACTION,
                    Issue.Severity.WARNING,
                    _partial_message(page),
                    standing,
                )
            )
    OpinionFinding.objects.bulk_create(cards)
    return len(cards)


#: What took a block out of the text, in words. ``opinion_ocr``
#: writes both reasons, and a card must not call the mask of the
#: opinion before a redaction.
_REASON_WORDS = {
    "redaction": "a redaction",
    "outside": "the mask of the opinion before",
}


def _unread_message(page: dict) -> str:
    """Return the line of one ``PAGE_NOT_READ`` card.

    :param page: One page of the document.
    :returns: The message.
    :rtype: str
    """
    if page.get("error") == UNMEASURED:
        return (
            "No engine measured this page, so no box of it is in points "
            "and nothing could be aligned. The page has no text."
        )
    return "No engine read this page, so it has no text."


def _partial_message(page: dict) -> str:
    """Return the line of one ``PARTIAL_REDACTION`` card.

    The reason is read off the drops the count came from: a group goes
    whole, and the box that took it is a redaction or the mask of the
    opinion before (``opinion_ocr.verdict`` writes both).

    :param page: One page of the document.
    :returns: The message.
    :rtype: str
    """
    count = page["counts"]["partial"]
    reasons = sorted(
        {
            _REASON_WORDS.get(drop["reason"], drop["reason"])
            for drop in page["dropped"]
            if drop["partial"]
        }
    )
    said = " or ".join(reasons) or "a box"
    return (
        f"{said[0].upper()}{said[1:]} covers part of {count} block(s) on "
        "this page. The whole block is out of the text."
    )


def _disagree_message(differing: int, missing: list[str]) -> str:
    """Return the line of one ``ENGINES_DISAGREE`` card.

    An engine that did not read the page at all is named: the reader
    must know that the text of the page comes from the others, and the
    count alone does not say it.

    :param differing: How many groups the engines did not read alike.
    :param missing: The engines whose read of the page failed.
    :returns: The message.
    :rtype: str
    """
    named = ", ".join(missing)
    if missing and not differing:
        return (
            f"{named} did not read this page. The text comes from the "
            "engine(s) that did."
        )
    count = (
        f"The engines differ in {differing} place(s) on this page. The "
        "vote picked a read for each."
    )
    if missing:
        return f"{named} did not read this page. {count}"
    return count


def _card(
    opinion: Opinion,
    page_in_opinion: int,
    check: str,
    severity: str,
    message: str,
    standing: dict,
) -> OpinionFinding:
    """Return one finding, muted when a dismissal names it."""
    return OpinionFinding(
        opinion=opinion,
        page_in_opinion=page_in_opinion,
        check_name=check,
        severity=severity,
        message=message,
        dismissal=standing.get((page_in_opinion, check)),
    )


# ---------------------------------------------------------------------------
# The write
# ---------------------------------------------------------------------------


def _is_row_fault(exc: Exception) -> bool:
    """Return whether a failed read is a fact about the row.

    An object that is not there, and an object that is not JSON: both
    say the row must be glued again, and both fail the same way at
    every retry. Everything else is the bucket, and the bucket comes
    back.

    **Known limit**, the one ``opinion_pdf`` documents: a permission
    fault reads as transient here, so a bucket policy that forbids the
    read holds the rows out of ERROR and logs a warning every tick
    instead. That is the safer way round, and the log says so.

    :param exc: What the read raised.
    :returns: Whether the row must answer for it.
    :rtype: bool
    """
    if isinstance(exc, (KeyError, ValueError)):
        return True
    error = (getattr(exc, "response", None) or {}).get("Error") or {}
    return str(error.get("Code", "")) in {"NoSuchKey", "404"}


def _read(key: str, code: str = UNREADABLE) -> dict:
    """Read one JSON object, and say which kind of fault stopped it.

    :param key: The object key.
    :param code: The code of the error a fault of the row raises.
    :returns: The document.
    :rtype: dict
    :raises EnsembleError: When the object is missing or is not JSON.
    :raises TransientFault: When the read itself failed.
    """
    try:
        return s3_sync.download_json_object(key)
    except Exception as exc:
        if _is_row_fault(exc):
            raise EnsembleError(
                f"the object at {key} is not in the bucket, or is not a "
                "document this module can read",
                code,
            )
        raise TransientFault(f"the read of {key} failed: {exc}") from exc


def load_documents(opinion: Opinion) -> dict[str, dict]:
    """Read one opinion's engine documents off S3.

    The manifest names the engines the glue wrote, so the read asks for
    no file the glue did not write.

    :param opinion: The row.
    :returns: ``{engine: the document}``.
    :rtype: dict[str, dict]
    :raises EnsembleError: When an object is missing or is not a
        document, or when the manifest names no engine this module
        knows.
    :raises TransientFault: When a read failed.
    """
    manifest_key = opinion_ocr.engine_key(opinion, "manifest")
    manifest = _read(manifest_key)
    names = [
        name
        for name in opinion_ocr.ENGINES
        if name in (manifest.get("engines") or {})
    ]
    if not names:
        raise EnsembleError(
            f"the manifest at {manifest_key} names no engine", NO_ENGINE
        )
    documents = {}
    for name in names:
        key = opinion_ocr.engine_key(opinion, name)
        document = _read(key)
        if not isinstance(document, dict) or "pages" not in document:
            raise EnsembleError(
                f"the object at {key} is not a document", UNREADABLE
            )
        documents[name] = document
    return documents


def write(opinion: Opinion, documents: dict[str, dict]) -> dict:
    """Write one opinion's text, findings and ensemble document.

    The document goes up first, then the rows and the findings and the
    stamp in one transaction, under a lock on the row: a revision that
    moved during the write wins the compare-and-swap, the rows and the
    cards go back, and the ensemble is due again.

    :param opinion: The row.
    :param documents: :func:`load_documents`.
    :returns: The document.
    :rtype: dict
    :raises EnsembleError: On a fact about the row.
    :raises TransientFault: When the upload failed.
    :raises RevisionMoved: When the OCR glue wrote again during this
        write, which keeps nothing.
    """
    document = build_document(opinion, documents)
    key = document_key(opinion)
    if not s3_sync.upload_json_object(key, document):
        raise TransientFault(f"the upload to {key} failed")

    try:
        with transaction.atomic():
            # One writer at a time on this row's text. The tick, the
            # button and the command can all be in this block at once,
            # and ``update_or_create`` between two of them is a lost
            # select and an ``IntegrityError``. The lock is held over
            # row writes alone: no HTTP call is inside it.
            Opinion.objects.select_for_update().filter(pk=opinion.pk).exists()
            write_rows(opinion, document)
            rebuild_findings(opinion, document)
            stamped = Opinion.objects.filter(
                pk=opinion.pk, ocr_glue_revision=opinion.ocr_glue_revision
            ).update(
                ensemble_revision=opinion.ocr_glue_revision,
                ensemble_attempts=0,
            )
            if not stamped:
                # The glue wrote the documents again while this ran, so
                # the rows and the cards above describe documents that
                # are gone. Take them back and leave the ensemble due.
                raise RevisionMoved(
                    "the OCR glue wrote again while the text was written"
                )
            # An ERROR this module wrote is answered by this success:
            # the row is readable again, and the operator who pressed
            # the button or ran the command is the one who decided
            # that. The message says whose ERROR it is, so no other
            # work's failure is cleared here.
            Opinion.objects.filter(
                pk=opinion.pk,
                status=OpinionReviewStatus.ERROR,
                error_message__startswith=MESSAGE_PREFIX,
            ).update(status=OpinionReviewStatus.PROCESSING)
            Opinion.objects.filter(
                pk=opinion.pk, error_message__startswith=MESSAGE_PREFIX
            ).update(error_message="")
    except RevisionMoved:
        logger.info(
            "%s of scan %s: the OCR glue moved while the text was written; "
            "the rows went back and the ensemble is due again",
            opinion,
            opinion.scan_id,
        )
        raise
    return document


def rerun(opinion: Opinion) -> dict:
    """Read the documents of one opinion and write its text again.

    The body of the endpoint and of the command. It waives the engine
    gate, which is the only way a two-engine volume is read.

    :param opinion: The row.
    :returns: The document.
    :rtype: dict
    :raises EnsembleError: On a fact about the row.
    :raises TransientFault: When a read or the upload failed.
    """
    return write(opinion, load_documents(opinion))


# ---------------------------------------------------------------------------
# The pass
# ---------------------------------------------------------------------------


def run_tick(limit: int = ENSEMBLE_PER_TICK) -> int:
    """Write the text of up to ``limit`` rows that owe it.

    The eleventh pass of the collect tick. The rows are walked newest
    scan first, the rule of ``opinion_ocr.glue_due``. A fault of one
    row spends that row's attempt and the pass goes on; a
    :class:`TransientFault` spends nothing and the row is due again on
    the next tick.

    :param limit: How many rows to write.
    :returns: How many rows were written.
    :rtype: int
    """
    if not s3_sync.s3_active():
        return 0
    rows = list(due(limit).select_related("scan", "apply_run"))
    if not rows:
        return 0
    started = time.monotonic()
    written = 0
    for opinion in rows:
        try:
            document = rerun(opinion)
        except RevisionMoved:
            # Nothing was written and nothing was spent. The row is due
            # at the revision the glue has now.
            continue
        except TransientFault as exc:
            logger.warning(
                "%s of scan %s: the ensemble waits for the bucket: %s",
                opinion,
                opinion.scan_id,
                exc,
            )
            continue
        except EnsembleError as exc:
            record_failure(opinion, str(exc))
            continue
        except Exception:
            logger.exception(
                "%s of scan %s: the ensemble raised",
                opinion,
                opinion.scan_id,
            )
            record_failure(opinion, "the ensemble raised; see the log")
            continue
        written += 1
        logger.info(
            "%s of scan %s: the text was written from %s (%d group(s), "
            "%d dropped, %d low-confidence word(s))",
            opinion,
            opinion.scan_id,
            ", ".join(document["engines"]),
            document["counts"]["groups"],
            document["counts"]["dropped"],
            document["counts"]["low_confidence"],
        )
    logger.info(
        "the ensemble wrote %d of %d due opinion(s) in %.1fs",
        written,
        len(rows),
        time.monotonic() - started,
    )
    return written
