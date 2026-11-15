"""The pictures and tables the readers saw, against the model's image boxes.

Every OCR engine labels a picture and a table beside the text it
reads, and the three agree with each other to within a few percent of
the page. The model's ``IMAGE`` box is the portal's one picture: the
opinion PDF pastes the original scan of it over the page, the opinion
text leaves it out, and the figure pass cuts it (#463). So a picture
the model missed is a photograph read as words, and a box the model
ran over the text around a picture is words pasted as a photograph.

This module stores the readers' marks as rows (``PictureReading``, one
per page, the rule of ``TextReading``) and derives two things from
them and the ``Detection`` rows alone, with no S3 read and no render:

- :func:`uncovered`, one review-2 finding per picture or table the
  readers marked that no live ``IMAGE`` box covers by half
  (``UNBOXED_PICTURE``), shown in the step-2 sidebar with a ``+ image``
  and on the Images section of the card review with Add image box;
- :func:`fit_for`, the readers' box for a live model ``IMAGE`` row they
  draw differently, which the Images card offers as Refine image box.

Nothing here writes a box: every change is a curator's click, through
``add_single_detection`` or ``update_detection``, because an ``IMAGE``
box takes a table's words out of the text, and a silent one would do
that with nobody looking.

The readers' marks are read from the documents the run carries at the
compute (:func:`engine_documents`): dots.mocr, which every volume has,
and Mistral and Surya when their reads have landed. A volume computed
with dots alone gets dots' boxes; a later recompute reads the others.
"""

import logging
import re
import statistics
from collections.abc import Collection, Iterator

from blackletter.models import Label
from django.db import transaction

from scanning import detections, opinion_ocr
from scanning.models import (
    ApplyRun,
    CheckName,
    Detection,
    Issue,
    PictureReading,
    Scan,
    TextReading,
)

logger = logging.getLogger(__name__)

PICTURE = "picture"
TABLE = "table"

#: The readers' own labels for a picture and a table, by the engine name
#: of ``opinion_ocr.ENGINES``. The engine table itself carries no
#: picture label on purpose (#463: the engines are about text, and a
#: picture of the text is a curated box), so the labels live here, where
#: the picture is the point. A test pins every key to that table.
PICTURE_LABELS: dict[str, dict[str, str]] = {
    "dots_mocr": {"Picture": PICTURE, "Table": TABLE},
    "mistral_ocr": {"image": PICTURE, "table": TABLE},
    "surya": {"Picture": PICTURE, "Figure": PICTURE, "Table": TABLE},
}

#: What a person calls each reader on a card.
READER_NAMES = {
    "dots_mocr": "dots.mocr",
    "mistral_ocr": "Mistral",
    "surya": "Surya",
}

#: The share of the page a mark must cover to count. Every reader marks
#: the West key at the head of each opinion as a picture, a hundred a
#: volume, and each is under half a percent of the page; a picture or a
#: table worth a box is over one.
MIN_SHARE = 0.01

#: Two readers' marks are the same thing at this IoU or more.
CLUSTER_IOU = 0.5

#: A live ``IMAGE`` box covers a readers' box, so it makes no card, at
#: this share of the readers' box or more.
COVERED_SHARE = 0.5

#: A table one reader alone marked counts only when it is at least this
#: share of the page wide, across both columns: a reader calls a
#: centred heading or a list a table (Mistral on 2337's page 90), and
#: no heading of a two-column page is wider than its column, while the
#: claim tables YOLO missed on 627 F.Supp.3d ran across both. A picture
#: from any reader, and a table two readers agree on, count whatever
#: their width.
LONE_TABLE_WIDTH = 0.5

#: A model ``IMAGE`` box and the readers' box agree at this IoU or more,
#: and the card offers no refinement. 2337's boxes that were right sat
#: at 0.87 and 0.89; the ones that swallowed the text around a picture
#: sat under 0.5.
FIT_IOU = 0.9

IMAGE_LABEL = Label.IMAGE.name

#: How far, in the row's render pixels, a caption may sit above or below a
#: picture and still be its caption: about a line of body type. 1955's
#: "Figure 18: EIA Hypothetical Dispatch Curve" sat 11 px above its chart;
#: the court's paragraphs over a table sat 33 px and more above it and are
#: no caption by their category.
CAPTION_GAP_PX = 40

