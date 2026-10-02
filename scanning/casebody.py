"""The final XML of an opinion: the approved text and the tagger's spans (#432).

Two inputs, and nothing else:

- the approved text (#375), the object at ``Opinion.approved_text_key``:
  the paragraphs of ``body`` with their marks, the ``footnotes``, and
  the page table with the printed numbers;
- the tagger's spans (#272), the object at ``Opinion.tag_key``: one
  ``{paragraph, start, end, label}`` per span, in the offsets of
  ``body[paragraph]["text"]``.

:func:`build` merges them into one XML document in the shape of the
CAP casebody, the shape CourtListener reads (``harvard_opinions.py``,
#408): the head matter (``parties``, ``docketnumber``, ``court``,
``decisiondate``, ``attorneys``, ...) before one ``opinion`` for each
writing of the cluster (#442), whose paragraphs carry the marks of the
approved text, the ``page-number`` of every page the text crosses, and
the ``footnote`` elements of that writing at its end.

**A writing starts at its author.** The majority starts where the head
matter ends (:func:`head_matter_end`); a concurrence or a dissent
starts at an ``author`` span that starts its paragraph
(:func:`opinion_starts`), and its ``type`` is read from its words
(:func:`opinion_type`, :data:`OPINION_TYPES`). A type the table does
not read refuses the XML (:class:`OpinionTypeError`), never a guess,
so a developer adds the phrase. A footnote goes to the writing that
holds its mark (:func:`assign_footnotes`).

**The spans are the tags, the approved text is the rest.** A span that
covers a whole paragraph is that paragraph's element (``<court>``); a
span that covers a part is an inline element inside it. The tagger was
trained on its own label names, and :data:`ELEMENTS` is the one table
from a label to the CAP element. ``heading`` and ``separator`` have no
CAP element and keep their own name.

**The text is the text the tagger read.** The approved text keeps a
``\\n`` where a line or a column ended, and a word cut there. The XML
writes the characters of ``markup.projected_characters``, the rule of
the tagger's input, so a word is joined and a paragraph reads as one
line, and every span and every mark moves through the same map.

The review page computes the document at each request, and the export
for CourtListener (``final_xml``, #408) stores the same build. Pure
standard library plus ``markup``, on purpose, like ``paragraphs``: a
test builds it from two dicts. :func:`display_html` and :func:`source_html` are the two views
of the review page's display (``views_process.opinion_final_xml``).
"""

from __future__ import annotations

import html as _html
import re
import xml.etree.ElementTree as ET
from bisect import bisect_left
from dataclasses import dataclass, field

from scanning import markup

#: The tagger label and the CAP element it writes. A label that is not
#: here is written by :func:`element_of`, so a new label of the model
#: shows in the XML and does not break the build.
ELEMENTS = {
    "party": "party",
    "separator": "separator",
    "docketnumber": "docketnumber",
    "court": "court",
    "datefiled": "decisiondate",
    "otherdate": "otherdate",
    "history": "history",
    "attorneys": "attorneys",
    "judges": "judges",
    "disposition": "disposition",
    "author": "author",
    "heading": "heading",
}

#: The version of the document :func:`build` writes, the ``schema``
#: attribute of ``<casebody>``. Raise it when the same two inputs give
#: another XML: the export (``final_xml``, #408) then writes every
#: stored document again, and CourtListener reads the number.
SCHEMA = 1

#: The CAP element that holds a run of ``party`` and ``separator``.
PARTIES = "parties"
PARTY_ELEMENTS = frozenset({ELEMENTS["party"], ELEMENTS["separator"]})

#: The label whose first paragraph starts the opinion (:func:`head_matter_end`).
AUTHOR = "author"
#: The labels of the caption alone. ``judges``, ``disposition`` and
#: ``heading`` are not here: the end of a main opinion and its body
#: hold them too.
HEAD_MATTER_LABELS = frozenset({
    "party", "separator", "docketnumber", "court", "datefiled",
    "otherdate", "attorneys", "history",
})  # fmt: skip

#: The element of a ``sup`` mark whose text is a footnote label.
FOOTNOTE_MARK = "footnotemark"
PAGE_NUMBER = "page-number"

#: The elements that give the text its shape. Every other element is a
#: role: a tag of the tagger (or ``parties``), drawn with the tint of its
#: role, the review sheet of centralia.
STRUCTURE = frozenset({
    "casebody", "opinion", "p", "blockquote", "heading", "footnote",
    "ul", "ol", "li", "table", "tr", "td", "em", "strong", "sup",
    FOOTNOTE_MARK, PAGE_NUMBER,
})  # fmt: skip

#: The ``type`` of a CAP ``opinion`` (#442). Every value is a key of
#: CourtListener's map from the Harvard type to ``Opinion.type``
#: (``harvard_opinions.map_opinion_type`` and
#: ``harvard_merge.HarvardConversionUtil.types_mapping``, which agree
#: at courtlistener 58784cac); :data:`CL_TYPES` is that key set, and a
#: test pins every type to it. A value outside it is ``combined`` in one
#: importer and an error in the other.
MAJORITY = "majority"
PLURALITY = "plurality"
UNANIMOUS = "unanimous"
CONCURRENCE = "concurrence"
IN_PART = "concurring-in-part-and-dissenting-in-part"
DISSENT = "dissent"
REHEARING = "rehearing"
REMITTITUR = "remittitur"
ON_THE_MERITS = "on-the-merits"
ON_MOTION_TO_STRIKE = "on-motion-to-strike-cost-bill"
CL_TYPES = frozenset({
    UNANIMOUS, MAJORITY, PLURALITY, CONCURRENCE, IN_PART, DISSENT,
    REMITTITUR, REHEARING, ON_THE_MERITS, ON_MOTION_TO_STRIKE,
})  # fmt: skip

