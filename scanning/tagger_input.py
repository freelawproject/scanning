"""Turn a volume's OCR document and its reviewed detections into the
case-law block tagger's input: one sequence per opinion, and the map
that places the answer back on the pages.

The worker (``scanning/runpod-caselaw-tagger/``) tags sequences and
returns character spans over the text it was sent. It never sees the
volume, so everything that decides *what* text an opinion holds is
decided here, from three sources:

- **The glued dots.mocr document** (``dots_mocr.finish_ready_runs``):
  per page, the cells in reading order with a category, a box and the
  text. It supplies the words and the geometry, and its categories are
  the fallback where nothing else speaks.
- **The reviewed detections**: the ``Detection`` rows that are
  ``live()`` after review 2 -- the model's boxes with the curator's
  approvals and deactivations folded in, plus the boxes a curator drew
  by hand. A cell whose centre lies in a ``FOOTNOTES`` box is a
  footnote, held out of the sequence and wired back at assembly by its
  ``<sup>`` mark. A ``KEY_ICON`` box closes an opinion. A cell in a
  head-band ``PAGE_HEADER``, ``PAGE_NUMBER`` or ``STATE_ABBREVIATION``
  box is furniture. An ``IMAGE`` box is a figure: not model input,
  recorded on the map so the assembly step can crop and embed it.
- **The redaction geometry**: the rects the redaction compute derives
  from those same boxes, which are what the final PDF blacks out. A
  cell whose centre lies in one is not sent. This is the one rule for
  West's editorial matter, the running heads, a stray key icon and the
  tables before the first opinion, and it is deliberately not a class
  list: the Supreme Court writes its own syllabus, the redaction rules
  know that, and this module must not second-guess them.

**Opinion boundaries come from the reviewed rows** when the caller
has them (``boundaries``, the ``OpinionBoundary`` rows of #240 PR C as
anchors in the frame): a person walked every caption to its key icon
in review 2, and that is the split (:func:`ranges_from_anchors`).
Without one, the fallback cuts at the reviewed ``KEY_ICON`` boxes
alone, since one icon closes one opinion; a caption box marks its
cells but opens nothing by itself.
With no boxes at all the volume is one sequence: that is a test
shape, not a production one.

**Printed page numbers come from the scan** (``Scan.ocr_results``, the
reading of #228 with the curator's corrections), handed in as
``printed_pages``. This module does not read the running head: the
prototype it was ported from had to, having no other source, and
scanning has one.

The port keeps what scanning has no other source for and what was
measured over sixteen volumes in that prototype: the column ordering
that puts table pages back in reading order, the markdown to inline-HTML
rendering, the footnote label split, dots' "Page-header" below the band
read as text, and the repetition-loop garbage filter. What it replaces
is the layout join, which read container-yolo boxes and dots' own
picture cells; here the reviewed rows are the layout.

Pure standard library, on purpose: this module runs on the daemon,
and the same serialization must be reproducible in a test with a dict.
"""

from __future__ import annotations

import html as _html
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

#: Bumped whenever the serialization changes in a way that would make a
#: stored input document differ for the same OCR document and boxes.
#: Part of every row's identity, so a converter change starts a new run
#: rather than reusing a result computed over different text.
CONVERTER_VERSION = 2

#: The frame every box here is measured in: a 200 dpi render of the
#: page. dots.mocr renders at ``dots_mocr.DPI`` and bl_warm at
#: ``yolo.DPI``, both 200, so the cells and the detections share it up
#: to a pixel of rounding. The ``Redaction`` and ``OpinionBoundary``
#: rows are in PDF points (#240), and :func:`points_to_frame` brings
#: them here; the page size in points is not in the OCR document, so
#: the scale is the constant, the fallback ``boundaries.to_points``
#: uses the other way, and the two answers differ by under a point.
FRAME_DPI = 200
POINTS_PER_INCH = 72.0

#: How far a block must reach past an anchor to be counted as at or
#: after it, in frame pixels: the caption's top and the first caption
#: cell's top are the same edge measured by two models, and a cell
#: above that ends a few pixels late must not open the opinion.
ANCHOR_SLACK_PX = 4.0

#: The dots.mocr layout categories, mapped to what they mean here.
KIND_BY_CATEGORY = {
    "Text": "text",
    "Section-header": "text",
    "List-item": "text",
    "Page-footer": "text",  # measured: dots' "footers" are body text
    "Table": "text",
    "Title": "text",
    "Formula": "text",
    "Caption": "text",
    "Footnote": "footnote",
    "Page-header": "header",
    "Picture": "figure",  # a key icon only when a KEY_ICON box says so
}

#: The running head (case name, folio, the "Cite as" line) lies within
#: the top 9 % of the page. A dots "Page-header" cell there is
#: furniture; one below it is body text dots mislabelled (a list
#: number, a parallel cite, a whole table page).
HEAD_BAND = 0.09

#: Detection labels, as ``Detection.label`` spells them.
LABEL_CAPTION = "CASE_CAPTION"
LABEL_KEY_ICON = "KEY_ICON"
LABEL_FOOTNOTES = "FOOTNOTES"
LABEL_IMAGE = "IMAGE"
FURNITURE_LABELS = frozenset(
    {"PAGE_HEADER", "PAGE_NUMBER", "STATE_ABBREVIATION"}
)