#: A reader cell that opens with one of these words is a picture's title
#: whatever category the reader gave it ("Table 2: ...", "Exhibit A").
CAPTION_START = re.compile(
    r"^\s*(?:figure|fig\.|table|exhibit|chart|graph|appendix)\b", re.I
)

Box = list[float]


def _positive(value) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and value > 0
    )


def _box(value) -> Box | None:
    """Return a box as four floats, or None for anything else."""
    try:
        box = [float(v) for v in value]
    except (TypeError, ValueError):
        return None
    if len(box) != 4 or box[2] <= box[0] or box[3] <= box[1]:
        return None
    return box


def _area(box: Box) -> float:
    return max(0.0, box[2] - box[0]) * max(0.0, box[3] - box[1])


def _inter(a: Box, b: Box) -> float:
    return max(0.0, min(a[2], b[2]) - max(a[0], b[0])) * max(
        0.0, min(a[3], b[3]) - max(a[1], b[1])
    )


def _iou(a: Box, b: Box) -> float:
    inter = _inter(a, b)
    union = _area(a) + _area(b) - inter
    return inter / union if union > 0 else 0.0


def _scaled(box: Box, sx: float, sy: float) -> Box:
    return [
        round(box[0] * sx, 1),
        round(box[1] * sy, 1),
        round(box[2] * sx, 1),
        round(box[3] * sy, 1),
    ]


# ---------------------------------------------------------------------------
# The documents and the rows
# ---------------------------------------------------------------------------


def page_size(document: dict, page: dict) -> tuple[float, float] | None:
    """Return the render a reader's boxes on a page are in.

    dots.mocr and Surya size each page (``origin_width`` by
    ``origin_height``); Mistral sizes the whole read once (``render``).

    :param document: The reader's document.
    :param page: One of its pages.
    :returns: ``(width, height)``, or None when the page has no size.
    :rtype: tuple[float, float] | None
    """
    for wide, high in (
        ("origin_width", "origin_height"),
        ("width", "height"),
        ("img_width", "img_height"),
    ):
        width, height = page.get(wide), page.get(high)
        if _positive(width) and _positive(height):
            return float(width), float(height)
    render = document.get("render")
    if isinstance(render, dict):
        width, height = render.get("width"), render.get("height")
        if _positive(width) and _positive(height):
            return float(width), float(height)
    return None


def read_documents(
    documents: dict[str, dict],
) -> dict[int, tuple[float, float, list[dict]]]:
    """Read the readers' picture and table marks of every page.

    :param documents: ``{engine name: document}`` in the order of
        ``opinion_ocr.ENGINES``; the first reader with a sized page sets
        the page's render.
    :returns: ``{page_index: (width, height, marks)}``, each mark
        ``{"engine", "kind", "box"}`` with its box in that render, for
        the pages with a mark.
    :rtype: dict[int, tuple[float, float, list[dict]]]
    """
    pages: dict[int, tuple[float, float, list[dict]]] = {}
    for name, document in documents.items():
        spec = opinion_ocr.ENGINES.get(name)
        labels = PICTURE_LABELS.get(name)
        if spec is None or labels is None or not isinstance(document, dict):
            continue
        for page in document.get("pages") or []:
            if not isinstance(page, dict):
                continue
            index = page.get("page_index")
            if not isinstance(index, int) or isinstance(index, bool):
                continue
            size = page_size(document, page)
            if size is None:
                continue
            width, height, marks = pages.get(index) or (size[0], size[1], [])
            sx, sy = width / size[0], height / size[1]
            for unit in page.get(spec.units_key) or []:
                if not isinstance(unit, dict):
                    continue
                kind = labels.get(unit.get(spec.type_key))
                if kind is None:
                    continue
                box = _box(unit.get("bbox"))
                if box is None:
                    continue
                marks.append(
                    {"engine": name, "kind": kind, "box": _scaled(box, sx, sy)}
                )
            pages[index] = (width, height, marks)
    return {index: entry for index, entry in pages.items() if entry[2]}


