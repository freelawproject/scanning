"""Parse the inline markup of the OCR engines into one shape (#404).

Each engine writes its formatting in its own dialect. dots.mocr and
Mistral write markdown in the unit text: ``*italic*``, ``**bold**``,
``## heading``, ``1. `` lists, ``¹`` superscripts, a ``<table>`` in
HTML; Mistral also writes a superscript as LaTeX, ``$^{4}$``. Surya
writes HTML in the block's ``html``: ``<i>``, ``<b>``, ``<sup>``,
``<h2>`` to ``<h4>``, ``<p>``, ``<br/>``, ``<ol><li>``, and entities.

Every parser returns a :class:`Parsed`: the plain text, standoff
:class:`Mark` entries over it (``em``, ``strong``, ``sup``, as character
offsets), and the block ``kind`` (``paragraph``, ``heading``,
``list_item``, ``table``). No document stores markup: the text is what
the ensemble compares and what the viewer draws with ``textContent``,
and the marks travel beside it. :func:`serialize` is the one writer of
a tagged string, for the editor, the XML and the tagger's projection.

**The whitespace contract.** ``Parsed.text`` holds one space between
words, keeps a line break as one ``\\n`` with no other whitespace
beside it, and has no leading or trailing whitespace. The ensemble's
``plain`` then changes no offset, only ``\\n`` to a space. The words
themselves are the engine's: a word broken at a line end
(``princi-\\nple``) stays broken, and joining it is its own step.

Measured over six volume documents (three engines, 2,300 pages) in the
comment of 2026-09-24 on #404: the heading level of a mark does not
follow the heading's own enumerator, so ``kind`` carries no level; the
engines mark italics where the print has them but each misses many,
so the ensemble unions the marks; ``<u>`` never occurs and is dropped.

Pure standard library, on purpose: the parse runs on the daemon and
must be reproducible in a test with a string.
"""

from __future__ import annotations

import html as _html
import re
from dataclasses import dataclass, field

EM = "em"
STRONG = "strong"
SUP = "sup"
INLINE_KINDS = (EM, STRONG, SUP)

#: A block mark (#411): it spans one run of paragraphs of a page's
#: text, never a part of one unit, so no parser writes it. The
#: ensemble writes it off the ``BLOCKQUOTE`` zone, and
#: :func:`serialize` writes its element once, at its two edges.
BLOCKQUOTE = "blockquote"
#: The two list marks (#428). Like a blockquote, a list spans a run of
#: groups of a page, so the ensemble writes it and no parser does.
BULLET_LIST = "ul"
NUMBERED_LIST = "ol"
LIST_TYPES = (BULLET_LIST, NUMBERED_LIST)
#: One item of a list (#428), over the item's text and never over its
#: marker: the parse takes the bullet off, because it is a glyph and
#: not a word, and it keeps the number of a numbered item, because an
#: opinion refers to "item (b)".
ITEM = "li"
#: The block marks, outermost first: the order :func:`serialize` opens
#: them in when they start together.
BLOCK_MARKS = (BLOCKQUOTE, BULLET_LIST, NUMBERED_LIST, ITEM)

PARAGRAPH = "paragraph"
HEADING = "heading"
LIST_ITEM = "list_item"
TABLE = "table"
BLOCK_KINDS = (PARAGRAPH, HEADING, LIST_ITEM, TABLE)

#: The order the tags nest in :func:`serialize`, outermost first, and
#: the order :func:`marks_of` sorts marks that start together.
_NESTING = (STRONG, EM, SUP)


@dataclass(frozen=True)
class Mark:
    """One formatted span of :attr:`Parsed.text`, ``end`` exclusive."""

    start: int
    end: int
    kind: str

    def as_dict(self) -> dict:
        return {"start": self.start, "end": self.end, "kind": self.kind}


@dataclass
class Parsed:
    """One unit's text in the shape every consumer reads."""

    text: str
    marks: list[Mark] = field(default_factory=list)
    kind: str = PARAGRAPH
    table: list[list[str]] | None = None


