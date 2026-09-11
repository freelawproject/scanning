"""Survey the review-2 findings the dots.mocr text could raise (#303).

Every review-2 finding is derived from rows, and every row comes from
YOLO. So a page YOLO read wrong raises nothing, and the volume reads as
clean. ``uncovered_headnote`` shows the shape of the gap: it needs a
``HEADNOTE`` detection above ``findings.HEADNOTE_CONFIDENCE``, so it
answers "the redaction missed a headnote YOLO found" and cannot answer
"YOLO missed a headnote".

The dots.mocr cells are a second witness, read by another model at
another resolution with no confidence gate. This module measures what
that witness would say, and **writes nothing**. Issue #303 keeps the
checks themselves behind this measurement, because a check a curator
cannot trust is worse than no check: it teaches them to approve past
the cards.

Three probes, one per candidate check:

- :func:`glyph_probe`. dots reads the West key icon as an arrow glyph.
  A cell holding one, with no ``KEY_ICON`` row near it, is a key icon
  YOLO missed, and ``KEY_ICON`` is copyrighted.
- :func:`bracket_probe`. A cell whose text opens with a bracketed
  number is a headnote, and a ``HEADNOTE_BRACKET`` or ``HEADNOTE`` row
  should stand near it.
- :func:`header_probe`. A ``Page-header`` cell is the running head,
  which is copyrighted and is painted white today. A cell no white row
  covers is a running head left in the deliverable.

**The probes name no glyph and no threshold as the answer.** The glyph
probe counts every character of the arrow and geometric blocks, not a
chosen list, so the survey reports which glyph the model actually
writes rather than confirming a guess. Whether a glyph is the key icon
is read off its own numbers: a glyph a ``KEY_ICON`` row nearly always
accompanies is the key icon, and one that stands alone is something
else.

**One space: the fraction of the page.** The cells are in the render's
pixels, the ``Detection`` rows in the detection render's pixels, and
the ``Redaction`` rows in PDF points. Each is divided by its own page
size before any two are compared, the rule
:func:`text_fit.fit_span` already follows. A page the worker
re-rendered (``PageCells.fallback``) is skipped by the redaction probe
alone, for the reason ``text_fit.fit_rows`` gives: the page width is
derived from the render there.

This module holds the projection with the text in it
(:func:`page_regions`), because ``text_fit.page_cells`` drops the text
and the category on purpose -- a volume document carries both for 1300
pages and the fit reads neither. The two share the one rule for which
document to read, ``text_fit.load_document``.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

from scanning import dots_mocr, text_fit
from scanning.models import Detection, Redaction
from scanning.page_numbers import HEADER_CATEGORY

#: The Unicode blocks a key icon could be read as. Counted whole rather
#: than sampled from a list, so a glyph nobody predicted still appears
#: in the survey: Arrows, Geometric Shapes, Dingbats (the arrow tail of
#: the block), and Miscellaneous Symbols and Arrows.
GLYPH_BLOCKS = (
    (0x2190, 0x21FF),
    (0x25A0, 0x25FF),
    (0x2794, 0x27BF),
    (0x2B00, 0x2BFF),
)

#: The glyphs issue #303 named as candidates, reported first in the
#: survey. Membership here changes no count; it only orders the report.
NAMED_GLYPHS = "⇨⇦⇒→➡▶◀"

#: A headnote's bracketed number at the head of a line: ``[1]``,
#: ``[2, 3]``, ``[4-6]``.
#:
#: Anchored, because a bracketed year inside a citation is not a
#: headnote. But the anchor may not be the first character: the key
#: icon glyph of the same headnote comes before the bracket on the
#: page, so a pattern anchored hard at the line start missed every
#: headnote that carries one -- which is to say the headnotes this
#: survey most wants to find. So a run of characters that are neither
#: letters nor digits may stand before it.
#:
#: Three digits at most, so the ``[1999]`` of a citation cannot match
#: even at the head of a line. A court's own numbered list is the false
#: hit the pattern cannot tell apart, and measuring that is what the
#: survey is for.
BRACKET_RE = re.compile(
    r"^[^0-9A-Za-z\[]*\[\s*\d{1,3}(?:\s*[,\-–]\s*\d{1,3})*\s*\]"
)

#: How near a row must be to count as "the same thing as this cell": a
#: row whose centre falls in the cell's box, grown by this fraction of
#: the page. A key icon sits at the head of its headnote and can fall
#: just outside the cell the model drew.
NEAR_PAD = 0.02

#: How much of a ``Page-header`` cell a white row must cover to count
#: as covering it. The share is measured against the best single row,
#: never a sum over rows, which would double-count two boxes that
#: overlap each other.
COVER_SHARE = 0.5

#: Points per inch, with :data:`dots_mocr.DPI` the page size in points
#: of a cell render. ``text_fit.fit_rows`` derives it the same way.
POINTS_PER_INCH = 72.0

#: The labels that answer each probe: a row of one of these near the
#: cell means YOLO saw what dots saw.
KEY_LABELS = ("KEY_ICON",)
HEADNOTE_LABELS = ("HEADNOTE_BRACKET", "HEADNOTE")


@dataclass(frozen=True)
class Region:
    """One dots.mocr layout cell, with the text kept.

    :param box: ``(x0, y0, x1, y1)`` in the render's pixels.
    :param category: The cell's ``category``, or the empty string.
    :param text: The cell's ``text``, or the empty string.
    """

    box: tuple[float, float, float, float]
    category: str
    text: str


@dataclass(frozen=True)
class PageRegions:
    """The cells of one page, with the render they were measured in.

    :param width: The render width in pixels (``origin_width``).
    :param height: The render height in pixels (``origin_height``).
    :param regions: The cells, in reading order as the model wrote them.
    :param fallback: Whether the worker flagged ``render_fallback``, so
        the page size cannot be derived from the render.
    """

    width: float
    height: float
    regions: tuple[Region, ...]
    fallback: bool = False


def page_regions(document: dict | None) -> dict[int, PageRegions]:
    """Read a glued OCR document into the cells of each page, with text.

    The twin of ``text_fit.page_cells``, which drops the text and the
    category. A page with no usable cell gives no entry: a failed page,
    a filtered page (#242), and the new page of an insert that no
    reader answered.

    :param document: The document ``dots_mocr.glue_run`` or
        ``apply._glue_ocr`` wrote, or None.
    :returns: ``{page_index: PageRegions}``.
    :rtype: dict[int, PageRegions]
    """
    if not isinstance(document, dict):
        return {}
    pages: dict[int, PageRegions] = {}
    for page in document.get("pages") or []:
        if not isinstance(page, dict):
            continue
        index = page.get("page_index")
        width = page.get("origin_width")
        height = page.get("origin_height")
        if not isinstance(index, int) or isinstance(index, bool):
            continue
        if not text_fit.is_positive(width) or not text_fit.is_positive(height):
            continue
        regions = []
        for cell in page.get("cells") or []:
            box = text_fit.cell_box(cell)
            if box is None:
                continue
            regions.append(
                Region(
                    box,
                    str(cell.get("category") or ""),
                    str(cell.get("text") or ""),
                )
            )
        if regions:
            pages[index] = PageRegions(
                float(width or 0),
                float(height or 0),
                tuple(regions),
                bool(page.get("render_fallback")),
            )
    return pages


def load_regions(scan, run) -> dict[int, PageRegions]:
    """Read the cells of the space ``run`` names, and never raise.

    The document choice is ``text_fit.load_document``'s, which is the
    one rule for it: a standing apply run means the run's glued OCR
    volume, and no run means the volume's own glued document.

    :param scan: The scan.
    :param run: The standing apply run, or None for the original's
        space.
    :returns: ``{page_index: PageRegions}``, empty when nothing loaded.
    :rtype: dict[int, PageRegions]
    """
    return page_regions(text_fit.load_document(scan, run))


@dataclass
class ProbeCounts:
    """What one probe found over the pages it read.

    :param cells: The cells the probe matched.
    :param covered: Those a row of the probe's labels stood near.
    :param uncovered: Those none did. The candidate finding.
    :param hits: The matches inside those cells, which exceeds
        ``cells`` when one cell holds several headnotes.
    :param pages: The pages holding at least one uncovered cell.
    """

    cells: int = 0
    covered: int = 0
    uncovered: int = 0
    hits: int = 0
    pages: set[tuple[int, int]] = field(default_factory=set)

    def add(self, other: ProbeCounts) -> None:
        """Fold another probe's counts into this one.

        :param other: The counts to add.
        :return: None.
        """
        self.cells += other.cells
        self.covered += other.covered
        self.uncovered += other.uncovered
        self.hits += other.hits
        self.pages |= other.pages

    def count(self, covered: bool, where: tuple[int, int], hits: int) -> None:
        """Count one matched cell.

        :param covered: Whether a row stood near it.
        :param where: ``(scan_pk, page_index)``, for the page count.
        :param hits: The matches inside the cell.
        :return: None.
        """
        self.cells += 1
        self.hits += hits
        if covered:
            self.covered += 1
        else:
            self.uncovered += 1
            self.pages.add(where)

    @property
    def share(self) -> float:
        """Return the share of matched cells no row stood near.

        :returns: The share, 0.0 when nothing matched.
        :rtype: float
        """
        return (self.uncovered / self.cells) if self.cells else 0.0


@dataclass
class Survey:
    """The running totals of a corpus pass.

    :param volumes: The volumes read.
    :param pages: The pages read.
    :param categories: Every ``category`` value seen, with its count.
    :param glyphs: Per glyph, the counts of :func:`glyph_probe`.
    :param brackets: The counts of :func:`bracket_probe`.
    :param headers: The counts of :func:`header_probe`.
    :param skipped: Per reason, the volumes the pass could not read.
    """

    volumes: int = 0
    pages: int = 0
    categories: Counter = field(default_factory=Counter)
    glyphs: dict[str, ProbeCounts] = field(default_factory=dict)
    brackets: ProbeCounts = field(default_factory=ProbeCounts)
    headers: ProbeCounts = field(default_factory=ProbeCounts)
    skipped: Counter = field(default_factory=Counter)

    def glyph(self, glyph: str) -> ProbeCounts:
        """Return the counts of one glyph, creating them on first sight.

        :param glyph: The character.
        :returns: Its counts.
        :rtype: ProbeCounts
        """
        return self.glyphs.setdefault(glyph, ProbeCounts())

    def add(self, other: Survey) -> None:
        """Fold one volume's totals into a corpus pass.

        :param other: The volume's own totals.
        :return: None.
        """
        self.volumes += other.volumes
        self.pages += other.pages
        self.categories.update(other.categories)
        self.skipped.update(other.skipped)
        self.brackets.add(other.brackets)
        self.headers.add(other.headers)
        for glyph, counts in other.glyphs.items():
            self.glyph(glyph).add(counts)


def survey_scan(scan, run, regions: dict[int, PageRegions]) -> Survey:
    """Run the three probes over one volume, and write nothing.

    :param scan: The scan.
    :param run: The run its rows are measured against
        (``detections.measured_run``), or None.
    :param regions: Its cells, :func:`load_regions`.
    :returns: The volume's own totals.
    :rtype: Survey
    """
    survey = Survey(volumes=1, pages=len(regions))
    detections = _detections_by_page(scan, run)
    whites = _white_rows_by_page(scan, run)
    for index, page in sorted(regions.items()):
        for region in page.regions:
            survey.categories[region.category or "(none)"] += 1
        glyph_probe(scan, index, page, detections.get(index, ()), survey)
        bracket_probe(scan, index, page, detections.get(index, ()), survey)
        header_probe(scan, index, page, whites.get(index, ()), survey)
    return survey


def glyph_probe(scan, index: int, page: PageRegions, rows, survey) -> None:
    """Count the cells holding an arrow glyph, and those no key icon met.

    Counted per glyph, so the survey says which character the model
    writes for a key icon instead of assuming one.

    :param scan: The scan, for the page address in the report.
    :param index: The page index.
    :param page: The page's cells.
    :param rows: The page's live detection rows.
    :param survey: The totals to write into.
    :return: None.
    """
    near = [row for row in rows if row.label in KEY_LABELS]
    for region in page.regions:
        found = Counter(ch for ch in region.text if _in_blocks(ch))
        if not found:
            continue
        covered = _row_near(region, page, near)
        for glyph, hits in found.items():
            survey.glyph(glyph).count(covered, (scan.pk, index), hits)


def bracket_probe(scan, index: int, page: PageRegions, rows, survey) -> None:
    """Count the cells opening with a bracketed number, and the uncovered.

    :param scan: The scan, for the page address in the report.
    :param index: The page index.
    :param page: The page's cells.
    :param rows: The page's live detection rows.
    :param survey: The totals to write into.
    :return: None.
    """
    near = [row for row in rows if row.label in HEADNOTE_LABELS]
    for region in page.regions:
        hits = sum(
            1 for line in region.text.splitlines() if BRACKET_RE.match(line)
        )
        if not hits:
            continue
        survey.brackets.count(
            _row_near(region, page, near), (scan.pk, index), hits
        )


def header_probe(scan, index: int, page: PageRegions, rows, survey) -> None:
    """Count the running-head cells, and those no white box covers.

    Skipped on a page the worker re-rendered: the white rows are in
    points and the page size is derived from the render there, so the
    two would be compared in different spaces.

    :param scan: The scan, for the page address in the report.
    :param index: The page index.
    :param page: The page's cells.
    :param rows: The page's standing white redaction rows, in points.
    :param survey: The totals to write into.
    :return: None.
    """
    if page.fallback:
        return
    width = page.width * POINTS_PER_INCH / dots_mocr.DPI
    height = page.height * POINTS_PER_INCH / dots_mocr.DPI
    if width <= 0 or height <= 0:
        return
    for region in page.regions:
        if region.category != HEADER_CATEGORY:
            continue
        cell = _fraction(region.box, page.width, page.height)
        covered = any(
            _cover_share(cell, _fraction(row, width, height)) >= COVER_SHARE
            for row in rows
        )
        survey.headers.count(covered, (scan.pk, index), 1)


def _detections_by_page(scan, run) -> dict[int, tuple]:
    """Return the live detection rows of the measured space, by page.

    :param scan: The scan.
    :param run: The measured run, or None for the original's space.
    :returns: ``{page_index: (row, ...)}``.
    :rtype: dict[int, tuple]
    """
    rows = Detection.objects.live().filter(
        scan=scan,
        label__in=(*KEY_LABELS, *HEADNOTE_LABELS),
        **_run_filter(run, "apply_run"),
    )
    by_page: dict[int, list] = {}
    for row in rows.only(
        "page_index",
        "label",
        "x0",
        "y0",
        "x1",
        "y1",
        "img_width",
        "img_height",
    ):
        by_page.setdefault(row.page_index, []).append(row)
    return {index: tuple(rows) for index, rows in by_page.items()}


def _white_rows_by_page(scan, run) -> dict[int, tuple]:
    """Return the standing white redaction boxes of the space, by page.

    A ``dismiss`` row carries no box of its own, so ``visible()`` is
    the read: a computed row under no standing dismissal, and a human
    ``add`` that is not withdrawn.

    :param scan: The scan.
    :param run: The measured run, or None for the original's space.
    :returns: ``{page_index: ((x0, y0, x1, y1), ...)}`` in points.
    :rtype: dict[int, tuple]
    """
    rows = Redaction.objects.visible().filter(
        scan=scan,
        fill=Redaction.Fill.WHITE,
        **_run_filter(run, "apply_run"),
    )
    by_page: dict[int, list] = {}
    for row in rows.only("page_index", "x0", "y0", "x1", "y1"):
        if row.bbox is None:
            continue
        by_page.setdefault(row.page_index, []).append(
            (row.x0, row.y0, row.x1, row.y1)
        )
    return {index: tuple(boxes) for index, boxes in by_page.items()}


def _run_filter(run, field_name: str) -> dict:
    """Return the filter that keeps the rows of one page space.

    :param run: The measured run, or None for the original's space.
    :param field_name: The run column's name on the model.
    :returns: The keyword filter.
    :rtype: dict
    """
    if run is None:
        return {f"{field_name}__isnull": True}
    return {field_name: run}


def _row_near(region: Region, page: PageRegions, rows) -> bool:
    """Return whether a row's centre falls in ``region``, padded.

    Both sides are fractions of their own page before they meet, so a
    cell render and a detection render of different pixel sizes still
    compare.

    :param region: The cell.
    :param page: Its page, for the render size.
    :param rows: The candidate detection rows.
    :returns: Whether one of them is near.
    :rtype: bool
    """
    x0, y0, x1, y1 = _fraction(region.box, page.width, page.height)
    for row in rows:
        if not row.img_width or not row.img_height:
            continue
        cx = ((row.x0 + row.x1) / 2) / row.img_width
        cy = ((row.y0 + row.y1) / 2) / row.img_height
        if (
            x0 - NEAR_PAD <= cx <= x1 + NEAR_PAD
            and y0 - NEAR_PAD <= cy <= y1 + NEAR_PAD
        ):
            return True
    return False


def _fraction(
    box: tuple[float, float, float, float], width: float, height: float
) -> tuple[float, float, float, float]:
    """Divide a box by its own page size.

    :param box: ``(x0, y0, x1, y1)``.
    :param width: The page width in the box's own unit.
    :param height: The page height in the box's own unit.
    :returns: The box as fractions of the page.
    :rtype: tuple
    """
    return (
        box[0] / width,
        box[1] / height,
        box[2] / width,
        box[3] / height,
    )


def _cover_share(cell, box) -> float:
    """Return the share of ``cell`` that ``box`` covers.

    :param cell: The cell box, as fractions of the page.
    :param box: The covering box, as fractions of the page.
    :returns: The share, 0.0 when they do not meet.
    :rtype: float
    """
    area = (cell[2] - cell[0]) * (cell[3] - cell[1])
    if area <= 0:
        return 0.0
    wide = min(cell[2], box[2]) - max(cell[0], box[0])
    tall = min(cell[3], box[3]) - max(cell[1], box[1])
    if wide <= 0 or tall <= 0:
        return 0.0
    return (wide * tall) / area


def _in_blocks(char: str) -> bool:
    """Return whether a character is in one of :data:`GLYPH_BLOCKS`.

    :param char: One character.
    :returns: Whether it is a candidate key-icon glyph.
    :rtype: bool
    """
    point = ord(char)
    return any(low <= point <= high for low, high in GLYPH_BLOCKS)