#: The labels whose box decides what a cell is (:func:`apply_boxes`).
CLAIMING_LABELS = (
    frozenset({LABEL_FOOTNOTES, LABEL_CAPTION, LABEL_KEY_ICON})
    | FURNITURE_LABELS
)

#: Kinds that are model input. Everything else is held out: footnotes
#: and figures for the assembly step, furniture and garbage for good.
INPUT_KINDS = frozenset({"text"})

# ── Markup ──────────────────────────────────────────────────────────
SUP_CHARS = "⁰¹²³⁴⁵⁶⁷⁸⁹"
_SUP_TRANS = str.maketrans(SUP_CHARS, "0123456789")
_SUP_RE = re.compile(f"[{SUP_CHARS}]+")
_KEEP_HYPHEN = {
    "non", "self", "cross", "co", "pre", "post", "anti", "ex", "semi",
    "well", "mid", "quasi", "pro", "multi", "all", "half", "de", "sub",
    "one", "two", "three", "four", "five", "six", "seven", "eight",
    "nine", "ten", "twenty", "thirty", "first", "second", "third",
    "fourth", "fifth", "sixth", "so", "in", "step", "then", "vice",
    "re", "attorney", "brother", "sister", "mother", "father", "son",
    "daughter", "counter", "long", "short", "high", "low", "full",
    "part", "on", "off", "out", "up", "down", "over", "under",
}  # fmt: skip
_HEADING_MD = re.compile(r"^\s*#{1,6}\s+", re.M)
_UNDERLINE = re.compile(r"</?u>")
_BOLD = re.compile(r"\*\*(?!\*)(.+?)(?<!\*)\*\*", re.S)
_LIST_MARK = re.compile(r"^\s*[\*•·]\s+")
_HYPHEN_EOL = re.compile(r"(\w+)-\n(\w+)")
_EM = re.compile(r"(?<![\w*])\*(?![\s*])(.+?)(?<![\s*])\*(?![\w*])")
_LONE_STAR = re.compile(r"(?:(?<=[^\s*])|(?<=\w\s))\*(?![\w*])(?!\s*\*)")
_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"[ \t ]+")
_NOT_LABEL = (
    r"(?!\s*(?:U\.\s?S\.\s?C|C\.\s?F\.\s?R|Fed\.|F\.\s?\d|So\.|S\.\s?Ct|"
    r"L\.\s?Ed|U\.S\.\s|Stat\.|Cir\.|Am\.\s?Jur))"
)
FOOTNOTE_LABEL = re.compile(
    rf"^\s*(?:(\d{{1,3}})\.\s+{_NOT_LABEL}|([{SUP_CHARS}]+)\s*|(\*{{1,3}}|†|‡)\s+)"
)
FOOTNOTE_LABEL_LOOSE = re.compile(
    rf"^\s*(?:(\d{{1,3}})(?:\.\s*{_NOT_LABEL}(?=\S)|(?:\)\s*|\s+){_NOT_LABEL}"
    rf"(?=[A-Z\"“(\[<*]))|([{SUP_CHARS}]+)\s*|(\*{{1,3}}|†|‡)\s+)"
)
_TABLE_TAGS = re.compile(
    r"</?(table|thead|tbody|tfoot|tr|th|td|caption|colgroup|col)\b[^>]*>",
    re.I,
)


@dataclass
class Rendered:
    """One cell's text in the three forms the pipeline needs."""

    html: str
    text: str
    marks: list[str] = field(default_factory=list)


def dehyphenate(s: str) -> str:
    """Join words dots broke at a line end, keeping real hyphens.

    :param s: Cell text with hard line breaks.
    :returns: The text with end-of-line hyphenation undone.
    :rtype: str
    """

    def repl(m: re.Match) -> str:
        left, right = m.group(1), m.group(2)
        if left.lower() in _KEEP_HYPHEN:
            return f"{left}-{right}"
        return left + right

    return _HYPHEN_EOL.sub(repl, s)


def _join_lines(s: str) -> str:
    s = dehyphenate(s)
    s = s.replace("\n", " ")
    return _WS.sub(" ", s).strip()


def strip_tags(h: str) -> str:
    """Inline HTML to plain text.

    :param h: Inline HTML.
    :returns: The text, entities unescaped.
    :rtype: str
    """
    return _html.unescape(_TAG.sub("", h))


def render(raw: str) -> Rendered:
    """Turn a dots.mocr cell's text into the tagger's inline HTML.

    dots hands back markdown-ish text: ``*italic*``, ``**bold**``,
    ``## heading``, ``<u>``, hard line breaks, end-of-line hyphenation,
    footnote marks as Unicode superscript digits. The model wants
    minimal HTML: ``<em>`` and ``<sup>`` only, escaped.

    :param raw: The cell's ``text``.
    :returns: The HTML, the plain text and the footnote marks in order.
    :rtype: Rendered
    """
    s = raw or ""
    s = _HEADING_MD.sub("", s)
    s = _UNDERLINE.sub("", s)
    s = _join_lines(s)
    s = _BOLD.sub(r"\1", s)
    s = _LIST_MARK.sub("• ", s)
    s = _html.escape(s, quote=False)
    s = _EM.sub(r"<em>\1</em>", s)
    s = _LONE_STAR.sub("<sup>*</sup>", s)

    def sup(m: re.Match) -> str:
        return f"<sup>{m.group(0).translate(_SUP_TRANS)}</sup>"

    s = _SUP_RE.sub(sup, s)
    marks = re.findall(r"<sup>([^<]+)</sup>", s)
    return Rendered(html=s, text=strip_tags(s), marks=marks)