#: The word forms of a role, a closed set: "concurrent" and
#: "concurrently" are no role.
_CONCUR = r"concur(?:s|red|ring|rence|rences)?\b"
_DISSENT = r"dissent(?:s|ed|ing)?\b"
#: The words of a writing that name its type, tried in this order over
#: the folded text of :func:`role_text`. "Dissenting in part" alone is
#: the type of both, the one CAP value of a partial dissent. Between
#: ``concur`` and ``dissent`` the earlier word wins
#: (:func:`opinion_type`), so "dissenting, in which X concurs" is a
#: dissent. A stage of the case comes last: its heading can sit above
#: the author of a dissent.
OPINION_TYPES: tuple[tuple[re.Pattern, str], ...] = (
    (
        re.compile(
            rf"\b{_CONCUR},? in part,? (?:and|&|but) {_DISSENT} in part\b"
            rf"|\b{_DISSENT},? in part,? (?:and|&|but) {_CONCUR} in part\b"
            rf"|\b{_DISSENT} in part\b"
        ),
        IN_PART,
    ),
    (re.compile(rf"\b(?:{_CONCUR}|{_DISSENT})"), ""),
    (re.compile(r"\brehearing\b"), REHEARING),
    (re.compile(r"\bon the merits\b"), ON_THE_MERITS),
    (re.compile(r"\bremittitur\b"), REMITTITUR),
    (re.compile(r"\bmotion to strike\b"), ON_MOTION_TO_STRIKE),
)
#: The start of a text after the author line that names its role:
#: "(dissenting).", "concurring.", "I respectfully dissent." A sentence
#: that names a role later ("We concur with the trial court that ...")
#: is the opinion's text, not its role.
_ROLE_LEAD = re.compile(
    r"\(?\s*(?:i\s+(?:respectfully\s+)?|specially\s+)?"
    rf"(?:{_CONCUR}|{_DISSENT})"
)
#: The types of a writing that end the one before it when its author
#: line is the first after the head matter: a per curiam majority has no
#: author line, and its first author is a concurrence's or a dissent's.
SEPARATE_TYPES = frozenset({CONCURRENCE, IN_PART, DISSENT})
#: The words of the first writing that make it other than the majority.
FIRST_OPINION_TYPES: tuple[tuple[re.Pattern, str], ...] = (
    (re.compile(r"\bplurality\b"), PLURALITY),
    (re.compile(r"\bunanimous"), UNANIMOUS),
)
#: How much of the text after the author :func:`role_text` reads: its
#: first sentence, cut at a period that does not end an abbreviation
#: of a name or a title ("J.", "C. J.", "Mr.").
_ROLE_REACH = 300
_SENTENCE_STOP = re.compile(
    r"(?<!\b[A-Z])(?<!\bJJ)(?<!\bMr)(?<!\bMrs)(?<!\bCh)(?<!\bJr)"
    r"[.;:!?](?=\s|$)"
)


#: The layers of the inline elements, outermost first: a list item
#: holds a span, and a span holds the marks of the approved text.
_BLOCK, _SPAN, _INLINE = 0, 1, 2
_INLINE_RANK = {markup.STRONG: 0, markup.EM: 1, markup.SUP: 2}


#: What a span may leave out at the edges of a paragraph it covers
#: whole: space and punctuation, never a word.
_EDGE = " \n\t.,;:"


def _covers(text: str, start: int, end: int) -> bool:
    """Return whether a span covers a paragraph whole.

    The worker trims a span to its words, so a span that leaves out
    only :data:`_EDGE` at the edges covers it whole. The one rule of
    "this paragraph is that element", and of "this paragraph is a
    caption line" (:func:`head_matter_end`).

    :param text: The paragraph's text.
    :param start: The span's start in it.
    :param end: The span's end, exclusive.
    :rtype: bool
    """
    return start <= len(text) - len(text.lstrip(_EDGE)) and end >= len(
        text.rstrip(_EDGE)
    )


class CasebodyError(Exception):
    """The spans do not describe the approved text."""


class OpinionTypeError(CasebodyError):
    """A writing whose words :data:`OPINION_TYPES` does not read (#442).

    The view logs it as an error, so a developer adds the phrase.
    """


@dataclass
class _Element:
    start: int
    end: int
    layer: int
    rank: int
    name: str
    attrs: dict = field(default_factory=dict)


#: An XML name, less the colon of a namespace. A label that is not one
#: (``other date``) would write a document no parser reads.
_XML_NAME = re.compile(r"[A-Za-z_][\w.-]*\Z")
#: The characters XML 1.0 does not allow, which OCR text can carry
#: (a vertical tab, a form feed, a lone surrogate).
_NOT_XML = dict.fromkeys(
    [
        *range(0x00, 0x09),
        0x0B,
        0x0C,
        *range(0x0E, 0x20),
        *range(0xD800, 0xE000),
        0xFFFE,
        0xFFFF,
    ]
)


def element_of(label: str) -> str:
    """Return the CAP element of one tagger label.

    A label of :data:`ELEMENTS` gives its CAP name. Any other label
    keeps its own name where that is an XML name; otherwise every
    character an XML name cannot hold is a ``-``. A ``label-`` goes
    before a name that would start badly, start with ``xml`` (the
    prefix XML keeps for itself), or be an element of the structure
    (:data:`STRUCTURE`, ``parties``), which a reader would take for
    one. So a new label of the model shows in the XML
    and never breaks it; the spans object keeps the label as it was.
    """
    if label in ELEMENTS:
        return ELEMENTS[label]
    name = re.sub(r"[^\w.-]", "-", label) or "label"
    if (
        not _XML_NAME.match(name)
        or name.lower().startswith("xml")
        or name in STRUCTURE
        or name == PARTIES
    ):
        name = f"label-{name}"
    return name