def engine_documents(
    scan: Scan, run: ApplyRun | None, primary
) -> dict[str, dict]:
    """Return the readers' documents of the space ``run`` names, by engine.

    The primary read (dots.mocr) is passed in, the compute's one read
    of it (``text_fit.load_document``). Every other engine of
    ``opinion_ocr.ENGINES`` is read here: the run's own document under
    a run (the field ``EngineSpec.key_field`` names), the glued volume
    document in the original's space. It never raises: an engine that
    has not read the volume, or whose document did not load, is left
    out and logged, and read again at the next compute.

    :param scan: The scan.
    :param run: The standing apply run, or None for the original's
        space.
    :param primary: The dots.mocr document, or None.
    :returns: ``{engine name: document}`` in the order of the table.
    :rtype: dict[str, dict]
    """
    from scanning import s3_sync

    documents: dict[str, dict] = {}
    for name, spec in opinion_ocr.ENGINES.items():
        if name == opinion_ocr.DEFAULT_ENGINE:
            if isinstance(primary, dict):
                documents[name] = primary
            continue
        try:
            if run is not None:
                key = getattr(run, spec.key_field, "") or ""
            else:
                key = spec.module.glued_volume_key(scan) or ""
            if not key:
                continue
            document = s3_sync.download_json_object(key)
        except Exception:
            logger.exception(
                "scan %s: the %s volume did not load; its pictures are read "
                "at the next compute",
                scan.pk,
                name,
            )
            continue
        if isinstance(document, dict):
            documents[name] = document
    return documents


def write_rows(
    scan: Scan, documents: dict[str, dict] | None, run: ApplyRun | None
) -> int:
    """Delete the scan's picture readings and write them again.

    The rule of ``undetected.write_rows``: a disposable row the compute
    owns, every reading of the scan replaced and not only the run's,
    and a primary document that did not load leaves the rows alone,
    because an empty set would withdraw every card of a volume whose
    S3 read failed.

    :param scan: The scan.
    :param documents: :func:`engine_documents`, or None.
    :param run: The apply run whose space the documents' pages are in.
    :returns: How many pages were written.
    """
    if not documents or opinion_ocr.DEFAULT_ENGINE not in documents:
        logger.warning(
            "scan %s: no OCR document, so the picture readings stay as they "
            "were",
            scan.pk,
        )
        return 0
    by_page = read_documents(documents)
    rows = []
    for index in sorted(by_page):
        width, height, marks = by_page[index]
        edit_id, source_page = detections.source_for_index(scan, index, run)
        rows.append(
            PictureReading(
                scan=scan,
                apply_run=run,
                source_edit_id=edit_id,
                source_page=source_page,
                source_fingerprint=scan.source_fingerprint or "",
                page_index=index,
                img_width=int(round(width)),
                img_height=int(round(height)),
                marks=marks,
            )
        )
    with transaction.atomic():
        PictureReading.objects.filter(scan=scan).delete()
        PictureReading.objects.bulk_create(rows, batch_size=500)
    logger.info(
        "scan %s: the pictures of %d page(s) written for run %s from %s",
        scan.pk,
        len(rows),
        run.pk if run else None,
        ", ".join(documents),
    )
    return len(rows)


# ---------------------------------------------------------------------------
# The readers' boxes
# ---------------------------------------------------------------------------


def clusters(marks: list[dict]) -> list[dict]:
    """Group a page's marks across the readers into one box each.

    A mark joins the first group it overlaps by :data:`CLUSTER_IOU`;
    a group's box is the median of each edge over its marks, so two
    readers that agree settle the edges and a third that drew wide
    moves none of them.

    :param marks: The page's marks, ``{"engine", "kind", "box"}``.
    :returns: One ``{"readers", "kinds", "box"}`` per group, the
        readers and the kinds sorted.
    :rtype: list[dict]
    """
    groups: list[list[dict]] = []
    for mark in marks:
        box = _box(mark.get("box"))
        if box is None:
            continue
        placed = {**mark, "box": box}
        for group in groups:
            if any(_iou(box, other["box"]) >= CLUSTER_IOU for other in group):
                group.append(placed)
                break
        else:
            groups.append([placed])
    return [
        {
            "readers": sorted({m.get("engine") or "" for m in group}),
            "kinds": sorted({m.get("kind") or "" for m in group}),
            "box": [
                round(statistics.median(m["box"][i] for m in group), 1)
                for i in range(4)
            ],
        }
        for group in groups
    ]


def reader_names(readers: list[str]) -> str:
    """Return the readers of a box as a person reads them."""
    names = [READER_NAMES.get(r, r) for r in readers]
    if len(names) <= 1:
        return "".join(names)
    return ", ".join(names[:-1]) + " and " + names[-1]