def split_footnote_label(
    raw: str, loose: bool = False
) -> tuple[str | None, str]:
    """Split a footnote cell into its label and its text.

    :param raw: The cell's text.
    :param loose: Accept the looser label shapes of a cell already
        known to be a footnote (``1 Text``, ``1) Text``).
    :returns: ``(label, rest)``; the label is None for a continuation.
    :rtype: tuple[str | None, str]
    """
    raw = _HEADING_MD.sub("", raw or "")
    m = (FOOTNOTE_LABEL_LOOSE if loose else FOOTNOTE_LABEL).match(raw)
    if (
        not m
        and loose
        and re.match(r"^\s*\*[A-Z]", raw)
        and raw.count("*") == 1
    ):
        return "*", raw.lstrip()[1:]
    if not m:
        return None, raw
    label = (
        m.group(1) or (m.group(2) or "").translate(_SUP_TRANS) or m.group(3)
    )
    return label, (raw or "")[m.end() :]


def table_html(raw: str) -> str:
    """Keep only the structure tags of a table dots emitted as HTML.

    :param raw: The cell's text.
    :returns: The table with attributes and foreign tags removed.
    :rtype: str
    """
    parts = re.split(r"(<[^>]+>)", raw.strip())
    out = []
    for part in parts:
        if part.startswith("<"):
            m = _TABLE_TAGS.fullmatch(part)
            if m:
                out.append(
                    ("</" if part.startswith("</") else "<")
                    + m.group(1).lower()
                    + ">"
                )
        else:
            out.append(part.replace("&nbsp;", " "))
    return "".join(out)


# ── The document as blocks ──────────────────────────────────────────
@dataclass
class Block:
    """One dots.mocr cell, classified.

    ``kind`` decides its fate: ``text`` is model input; ``footnote`` and
    ``figure`` are held out for the assembly step; ``header``,
    ``picture`` (a key icon), ``empty``, ``garbage`` and ``redacted``
    are never sent.
    """

    idx: int
    page_index: int
    pdf_page: int
    printed_page: str | None
    cell_index: int
    category: str
    bbox: list[int]
    raw: str
    kind: str
    html: str = ""
    text: str = ""
    marks: list[str] = field(default_factory=list)
    fn_label: str | None = None
    box: str | None = None  # the reviewed box that claimed the cell


@dataclass
class PageInfo:
    """One page of the OCR document."""

    page_index: int
    pdf_page: int
    printed_page: str | None
    failed: bool
    width: int  # the render the cell boxes are measured in
    height: int


@dataclass
class Volume:
    """The OCR document as ordered blocks."""

    meta: dict
    pages: dict[int, PageInfo]
    blocks: list[Block]
    skipped_pages: list[int]

    def text_blocks(self) -> list[Block]:
        return [b for b in self.blocks if b.kind in INPUT_KINDS]


def md_cells(md: str) -> list[dict]:
    """Pseudo-cells for a page whose layout JSON failed but whose
    transcript survived: one cell per markdown paragraph, no box.

    Position-dependent steps (column order, the boxes) skip these
    cells, so a page read this way is sent as body text in transcript
    order, footnotes excepted.

    :param md: The page's ``md``.
    :returns: Cells shaped like dots' own, with empty boxes.
    :rtype: list[dict]
    """
    paras = [x.strip() for x in re.split(r"\n\s*\n", md) if x.strip()]
    marks = {
        m.translate(_SUP_TRANS) for m in re.findall(f"[{SUP_CHARS}]+", md)
    }
    out = []
    for t in paras:
        cat = "Text"
        if t == "![]()":
            cat, t = "Picture", ""
        elif t.startswith("#"):
            cat = "Section-header"
        elif (m := re.match(r"^(\d{1,3})\.\s", t)) and m.group(1) in marks:
            cat = "Footnote"
        elif t.startswith("> "):
            t = re.sub(r"(?m)^> ?", "", t)
        out.append({"category": cat, "bbox": [], "text": t})
    return out