def _escape(text: str) -> str:
    return _html.escape(text.translate(_NOT_XML), quote=False)


def _attrs(attrs: dict) -> str:
    return "".join(
        f' {name}="{_html.escape(str(value).translate(_NOT_XML), quote=True)}"'
        for name, value in attrs.items()
        if value is not None
    )


def _open(element: _Element) -> str:
    return f"<{element.name}{_attrs(element.attrs)}>"


def _write(
    text: str, elements: list[_Element], inserts: dict[int, list[str]]
) -> str:
    """Write ``text`` with its elements, every one closed in its parent.

    At each edge the elements that cover the next character are sorted
    outermost first (layer, then start, then the longer one); the ones
    already open and still wanted stay open, and the rest close and
    open again. So an element that crosses the edge of another is cut
    there, and every other element is written once. An insert (a page
    number) goes after the closes and before the opens of its edge.
    """
    edges = sorted(
        {0, len(text), *inserts}
        | {e.start for e in elements}
        | {e.end for e in elements}
    )
    edges = [edge for edge in edges if 0 <= edge <= len(text)]
    stack: list[_Element] = []
    parts: list[str] = []
    for index, edge in enumerate(edges):
        wanted = sorted(
            (e for e in elements if e.start <= edge < e.end),
            key=lambda e: (e.layer, e.start, -e.end, e.rank),
        )
        shared = 0
        while (
            shared < min(len(stack), len(wanted))
            and stack[shared] is wanted[shared]
        ):
            shared += 1
        parts.extend(f"</{e.name}>" for e in reversed(stack[shared:]))
        parts.extend(inserts.get(edge, []))
        parts.extend(_open(e) for e in wanted[shared:])
        stack = wanted
        if index + 1 < len(edges):
            parts.append(_escape(text[edge : edges[index + 1]]))
    return "".join(parts)


def _page_number(printed: str) -> str:
    return (
        f'<{PAGE_NUMBER} label="{_html.escape(printed, quote=True)}">'
        f"*{_escape(printed)}</{PAGE_NUMBER}>"
    )


def _paragraph(
    paragraph: dict,
    spans: list[dict],
    labels: frozenset[str],
    breaks: dict[int, str],
) -> tuple[str, str]:
    """Write one paragraph of the approved text.

    :param paragraph: A paragraph of ``body`` or of a footnote.
    :param spans: The spans on it, in the offsets of its ``text``.
    :param labels: The footnote labels, for :data:`FOOTNOTE_MARK`.
    :param breaks: ``{offset in text: printed number}`` of the pages
        that start in it.
    :returns: The element, and its inner XML.
    :rtype: tuple[str, str]
    """
    if paragraph.get("kind") == markup.TABLE:
        # A page that starts at a table is numbered in its first cell:
        # the star number is a citation anchor, and no page may lose it.
        numbers = "".join(_page_number(breaks[at]) for at in sorted(breaks))
        table = [list(row) for row in paragraph.get("table") or [] if row]
        cells = [[_escape(cell) for cell in row] for row in table] or [[""]]
        cells[0][0] = numbers + cells[0][0]
        if not table and not numbers:
            cells = []
        rows = "".join(
            "<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>"
            for row in cells
        )
        return "table", rows
    source = paragraph.get("text") or ""
    marks = paragraph.get("marks") or []
    items = frozenset(
        mark["start"]
        for mark in marks
        if mark.get("kind") == markup.ITEM and mark.get("start", 0) > 0
    )
    # The characters of the tagger's input, less the line end before an
    # item, which the projection writes as a block edge.
    kept = [
        (position, char)
        for position, char in markup.projected_characters(source, items)
        if not (position + 1 in items and source[position] == "\n")
    ]
    text = "".join(char for _, char in kept)
    sources = [position for position, _ in kept]

    def at(offset: int) -> int:
        return bisect_left(sources, offset)

    listed = paragraph.get("kind") == markup.LIST_ITEM and any(
        mark.get("kind") == markup.ITEM for mark in marks
    )
    if listed:
        name = (
            markup.NUMBERED_LIST
            if paragraph.get("list") == markup.NUMBERED_LIST
            else markup.BULLET_LIST
        )
    elif paragraph.get("kind") == markup.HEADING:
        name = "heading"
    else:
        name = "p"

    elements: list[_Element] = []
    for mark in marks:
        kind = mark.get("kind")
        start, end = at(mark.get("start", 0)), at(mark.get("end", 0))
        if end <= start:
            continue
        if kind == markup.ITEM:
            elements.append(_Element(start, end, _BLOCK, 0, "li"))
        elif kind in _INLINE_RANK:
            element = kind
            if kind == markup.SUP and text[start:end].strip() in labels:
                element = FOOTNOTE_MARK
            elements.append(
                _Element(start, end, _INLINE, _INLINE_RANK[kind], element)
            )

    whole = None
    for span in spans:
        start, end = at(span["start"]), at(span["end"])
        if not text[start:end].strip():
            continue
        element = element_of(span["label"])
        if (
            whole is None
            and name in ("p", "heading")
            and not paragraph.get("blockquote")
            and _covers(text, start, end)
        ):
            whole = element
            continue
        elements.append(_Element(start, end, _SPAN, 0, element))
    if whole:
        name = whole
    # A span named as its paragraph adds nothing: its text stays.
    elements = [
        e for e in elements if not (e.layer == _SPAN and e.name == name)
    ]
    inserts: dict[int, list[str]] = {}
    for offset, printed in breaks.items():
        inserts.setdefault(at(offset), []).append(_page_number(printed))
    inner = _write(text, elements, inserts)
    if paragraph.get("blockquote"):
        if name == "p":
            name = "blockquote"
        else:
            inner = f"<{name}>{inner}</{name}>"
            name = "blockquote"
    return name, inner