# ── The markdown dialect ────────────────────────────────────────────
SUP_CHARS = "⁰¹²³⁴⁵⁶⁷⁸⁹"
_SUP_TRANS = str.maketrans(SUP_CHARS, "0123456789")
_HEADING_MD = re.compile(r"^[ \t]*#{1,6}[ \t]+")
#: A bullet at the start of a line (#428). A glyph bullet is a bullet
#: under any label, and so is a dash. A ``*`` is one only under a list
#: label: on the six volumes of #404 a ``* `` at the start of a unit
#: with no list label is a footnote symbol (``* The syllabus
#: constitutes no part...``) in 36 of 36 units. A ``*`` that another
#: ``*`` follows is an asterism (``* * *``) under any label. The
#: middle dot is a bullet at the start of a line alone: an old print
#: sets a decimal point with it.
BULLET_GLYPHS = "•·▪◦●"
_GLYPH_BULLET = re.compile(rf"^([ \t]*)[{BULLET_GLYPHS}][ \t]*(?=\S)", re.M)
_DASH_BULLET = re.compile(r"^([ \t]*)-[ \t]+(?=\S)", re.M)
_STAR_BULLET = re.compile(r"^([ \t]*)\*(?![ \t]*\*)[ \t]+(?=\S)", re.M)
#: A bullet of the HTML dialect: at the start of the text, or after a
#: tag or a line break (Surya writes ``<p>• A<br/>• B</p>``).
_HTML_BULLET = re.compile(
    rf"(^|>|\n)([ \t]*)(?:[{BULLET_GLYPHS}]|&bull;|&#8226;)[ \t]*(?=\S)"
)
#: The enumerator of a numbered item: ``1.``, ``1)``, ``(1)``, ``a.``,
#: ``(a)``, ``iv.``, ``(iv)``. It starts an item only at the start of a
#: unit the engine labels a list (#428): a line inside a unit that
#: starts with ``v. Smith`` is a wrapped case name, and a paragraph
#: that starts with ``5.`` is a footnote or a numbered paragraph far
#: more often than a list item. A ``[2]`` is a headnote number.
_ENUMERATOR = (
    r"(?:\((?:\d{1,3}|[a-zA-Z]|[ivxlcdmIVXLCDM]{1,6})\)"
    r"|(?:\d{1,3}|[a-zA-Z]|[ivxlcdmIVXLCDM]{1,6})[.)])"
)
ENUMERATOR = re.compile(rf"^{_ENUMERATOR}$")
_ENUMERATED_START = re.compile(rf"^[ \t]*{_ENUMERATOR}[ \t]+\S")
#: The start of an item inside a text under parse: a character of the
#: private use area, which no engine writes. :class:`_Out` reads it as
#: "the next word starts an item" and adds no character.
_ITEM_START = "\ue000"
#: The flag :class:`_Out` puts on the first character of an item.
_ITEM_FLAG = "item"
_TABLE_START = re.compile(r"^\s*<table\b", re.I)
#: A tag of either dialect: a name and attributes with values. Surya
#: copies a literal angle bracket of the print unescaped (``"Untrue
#: <a false statement>"``), and a looser shape would eat those words.
_TAG_SHAPE = r"<(?P<close>/?)(?P<name>[a-zA-Z][a-zA-Z0-9]*)(?:\s+[a-zA-Z-]+=\"[^\"]*\")*\s*/?>"