def column_order(
    cells: list[dict], width: int, height: int
) -> tuple[list[int], bool]:
    """Return a page's cell indexes in reading order: left column top
    to bottom, then right, band by band, a full-width cell closing a
    band; footnotes keep their place at the end.

    dots usually emits this order itself, but on table pages it walks
    the two columns row by row, so every page is put in column order.

    :param cells: The page's cells.
    :param width: The frame width.
    :param height: The frame height.
    :returns: ``(order, changed)``.
    :rtype: tuple[list[int], bool]
    """
    W = width or 1708
    mid, tol = W / 2, 0.03 * W
    body = [
        c["bbox"]
        for c in cells
        if c.get("bbox")
        and c.get("category") not in ("Page-header", "Picture")
        and c["bbox"][2] - c["bbox"][0] < 0.42 * W
        and not (c["bbox"][0] < 0.45 * W < 0.55 * W < c["bbox"][2])
    ]
    left_edges = [bb[2] for bb in body if (bb[0] + bb[2]) / 2 < W / 2]
    right_edges = [bb[0] for bb in body if (bb[0] + bb[2]) / 2 >= W / 2]
    if len(left_edges) >= 2 and len(right_edges) >= 2:
        gutter = (max(left_edges) + min(right_edges)) / 2
        if (
            min(right_edges) - max(left_edges) >= 0.01 * W
            and 0.42 * W <= gutter <= 0.58 * W
        ):
            mid = gutter
    band_top = HEAD_BAND * (height or 2212)
    head, main, foot = [], [], []
    for i, c in enumerate(cells):
        bb = c.get("bbox") or [0, 0, 0, 0]
        if c.get("category") == "Footnote":
            foot.append(i)
        elif c.get("category") == "Page-header" and bb[1] < band_top:
            head.append(i)
        else:
            main.append(i)

    def side(i):
        bb = cells[i].get("bbox") or [0, 0, 0, 0]
        if bb[2] <= mid + tol:
            return "L"
        if bb[0] >= mid - tol:
            return "R"
        return "S"

    def y0(i):
        return (cells[i].get("bbox") or [0, 0, 0, 0])[1]

    out, band = list(head), []

    def flush():
        out.extend(sorted((i for i in band if side(i) == "L"), key=y0))
        out.extend(sorted((i for i in band if side(i) == "R"), key=y0))
        band.clear()

    for i in sorted(main, key=y0):
        if side(i) == "S":
            flush()
            out.append(i)
        else:
            band.append(i)
    flush()
    out.extend(foot)
    return out, out != list(range(len(cells)))


def _page_frame(p: dict) -> tuple[int, int]:
    """Return the frame the page's cell boxes are measured in.

    The worker rescales every box into the rendered page image
    (``origin_width`` x ``origin_height``); older documents carry only
    the model-input frame, which differs by half a percent.
    """
    return (
        p.get("origin_width") or p.get("input_width") or 1708,
        p.get("origin_height") or p.get("input_height") or 2212,
    )


def load_volume(
    document: dict, printed_pages: dict[int, str | None] | None = None
) -> Volume:
    """Read a glued dots.mocr volume document into ordered blocks.

    :param document: The parsed ``r{run}-volume.json``.
    :param printed_pages: The printed page number per page index, from
        ``Scan.ocr_results``; None where nothing was read.
    :returns: The volume: pages and every cell as a classified block.
    :rtype: Volume
    """
    printed = printed_pages or {}
    meta = {
        k: document.get(k)
        for k in (
            "schema_version",
            "engine",
            "action",
            "scan_pk",
            "run",
            "source_page_count",
        )
    }
    pages: dict[int, PageInfo] = {}
    blocks: list[Block] = []
    skipped: list[int] = []
    reordered = 0
    md_pages: list[int] = []
    for p in sorted(
        document.get("pages") or [], key=lambda p: p["page_index"]
    ):
        pi = p["page_index"]
        cells = p.get("cells")
        from_md = False
        if cells is None:
            if (p.get("md") or "").strip():
                cells, from_md = md_cells(p["md"]), True
                md_pages.append(pi)
            else:
                skipped.append(pi)
                cells = []
        width, height = _page_frame(p)
        band_top = HEAD_BAND * height
        pdf_page = p.get("pdf_page", pi + 1)
        pages[pi] = PageInfo(
            pi,
            pdf_page,
            printed.get(pi),
            p.get("cells") is None and not from_md,
            width,
            height,
        )
        order, changed = column_order(cells, width, height)
        reordered += changed
        for ci in order:
            c = cells[ci]
            cat = c.get("category", "Text")
            raw = c.get("text") or ""
            bbox = list(c.get("bbox") or [])
            kind = KIND_BY_CATEGORY.get(cat, "text")
            if kind == "header" and bbox and bbox[1] >= band_top:
                # dots' "Page-header" below the band is body text it
                # mislabelled: a list number, a parallel cite.
                kind = "text"
            if kind in ("text", "footnote") and not raw.strip():
                kind = "empty"
            b = Block(
                idx=len(blocks),
                page_index=pi,
                pdf_page=pdf_page,
                printed_page=printed.get(pi),
                cell_index=ci,
                category=cat,
                bbox=bbox,
                raw=raw,
                kind=kind,
            )
            if (
                kind == "text"
                and cat == "Table"
                and raw.lstrip().startswith("<table")
            ):
                b.html = table_html(raw)
                b.text = " ".join(re.sub(r"<[^>]+>", " ", raw).split())
            elif kind == "text":
                r = render(raw)
                b.html, b.text, b.marks = r.html, r.text, r.marks
            elif kind == "footnote":
                label, rest = split_footnote_label(raw, loose=True)
                r = render(rest)
                b.fn_label, b.html, b.text, b.marks = (
                    label,
                    r.html,
                    r.text,
                    r.marks,
                )
            blocks.append(b)
    meta["pages_reordered_into_columns"] = reordered
    meta["pages_from_markdown"] = md_pages
    _drop_repetition_garbage(blocks, meta)
    return Volume(meta=meta, pages=pages, blocks=blocks, skipped_pages=skipped)