def _spans_by_paragraph(body: list[dict], spans: list[dict]) -> dict:
    by: dict[int, list[dict]] = {}
    for span in spans:
        index, start, end = (
            span.get("paragraph"),
            span.get("start"),
            span.get("end"),
        )
        if not (
            isinstance(index, int)
            and isinstance(start, int)
            and isinstance(end, int)
            and isinstance(span.get("label"), str)
        ):
            raise CasebodyError(f"a span has no address: {str(span)[:200]}")
        if not 0 <= index < len(body):
            raise CasebodyError(
                f"a span names paragraph {index} of a body of {len(body)}"
            )
        if not 0 <= start < end <= len(body[index].get("text") or ""):
            raise CasebodyError(
                f"a span of paragraph {index} is outside its text: "
                f"{start}-{end}"
            )
        by.setdefault(index, []).append(span)
    return {
        index: sorted(rows, key=lambda s: s["start"])
        for index, rows in by.items()
    }


def head_matter_end(body: list[dict], by: dict[int, list[dict]]) -> int:
    """Return the index of the first paragraph of the opinion.

    The head matter ends at the first ``author`` span. The tagger can
    miss a line of the caption (a "Rehearing denied" line, an OCR
    fragment), so a paragraph with no span does not end it: it ends
    after the last paragraph before that author that a span of
    :data:`HEAD_MATTER_LABELS` covers whole (:func:`_covers`), where
    that is later than the leading run of tagged paragraphs. A per curiam opinion has no author line,
    and the first author span is its dissent's; its text holds none of
    those labels, so its head matter ends with the leading run.

    :param body: The body paragraphs.
    :param by: The spans of each paragraph (:func:`_spans_by_paragraph`).
    :returns: The index.
    :rtype: int
    """
    author = next(
        (
            index
            for index in sorted(by)
            if any(span["label"] == AUTHOR for span in by[index])
        ),
        len(body),
    )
    run = 0
    while run < author and run in by:
        run += 1
    # A caption line is a whole block: a date or a court named in a
    # sentence of the opinion is a span over some of its words.
    caption = [
        index
        for index in sorted(by)
        if index < author
        and any(
            span["label"] in HEAD_MATTER_LABELS
            and _covers(
                body[index].get("text") or "", span["start"], span["end"]
            )
            for span in by[index]
        )
    ]
    return max(run, caption[-1] + 1) if caption else run


def _starts_paragraph(text: str, span: dict) -> bool:
    """Return whether a span starts at the first word of its paragraph."""
    return not text[: span["start"]].strip(_EDGE)


def opinion_starts(
    body: list[dict], by: dict[int, list[dict]], split: int
) -> list[int]:
    """Return the first paragraph of every writing after the head matter.

    A writing starts at an ``author`` span that starts its paragraph,
    the author line of a concurrence or a dissent ("Ciklin, J.,
    concurring in part and dissenting in part.", or "Andrews, J.
    (dissenting). Assisting ..."). The vote line names its judges in
    the middle of a sentence ("Ciklin, J., concurs in part and dissents
    in part with opinion."), where the tagger cuts it into ``judges``
    and ``author`` spans of a word or two (scan 3593, opinion 826.0);
    none of those starts its paragraph, so none starts a writing.

    The first entry is ``split`` whatever it holds. The first author
    line after it starts a writing only where its words name a
    concurrence or a dissent (:data:`SEPARATE_TYPES`): a per curiam
    majority has no author line, and its first author is a dissent's;
    an untagged line the head matter did not take ("OPINION") can come
    before the majority's own author line. Every later author line
    starts a writing.

    :param body: The body paragraphs.
    :param by: The spans of each paragraph (:func:`_spans_by_paragraph`).
    :param split: :func:`head_matter_end`.
    :returns: The indexes, ``split`` first, rising.
    :rtype: list[int]
    """
    starts = [split]
    authors = [
        index
        for index in sorted(by)
        if index >= split
        and any(
            span["label"] == AUTHOR
            and _starts_paragraph(body[index].get("text") or "", span)
            for span in by[index]
        )
    ]
    if authors and authors[0] > split:
        # The first author line ends the majority only when it names a
        # concurrence or a dissent; else it is the majority's own, after
        # an untagged line the head matter did not take ("OPINION").
        words = role_text(body, by, authors[0])
        if not any(
            _type_of(words, OPINION_TYPES) == value for value in SEPARATE_TYPES
        ):
            authors = authors[1:]
    starts.extend(index for index in authors if index > split)
    return starts


def _author_line(
    body: list[dict], by: dict[int, list[dict]], start: int, end: int
) -> int:
    """Return the author line of the first writing, or ``start``.

    The first writing can open with an untagged line the head matter did
    not take ("OPINION") before its author line, and its type ("a
    plurality opinion") is in that author line (:func:`opinion_starts`).

    :param body: The body paragraphs.
    :param by: The spans of each paragraph.
    :param start: The first paragraph of the writing.
    :param end: The first paragraph after it.
    :returns: The first paragraph in that range that an ``author`` span
        starts, or ``start`` when none does (a per curiam).
    :rtype: int
    """
    return next(
        (
            index
            for index in range(start, end)
            if any(
                span["label"] == AUTHOR
                and _starts_paragraph(body[index].get("text") or "", span)
                for span in by.get(index, [])
            )
        ),
        start,
    )