def _sized_rows(scan: Scan, run: ApplyRun | None, pages: set[int]):
    """Return the live rows of the pages that carry a render size."""
    return [
        row
        for row in Detection.objects.live().filter(
            scan=scan, apply_run=run, page_index__in=pages
        )
        if row.img_width and row.img_height
    ]


def uncovered(
    scan: Scan,
    run: ApplyRun | None,
    pages: Collection[int] | None = None,
) -> Iterator[dict]:
    """Yield one finding per picture or table no image box covers.

    Rows only: the readings of the measured run and the live ``IMAGE``
    rows of their pages, a curator's own box included, so a box drawn
    by hand answers the card at the next rebuild. A readers' box under
    :data:`MIN_SHARE` of the page is a key icon and makes none.

    The finding's box is in the pixels of the detections' render, the
    space every finding's box is in, and ``guess`` repeats it, so the
    sidebar's ``+ image`` adds the readers' box as it stands.

    :param scan: The scan.
    :param run: The apply run the rows are measured against, or None.
    :param pages: The page indexes to judge, or None for every page.
    :yields: The finding dicts ``findings.rebuild`` writes.
    """
    rows = PictureReading.objects.filter(scan=scan, apply_run=run)
    if pages is not None:
        rows = rows.filter(page_index__in=pages)
    readings = list(rows.order_by("page_index"))
    if not readings:
        return
    sized: dict[int, Detection] = {}
    images: dict[int, list[Detection]] = {}
    for row in _sized_rows(scan, run, {r.page_index for r in readings}):
        sized.setdefault(row.page_index, row)
        if row.label == IMAGE_LABEL:
            images.setdefault(row.page_index, []).append(row)
    for reading in readings:
        if not reading.img_width or not reading.img_height:
            continue
        frame = sized.get(reading.page_index)
        if frame is not None:
            width, height = frame.img_width, frame.img_height
        else:
            width, height = reading.img_width, reading.img_height
        sx, sy = width / reading.img_width, height / reading.img_height
        covers = [
            [
                d.x0 * width / d.img_width,
                d.y0 * height / d.img_height,
                d.x1 * width / d.img_width,
                d.y1 * height / d.img_height,
            ]
            for d in images.get(reading.page_index, [])
        ]
        for cluster in counted(reading):
            box = _scaled(cluster["box"], sx, sy)
            area = _area(box)
            if any(
                _inter(box, cover) / area >= COVERED_SHARE for cover in covers
            ):
                continue
            yield _finding(reading, cluster, box, width, height)


def _lone_narrow_table(cluster: dict, width: int) -> bool:
    """Return whether one reader alone called a block inside a column a
    table (:data:`LONE_TABLE_WIDTH`)."""
    box = cluster["box"]
    return (
        len(cluster["readers"]) == 1
        and cluster["kinds"] == [TABLE]
        and (box[2] - box[0]) < LONE_TABLE_WIDTH * width
    )


def counted(reading: PictureReading) -> list[dict]:
    """Return the readers' boxes of a page that count, in its render.

    **The one filter** both readers of the rows apply (:func:`uncovered`
    for the card, :func:`fit_for` for the refinement): the clusters of
    the page's marks less a box under :data:`MIN_SHARE` of the page (a
    key icon) and a table one reader alone marked inside a column
    (:func:`_lone_narrow_table`), so a block the card calls noise is
    never the box a model row is pulled to either.

    :param reading: The page's row.
    :returns: The clusters that count.
    :rtype: list[dict]
    """
    page_area = reading.img_width * reading.img_height
    return [
        cluster
        for cluster in clusters(reading.marks or [])
        if _area(cluster["box"]) >= MIN_SHARE * page_area
        and not _lone_narrow_table(cluster, reading.img_width)
    ]