#: One pass over a markdown text. The alternatives are tried in this
#: order at every position: bold before italic, the LaTeX superscript
#: before a dollar amount can read as one, a tag before an entity. An
#: italic may cross one line break (Mistral keeps the print's lines,
#: and a case name wraps) and no more: a star page (``at *5``) opens
#: like an italic, and the closer must not be found lines away.
_MD = re.compile(
    r"(?P<escape>\\(?P<escaped>[*_#\\]))"
    r"|(?P<both>\*\*\*(?!\s)(?P<both_in>[^*\n]+?)(?<!\s)\*\*\*)"
    r"|(?P<strong>\*\*(?!\s)(?P<strong_in>.+?)(?<!\s)\*\*)"
    r"|(?P<em>(?<![\w*])\*(?![\s*])(?P<em_in>[^*\n]+?(?:\n[^*\n]+?)?)(?<![\s*])\*(?![\w*]))"
    r"|(?P<latex>\$\^\{(?P<latex_in>[^}]*)\}\$)"
    rf"|(?P<usup>[{SUP_CHARS}]+)"
    r"|(?P<image>!\[[^\]]*\]\([^)]*\))"
    rf"|(?P<tag>{_TAG_SHAPE})"
    r"|(?P<entity>&(?:#\d+|#x[0-9a-fA-F]+|[a-zA-Z]+);)"
    r"|(?P<star>(?:(?<=[^\s*])|(?<=\w\s))\*(?![\w*])(?!\s*\*))",
    re.S,
)

# ── The HTML dialect ────────────────────────────────────────────────
_HTML = re.compile(
    rf"(?P<tag>{_TAG_SHAPE})"
    r"|(?P<entity>&(?:#\d+|#x[0-9a-fA-F]+|[a-zA-Z]+);)"
)
_INLINE_TAGS = {"i": EM, "em": EM, "b": STRONG, "strong": STRONG, "sup": SUP}
#: ``heading`` is :func:`serialize`'s own element, read back the same.
_HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6", "heading"}
#: A closing tag that ends a line of the text. ``<br>`` is one too.
_BREAK_TAGS = {"p", "div", "li", "tr", "br"} | _HEADING_TAGS
_ROW = re.compile(r"<tr\b[^>]*>(.*?)</tr>", re.I | re.S)
_CELL = re.compile(r"<t[dh]\b[^>]*>(.*?)</t[dh]>", re.I | re.S)
#: Inside a table cell, an inline tag is dropped without a space, so
#: ``Rents<sup>30</sup>`` stays one word; any other tag is a space.
_INLINE_TAG = re.compile(r"</?(?:i|em|b|strong|sup|sub|u)\b[^>]*>", re.I)
_TAG = re.compile(r"<[^>]+>")
_SPACES = re.compile(r"[ \t \r\f\v]+")


class _Out:
    """The text under construction: one flag set per character.

    Every rule adds characters under the marks open at that moment, so
    a nested mark costs nothing and no offset is ever moved by hand.
    The marks are read off the runs of flags at the end.
    """

    def __init__(self) -> None:
        self.chars: list[str] = []
        self.flags: list[frozenset[str]] = []
        self.open: dict[str, int] = {}
        self.kind: str = PARAGRAPH
        self.item_open = False

    def add(self, text: str) -> None:
        flags = frozenset(kind for kind, count in self.open.items() if count)
        for char in text:
            if char == _ITEM_START:
                self.begin_item()
                continue
            if self.item_open and not char.isspace():
                self.chars.append(char)
                self.flags.append(flags | {_ITEM_FLAG})
                self.item_open = False
                continue
            self.chars.append(char)
            self.flags.append(flags)

    def begin_item(self) -> None:
        """Start an item at the next character that is not a space."""
        self.item_open = True
        self.set_kind(LIST_ITEM)

    def begin(self, kind: str) -> None:
        self.open[kind] = self.open.get(kind, 0) + 1

    def end(self, kind: str) -> None:
        if self.open.get(kind):
            self.open[kind] -= 1

    def newline(self) -> None:
        self.add("\n")

    def set_kind(self, kind: str) -> None:
        if self.kind == PARAGRAPH:
            self.kind = kind


def _tag(out: _Out, closing: bool, name: str) -> None:
    """Apply one tag of either dialect. An unknown tag is dropped."""
    name = name.lower()
    if name in _INLINE_TAGS:
        (out.end if closing else out.begin)(_INLINE_TAGS[name])
    elif name == "br":
        out.newline()
    elif name in _HEADING_TAGS:
        out.set_kind(HEADING)
        if closing:
            out.newline()
    elif name == "li":
        if closing:
            out.newline()
        else:
            out.begin_item()
    elif closing and name in _BREAK_TAGS:
        out.newline()


