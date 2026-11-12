"""A picture across both columns of a page two opinions share (#620).

An opinion can end with full-width matter (a form, a table, an exhibit
the model boxes as one ``IMAGE``), and the next opinion starts under it
on the same page. The reading order of such a page is not "the left
column, then the right one": it is the columns above the picture, the
picture, then the columns below it. Two things went wrong without that
order, seen on three opinions in a row of one real volume:

* The masks over the neighbour opinion (blackletter's
  ``_outside_opinion_rects``) know two columns alone. A page with no
  ``TEXT_COLUMN`` box got no mask, so both opinions kept the whole
  page; a page whose column boxes the model drew over the picture got
  masks that cut the picture at the gutter, half to each opinion.
* The model ran the ``IMAGE`` box to the foot of the page, over the key
  icon and the next opinion's caption, so the picture rule of the OCR
  glue took the next opinion's caption out of its own text.

**The rule**, in two parts that share the test of :func:`is_full_width`:

    A model ``IMAGE`` box across both columns that holds a key icon
    ends where the columns under it start
    (:func:`trim_rows`, in the compute, beside the foot trim of the
    columns). The masks of a page that holds a picture across both
    columns are read in the order above (:func:`outside`): the picture
    goes whole to the opinion whose text holds its place, whatever the
    column of the key or of the caption.

The box is the one fact both parts meet at: the trim writes the row a
curator sees and can resize, and the masks read the rows alone. A box
that still holds the anchor of a boundary (a hand-drawn box, a picture
under its caption) keeps blackletter's masks on its page and is the
``image_over_boundary`` card of step 2 (:func:`over_boundary`), so a
person moves the box.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from blackletter.models import Label

from scanning.columns import column_groups
from scanning.text_fit import PageCells

logger = logging.getLogger(__name__)

Box = tuple[float, float, float, float]
Span = tuple[float, float]

#: A picture is across both columns when it covers at least this much
#: of the width of each. A picture set in one column reaches the other
#: by a sliver at most, and the column order is right for it.
FULL_WIDTH_SHARE = 0.5

#: An ``IMAGE`` box narrower than this fraction of the page is across
#: no two columns, whatever the columns are: the test that spares a
#: page the read of its neighbours' column boxes.
MIN_PAGE_SHARE = 0.5

#: How far inside a picture, in points, the anchor of a boundary may
#: lie and still be read as beside it: the trim ends a box at the top
#: of the print under it, where a caption's own anchor is.
ANCHOR_SLACK_PT = 3.0

#: How far inside its box, in points, an anchor is read for its
#: column. The start anchor is the left edge of a caption and the end
#: anchor the right edge of a key icon, each beside the gutter when it
#: is in the facing column, and the gutter of a page with no column box
#: is the volume's, a few points off this page's.
ANCHOR_INSET_PT = 12.0

#: The same slack in the pixels of the render the rows are in, a hair
#: under it, so a box the masks refuse is always one the trim and the
#: card name.
ANCHOR_SLACK_PX = 8.0

#: How far under its top ``blackletter.api.generate`` starts painting a
#: mask. A mask that starts on a picture's top edge starts that much
#: above it.
PAINT_INSET_PT = 3.0

#: The widest blank, in render pixels, between two blocks of one
#: column. The column under a picture is walked upwards from the key
#: icon or the caption by its cells, and a wider blank than this is the
#: end of the column: the picture's own last lines are above it.
COLUMN_GAP_PX = 60.0

#: How far past the gutter centre, in render pixels, a cell reaches
#: before it is a line across both columns: the picture's own matter,
#: never a column's.
GUTTER_REACH_PX = 12.0

#: The smallest trim worth a write, in render pixels: the model boxes a
#: picture's last line a little long, and under this the box is right.
MIN_TRIM_PX = 20.0

#: The least a trim leaves of a picture, in render pixels. A box that
#: would end above this holds its caption at its top, where the picture
#: is under the anchor and no trim of the foot is the answer.
MIN_KEPT_PX = 60.0

#: The gap left between a trimmed box and the column under it when no
#: cell says where the picture's own matter ends, in render pixels.
FOOT_GAP_PX = 2.0

IMAGE = Label.IMAGE.name

#: "Whatever the run": the read of the compute, which shapes every
#: live row of the scan, where None names the original's space.
_ANY_RUN = object()


# ---------------------------------------------------------------------------
# The geometry, in one space
# ---------------------------------------------------------------------------


def is_full_width(box: Box, left: Span, right: Span) -> bool:
    """Return whether a box lies across both columns.

    :param box: ``(x0, y0, x1, y1)``.
    :param left: The left column's ``(x0, x1)``, in the same space.
    :param right: The right column's, the same way.
    """
    for x0, x1 in (left, right):
        width = x1 - x0
        if width <= 0:
            return False
        if min(box[2], x1) - max(box[0], x0) < width * FULL_WIDTH_SHARE:
            return False
    return True


def own_columns(spans: list[Span]) -> tuple[Span, Span] | None:
    """Return the two columns the column boxes of a page make, or None.

    :param spans: ``(x0, x1)`` per ``TEXT_COLUMN`` box of the page.
    :returns: ``(left, right)``, or None for a page with one column,
        three, or no column box.
    """
    groups = column_groups(spans)
    if len(groups) != 2:
        return None
    left, right = (
        (min(spans[i][0] for i in g), max(spans[i][1] for i in g))
        for g in groups
    )
    return left, right


def halves(box: Box) -> tuple[Span, Span]:
    """Return the two halves of a box as columns.

    The fallback of a page with no column box in a volume with none
    nearby: a picture across the page spans the text block, so its
    halves are the columns, a little narrow.
    """
    middle = (box[0] + box[2]) / 2
    return (box[0], middle), (middle, box[2])


@dataclass(frozen=True)
class Layout:
    """The reading order of one page that holds a full-width picture.

    Every value is in PDF points.

    :param left: The left column's ``(x0, x1)``, out to the pictures'
        left edge and in to the gutter centre: a mask over a column is
        as wide as the text block's half, because the columns of a page
        with no column box are another page's.
    :param right: The right column's, the same way.
    :param top: Where the text starts, under the running head.
    :param bottom: Where it ends, above the footnotes.
    :param bands: The full-width pictures, top to bottom.
    """

    left: Span
    right: Span
    top: float
    bottom: float
    bands: tuple[Box, ...]

    @property
    def divide(self) -> float:
        return (self.left[1] + self.right[0]) / 2


@dataclass(frozen=True)
class Mask:
    """One mask of :func:`outside`.

    :param box: The rect, in PDF points.
    :param column: ``"left"`` or ``"right"`` for a mask over one
        column, ``""`` for one across the page.
    """

    box: Box
    column: str = ""


def outside(
    layout: Layout,
    x: float,
    y: float,
    is_first: bool,
    is_last: bool,
    end: tuple[float, float] | None = None,
) -> list[Mask] | None:
    """Return the masks over the neighbours' share of a banded page.

    The page is read as the columns above the first picture, that
    picture, the columns under it, and so on. On an opinion's first
    page everything before its caption in that order is masked, on its
    last page everything after its key, so a picture goes whole to one
    opinion. A page with no picture gives blackletter's own masks.

    :param layout: The page.
    :param x: The start anchor's x, read when ``is_first``.
    :param y: The start anchor's y.
    :param is_first: Whether the page is the opinion's first.
    :param is_last: Whether it is its last.
    :param end: The end anchor, read when ``is_last``.
    :returns: The masks, or None when an anchor lies inside a picture:
        the box is drawn over the print of a boundary, and nothing
        here says where the picture ends.
    """
    masks: list[Mask] = []
    x0 = min([layout.left[0]] + [b[0] for b in layout.bands])
    x1 = max([layout.right[1]] + [b[2] for b in layout.bands])

    def _region(at: float) -> tuple[float, float, bool, bool] | None:
        """The column region that holds ``at``, and what lies beside."""
        for band in layout.bands:
            if band[1] + ANCHOR_SLACK_PT < at < band[3] - ANCHOR_SLACK_PT:
                return None
        above = [b for b in layout.bands if (b[1] + b[3]) / 2 < at]
        below = [b for b in layout.bands if (b[1] + b[3]) / 2 >= at]
        top = max([layout.top] + [b[3] for b in above])
        bottom = min([layout.bottom] + [b[1] for b in below])
        return top, bottom, bool(above), bool(below)

    if is_first:
        region = _region(y)
        if region is None:
            return None
        top, bottom, above, _below = region
        if above:
            masks.append(Mask((x0, layout.top, x1, top)))
        if x + ANCHOR_INSET_PT < layout.divide:
            masks.append(
                Mask((layout.left[0], top, layout.left[1], y), "left")
            )
        else:
            masks.append(
                Mask((layout.left[0], top, layout.left[1], bottom), "left")
            )
            masks.append(
                Mask((layout.right[0], top, layout.right[1], y), "right")
            )
    if is_last and end is not None:
        end_x, end_y = end
        region = _region(end_y)
        if region is None:
            return None
        top, bottom, _above, below = region
        if end_x - ANCHOR_INSET_PT >= layout.divide:
            masks.append(
                Mask(
                    (layout.right[0], end_y, layout.right[1], bottom), "right"
                )
            )
        else:
            masks.append(
                Mask((layout.left[0], end_y, layout.left[1], bottom), "left")
            )
            masks.append(
                Mask((layout.right[0], top, layout.right[1], bottom), "right")
            )
        if below:
            # blackletter paints a mask three points under its top, a
            # blank under a key icon and the picture's first line here.
            masks.append(
                Mask(
                    (
                        x0,
                        max(end_y, bottom - PAINT_INSET_PT),
                        x1,
                        layout.bottom,
                    )
                )
            )
    return [m for m in masks if m.box[0] < m.box[2] and m.box[1] < m.box[3]]


# ---------------------------------------------------------------------------
# The columns of a page, off the rows
# ---------------------------------------------------------------------------


def neighbour_rows(scan, pages: set[int]) -> dict[int, list]:
    """Return the live column rows near ``pages``, by page.

    One query, for the pages whose own boxes make no two columns: the
    volume's columns are read off the nearest page that has them
    (``undetected._column_geometry``).

    :param scan: The scan.
    :param pages: The pages asked about.
    """
    from scanning.models import Detection
    from scanning.undetected import NEIGHBOUR_PAGES

    wanted: set[int] = set()
    for page in pages:
        wanted.update(
            range(page - NEIGHBOUR_PAGES, page + NEIGHBOUR_PAGES + 1)
        )
    wanted = {page for page in wanted if page >= 0}
    by_page: dict[int, list] = {}
    if not wanted:
        return by_page
    for row in Detection.objects.live().filter(
        scan=scan, label=Label.TEXT_COLUMN.name, page_index__in=wanted
    ):
        if row.img_width:
            by_page.setdefault(row.page_index, []).append(row)
    return by_page


def page_columns(
    spans: list[Span],
    pictures: list[Box],
    page_index: int,
    width: float,
    neighbours: dict[int, list],
) -> tuple[Span, Span] | None:
    """Return the two columns of a page, in its render pixels.

    The page's own column boxes when they make two columns. A page
    with no column box at all takes the volume's, off the nearest page
    that has them, or the halves of its widest picture when the volume
    has none nearby. A page whose own boxes make one column, or three,
    has no two columns to read: it is a one-column page, and masks
    over the halves of its lines would cut them.

    :param spans: ``(x0, x1)`` per column box of the page.
    :param pictures: The page's ``IMAGE`` boxes.
    :param page_index: The page.
    :param width: Its render width.
    :param neighbours: :func:`neighbour_rows`.
    """
    from scanning.undetected import _column_geometry

    if spans:
        return own_columns(spans)
    found = _column_geometry(neighbours, page_index, width)
    if found is None and pictures:
        found = halves(max(pictures, key=lambda b: b[2] - b[0]))
    return found


def may_span(box: Box, width: float) -> bool:
    """Return whether a box is wide enough to lie across two columns."""
    return width > 0 and box[2] - box[0] >= width * MIN_PAGE_SHARE


# ---------------------------------------------------------------------------
# The trim of the model's box
# ---------------------------------------------------------------------------


def column_top(
    box: Box,
    anchor: Box,
    cells: list[Box],
    divide: float,
) -> tuple[float, float | None]:
    """Return where the columns under a picture start, and its own foot.

    From the highest key icon or caption inside the box, upwards, in
    the column that faces it: a cell on that side of the gutter that
    ends within :data:`COLUMN_GAP_PX` above the top found so far is the
    column's, unless a line across both columns lies between. The
    facing column of a key icon starts above the icon, because the icon
    is set under a blank. The anchor's own column is not walked: what
    is above a key or a caption there is the picture's.

    :param box: The picture's box, in render pixels.
    :param anchor: The highest anchor inside it.
    :param cells: The page's cells, in the same pixels.
    :param divide: The gutter centre.
    :returns: ``(top, foot)``: the top of the columns, and the foot of
        the lowest cell above it inside the box, or None with no cell.
    """
    inside = [
        c
        for c in cells
        if box[0] <= (c[0] + c[2]) / 2 <= box[2] and c[3] > box[1]
    ]
    across = [
        c
        for c in inside
        if c[0] < divide - GUTTER_REACH_PX and c[2] > divide + GUTTER_REACH_PX
    ]
    anchor_left = (anchor[0] + anchor[2]) / 2 < divide
    facing = [
        c
        for c in inside
        if c not in across and ((c[0] + c[2]) / 2 < divide) != anchor_left
    ]
    top = anchor[1]
    while True:
        found = [
            c
            for c in facing
            if c[1] < top
            and c[3] >= top - COLUMN_GAP_PX
            and not any(k[3] > c[1] and k[1] < top for k in across)
        ]
        if not found:
            break
        top = min(c[1] for c in found)
    foot = max((c[3] for c in inside if c[3] <= top), default=None)
    return top, foot


#: One key icon or caption box of a page: the box and its label.
Anchor = tuple[Box, str]

KEY = Label.KEY_ICON.name
CAPTION = Label.CASE_CAPTION.name


def held_anchors(box: Box, anchors: list[Anchor]) -> list[Anchor]:
    """Return the anchors a picture box is drawn over.

    The point is the one a boundary is anchored at, the test of
    :func:`outside`: the top of a caption, the foot of a key icon,
    more than :data:`ANCHOR_SLACK_PX` inside the box. So a box the
    masks cannot answer for is a box this names, whatever share of the
    caption it covers.

    :param box: A picture's box.
    :param anchors: The key icon and caption boxes of its page, in the
        same render pixels.
    """
    held = []
    for anchor in anchors:
        a, label = anchor
        at = a[3] if label == KEY else a[1]
        if (
            box[0] <= (a[0] + a[2]) / 2 <= box[2]
            and box[1] + ANCHOR_SLACK_PX < at < box[3] - ANCHOR_SLACK_PX
        ):
            held.append(anchor)
    return held


def trimmed_foot(
    box: Box,
    anchors: list[Anchor],
    cells: list[Box],
    divide: float,
) -> float | None:
    """Return the foot a full-width picture box should have, or None.

    Only a box that holds a key icon is trimmed: the key ends the
    opinion the picture belongs to, so what follows it is the next
    one's. A caption alone inside a box may be the caption of a form
    the picture reproduces, and that box is left for the card.

    :param box: The model's box, in render pixels.
    :param anchors: The key icon and caption boxes of the page.
    :param cells: The page's cells, in the same pixels; empty for a
        page nobody read, whose box ends at the anchor.
    :param divide: The gutter centre.
    :returns: The new ``y1``, or None when the box holds no key icon
        or the trim is not worth a write.
    """
    held = held_anchors(box, anchors)
    if not any(label == KEY for _box, label in held):
        return None
    highest = min((a for a, _label in held), key=lambda a: a[1])
    top, foot = column_top(box, highest, cells, divide)
    new = (foot + top) / 2 if foot is not None else top - FOOT_GAP_PX
    if box[3] - new < MIN_TRIM_PX or new - box[1] < MIN_KEPT_PX:
        return None
    return new


def _scaled_cells(page: PageCells | None, width: float, height: float):
    """Return a page's cells in the pixels of a render."""
    if page is None or page.width <= 0 or page.height <= 0:
        return []
    sx, sy = width / page.width, height / page.height
    return [(c[0] * sx, c[1] * sy, c[2] * sx, c[3] * sy) for c in page.boxes]