def _finding(
    reading: PictureReading,
    cluster: dict,
    box: Box,
    width: int,
    height: int,
) -> dict:
    """Build the finding dict of one picture or table in no image box.

    The metadata is what ``findings.address_of`` keys a dismissal by,
    plus ``label_id`` and ``guess`` for the sidebar's ``+`` and the
    readers for the card.
    """
    kinds = cluster["kinds"]
    what = " or ".join(kinds) if kinds else "picture"
    message = (
        f"{reader_names(cluster['readers'])} read a {what} here that no "
        "image box covers. Add an image box over it, or dismiss the card "
        "if it is not one: the opinion PDF pastes the original scan of an "
        "image box over the page, and the opinion text leaves it out."
    )
    return {
        "check_name": CheckName.UNBOXED_PICTURE,
        "target": Issue.Target.DETECTION,
        "page_number": reading.page_index + 1,
        "message": message,
        "severity": Issue.Severity.WARNING,
        "metadata": {
            "page_index": reading.page_index,
            "label": IMAGE_LABEL,
            "label_id": Label.IMAGE.value,
            "bbox": box,
            "guess": box,
            "img_width": width,
            "img_height": height,
            "source_edit": reading.source_edit_id,
            "source_page": reading.source_page,
            "readers": cluster["readers"],
            "kinds": kinds,
        },
    }


def fit_for(
    scan: Scan, run: ApplyRun | None, rows: list[Detection]
) -> dict[int, dict]:
    """Return the readers' boxes for every model image row they draw differently.

    For each row, the union of the readers' boxes the row covers by
    :data:`COVERED_SHARE` (a box the model drew over two photographs
    side by side is pulled in to both, never to one of them), or the
    one readers' box that overlaps it most when it covers none that
    far (a box the model drew short of a picture grows to it), in the
    row's own render (the space ``update_detection`` writes), when its
    IoU with the row is under :data:`FIT_IOU`: the Images card offers
    it as Refine image box. A row the readers agree with gets no entry.
    A row no reader marked a picture or table under, on a page the
    readers read, gets an entry with no box, so the card can say so.

    **The options** (``options``) are what Refine cycles through: the
    union (``box``); the union over the caption right above or below
    (:func:`with_captions`); and, when the readers see two pictures or
    more under the row that do not touch (:func:`separate`), one box
    per picture, with and without their captions. A split keeps the
    text between two pictures out of the image, which the union takes
    in (1963's page 196: the court's paragraph between two figures). A
    row whose union the readers agree with still gets an entry when it
    has a split or a caption to offer.

    :param scan: The scan.
    :param run: The apply run the rows are measured against, or None.
    :param rows: The live model ``IMAGE`` rows.
    :returns: ``{row pk: {"box", "readers", "kinds", "iou", "options"}}``;
        each option ``{"label", "boxes"}``.
    :rtype: dict[int, dict]
    """
    if not rows:
        return {}
    pages = {r.page_index for r in rows}
    readings = {
        r.page_index: r
        for r in PictureReading.objects.filter(
            scan=scan, apply_run=run, page_index__in=pages
        )
    }
    texts = {
        t.page_index: t
        for t in TextReading.objects.filter(
            scan=scan, apply_run=run, page_index__in=pages
        )
    }
    # Every live box of those pages, each in its own render: what already
    # covers a block of text the refined box gives up.
    page_boxes: dict[int, list[tuple[int, Box, int, int]]] = {}
    for d in Detection.objects.live().filter(scan=scan, page_index__in=pages):
        page_boxes.setdefault(d.page_index, []).append(
            (
                d.pk,
                [d.x0, d.y0, d.x1, d.y1],
                d.img_width or 0,
                d.img_height or 0,
                d.label,
            )
        )
    out: dict[int, dict] = {}
    for row in rows:
        reading = readings.get(row.page_index)
        if (
            reading is None
            or not reading.img_width
            or not reading.img_height
            or not row.img_width
            or not row.img_height
        ):
            continue
        sx = row.img_width / reading.img_width
        sy = row.img_height / reading.img_height
        own = [row.x0, row.y0, row.x1, row.y1]
        inside: list[tuple[Box, dict]] = []
        best = None
        for cluster in counted(reading):
            box = _scaled(cluster["box"], sx, sy)
            if _inter(own, box) / _area(box) >= COVERED_SHARE:
                inside.append((box, cluster))
            score = _iou(own, box)
            if score > 0 and (best is None or score > best[0]):
                best = (score, box, cluster)
        if best is None:
            out[row.pk] = {
                "box": None,
                "readers": [],
                "kinds": [],
                "iou": 0.0,
                "options": [],
            }
            continue
        chosen = inside or [(best[1], best[2])]
        fit = _union([b for b, _ in chosen])
        captions = caption_cells(texts.get(row.page_index), row)
        options = refine_options(
            fit, [(b, c["readers"]) for b, c in chosen], captions
        )
        cells = text_cells(texts.get(row.page_index), row)
        others = [
            (
                _scaled(b, row.img_width / w, row.img_height / h)
                if w and h
                else b,
                label,
            )
            for pk, b, w, h, label in page_boxes.get(row.page_index, [])
            if pk != row.pk
        ]
        gutter = page_gutter(others, row.img_width)
        for option in options:
            option["boxes"] = [
                [round(v, 1) for v in b]
                for b in absorb_sideways(own, option["boxes"], cells)
            ]
            option["fill"] = freed_text(
                own, option["boxes"], cells, others, gutter
            )
        score = _iou(own, fit)
        if score < FIT_IOU or len(options) > 1:
            out[row.pk] = {
                "box": [round(v, 1) for v in fit],
                "readers": sorted(
                    {r for _, c in chosen for r in c["readers"]}
                ),
                "kinds": sorted({k for _, c in chosen for k in c["kinds"]}),
                "iou": round(score, 2),
                "options": options,
            }
    return out