def _scan_markdown(text: str, out: _Out) -> None:
    at = 0
    for match in _MD.finditer(text):
        out.add(text[at : match.start()])
        at = match.end()
        group = match.lastgroup
        if group == "escape":
            # A markdown escape: ``\*`` is the character, as the print
            # has it (Mistral writes an asterism as ``\* \* \*``).
            out.add(match.group("escaped"))
        elif group == "both":
            out.begin(STRONG)
            out.begin(EM)
            _scan_markdown(match.group("both_in"), out)
            out.end(EM)
            out.end(STRONG)
        elif group == "strong":
            out.begin(STRONG)
            _scan_markdown(match.group("strong_in"), out)
            out.end(STRONG)
        elif group == "em":
            out.begin(EM)
            _scan_markdown(match.group("em_in"), out)
            out.end(EM)
        elif group == "latex":
            out.begin(SUP)
            out.add(match.group("latex_in").strip())
            out.end(SUP)
        elif group == "usup":
            out.begin(SUP)
            out.add(match.group("usup").translate(_SUP_TRANS))
            out.end(SUP)
        elif group == "star":
            out.begin(SUP)
            out.add("*")
            out.end(SUP)
        elif group == "tag":
            _tag(out, bool(match.group("close")), match.group("name"))
        elif group == "entity":
            out.add(_html.unescape(match.group("entity")))
        # An image placeholder adds nothing.
    out.add(text[at:])


def _scan_html(text: str, out: _Out) -> None:
    at = 0
    for match in _HTML.finditer(text):
        out.add(text[at : match.start()])
        at = match.end()
        if match.lastgroup == "tag":
            _tag(out, bool(match.group("close")), match.group("name"))
        else:
            out.add(_html.unescape(match.group("entity")))
    out.add(text[at:])


def _normalize_whitespace(out: _Out) -> None:
    """Apply the whitespace contract: one space, one ``\\n``, no edges.

    A kept space takes the flags both its neighbours share, so a phrase
    keeps its italic across its spaces and a mark never starts or ends
    on a space.
    """
    chars: list[str] = []
    flags: list[frozenset[str]] = []
    run: list[str] = []
    for char, flag in zip(out.chars, out.flags, strict=True):
        if char.isspace():
            run.append(char)
            continue
        if run and chars:
            chars.append("\n" if "\n" in run else " ")
            flags.append(frozenset())
        run = []
        chars.append(char)
        flags.append(flag)
    for index, char in enumerate(chars):
        if char in (" ", "\n") and 0 < index < len(chars) - 1:
            flags[index] = (flags[index - 1] & flags[index + 1]) - {_ITEM_FLAG}
    out.chars, out.flags = chars, flags


def item_marks(text: str, starts: list[int]) -> list[Mark]:
    """Return one :data:`ITEM` mark per item of ``text`` (#428).

    An item runs from its start to the start of the next one, less the
    whitespace before it, and the last one to the end of the text. The
    text before the first start is no item: it is the end of an item
    that began above, in another unit or another column.

    :param text: The text.
    :param starts: The offsets where the items start.
    :returns: The marks, in order.
    :rtype: list[Mark]
    """
    edges = sorted(set(starts))
    marks = []
    for index, start in enumerate(edges):
        end = edges[index + 1] if index + 1 < len(edges) else len(text)
        end = start + len(text[start:end].rstrip())
        if end > start:
            marks.append(Mark(start, end, ITEM))
    return marks


def sort_key(kind: str) -> int:
    """Return the rank of a mark kind among the marks that start
    together: the block marks outermost first, then :data:`_NESTING`."""
    if kind in BLOCK_MARKS:
        return BLOCK_MARKS.index(kind)
    if kind in _NESTING:
        return len(BLOCK_MARKS) + _NESTING.index(kind)
    return len(BLOCK_MARKS) + len(_NESTING)