def _drop_repetition_garbage(blocks: list[Block], meta: dict) -> None:
    """Drop a dots.mocr repetition loop: the same paragraph emitted
    into three or more consecutive cells of one page.

    A printed page never says the same paragraph three times; the model
    looping does (#238 on the worker side). Seventeen such runs in the
    eleven encoder-testing volumes, e.g. four cells of "The system is
    not designed to provide the information which is necessary to the
    Commonwealth..." down one column of 1975 p. 142.
    """
    by_page: dict[int, list[Block]] = {}
    for b in blocks:
        if b.kind == "text":
            by_page.setdefault(b.page_index, []).append(b)
    garbage = 0
    for cells in by_page.values():
        run: list[Block] = []
        for b in cells + [None]:
            if (
                b is not None
                and run
                and b.text[:120] == run[-1].text[:120]
                and len(b.text) > 40
            ):
                run.append(b)
                continue
            if len(run) >= 3:
                for x in run:
                    x.kind = "garbage"
                    garbage += 1
            run = [b] if b is not None else []
    meta["garbage_cells"] = garbage


# ── The reviewed boxes ──────────────────────────────────────────────
@dataclass(frozen=True)
class Box:
    """One reviewed detection, in the frame the model read.

    Built from a ``Detection`` row or from a dict with the same field
    names, so a test needs no database.
    """

    page_index: int
    label: str
    x0: float
    y0: float
    x1: float
    y1: float
    img_width: int
    img_height: int

    def contains(self, x: float, y: float) -> bool:
        return self.x0 <= x <= self.x1 and self.y0 <= y <= self.y1


def boxes_from_detections(detections: Iterable[Any]) -> list[Box]:
    """Turn ``Detection`` rows (or dicts shaped like them) into boxes.

    :param detections: The reviewed rows: ``Detection.objects.filter(
        scan=scan).live()``.
    :returns: One box per row.
    :rtype: list[Box]
    """
    boxes = []
    for d in detections:
        get = (
            (lambda k: d[k])
            if isinstance(d, dict)
            else (lambda k: getattr(d, k))
        )
        boxes.append(
            Box(
                page_index=int(get("page_index")),
                label=str(get("label")),
                x0=float(get("x0")),
                y0=float(get("y0")),
                x1=float(get("x1")),
                y1=float(get("y1")),
                img_width=int(get("img_width") or 0),
                img_height=int(get("img_height") or 0),
            )
        )
    return boxes


def _centre_in_frame(
    b: Block, page: PageInfo, box: Box
) -> tuple[float, float]:
    """Return the block's centre scaled into the box's frame."""
    sx = (box.img_width or page.width) / (page.width or 1)
    sy = (box.img_height or page.height) / (page.height or 1)
    return (
        (b.bbox[0] + b.bbox[2]) / 2 * sx,
        (b.bbox[1] + b.bbox[3]) / 2 * sy,
    )


def apply_boxes(
    vol: Volume,
    boxes: list[Box],
    redaction_rects: dict[int, list[tuple[float, float, float, float]]]
    | None = None,
) -> dict:
    """Let the reviewed boxes decide what each cell is.

    :param vol: The volume.
    :param boxes: The reviewed detections.
    :param redaction_rects: Per page index, the boxes the final PDF
        paints over, in the frame (the ``Redaction`` rows a reader
        paints, through :func:`points_to_frame`; ``tagger.redaction_rects``).
        A cell whose centre lies in one is ``redacted`` and never sent.
    :returns: Counts of what changed, for the log.
    :rtype: dict
    """
    stats: dict[str, int] = {
        "footnote_cells": 0,
        "caption_cells": 0,
        "furniture_cells": 0,
        "redacted_cells": 0,
        "key_icons": 0,
        "images": 0,
    }
    by_page: dict[int, list[Box]] = {}
    for box in boxes:
        by_page.setdefault(box.page_index, []).append(box)
        if box.label == LABEL_KEY_ICON:
            stats["key_icons"] += 1
        elif box.label == LABEL_IMAGE:
            stats["images"] += 1
    rects = redaction_rects or {}
    for b in vol.blocks:
        if not b.bbox or b.page_index not in vol.pages:
            continue
        page = vol.pages[b.page_index]
        page_boxes = by_page.get(b.page_index, [])
        frame_box = next((bx for bx in page_boxes if bx.img_width), None)
        if frame_box is None and not rects.get(b.page_index):
            continue
        ref = frame_box or Box(
            b.page_index, "", 0, 0, 0, 0, page.width, page.height
        )
        cx, cy = _centre_in_frame(b, page, ref)
        for x0, y0, x1, y1 in rects.get(b.page_index, []):
            if (
                x0 <= cx <= x1
                and y0 <= cy <= y1
                and b.kind in ("text", "footnote")
            ):
                b.kind, b.box = "redacted", "redaction"
                stats["redacted_cells"] += 1
                break
        if b.kind == "redacted":
            continue
        hit = next(
            (
                bx
                for bx in page_boxes
                if bx.label in CLAIMING_LABELS and bx.contains(cx, cy)
            ),
            None,
        )
        if hit is None:
            continue
        if hit.label == LABEL_FOOTNOTES and b.kind in ("text", "footnote"):
            if b.kind == "text":
                label, rest = split_footnote_label(b.raw, loose=True)
                r = render(rest)
                b.fn_label, b.html, b.text, b.marks = (
                    label,
                    r.html,
                    r.text,
                    r.marks,
                )
                b.category = b.category + "→Footnote(box)"
                stats["footnote_cells"] += 1
            b.kind, b.box = "footnote", LABEL_FOOTNOTES
        elif hit.label == LABEL_CAPTION and b.kind == "text":
            b.box = LABEL_CAPTION
            stats["caption_cells"] += 1
        elif hit.label == LABEL_KEY_ICON and b.kind in ("text", "figure"):
            # The ornament and its "KEY NUMBER SYSTEM" line, whatever
            # dots called them: a boundary, never content.
            b.kind, b.box = "picture", LABEL_KEY_ICON
        elif hit.label in FURNITURE_LABELS and b.kind == "text":
            if (
                hit.y1 <= (hit.img_height or page.height) * 0.12
                and len(b.text) < 160
            ):
                b.kind, b.box = "header", hit.label
                stats["furniture_cells"] += 1
    return stats