def _first_sentence(text: str) -> str:
    """Return the first sentence of a text, at most :data:`_ROLE_REACH`."""
    text = text[:_ROLE_REACH]
    stop = _SENTENCE_STOP.search(text)
    return text[: stop.end()] if stop else text


def role_text(body: list[dict], by: dict[int, list[dict]], index: int) -> str:
    """Return the words that name the type of the writing at ``index``.

    The author line, and the first sentence after it where that
    sentence starts with a role (:data:`_ROLE_LEAD`): in the same
    paragraph ("Andrews, J. (dissenting). Assisting ..."), or in the
    next one when the author line is a paragraph of its own (the CAP
    shape of Palsgraf, ``<author>`` then ``<p>(dissenting). ...``). A
    heading just above the author goes first: a stage of the case
    ("ON PETITION FOR REHEARING") is named there.

    :param body: The body paragraphs.
    :param by: The spans of each paragraph.
    :param index: The first paragraph of the writing.
    :returns: The text, folded: lower case, one space between words.
    :rtype: str
    """
    if index >= len(body):
        return ""
    text = body[index].get("text") or ""
    author = next(
        (
            span
            for span in by.get(index, [])
            if span["label"] == AUTHOR and _starts_paragraph(text, span)
        ),
        None,
    )
    end = author["end"] if author else 0
    parts = []
    if index > 0 and body[index - 1].get("kind") == markup.HEADING:
        parts.append(body[index - 1].get("text") or "")
    parts.append(text[:end])
    rest = text[end:]
    if not rest.strip(_EDGE) and index + 1 < len(body):
        rest = body[index + 1].get("text") or ""
    sentence = _first_sentence(rest.lstrip(_EDGE))
    if _ROLE_LEAD.match(sentence.lower()):
        parts.append(sentence)
    return " ".join(" ".join(parts).lower().split())


def _type_of(words: str, table) -> str | None:
    """Return the type of the first rule of ``table`` that ``words``
    match, or None. The rule of no value is ``concur`` or ``dissent``,
    whichever comes first."""
    for pattern, value in table:
        found = pattern.search(words)
        if found:
            if value:
                return value
            return (
                DISSENT if found.group().startswith("dissent") else CONCURRENCE
            )
    return None


def opinion_type(
    body: list[dict], by: dict[int, list[dict]], index: int, first: bool
) -> str:
    """Return the CAP ``type`` of the writing that starts at ``index``.

    The first writing is the majority, a plurality or a unanimous
    opinion where its author line says so (:data:`FIRST_OPINION_TYPES`).
    Every later one is read from its words by :data:`OPINION_TYPES`:
    the first rule that matches decides, and the rule of ``concur`` and
    ``dissent`` takes the word that comes first.

    :param body: The body paragraphs.
    :param by: The spans of each paragraph.
    :param index: The first paragraph of the writing.
    :param first: Whether it is the first writing of the cluster.
    :returns: A value of :data:`CL_TYPES`.
    :rtype: str
    :raises OpinionTypeError: When a later writing names no type the
        table reads.
    """
    words = role_text(body, by, index)
    if first:
        return _type_of(words, FIRST_OPINION_TYPES) or MAJORITY
    value = _type_of(words, OPINION_TYPES)
    if value:
        return value
    raise OpinionTypeError(
        f"paragraph {index} starts a writing whose type is not read: "
        f"{words[:200]!r}"
    )


def footnote_marks(paragraph: dict, labels: frozenset[str]) -> list[str]:
    """Return the footnote labels a paragraph cites, in order.

    The rule of :data:`FOOTNOTE_MARK` in :func:`_paragraph`: a ``sup``
    mark whose text is a footnote label.

    :param paragraph: A body paragraph.
    :param labels: The footnote labels.
    :rtype: list[str]
    """
    text = paragraph.get("text") or ""
    return [
        text[mark["start"] : mark["end"]].strip()
        for mark in sorted(
            paragraph.get("marks") or [], key=lambda m: m.get("start", 0)
        )
        if mark.get("kind") == markup.SUP
        and text[mark.get("start", 0) : mark.get("end", 0)].strip() in labels
    ]


def assign_footnotes(
    body: list[dict], notes: list[dict], starts: list[int]
) -> list[int]:
    """Return the writing each footnote belongs to (#442).

    A note goes to the first writing, at or after the writing of the
    note before it, that holds a mark of its label no note took yet.
    The walk never goes back, so a dissent that numbers its notes from
    1 again takes its own note 1 (``paragraphs.mark_restarts`` keeps
    that note apart in the approved text). The search stops at the
    last of those writings that holds the note's first page: a note
    whose mark is not a ``sup`` must not take the mark of the dissent's
    note of the same label. A note with no such mark goes to the first
    writing that holds its first page, and with no page to the writing
    of the note before it.

    :param body: The body paragraphs.
    :param notes: The footnotes of the approved text, in reading order.
    :param starts: :func:`opinion_starts`.
    :returns: An index into ``starts`` for every note.
    :rtype: list[int]
    """
    labels = frozenset(
        str(note["label"]) for note in notes if note.get("label") is not None
    )
    bounds = list(zip(starts, [*starts[1:], len(body)], strict=True))
    marks = []
    pages = []
    for start, end in bounds:
        cited: dict[str, int] = {}
        held: set = set()
        for paragraph in body[start:end]:
            for label in footnote_marks(paragraph, labels):
                cited[label] = cited.get(label, 0) + 1
            held.update(paragraph.get("pages") or [])
        marks.append(cited)
        pages.append(held)
    owners = []
    cursor = 0
    for note in notes:
        label = note.get("label")
        first = (note.get("pages") or [None])[0]
        holders = [
            at for at in range(cursor, len(bounds)) if first in pages[at]
        ]
        # A mark never takes a note past the writings of its first page:
        # a mark an engine did not read as ``sup`` would else send the
        # note, and every note after it, to the next writing that holds
        # a mark of the same label. The last of them is the bound, as a
        # dissent can start in the middle of a page.
        last = holders[-1] if holders else len(bounds) - 1
        owner = next(
            (
                at
                for at in range(cursor, last + 1)
                if label is not None and marks[at].get(str(label), 0) > 0
            ),
            None,
        )
        if owner is not None:
            marks[owner][str(label)] -= 1
        else:
            owner = holders[0] if holders else cursor
        owners.append(owner)
        cursor = owner
    return owners