def _union(boxes: list[Box]) -> Box:
    return [
        min(b[0] for b in boxes),
        min(b[1] for b in boxes),
        max(b[2] for b in boxes),
        max(b[3] for b in boxes),
    ]


def separate(boxes: list[Box]) -> list[Box]:
    """Return the pictures of ``boxes``: the boxes that touch joined into one.

    Two readers' boxes that overlap or touch are one picture (a photograph
    the readers cut in two, two photographs printed edge to edge); boxes
    with a gap between them are separate pictures, whatever lies in the
    gap. Top to bottom, then left to right.
    """
    groups: list[Box] = []
    for box in sorted(boxes, key=lambda b: (b[1], b[0])):
        for i, g in enumerate(groups):
            if (
                box[0] <= g[2]
                and g[0] <= box[2]
                and box[1] <= g[3]
                and g[1] <= box[3]
            ):
                groups[i] = _union([g, box])
                break
        else:
            groups.append(list(box))
    # A join can make a group touch one placed before it: join until none do.
    merged = True
    while merged:
        merged = False
        for i in range(len(groups)):
            for j in range(i + 1, len(groups)):
                a, b = groups[i], groups[j]
                if (
                    a[0] <= b[2]
                    and b[0] <= a[2]
                    and a[1] <= b[3]
                    and b[1] <= a[3]
                ):
                    groups[i] = _union([a, b])
                    del groups[j]
                    merged = True
                    break
            if merged:
                break
    return sorted(groups, key=lambda b: (b[1], b[0]))


def caption_cells(text: TextReading | None, row: Detection) -> list[Box]:
    """Return the boxes of the reader cells of a page that are a caption,
    in the row's render: the reader's ``Caption`` category, or a cell that
    opens with "Figure", "Table", "Exhibit" and the like
    (:data:`CAPTION_START`). A court's paragraph is no caption, whatever
    its place."""
    if text is None or not text.img_width or not text.img_height:
        return []
    sx = row.img_width / text.img_width
    sy = row.img_height / text.img_height
    out = []
    for cell in text.cells or []:
        if (
            cell.get("kind") or ""
        ).lower() == "caption" or CAPTION_START.match(cell.get("text") or ""):
            out.append(_scaled(cell["box"], sx, sy))
    return out


def with_captions(box: Box, captions: list[Box]) -> Box:
    """Return ``box`` grown over the caption right above or below it: a caption
    cell within :data:`CAPTION_GAP_PX` of the box's top or bottom that shares
    half the narrower one's width with it. The nearest above and the nearest
    below, never one further out."""
    above = below = None
    for cap in captions:
        shared = min(box[2], cap[2]) - max(box[0], cap[0])
        if shared < 0.5 * min(box[2] - box[0], cap[2] - cap[0]):
            continue
        gap_above = box[1] - cap[3]
        gap_below = cap[1] - box[3]
        if 0 <= gap_above <= CAPTION_GAP_PX and (
            above is None or gap_above < above[0]
        ):
            above = (gap_above, cap)
        if 0 <= gap_below <= CAPTION_GAP_PX and (
            below is None or gap_below < below[0]
        ):
            below = (gap_below, cap)
    parts = [box] + [hit[1] for hit in (above, below) if hit is not None]
    return _union(parts)


