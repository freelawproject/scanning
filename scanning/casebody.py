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
``decisiondate``, ``attorneys``, ...) before one ``opinion``, whose
paragraphs carry the marks of the approved text, the ``page-number``
of every page the text crosses, and the ``footnote`` elements at its
end.

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

The document is computed at each request and stored nowhere: the
export for CourtListener is #408. Pure standard library plus
``markup``, on purpose, like ``paragraphs``: a test builds it from two
dicts. :func:`display_html` and :func:`source_html` are the two views
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

#: The CAP element that holds a run of ``party`` and ``separator``.
PARTIES = "parties"
PARTY_ELEMENTS = frozenset({ELEMENTS["party"], ELEMENTS["separator"]})

#: The label whose first paragraph starts the opinion: the head matter
#: is every paragraph before it.
AUTHOR = "author"

#: The element of a ``sup`` mark whose text is a footnote label.
FOOTNOTE_MARK = "footnotemark"
PAGE_NUMBER = "page-number"

#: The layers of the inline elements, outermost first: a list item
#: holds a span, and a span holds the marks of the approved text.
_BLOCK, _SPAN, _INLINE = 0, 1, 2
_INLINE_RANK = {markup.STRONG: 0, markup.EM: 1, markup.SUP: 2}


#: What a span may leave out at the edges of a paragraph it covers
#: whole: space and punctuation, never a word.
_EDGE = " \n\t.,;:"


class CasebodyError(Exception):
    """The spans do not describe the approved text."""


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
_NOT_XML = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]")


def element_of(label: str) -> str:
    """Return the CAP element of one tagger label.

    A label of :data:`ELEMENTS` gives its CAP name. Any other label
    keeps its own name where that is an XML name; otherwise every
    character an XML name cannot hold is a ``-``, after a ``label-``
    where the name would start badly or with ``xml``, the prefix XML
    keeps for itself. So a new label of the model shows in the XML
    and never breaks it; the spans object keeps the label as it was.
    """
    if label in ELEMENTS:
        return ELEMENTS[label]
    if _XML_NAME.match(label) and not label.lower().startswith("xml"):
        return label
    name = re.sub(r"[^\w.-]", "-", label) or "label"
    if not _XML_NAME.match(name) or name.lower().startswith("xml"):
        name = f"label-{name}"
    return name


def _escape(text: str) -> str:
    return _html.escape(_NOT_XML.sub("", text), quote=False)


def _attrs(attrs: dict) -> str:
    return "".join(
        f' {name}="{_html.escape(_NOT_XML.sub("", str(value)), quote=True)}"'
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
    # The worker trims a span to its words, so a span that leaves out a
    # full stop at an edge still covers the paragraph whole.
    body_start = len(text) - len(text.lstrip(_EDGE))
    body_end = len(text.rstrip(_EDGE))
    for span in spans:
        start, end = at(span["start"]), at(span["end"])
        if not text[start:end].strip():
            continue
        element = element_of(span["label"])
        if (
            whole is None
            and name in ("p", "heading")
            and not paragraph.get("blockquote")
            and start <= body_start
            and end >= body_end
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

    The head matter is the leading run of paragraphs that carry a span,
    and it ends early at an ``author`` span inside that run. An author
    after the run is no end of the head matter: a per curiam opinion has
    no author line, and the first author span is then its dissent's.

    :param body: The body paragraphs.
    :param by: The spans of each paragraph (:func:`_spans_by_paragraph`).
    :returns: The index.
    :rtype: int
    """
    run = 0
    while run < len(body) and run in by:
        run += 1
    for index in range(run):
        if any(span["label"] == AUTHOR for span in by[index]):
            return index
    return run


def _comment(text: str) -> str:
    return "<!-- " + re.sub(r"-{2,}", "-", text) + " -->"


def build(approved: dict, tags: dict) -> str:
    """Return the final XML of one opinion.

    :param approved: The approved object (``paragraphs.approved_document``).
    :param tags: The spans object (``tagger.glue_run``).
    :returns: The XML document, one block element per line.
    :rtype: str
    :raises CasebodyError: When a span does not address the body.
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
            }
        )
        + ">"
    )
    out.extend(lines(blocks[:split], "  "))
    out.append("  <opinion>")
    out.extend(lines(blocks[split:], "    "))
    for note in notes:
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
#: The elements that give the text its shape. Every other element is a
#: role: a tag of the tagger (or ``parties``), drawn with the tint of its
#: role, the review sheet of centralia.
STRUCTURE = frozenset({
    "casebody", "opinion", "p", "blockquote", "heading", "footnote",
    "ul", "ol", "li", "table", "tr", "td", "em", "strong", "sup",
    FOOTNOTE_MARK, PAGE_NUMBER,
})  # fmt: skip
#: The marks of the approved text, drawn as the text they format.
_PLAIN = frozenset({"em", "strong", "sup", "ul", "ol", "li", "blockquote"})


def _role(tag: str) -> str:
    return _html.escape(tag, quote=True)


def _note_id(label: str) -> str:
    """Return the HTML id of the footnote with this label.

    Every character that is not a letter or a digit is spelled as its
    code point, so ``*`` and ``†`` give ids too, and two labels never
    give one id.
    """
    safe = re.sub(r"[^A-Za-z0-9]", lambda m: f"_{ord(m.group()):x}", label)
    return f"cb-fn-{safe}"


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
    such link: CAP names a note by its label alone.

    :param xml: From :func:`build`.
    :returns: The HTML fragment.
    :rtype: str
    """
    root = ET.fromstring(xml)
    #: The mark ids of each footnote label, in the order they read.
    marks: dict[str, list[str]] = {}

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
            ref = f"{_note_id(label)}-ref-{len(refs) + 1}"
            refs.append(ref)
            return (
                f'<sup class="cb-fnmark" id="{ref}" title="footnotemark">'
                f'<a href="#{_note_id(label)}">{inner}</a></sup>'
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

    opinion = root.find("opinion")
    head = [child for child in root if child.tag != "opinion"]
    parts = ['<div class="cb-doc">']
    if head:
        parts.append(
            '<section class="cb-headmatter"><h2>headmatter</h2>'
            f"{rows(head)}</section>"
        )
    if opinion is not None:
        body = [child for child in opinion if child.tag != "footnote"]
        notes = [child for child in opinion if child.tag == "footnote"]
        parts.append(
            f'<section class="cb-opinion"><h2>opinion</h2>{rows(body)}'
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
                    anchor = f' id="{_note_id(label)}"'
                    seen.add(label)
                text = _escape(label or "")
                if refs and anchor:
                    text = (
                        f'<a href="#{refs[0]}" title="Back to the text">'
                        f"{text}</a>"
                    )
                    text += "".join(
                        f' <a class="cb-back" href="#{ref}" '
                        f'title="Back to mark {n}">&#8617;{n}</a>'
                        for n, ref in enumerate(refs[1:], start=2)
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
    r"(?P<comment><!--.*?-->)"
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