# ── Opinion boundaries ──────────────────────────────────────────────
def _key_icon_cuts(vol: Volume, boxes: list[Box]) -> set[int]:
    """Return the block indexes at which a reviewed key icon closes an
    opinion: the index just after the last block the icon follows in
    reading order on its page.

    dots usually emits a Picture cell for the ornament, and
    :func:`apply_boxes` has marked it ``picture``; the cut is after it.
    An icon with no cell of its own is placed by geometry: after the
    last block on the page in the same column whose top is above the
    icon's bottom.
    """
    cuts: set[int] = set()
    blocks_by_page: dict[int, list[Block]] = {}
    for b in vol.blocks:
        blocks_by_page.setdefault(b.page_index, []).append(b)
    for box in boxes:
        if box.label != LABEL_KEY_ICON or box.page_index not in vol.pages:
            continue
        page = vol.pages[box.page_index]
        page_blocks = blocks_by_page.get(box.page_index, [])
        marked = [
            b
            for b in page_blocks
            if b.box == LABEL_KEY_ICON
            and b.bbox
            and box.contains(*_centre_in_frame(b, page, box))
        ]
        if marked:
            cuts.add(max(b.idx for b in marked) + 1)
            continue
        icon_cx = (box.x0 + box.x1) / 2
        mid = (box.img_width or page.width) / 2
        before = []
        for b in page_blocks:
            if not b.bbox or b.kind not in (
                "text",
                "footnote",
                "picture",
                "figure",
            ):
                continue
            cx, cy = _centre_in_frame(b, page, box)
            if cy <= box.y1 and (cx < mid) == (icon_cx < mid):
                before.append(b.idx)
        if before:
            cuts.add(max(before) + 1)
        elif page_blocks:
            cuts.add(min(b.idx for b in page_blocks))
    return cuts


def points_to_frame(x: float, y: float) -> tuple[float, float]:
    """Convert a point on the page to the 200 dpi frame the cells use.

    :param x: PDF points from the left.
    :param y: PDF points from the top.
    :returns: ``(x, y)`` in frame pixels.
    :rtype: tuple[float, float]
    """
    scale = FRAME_DPI / POINTS_PER_INCH
    return x * scale, y * scale


Anchor = tuple[int, float, float]
"""``(page_index, x, y)`` in the frame: the caption's top-left corner
opens an opinion, the key icon's bottom-right corner closes it."""


def _column(page: PageInfo, x: float) -> int:
    """0 for the left column of the page, 1 for the right."""
    return 0 if x < page.width / 2 else 1


def _follows(
    page: PageInfo, b: Block, anchor_x: float, anchor_y: float
) -> bool:
    """Whether the block comes at or after the point in reading order.

    Reading order on a two-column page is the left column, then the
    right (:func:`column_order`), so a block in the right column
    follows every point in the left one, and a block in the point's
    own column follows it when it reaches below it.
    """
    bc, ac = (
        _column(page, (b.bbox[0] + b.bbox[2]) / 2),
        _column(page, anchor_x),
    )
    if bc != ac:
        return bc > ac
    return b.bbox[3] > anchor_y + ANCHOR_SLACK_PX


def _precedes(
    page: PageInfo, b: Block, anchor_x: float, anchor_y: float
) -> bool:
    """Whether the block comes before the point in reading order: in an
    earlier column, or in the point's column with its top above it."""
    bc, ac = (
        _column(page, (b.bbox[0] + b.bbox[2]) / 2),
        _column(page, anchor_x),
    )
    if bc != ac:
        return bc < ac
    return b.bbox[1] < anchor_y - ANCHOR_SLACK_PX