def refine_options(
    fit: Box, chosen: list[tuple[Box, list[str]]], captions: list[Box]
) -> list[dict]:
    """Return what Refine cycles through, the likeliest first.

    ``chosen`` is each picture under the row with the readers that marked
    it. When some of them only one reader saw (Surya calling the court's
    bulleted list a table over 432 F.Supp.3d's page 892, above a map all
    three readers marked), the options built from what two readers or
    more agree on come first: a lone reader is where a wrong mark comes
    from. Then the readers' box over everything, with its caption, and a
    split into one box per picture when they see two or more.

    Each option is ``{"label", "boxes"}``, the boxes rounded. An option that
    gives the same boxes as one before it is left out.
    """

    def rounded(boxes):
        return [[round(v, 1) for v in b] for b in boxes]

    def build(name, boxes):
        out = [
            (name, [_union(boxes)]),
            (
                name + ", with the caption",
                [with_captions(_union(boxes), captions)],
            ),
        ]
        parts = separate(boxes)
        if len(parts) >= 2:
            out.append(
                (
                    f"Split into {len(parts)} boxes"
                    + name[len("Refine to") :].replace(
                        " the readers' box", ""
                    ),
                    parts,
                )
            )
            out.append(
                (
                    f"Split into {len(parts)} boxes, with captions"
                    + name[len("Refine to") :].replace(
                        " the readers' box", ""
                    ),
                    [with_captions(p, captions) for p in parts],
                )
            )
        return out

    boxes = [b for b, _ in chosen]
    agreed = [b for b, readers in chosen if len(readers) >= 2]
    candidates = []
    if agreed and len(agreed) < len(boxes):
        candidates += build("Refine to what the readers agree on", agreed)
    candidates += build("Refine to the readers' box", boxes)
    options, seen = [], []
    for label, bs in candidates:
        label = label.replace(
            "Refine to the readers' box, with the caption",
            "Refine with the caption",
        )
        r = rounded(bs)
        if r in seen:
            continue
        seen.append(r)
        options.append({"label": label, "boxes": r})
    return options


#: A text cell counts as under a box when this share of it lies inside.
CELL_SHARE = 0.5

#: Two freed text cells of one column this close (row-render pixels) are
#: one block, so a paragraph's cells get one box and not one each.
BLOCK_GAP_PX = 40

#: The reader categories that are a footnote, given a footnote box; every
#: other category of text (a paragraph, a heading, a list item) a column box.
FOOTNOTE_KINDS = frozenset({"footnote"})


def text_cells(
    text: TextReading | None, row: Detection
) -> list[tuple[Box, str]]:
    """Return the reader's text cells of a page, ``(box, kind)``, in the row's render."""
    if text is None or not text.img_width or not text.img_height:
        return []
    sx = row.img_width / text.img_width
    sy = row.img_height / text.img_height
    return [
        (_scaled(c["box"], sx, sy), (c.get("kind") or "").lower())
        for c in text.cells or []
        if c.get("box")
    ]