@dataclass(frozen=True)
class Held:
    """One full-width picture box that holds an anchor.

    :param row: The ``IMAGE`` row.
    :param anchors: The key icon and caption boxes of its page, in the
        row's render pixels.
    :param divide: The gutter centre of its page, the same way.
    """

    row: object
    anchors: list[Anchor]
    divide: float


def held_pictures(
    scan,
    pages=None,
    run=_ANY_RUN,
    model_only: bool = False,
    sized_keys: bool = False,
) -> list[Held]:
    """Return the full-width picture boxes that hold a key or a caption.

    **The one rule** of "this image box is drawn over the end of an
    opinion or the start of the next": a live ``IMAGE`` row across both
    columns of its page (:func:`is_full_width`) that holds a key icon
    or a caption (:func:`held_anchors`). An anchor counts when it passes
    blackletter's gate for its label, the anchors the pairing reads; a
    hand-drawn one always does. The trim reads it for the model's
    boxes, and the step-2 card for every box the trim left.

    Three queries at most, four with ``sized_keys``, and one for a
    volume with no picture wide enough to ask.

    :param scan: The scan.
    :param pages: The page indexes to read, or None for every page.
    :param run: The apply run of the rows, or nothing for every live
        row, the read of the compute.
    :param model_only: Leave out the boxes a curator drew.
    :param sized_keys: Count a key icon only when it also passes
        blackletter's size filter over the volume's keys
        (``boundaries.key_icon_rows``), the pairing's own rule. It
        reads every key icon of the scan, so the compute asks for it
        and a click's rebuild does not: the trim writes a box, and a
        key-shaped mark in an exhibit must not cut the picture, while
        a card is only a question and may ask once too often.
    :returns: The boxes, by page.
    """
    from blackletter.scanner import label_confidence

    from scanning.findings import model_rows_are_bl_warm
    from scanning.models import Detection

    live = Detection.objects.live().filter(scan=scan)
    if run is not _ANY_RUN:
        live = live.filter(apply_run=run)
    if pages is not None:
        live = live.filter(page_index__in=pages)
    rows = live.filter(label=IMAGE)
    if model_only:
        rows = rows.exclude(model_name=Detection.ModelName.MANUAL)
    pictures: dict[int, list] = {}
    for row in rows.order_by("page_index", "y0"):
        box = (row.x0, row.y0, row.x1, row.y1)
        if row.img_width and may_span(box, float(row.img_width)):
            pictures.setdefault(row.page_index, []).append(row)
    if not pictures:
        return []

    bl_warm = model_rows_are_bl_warm(scan)
    gates = {
        label.name: label_confidence(label, bl_warm)
        for label in (Label.KEY_ICON, Label.CASE_CAPTION)
    }
    anchors: dict[int, list[Anchor]] = {}
    kept = list(live.filter(label__in=gates, page_index__in=pictures))
    kept = [row for row in kept if row.confidence >= gates[row.label]]
    if sized_keys:
        from scanning import boundaries

        volume = Detection.objects.live().filter(scan=scan, label=KEY)
        sized = {
            row.pk for row in boundaries.key_icon_rows(list(volume), bl_warm)
        }
        kept = [row for row in kept if row.label != KEY or row.pk in sized]
    for row in kept:
        anchors.setdefault(row.page_index, []).append(
            ((row.x0, row.y0, row.x1, row.y1), row.label)
        )
    pictures = {i: rows for i, rows in pictures.items() if i in anchors}
    if not pictures:
        return []

    neighbours = neighbour_rows(scan, set(pictures))
    found = []
    for page_index, rows in sorted(pictures.items()):
        width = float(rows[0].img_width)
        spans = [
            (r.x0 * width / r.img_width, r.x1 * width / r.img_width)
            for r in neighbours.get(page_index, [])
        ]
        boxes = [(r.x0, r.y0, r.x1, r.y1) for r in rows]
        columns = page_columns(spans, boxes, page_index, width, neighbours)
        if columns is None:
            continue
        left, right = columns
        for row, box in zip(rows, boxes):
            if is_full_width(box, left, right) and held_anchors(
                box, anchors[page_index]
            ):
                found.append(
                    Held(row, anchors[page_index], (left[1] + right[0]) / 2)
                )
    return found