def ranges_from_anchors(
    vol: Volume, boundaries: list[tuple[Anchor, Anchor]]
) -> list[tuple[int, int]]:
    """Turn the reviewed opinion boundaries into block ranges.

    A boundary is two anchors (#240, PR C): the start is the top-left
    corner of the caption, the end the bottom-right corner of the key
    icon. An opinion runs from the first block of the start page at or
    after the start point in reading order, to the last block of the
    end page before the end point (:func:`_follows`, :func:`_precedes`:
    the left column reads before the right, and inside a column the
    block's edge against the point decides). An anchor after every
    block of its page opens on the next page. Ranges are sorted by start and clipped at the next
    start, so a block is in one opinion at most; a block in no range
    (the orders between two opinions, a table the redactions did not
    cover) is not sent, and ``vol.meta["blocks_outside_boundaries"]``
    counts the text blocks that were left out, for the log.

    :param vol: The volume, after :func:`apply_boxes`.
    :param boundaries: ``[(start, end), ...]``, each an :data:`Anchor`
        in the frame (:func:`points_to_frame`).
    :returns: ``(start, end)`` block ranges, end exclusive, in order.
    :rtype: list[tuple[int, int]]
    """
    # The running head is placed first on its page by ``column_order``
    # whichever column it spans, and a garbage cell sits anywhere, so
    # neither may answer for the flow of the text around an anchor.
    by_page: dict[int, list[Block]] = {}
    for b in vol.blocks:
        if (
            b.bbox
            and b.page_index in vol.pages
            and b.kind not in ("header", "garbage")
        ):
            by_page.setdefault(b.page_index, []).append(b)
    n = len(vol.blocks)

    def first_after(page_index: int) -> int:
        later = [b.idx for b in vol.blocks if b.page_index > page_index]
        return min(later) if later else n

    def start_of(anchor: Anchor) -> int:
        pi, x, y = anchor
        page = vol.pages.get(pi)
        if page is None:
            return first_after(pi)
        for b in by_page.get(pi, []):
            if _follows(page, b, x, y):
                return b.idx
        return first_after(pi)

    def end_of(anchor: Anchor, start: int) -> int:
        pi, x, y = anchor
        page = vol.pages.get(pi)
        if page is None:
            return max(start, first_after(pi))
        last = None
        for b in by_page.get(pi, []):
            if _precedes(page, b, x, y):
                last = b.idx
        return max(start, last + 1 if last is not None else start)

    ranges = sorted(
        (s, end_of(end, s))
        for s, end in ((start_of(start), end) for start, end in boundaries)
    )
    clipped: list[tuple[int, int]] = []
    for i, (a, b) in enumerate(ranges):
        if i + 1 < len(ranges):
            b = min(b, ranges[i + 1][0])
        if b > a:
            clipped.append((a, b))
    covered = {i for a, b in clipped for i in range(a, b)}
    vol.meta["blocks_outside_boundaries"] = sum(
        1 for b in vol.text_blocks() if b.idx not in covered
    )
    return clipped


def opinion_ranges(
    vol: Volume,
    boxes: list[Box],
    boundaries: list[tuple[Anchor, Anchor]] | None = None,
) -> list[tuple[int, int]]:
    """Return the block ranges of the volume's opinions, in order.

    :param vol: The volume, after :func:`apply_boxes`.
    :param boxes: The reviewed detections.
    :param boundaries: The reviewed opinion boundaries as anchors in
        the frame, one per opinion as a person left them in review 2
        (``OpinionBoundary``, #240 PR C). When given they are the
        answer, through :func:`ranges_from_anchors`; without them the
        reviewed key icons cut the volume.
    :returns: ``(start, end)`` block ranges, end exclusive. From the
        key icons they cover every block from the first input block on,
        and a range with no input block is folded into the one before
        it; from the boundaries a block between two opinions is in no
        range.
    :rtype: list[tuple[int, int]]
    """
    text_idx = [b.idx for b in vol.blocks if b.kind in INPUT_KINDS]
    if not text_idx:
        return []
    if boundaries:
        return ranges_from_anchors(vol, boundaries)
    else:
        # Key icons only. A ``CASE_CAPTION`` box is not allowed to open
        # an opinion on its own: bl_warm draws caption boxes on the rows
        # of an orders table too, and on volume 2574 letting them cut
        # made 373 opinions of 303. One icon closes one opinion, and
        # which caption belongs to it is the pairing's question, which
        # a person answered in review 2; that answer arrives through
        # ``boundaries``. A caption still marks its cells on the map.
        cuts = _key_icon_cuts(vol, boxes)
        starts = sorted(
            {text_idx[0]}
            | {c for c in cuts if text_idx[0] < c < len(vol.blocks)}
        )
        ranges = list(
            zip(starts, starts[1:] + [len(vol.blocks)], strict=False)
        )
    merged: list[tuple[int, int]] = []
    for a, b in ranges:
        if merged and not any(
            vol.blocks[i].kind in INPUT_KINDS for i in range(a, b)
        ):
            merged[-1] = (merged[-1][0], b)
        else:
            merged.append((a, b))
    return merged


# ── Serialization ───────────────────────────────────────────────────
def _join(strs: list[str]) -> tuple[str, list[tuple[int, int]]]:
    ranges = []
    pos = 0
    for s in strs:
        ranges.append((pos, pos + len(s)))
        pos += len(s) + 1
    return "\n".join(strs), ranges