def freed_text(
    own: Box,
    boxes: list[Box],
    cells: list[tuple[Box, str]],
    others: list[tuple[Box, str]],
    gutter: float | None = None,
) -> list[dict]:
    """Return the blocks of text a refine would leave in no box, with the box
    each needs.

    A cell is freed when the model's box covered it (:data:`CELL_SHARE`)
    and neither a box of the option nor another live box of the page does:
    the court's paragraph between two figures a split takes the union off,
    the lines above a table the refine pulls the box down from. Freed cells
    of one column within :data:`BLOCK_GAP_PX` of each other are one block;
    a block of footnote cells gets a ``FOOTNOTES`` box, any other a
    ``TEXT_COLUMN`` box, which joins the column it lies in
    (``columns.column_groups``). A cell is a footnote when the reader
    says so, or when it starts at or below the top of a footnote box of
    its column: a reader labels a footnote ``Text`` as often as not.
    A column box is one column wide: a block that crosses the ``gutter``
    (a cell the reader merged across both columns) is cut there into a
    box for each side.

    :returns: ``[{"label", "box"}]`` in the row's render, top to bottom.
    """

    def under(cell, box):
        return _inter(cell, box) >= CELL_SHARE * max(_area(cell), 1e-9)

    freed: list[tuple[Box, str]] = []
    for cell, kind in cells:
        if not under(cell, own):
            continue
        if any(under(cell, b) for b in boxes) or any(
            under(cell, o) for o, _ in others
        ):
            continue
        in_notes = any(
            lab == Label.FOOTNOTES.name
            and min(o[2], cell[2]) - max(o[0], cell[0]) > 0
            and cell[1] >= o[1] - 5
            for o, lab in others
        )
        label = (
            Label.FOOTNOTES.name
            if kind in FOOTNOTE_KINDS or in_notes
            else Label.TEXT_COLUMN.name
        )
        freed.append((cell, label))
    blocks: list[list] = []
    for cell, label in sorted(freed, key=lambda c: (c[0][1], c[0][0])):
        for block in blocks:
            b, lab = block
            same_column = min(b[2], cell[2]) - max(b[0], cell[0]) > 0
            if lab == label and same_column and cell[1] - b[3] <= BLOCK_GAP_PX:
                block[0] = _union([b, cell])
                break
        else:
            blocks.append([list(cell), label])
    out = []
    for box, label in sorted(blocks, key=lambda b: (b[0][1], b[0][0])):
        if (
            gutter is not None
            and box[0] < gutter - GUTTER_REACH_PX
            and box[2] > gutter + GUTTER_REACH_PX
        ):
            pieces = [
                [box[0], box[1], gutter - GUTTER_GAP_PX, box[3]],
                [gutter + GUTTER_GAP_PX, box[1], box[2], box[3]],
            ]
        else:
            pieces = [box]
        out.extend(
            {"label": label, "box": [round(v, 1) for v in piece]}
            for piece in pieces
        )
    return out


#: A block crosses the gutter when it reaches this far into both columns.
GUTTER_REACH_PX = 40

#: Each half of a block cut at the gutter stops this far from its middle.
GUTTER_GAP_PX = 8


def page_gutter(others: list[tuple[Box, str]], width: float) -> float | None:
    """Return the x of the page's gutter, in the row's render: midway between
    its two column boxes when it has two side by side, else the middle of the
    page. None on a page too narrow to have two columns."""
    cols = sorted(
        (b for b, lab in others if lab == Label.TEXT_COLUMN.name),
        key=lambda b: b[0],
    )
    for left, right in zip(cols, cols[1:]):
        if left[2] <= right[0] + 10:
            return (left[2] + right[0]) / 2
    return width / 2 if width else None


#: A reader cell this many times taller than it is wide is a line of text
#: set sideways: a chart's axis label, a landscape table's headings. Text of
#: the court runs across the page, never down it.
SIDEWAYS_RATIO = 2.5

#: How far, in row-render pixels, a sideways cell may stand from an image box
#: of the option and still be part of that picture: 1963's page 704 set
#: each sideways table title 61-62 px left of its table.
SIDEWAYS_GAP_PX = 90


def absorb_sideways(
    own: Box, boxes: list[Box], cells: list[tuple[Box, str]]
) -> list[Box]:
    """Return ``boxes`` grown over the sideways text beside them.

    A cell inside the model's box (:data:`CELL_SHARE`) that is
    :data:`SIDEWAYS_RATIO` times taller than wide and within
    :data:`SIDEWAYS_GAP_PX` of an image box is part of that picture (a
    rotated label), so the box grows over it rather than the cell being
    given a text column box. Repeated until no box grows, so a run of
    rotated labels joins one after the other.
    """
    out = [list(b) for b in boxes]
    sideways = [
        c
        for c, _ in cells
        if (c[3] - c[1]) >= SIDEWAYS_RATIO * max(c[2] - c[0], 1e-9)
        and _inter(c, own) >= CELL_SHARE * max(_area(c), 1e-9)
    ]
    grew = True
    while grew:
        grew = False
        for c in sideways:
            for i, b in enumerate(out):
                if _inter(c, b) >= CELL_SHARE * _area(c):
                    continue
                gap_x = max(b[0] - c[2], c[0] - b[2], 0)
                gap_y = max(b[1] - c[3], c[1] - b[3], 0)
                if gap_x <= SIDEWAYS_GAP_PX and gap_y <= SIDEWAYS_GAP_PX:
                    out[i] = _union([b, c])
                    grew = True
                    break
    return out