def marks_of(flags: list[frozenset[str]]) -> list[Mark]:
    """Read the marks off the runs of flags, sorted by start."""
    marks: list[Mark] = []
    for kind in _NESTING:
        start = None
        for index, flag in enumerate([*flags, frozenset()]):
            if kind in flag and start is None:
                start = index
            elif kind not in flag and start is not None:
                marks.append(Mark(start, index, kind))
                start = None
    return sorted(marks, key=lambda m: (m.start, _NESTING.index(m.kind)))


def _finish(out: _Out) -> Parsed:
    _normalize_whitespace(out)
    text = "".join(out.chars)
    starts = [i for i, flag in enumerate(out.flags) if _ITEM_FLAG in flag]
    marks = marks_of(out.flags)
    if out.kind == LIST_ITEM:
        marks = sorted(
            item_marks(text, starts) + marks,
            key=lambda m: (m.start, sort_key(m.kind)),
        )
    return Parsed(text=text, marks=marks, kind=out.kind)


def _cell_text(fragment: str) -> str:
    text = _html.unescape(_TAG.sub(" ", _INLINE_TAG.sub("", fragment)))
    return _SPACES.sub(" ", text.replace("\n", " ")).strip()


def parse_table(text: str) -> Parsed:
    """Read a ``<table>`` the engines write in HTML as rows of cells.

    :param text: The unit's text, starting with ``<table``.
    :returns: ``kind == "table"``, ``table`` the rows, ``text`` the
        cells joined by a space per row and a line per row, no marks.
    :rtype: Parsed
    """
    rows = [
        [_cell_text(cell) for cell in _CELL.findall(row)]
        for row in _ROW.findall(text)
    ]
    rows = [row for row in rows if row]
    lines = [" ".join(cell for cell in row if cell) for row in rows]
    return Parsed(
        text="\n".join(line for line in lines if line),
        kind=TABLE,
        table=rows,
    )


def parse_markdown(text: str, kind: str | None = None) -> Parsed:
    """Parse the dialect of dots.mocr and Mistral.

    :param text: The cell's or the block's text as the engine wrote it.
    :param kind: The kind the engine's own label gives the unit
        (``Section-header`` is a heading whether or not it wrote
        ``##``), or None for what the text says.
    :returns: The plain text, its marks and its kind.
    :rtype: Parsed
    """
    text = text or ""
    if _TABLE_START.match(text):
        return parse_table(text)
    out = _Out()
    match = _HEADING_MD.match(text)
    if match:
        out.set_kind(HEADING)
        text = text[match.end() :]
    if kind and kind != TABLE:
        # The engine's layout label, before the shape of the first
        # line: a ``Section-header`` that reads ``1. Standard of
        # Review`` is a heading and not a list item. A ``Table`` label
        # over a text that is no ``<table>`` names no table: the rows
        # are the table, and a kind with no rows would show no text.
        out.set_kind(kind)
    if out.kind in (PARAGRAPH, LIST_ITEM):
        text = _mark_items(text, listed=out.kind == LIST_ITEM)
    _scan_markdown(text, out)
    return _finish(out)


def _mark_items(text: str, listed: bool) -> str:
    """Put :data:`_ITEM_START` where each item of a text starts (#428).

    A bullet at the start of a line is replaced: it is the marker, not
    a word. Under a list label a ``*`` is a bullet too, and a text that
    starts with an enumerator starts an item there, the enumerator
    kept. Mistral writes ``- (a) the named insured``: the dash is its
    markdown list and the ``(a)`` is the print's.

    :param text: The unit's text, after the heading marks.
    :param listed: Whether the engine labels the unit a list.
    :returns: The text with the item starts in it.
    :rtype: str
    """
    marked = rf"\1{_ITEM_START}"
    text = _GLYPH_BULLET.sub(marked, text)
    text = _DASH_BULLET.sub(marked, text)
    if listed:
        text = _STAR_BULLET.sub(marked, text)
        if _ENUMERATED_START.match(text):
            text = _ITEM_START + text
    return text


