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
    """Return the readers' box for every model image row they draw differently.

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

    :param scan: The scan.
    :param run: The apply run the rows are measured against, or None.
    :param rows: The live model ``IMAGE`` rows.
    :returns: ``{row pk: {"box", "readers", "kinds", "iou"}}``.
    :rtype: dict[int, dict]
    """
    if not rows:
        return {}
    readings = {
        r.page_index: r
        for r in PictureReading.objects.filter(
            scan=scan,
            apply_run=run,
            page_index__in={r.page_index for r in rows},
        )
    }
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
            out[row.pk] = {"box": None, "readers": [], "kinds": [], "iou": 0.0}
            continue
        chosen = inside or [(best[1], best[2])]
        fit = [
            min(b[0] for b, _ in chosen),
            min(b[1] for b, _ in chosen),
            max(b[2] for b, _ in chosen),
            max(b[3] for b, _ in chosen),
        ]
        score = _iou(own, fit)
        if score < FIT_IOU:
            out[row.pk] = {
                "box": [round(v, 1) for v in fit],
                "readers": sorted(
                    {r for _, c in chosen for r in c["readers"]}
                ),
                "kinds": sorted({k for _, c in chosen for k in c["kinds"]}),
                "iou": round(score, 2),
            }
    return out