def _comment(text: str) -> str:
    return "<!-- " + re.sub(r"-{2,}", "-", text) + " -->"


def build(approved: dict, tags: dict, *, ids: dict | None = None) -> str:
    """Return the final XML of one opinion: one ``opinion`` per writing.

    :param approved: The approved object (``paragraphs.approved_document``).
    :param tags: The spans object (``tagger.glue_run``).
    :param ids: The ``scan-id`` and ``opinion-id`` attributes of
        ``<casebody>`` (#408): the portal's primary keys, which the
        approved object does not hold. CourtListener keeps the XML and
        reads them from it, so the two ids name the redacted PDF later.
    :returns: The XML document, one block element per line.
    :rtype: str
    :raises CasebodyError: When a span does not address the body.
    :raises OpinionTypeError: When a writing names no type the table
        reads (:func:`opinion_type`).
    """
    body = approved.get("body") or []
    by = _spans_by_paragraph(body, tags.get("spans") or [])
    notes = approved.get("footnotes") or []
    labels = frozenset(
        str(note["label"]) for note in notes if note.get("label") is not None
    )
    printed = {
        page.get("page_in_opinion"): page.get("printed")
        for page in approved.get("pages") or []
    }
    opinion = approved.get("opinion") or {}

    def breaks_of(paragraph: dict, last_page) -> dict[int, str]:
        breaks = {}
        pages = paragraph.get("pages") or []
        if last_page is not None and pages and pages[0] != last_page:
            breaks[0] = printed.get(pages[0])
        for entry in paragraph.get("page_breaks") or []:
            breaks[entry["offset"]] = printed.get(entry["page_in_opinion"])
        return {offset: value for offset, value in breaks.items() if value}

    blocks: list[tuple[str, str]] = []
    last_page = None
    for index, paragraph in enumerate(body):
        name, inner = _paragraph(
            paragraph,
            by.get(index, []),
            labels,
            breaks_of(paragraph, last_page),
        )
        blocks.append((name, inner))
        pages = paragraph.get("pages") or []
        if pages:
            last_page = pages[-1]

    def lines(rows: list[tuple[str, str]], indent: str) -> list[str]:
        out: list[str] = []
        at = 0
        while at < len(rows):
            if rows[at][0] in PARTY_ELEMENTS:
                run = []
                while at < len(rows) and rows[at][0] in PARTY_ELEMENTS:
                    run.append(f"<{rows[at][0]}>{rows[at][1]}</{rows[at][0]}>")
                    at += 1
                out.append(f"{indent}<{PARTIES}>{' '.join(run)}</{PARTIES}>")
                continue
            name, inner = rows[at]
            out.append(f"{indent}<{name}>{inner}</{name}>")
            at += 1
        return out

    split = head_matter_end(body, by)
    starts = opinion_starts(body, by, split)
    ends = [*starts[1:], len(body)]
    types = [
        opinion_type(
            body,
            by,
            _author_line(body, by, start, end) if at == 0 else start,
            first=at == 0,
        )
        for at, (start, end) in enumerate(zip(starts, ends, strict=True))
    ]
    owners = assign_footnotes(body, notes, starts)
    out = ['<?xml version="1.0" encoding="utf-8"?>']
    out.append(
        _comment(
            f"scan {opinion.get('scan')}, opinion "
            f"{opinion.get('first_printed_page')}.{opinion.get('index_in_page')}; "
            f"approved text {tags.get('approved_text_key')}; "
            f"tagger run {tags.get('run')}, model {tags.get('model')}"
        )
    )
    out.append(
        "<casebody"
        + _attrs(
            {
                "firstpage": opinion.get("first_printed_page"),
                "lastpage": opinion.get("last_printed_page"),
                **(ids or {}),
                "schema": SCHEMA,
            }
        )
        + ">"
    )
    out.extend(lines(blocks[:split], "  "))
    for at, (start, end) in enumerate(zip(starts, ends, strict=True)):
        out.append(f"  <opinion{_attrs({'type': types[at]})}>")
        out.extend(lines(blocks[start:end], "    "))
        for note, owner in zip(notes, owners, strict=True):
            if owner != at:
                continue
            label = note.get("label")
            out.append(f"    <footnote{_attrs({'label': label})}>")
            for paragraph in note.get("paragraphs") or []:
                name, inner = _paragraph(paragraph, [], labels, {})
                out.append(f"      <{name}>{inner}</{name}>")
            out.append("    </footnote>")
        out.append("  </opinion>")
    out.append("</casebody>")
    xml = "\n".join(out) + "\n"
    try:
        ET.fromstring(xml)
    except ET.ParseError as exc:
        # A rule above missed a case: refuse it, never send it.
        raise CasebodyError(f"the XML is not well-formed: {exc}") from exc
    return xml