def build_input(
    vol: Volume,
    ranges: list[tuple[int, int]],
    boxes: list[Box],
    *,
    scan_pk: int,
    reporter: str | None = None,
    reporter_volume: int | str | None = None,
) -> tuple[dict, dict]:
    """Serialize the opinions for the worker, and write the map.

    :param vol: The volume, after :func:`apply_boxes`.
    :param ranges: From :func:`opinion_ranges`.
    :param boxes: The reviewed detections, for the image boxes.
    :param scan_pk: The scan, for the sequence ids.
    :param reporter: The reporter's short name, for the map.
    :param reporter_volume: The volume number, for the map.
    :returns: ``(input_document, map_document)``. The input is
        ``{"sequences": [{"id", "text"}]}``, the worker's contract. The
        map has, per sequence, each block's character range in ``text``
        and its page, cell and box; the footnote cells and image boxes
        held out of it; and how the segment was opened.
    :rtype: tuple[dict, dict]
    """
    images_by_page: dict[int, list[Box]] = {}
    for bx in boxes:
        if bx.label == LABEL_IMAGE:
            images_by_page.setdefault(bx.page_index, []).append(bx)
    sequences, maps = [], []
    for k, (a, b) in enumerate(ranges, 1):
        segment = vol.blocks[a:b]
        tb = [x for x in segment if x.kind in INPUT_KINDS]
        if not tb:
            continue
        strs = [f"<p>{x.html}</p>" for x in tb]
        text, char_ranges = _join(strs)
        sid = f"{scan_pk}-{k:04d}"
        sequences.append({"id": sid, "text": text})
        pages_in = sorted({x.page_index for x in segment})
        opened_by = "start"
        if a > 0 and vol.blocks[a - 1].kind == "picture":
            opened_by = "key_icon"
        elif tb[0].box == LABEL_CAPTION:
            opened_by = "caption"
        maps.append(
            {
                "id": sid,
                "opened_by": opened_by,
                "block_range": [a, b],
                "page_indexes": [pages_in[0], pages_in[-1]]
                if pages_in
                else [],
                "printed_pages": [tb[0].printed_page, tb[-1].printed_page],
                "blocks": [
                    {
                        "start": r[0],
                        "end": r[1],
                        "page_index": x.page_index,
                        "pdf_page": x.pdf_page,
                        "printed_page": x.printed_page,
                        "cell_index": x.cell_index,
                        "bbox": x.bbox,
                        "wrapper": "p",
                        "box": x.box,
                    }
                    for x, r in zip(tb, char_ranges, strict=True)
                ],
                "footnotes": [
                    {
                        "page_index": x.page_index,
                        "pdf_page": x.pdf_page,
                        "cell_index": x.cell_index,
                        "bbox": x.bbox,
                        "label": x.fn_label,
                    }
                    for x in segment
                    if x.kind == "footnote"
                ],
                "images": [
                    {
                        "page_index": bx.page_index,
                        "bbox": [bx.x0, bx.y0, bx.x1, bx.y1],
                        "frame": [bx.img_width, bx.img_height],
                    }
                    for pi in pages_in
                    for bx in images_by_page.get(pi, [])
                ],
            }
        )
    input_document = {"sequences": sequences}
    map_document = {
        "scan_pk": scan_pk,
        "reporter": reporter,
        "reporter_volume": reporter_volume,
        "ocr_run": vol.meta.get("run"),
        "source_page_count": vol.meta.get("source_page_count"),
        "converter_version": CONVERTER_VERSION,
        "sequences": maps,
    }
    return input_document, map_document


def text_digest(input_document: dict) -> str:
    """Return a stable digest of an input document's text, the part of
    a row's identity that says which words were sent.

    :param input_document: From :func:`build_input`.
    :returns: A hex SHA-256.
    :rtype: str
    """
    import hashlib

    h = hashlib.sha256()
    for entry in input_document["sequences"]:
        h.update(entry["id"].encode("utf-8"))
        h.update(b"\0")
        h.update(entry["text"].encode("utf-8"))
        h.update(b"\0")
    return h.hexdigest()


def convert(
    document: dict,
    detections: Iterable[Any],
    *,
    scan_pk: int,
    reporter: str | None = None,
    reporter_volume: int | str | None = None,
    printed_pages: dict[int, str | None] | None = None,
    redaction_rects: dict | None = None,
    boundaries: list[tuple[Anchor, Anchor]] | None = None,
) -> tuple[dict, dict, dict]:
    """The whole conversion, in one call.

    :param document: The glued dots.mocr volume document.
    :param detections: The reviewed ``Detection`` rows (or dicts).
    :param scan_pk: The scan.
    :param reporter: The reporter's short name.
    :param reporter_volume: The volume number.
    :param printed_pages: See :func:`load_volume`.
    :param redaction_rects: See :func:`apply_boxes`.
    :param boundaries: See :func:`opinion_ranges`.
    :returns: ``(input_document, map_document, stats)``. The map says
        in ``opinions_from`` whether the boundaries or the key icons
        cut the volume.
    :rtype: tuple[dict, dict, dict]
    """
    vol = load_volume(document, printed_pages)
    boxes = boxes_from_detections(detections)
    stats = apply_boxes(vol, boxes, redaction_rects)
    ranges = opinion_ranges(vol, boxes, boundaries)
    input_document, map_document = build_input(
        vol,
        ranges,
        boxes,
        scan_pk=scan_pk,
        reporter=reporter,
        reporter_volume=reporter_volume,
    )
    map_document["opinions_from"] = "boundaries" if boundaries else "key_icons"
    stats.update(
        {
            "pages": len(vol.pages),
            "blocks_outside_boundaries": vol.meta.get(
                "blocks_outside_boundaries", 0
            ),
            "blocks": len(vol.blocks),
            "input_blocks": len(vol.text_blocks()),
            "opinions": len(input_document["sequences"]),
            "chars": sum(len(e["text"]) for e in input_document["sequences"]),
            "garbage_cells": vol.meta.get("garbage_cells", 0),
            "skipped_pages": len(vol.skipped_pages),
        }
    )
    return input_document, map_document, stats


def dumps(document: dict) -> str:
    """Serialize a document the way it is stored, so a digest of the
    bytes is reproducible.

    :param document: The input or map document.
    :returns: Compact JSON, keys in order, non-ASCII kept.
    :rtype: str
    """
    return json.dumps(
        document, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    )