def trim_rows(scan, cells: dict[int, PageCells]) -> int:
    """Trim the full-width ``IMAGE`` rows of a scan off the next opinion.

    Model rows only, smaller only: a box a curator drew reaches the
    database as drawn, and :func:`over_boundary` asks about it, as it
    does about a box that holds a caption and no key icon.

    :param scan: The scan.
    :param cells: The cells of each page, ``text_fit.page_cells``.
    :returns: How many rows were written.
    :rtype: int
    """
    from scanning.models import Detection

    held = held_pictures(scan, model_only=True, sized_keys=True)
    changed = []
    for entry in held:
        row = entry.row
        page_cells = _scaled_cells(
            cells.get(row.page_index),
            float(row.img_width),
            float(row.img_height),
        )
        new = trimmed_foot(
            (row.x0, row.y0, row.x1, row.y1),
            entry.anchors,
            page_cells,
            entry.divide,
        )
        if new is None:
            continue
        row.y1 = round(new, 1)
        changed.append(row)
    if changed:
        Detection.objects.bulk_update(changed, ["y1"], batch_size=500)
    if held:
        logger.info(
            "full-width pictures: %d box(es) hold a key icon or a caption, "
            "%d trimmed to the columns under them",
            len(held),
            len(changed),
        )
    return len(changed)