# ── The display (#432) ──────────────────────────────────────────────
#: The marks of the approved text, drawn as the text they format.
_PLAIN = frozenset({"em", "strong", "sup", "ul", "ol", "li", "blockquote"})


def _role(tag: str) -> str:
    return _html.escape(tag, quote=True)


def _note_id(label: str, writing: int) -> str:
    """Return the HTML id of the footnote with this label in a writing.

    Every character that is not a letter or a digit is spelled as its
    code point, so ``*`` and ``†`` give ids too, and two labels never
    give one id. The writing is in the id because a concurrence or a
    dissent can number its notes from 1 again (#442), the rule of
    centralia's ``render_opinion_ingest``.
    """
    safe = re.sub(r"[^A-Za-z0-9]", lambda m: f"_{ord(m.group()):x}", label)
    return f"cb-fn-{writing}-{safe}"


#: The words a person reads for each ``type`` of a writing (#442).
TYPE_NAMES = {
    MAJORITY: "Majority",
    PLURALITY: "Plurality",
    UNANIMOUS: "Unanimous",
    CONCURRENCE: "Concurrence",
    IN_PART: "Concurring in part and dissenting in part",
    DISSENT: "Dissent",
    REHEARING: "Rehearing",
    REMITTITUR: "Remittitur",
    ON_THE_MERITS: "On the merits",
    ON_MOTION_TO_STRIKE: "On motion to strike cost bill",
}


def _writing_author(opinion: ET.Element) -> str:
    """Return the author line that opens one writing, or ``""``.

    The author is the author line that opens the writing, a paragraph
    of its own or the start of the first one; a per curiam writing has
    none, and an ``author`` span of its vote line is no author line.

    :param opinion: An ``opinion`` element of :func:`build`.
    :returns: The text, one space between words.
    """
    first = next((child for child in opinion if child.tag != "footnote"), None)
    author = None
    if first is not None and first.tag == AUTHOR:
        author = first
    elif (
        first is not None
        and len(first)
        and first[0].tag == AUTHOR
        and not (first.text or "").strip()
    ):
        author = first[0]
    if author is None:
        return ""
    return " ".join("".join(author.itertext()).split())


def _writing_title(opinion: ET.Element) -> str:
    """Return the name of one writing: its type, then its author.

    :param opinion: An ``opinion`` element of :func:`build`.
    :returns: Escaped HTML.
    """
    kind = opinion.get("type") or ""
    name = TYPE_NAMES.get(kind, kind or "Opinion")
    who = _writing_author(opinion)
    return _escape(f"{name} — {who}" if who else name)