def parse_html(html: str, kind: str | None = None) -> Parsed:
    """Parse the dialect of Surya.

    :param html: The block's ``html``.
    :param kind: The kind the engine's own label gives the unit, or
        None for what the tags say.
    :returns: The plain text, its marks and its kind.
    :rtype: Parsed
    """
    html = html or ""
    if _TABLE_START.match(html):
        return parse_table(html)
    out = _Out()
    _scan_html(_HTML_BULLET.sub(rf"\1\2{_ITEM_START}", html), out)
    if kind and kind != TABLE:
        out.set_kind(kind)
    return _finish(out)


# ── After the parse ─────────────────────────────────────────────────
def shift(marks: list[Mark], deleted: list[tuple[int, int]]) -> list[Mark]:
    """Move the marks over deletions made in their text.

    :param marks: Marks over the text before the deletions.
    :param deleted: ``(start, end)`` of every deleted span, in the
        offsets of that same text.
    :returns: The marks over the text after the deletions: a mark
        inside a deletion is gone, one across it is clipped, one after
        it moves left.
    :rtype: list[Mark]
    """
    if not deleted:
        return list(marks)
    gone: set[int] = set()
    for start, end in deleted:
        gone.update(range(start, end))
    kept_before: list[int] = []
    count = 0
    for index in range(max((end for _, end in deleted), default=0) + 1):
        kept_before.append(count)
        if index not in gone:
            count += 1
    last = len(kept_before) - 1

    def new(offset: int) -> int:
        if offset > last:
            return kept_before[last] + (offset - last)
        return kept_before[offset]

    out = []
    for mark in marks:
        start, end = new(mark.start), new(mark.end)
        if end > start:
            out.append(Mark(start, end, mark.kind))
    return out


def serialize(parsed: Parsed) -> str:
    """Write the one tagged string of a parsed unit, or of a page.

    A mark of :data:`BLOCK_MARKS` is written once, at its two edges,
    and outside every inline element (#411): a blockquote spans
    paragraphs, and an element per segment would write one quote per
    italic. Block marks that start together open outermost first, the
    order of :data:`BLOCK_MARKS`, and close in the reverse order. Its edges cut the inline marks, so every element closes
    inside the one it opened in. A caller serializes a page's text with
    the marks of its section (``OpinionText.marks``).

    :param parsed: The unit, or a page's text and its marks.
    :returns: The text with ``<em>``, ``<strong>`` and ``<sup>`` at the
        marks (outer to inner in :data:`_NESTING` order, one element
        per segment), ``<blockquote>``, ``<ul>``, ``<ol>`` and ``<li>``
        at the block marks, ``&``, ``<`` and ``>`` escaped, and the
        block kind as ``<heading>`` or a ``<table>``. A list item is
        its ``li`` marks (#428), never its kind: a unit that goes on
        with the item of the unit above has the kind and no mark.
    :rtype: str
    """
    if parsed.kind == TABLE:
        rows = "".join(
            "<tr>"
            + "".join(
                f"<td>{_html.escape(cell, quote=False)}</td>" for cell in row
            )
            + "</tr>"
            for row in parsed.table or []
        )
        return f"<table>{rows}</table>"
    text = parsed.text
    edges = sorted(
        {
            0,
            len(text),
            *(m.start for m in parsed.marks),
            *(m.end for m in parsed.marks),
        }
    )
    blocks = sorted(
        (m for m in parsed.marks if m.kind in BLOCK_MARKS and m.end > m.start),
        key=lambda m: (-m.end, sort_key(m.kind)),
    )
    parts: list[str] = []
    for start, end in zip(edges, edges[1:], strict=False):
        parts.extend(f"<{m.kind}>" for m in blocks if m.start == start)
        covering = [
            m.kind for m in parsed.marks if m.start <= start and end <= m.end
        ]
        segment = _html.escape(text[start:end], quote=False)
        for kind in reversed(_NESTING):
            if kind in covering:
                segment = f"<{kind}>{segment}</{kind}>"
        parts.append(segment)
        parts.extend(f"</{m.kind}>" for m in reversed(blocks) if m.end == end)
    body = "".join(parts)
    if parsed.kind == HEADING:
        return f"<heading>{body}</heading>"
    return body