def display_html(xml: str) -> str:
    """Return the XML drawn as the opinion reads, with its tags tinted.

    The shape of centralia's review sheet: the head matter is one block
    of rows, each tinted by its role, and the role is named in the left
    margin where a run of it starts; the opinion is text in paragraphs
    under a rule; a tag inside a paragraph is a tint on its words; the
    footnotes close the opinion. ``data-role`` names the element, so
    the style sheet keys every colour off one attribute. Everything is
    escaped here, so the template marks the result safe.

    A footnote mark links to its footnote, and the footnote's label
    links back to the first mark of that label (:func:`_note_id`); a
    note that more marks cite links back to each. The XML carries no
    such link: CAP names a note by its label alone. A mark links only
    inside its own writing, whose notes can repeat the labels of
    another.

    Every writing of the cluster is a section of its own, headed by its
    type and its author, and a cluster of two or more writings opens
    with one link to each (#442).

    :param xml: From :func:`build`.
    :returns: The HTML fragment.
    :rtype: str
    """
    root = ET.fromstring(xml)
    #: The mark ids of each footnote label of the writing being drawn,
    #: in the order they read.
    marks: dict[str, list[str]] = {}
    # The head matter's notes belong to the first writing
    # (:func:`assign_footnotes`), so its marks link there.
    writing = 1

    def inline(node: ET.Element) -> str:
        """The content of a node, its children drawn inline."""
        return _escape(node.text or "") + "".join(
            draw_inline(child) + _escape(child.tail or "") for child in node
        )

    def draw_inline(node: ET.Element) -> str:
        tag = node.tag
        inner = inline(node)
        if tag in _PLAIN:
            return f"<{tag}>{inner}</{tag}>"
        if tag == FOOTNOTE_MARK:
            label = "".join(node.itertext()).strip()
            refs = marks.setdefault(label, [])
            ref = f"{_note_id(label, writing)}-ref-{len(refs) + 1}"
            refs.append(ref)
            return (
                f'<sup class="cb-fnmark" id="{ref}" title="footnotemark">'
                f'<a href="#{_note_id(label, writing)}">{inner}</a></sup>'
            )
        if tag == PAGE_NUMBER:
            return f'<span class="cb-pg" title="page-number">{inner}</span>'
        if tag in STRUCTURE:
            return inner
        return (
            f'<span class="cb-tag" data-role="{_role(tag)}" '
            f'title="{_role(tag)}">{inner}</span>'
        )

    def rows(children: list[ET.Element]) -> str:
        """Blocks in order; a run of one role is named once, in the margin."""
        out = []
        last = None
        for child in children:
            tag = child.tag
            if tag == "p":
                out.append(f"<p>{inline(child)}</p>")
                last = None
            elif tag == "heading":
                out.append(f'<h3 class="cb-heading">{inline(child)}</h3>')
                last = None
            elif tag in ("blockquote", "ul", "ol"):
                out.append(draw_inline(child))
                last = None
            elif tag == "table":
                cells = "".join(
                    "<tr>"
                    + "".join(f"<td>{inline(cell)}</td>" for cell in row)
                    + "</tr>"
                    for row in child
                )
                out.append(f'<table class="cb-tb">{cells}</table>')
                last = None
            else:
                start = " role-start" if tag != last else ""
                out.append(
                    f'<div class="cb-row{start}" data-role="{_role(tag)}">'
                    f"{inline(child)}</div>"
                )
                last = tag
        return "".join(out)

    opinions = root.findall("opinion")
    head = [child for child in root if child.tag != "opinion"]
    parts = ['<div class="cb-doc">']
    if len(opinions) > 1:
        links = "".join(
            f'<li><a href="#cb-op-{n}">{_writing_title(opinion)}</a></li>'
            for n, opinion in enumerate(opinions, start=1)
        )
        parts.append(
            '<nav class="cb-opinions" aria-label="Opinions of the cluster">'
            f"<h2>{len(opinions)} opinions</h2><ol>{links}</ol></nav>"
        )
    if head:
        parts.append(
            '<section class="cb-headmatter"><h2>headmatter</h2>'
            f"{rows(head)}</section>"
        )
    for n, opinion in enumerate(opinions, start=1):
        writing = n
        if n > 1:
            # The first writing keeps the marks of the head matter.
            marks = {}
        kind = opinion.get("type") or ""
        body = [child for child in opinion if child.tag != "footnote"]
        notes = [child for child in opinion if child.tag == "footnote"]
        parts.append(
            f'<section class="cb-opinion" id="cb-op-{n}" '
            f'data-type="{_role(kind)}"><h2>'
            f'<span class="cb-chip" title="opinion type">{_role(kind)}</span> '
            f"{_escape(_writing_author(opinion))}</h2>{rows(body)}"
        )
        if notes:
            parts.append('<div class="cb-fns">')
            seen: set[str] = set()
            for note in notes:
                label = note.get("label")
                refs = marks.get(label or "") if label is not None else None
                # A second note of the same label is no target: an id
                # names one element.
                anchor = ""
                if label is not None and label not in seen:
                    anchor = f' id="{_note_id(label, writing)}"'
                    seen.add(label)
                text = _escape(label or "")
                if refs and anchor:
                    text = (
                        f'<a href="#{refs[0]}" title="Back to the text">'
                        f"{text}</a>"
                    )
                    text += "".join(
                        f' <a class="cb-back" href="#{ref}" '
                        f'title="Back to mark {k}">&#8617;{k}</a>'
                        for k, ref in enumerate(refs[1:], start=2)
                    )
                parts.append(
                    f'<div class="cb-fn"{anchor}><span class="cb-lbl">'
                    f"{text}</span><div>{rows(list(note))}</div></div>"
                )
            parts.append("</div>")
        parts.append("</section>")
    parts.append("</div>")
    return "".join(parts)


#: One token of the XML text, read before the escape. Every part of a
#: tag is a character class its neighbours cannot match (a value is
#: ``"[^"]*"``, and :func:`build` escapes ``"`` in every value), so no
#: input makes the pattern backtrack.
_SOURCE_TOKEN = re.compile(
    r"(?P<comment><!--.*?--!?>)"
    r"|(?P<decl><\?[^?]*\?>)"
    r"|<(?P<close>/?)(?P<name>[\w-]+)"
    r'(?P<attrs>(?:\s+[\w-]+="[^"]*")*)(?P<end>\s*/?)>',
    re.S,
)
_SOURCE_ATTR = re.compile(r'(\s+)([\w-]+)="([^"]*)"')


def source_html(xml: str) -> str:
    """Return the XML text escaped, with its tags coloured.

    :param xml: From :func:`build`.
    :returns: The HTML fragment for a ``<pre>``.
    :rtype: str
    """

    def escape(text: str) -> str:
        return _html.escape(text, quote=True)

    def token(match: re.Match) -> str:
        for group in ("comment", "decl"):
            if match.group(group):
                return (
                    f'<span class="x-comment">{escape(match.group(group))}'
                    "</span>"
                )
        name = match.group("name")
        attrs = _SOURCE_ATTR.sub(
            lambda attr: (
                f'{attr.group(1)}<span class="x-attr">{escape(attr.group(2))}'
                f'</span>=<span class="x-value">&quot;{escape(attr.group(3))}'
                "&quot;</span>"
            ),
            match.group("attrs"),
        )
        role = "" if name in STRUCTURE else f' data-role="{escape(name)}"'
        return (
            f'<span class="x-tag"{role}>&lt;{match.group("close")}'
            f"{escape(name)}{attrs}{escape(match.group('end'))}&gt;</span>"
        )

    parts = []
    at = 0
    for match in _SOURCE_TOKEN.finditer(xml):
        parts.append(escape(xml[at : match.start()]))
        parts.append(token(match))
        at = match.end()
    parts.append(escape(xml[at:]))
    return "".join(parts)


def label_counts(tags: dict) -> list[tuple[str, str, int]]:
    """Return ``(label, element, count)`` of every label of the spans,
    in the order of :data:`ELEMENTS`, then the unknown labels."""
    counts: dict[str, int] = {}
    for span in tags.get("spans") or []:
        counts[span.get("label")] = counts.get(span.get("label"), 0) + 1
    order = [label for label in ELEMENTS if label in counts] + sorted(
        label for label in counts if label not in ELEMENTS
    )
    return [(label, element_of(label), counts[label]) for label in order]
