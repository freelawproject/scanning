"""The OCR documents of one opinion (#350).

Every engine reads the whole volume, and its read is glued into one
document per corrected volume (``ApplyRun.ocr_key`` for dots.mocr,
``ApplyRun.extract_key`` for Mistral, ``ApplyRun.surya_key`` for
Surya). The third review (#334) and the
OCR ensemble (#317) work on one opinion at a time, and they must never
see the text under a redaction or the text of the neighbour opinion on
a shared page. This module cuts each engine's document to the pages of
one ``Opinion`` row, computes that exclusion once, and writes the
result under the row's ``glue_prefix``.

**Verdicts, not deletions.** Every unit (a dots.mocr cell, a Mistral
block) of the opinion's pages is in the file, with an ``exclusion``
and a ``share``. The ensemble experiment of #317 showed that a drop
before the alignment harms it: the engines' boxes differ, so a box a
redaction covers in one engine and not in the other leaves one-engine
regions behind. The ensemble aligns first and drops the aligned group
any of whose units carries an exclusion; :func:`kept_units` is the one
reader for a consumer that wants the clean text alone. The excluded
text stays under ``jobs/``, which nothing serves without a login.

**A printed page number is not opinion text either** (#396). It is
approved by review 1 and frozen by the apply in the printed-page map
(``apply.printed_pages``), which holds the value of every final page
and no position: every engine reads the number inside a larger unit,
with the running head. So the glue reads the approved value and finds
it in each engine's own text: a unit that sits in the head or the foot
band and one of whose lines ends in that value
(``page_numbers.carries_number``) carries the exclusion
``page_number``, and the running head goes with it. Both conditions
are required. The band alone takes every head cell, number or not;
the value alone takes a body line that ends in the same digits, which
is opinion text. Nothing is read off a dots.mocr box, so a page
dots.mocr failed still loses the headers of the other engines, and a
page whose approved number the engines did not print (a curator's
label on an inserted page, or no number at all) keeps every unit.

**A unit nobody could measure is not clean text.** A unit with no box,
or on a page whose size no detection and no render gives, carries the
third verdict :data:`UNJUDGED`, counts in ``unjudged`` on the page and
in the manifest, and stays out of :func:`kept_units`. It may be under
a redaction, and the rule of the module is that no reader sees the
text under one, so "not judged" never reads as "judged and clean".

**One unit shape for every engine**, so the reader keeps one adapter:
``id``, ``type``, ``text``, ``bbox`` (the engine's render pixels, as
glued), ``box_pt`` (the same box in PDF points, the space every
review-2 row and the viewer use), ``exclusion`` and ``share``. The
page ``md`` is not copied: it is the whole page, redacted text
included, and it cannot carry a verdict.

**The path is the invariant key.** ``Opinion.glue_prefix`` is
``jobs/opinions/{first_printed_page}.{index_in_page}/r{glue_revision}/``,
so a script that walks the bucket finds an opinion by the printed page
it looks at, with no database. One ``{engine}.json`` per engine of
:data:`ENGINES`, and ``manifest.json`` last: only a complete manifest
reads as "glued", the rule of ``sharding.ensure_shards``.

**The ledger is on the row.** ``Opinion.ocr_glue_revision ==
glue_revision`` is the one rule for "the OCR glue exists"
(:func:`is_written`), a query and never an S3 HEAD. The pass
(:func:`glue_due`) runs on the collect tick. It walks the due scans
newest first and glues the first one whose inputs load, up to
:data:`OPINIONS_PER_TICK` rows of it, so the volume documents are
parsed once per tick and a held volume holds no other. A fact about
the scan (no corrected volume, a stale redaction set, an engine the
run still owes, a document that does not pull) holds that scan and
spends nothing, the rule of ``apply.gates_closed``. A fact about the
row (a lost boundary, a page the document lacks) spends an attempt,
and at
:data:`MAX_ATTEMPTS` the row is ``ERROR``: loud, then quiet, the rule
of ``ApplyRun.attempts``. The way back is the next approval, which
raises the revision (``opinions.create_rows``).

**An engine the run owes holds the scan.** A Mistral batch takes up to
a day, and a curator can approve review 2 while it runs. The ensemble
needs every vote, so the glue waits for a read it knows is coming
(:func:`engines_owed`). An engine nobody asked to read is not owed;
when its read arrives later, ``reglue_opinion_ocr`` raises the
revision, and the pass writes the row again. No pass watches the run
for a late key.
"""

from __future__ import annotations

import functools
import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from django.db.models import F, Q
from django.utils import timezone

from scanning import (
    apply,
    boundaries,
    dots_mocr,
    mistral_ocr,
    page_numbers,
    redactions,
    review_states,
    s3_sync,
    surya,
    yolo,
)
from scanning.models import (
    DEAD_JOB_STATUSES,
    Detection,
    Opinion,
    OpinionReviewStatus,
    Scan,
    Status,
)

logger = logging.getLogger(__name__)

#: Version of the documents this module writes.
SCHEMA_VERSION = 1

#: The file that says a revision is glued, written last.
MANIFEST = "manifest.json"

#: A unit a covering box takes this share of or more carries an
#: ``exclusion``. The ensemble experiment's ``DROP_FRACTION`` (#317): a
#: region a redaction zone covered by a tenth or more was dropped.
EXCLUDE_SHARE = 0.10

#: Below this share an excluded unit is **partial**: the box covers a
#: part of a cell, which is a box that is too small or too large, and
#: the ``PARTIAL_REDACTION`` card of #334 asks a human to look.
FULL_SHARE = 0.90

#: How many rows one tick writes, all of one scan. One row is a cut of
#: about four pages, a geometry over about 800 units and 30 boxes and
#: two PUTs, about a third of a second; ten hold the serial scheduler
#: for about three seconds.
OPINIONS_PER_TICK = 10

#: Failed ticks on one row at one revision before the row is ERROR.
MAX_ATTEMPTS = 3

#: The verdict of a unit nobody could measure: no box, or a page with
#: no size. Not clean text, and not a redaction either.
UNJUDGED = "unjudged"

#: The verdict of the unit that holds the printed page number (#396):
#: in the head or the foot band, and one of its lines ends in the
#: approved number of its page. Taken whole, running head included.
PAGE_NUMBER = "page_number"

#: The start of every ``Opinion.error_message`` this module writes, so
#: a success clears its own message and nobody else's: the field is
#: shared with every work that prepares the review.
MESSAGE_PREFIX = "OCR glue: "

#: Points per inch, for the page size a 200-dpi render implies.
POINTS_PER_INCH = 72.0


class OpinionGlueError(Exception):
    """A fact about one row: the glue of this opinion cannot be written.

    Spends an attempt on the row. The message goes to
    ``Opinion.error_message``.
    """


class ScanHeld(Exception):
    """A fact about the scan: no row of it can be glued this tick.

    Spends nothing. The message is logged once per tick.
    """


# ---------------------------------------------------------------------------
# The engines
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EngineSpec:
    """How one engine's corrected-volume document is read.

    :param name: The ``JobEngine`` value, and the file name. What a
        person calls the engine is that choice's own label
        (``JobEngine(name).label``) and is not copied here (#381).
    :param key_field: The ``ApplyRun`` field that names the document.
    :param units_key: The page field that holds the units.
    :param text_key: The unit field that holds the text.
    :param type_key: The unit field that holds the engine's label.
    :param frame: Returns the render size ``(width, height)`` a page's
        boxes are measured in, or None.
    :param module: The stage's module (``dots_mocr``, ``mistral_ocr``,
        ``surya``), for the two functions all three share:
        ``glued_volume_key(scan)`` and ``run_summary(scan)``. The
        volume document is what the text overlay reads (#381), and the
        corrected volume's is what this module reads.
    :param owed_rows: Returns the rows that say a read of this engine
        is on its way for a scan and run: a live volume run, or the
        rows of the run's own edited pages.
    """

    name: str
    key_field: str
    units_key: str
    text_key: str
    type_key: str
    frame: Callable[[dict, dict], tuple[float, float] | None]
    module: object
    owed_rows: Callable[[Scan, object], list]

    def document_key(self, run) -> str:
        """The S3 key of this engine's document for ``run``."""
        return getattr(run, self.key_field) or ""

    @property
    def fields(self) -> dict[str, str]:
        """The field names a reader of this engine's pages needs.

        The three names that differ between the engines, in one dict.
        The text overlay's endpoint answers it beside the presigned URL
        (#381), so the browser reads a document it knows nothing about
        and a fourth engine is one more entry of :data:`ENGINES`.

        :returns: ``{"units", "text", "type"}``.
        :rtype: dict[str, str]
        """
        return {
            "units": self.units_key,
            "text": self.text_key,
            "type": self.type_key,
        }


def _page_frame(page: dict, document: dict) -> tuple[float, float] | None:
    """The render size of one page, off the page itself.

    The rule of the two engines whose worker renders each page and
    reports that render's size: dots.mocr and Surya both write
    ``origin_width`` and ``origin_height``, and every box of the page
    is in that pixel space.
    """
    width, height = page.get("origin_width"), page.get("origin_height")
    if _positive(width) and _positive(height):
        return float(width), float(height)
    return None


def _mistral_frame(page: dict, document: dict) -> tuple[float, float] | None:
    """The render size of a Mistral page: the document's ``render``."""
    render = document.get("render") or {}
    width = render.get("width", mistral_ocr.RENDER_W)
    height = render.get("height", mistral_ocr.RENDER_H)
    if _positive(width) and _positive(height):
        return float(width), float(height)
    return None


def _extract_owed_rows(stage, scan: Scan, run) -> list:
    """The rows that say one ``EXTRACT`` read is on its way, last step
    first.

    The rule of both engines a person starts (#245, #368), which read
    in two steps: the volume rows, then the rows of the pages a curator
    changed, which the tick creates once the volume is glued. **The
    later step decides**, because the earlier one is already done when
    it exists: a consumed volume run with a dead apply row is a read
    nothing will finish, and asking the volume first would call it owed
    for good. A scan with no apply row yet falls back to the volume
    rows, which is the window before the tick creates them.

    :param stage: The engine's module (``mistral_ocr`` or ``surya``).
    :param scan: The scan.
    :param run: The final apply run.
    :returns: The rows of the last step that exists.
    :rtype: list
    """
    return stage.apply_jobs(scan, run) or stage.live_extract_jobs(scan)


#: The engines, in the order the ensemble votes (#365). Surya is last
#: (#368): nobody has measured it against the other two on this
#: corpus, so it takes the rank that moves neither of them. A
#: measurement is what moves it.
#:
#: A third engine is one entry here and no other code. Every reader
#: walks this table: the glue, the files index, the file route,
#: ``engines_owed``, and the text overlay of the viewer (#381).
#:
#: What a person calls an engine is not here: ``JobEngine`` carries
#: that already, as the label of its own choice.
ENGINES: dict[str, EngineSpec] = {
    "dots_mocr": EngineSpec(
        name="dots_mocr",
        key_field="ocr_key",
        units_key="cells",
        text_key="text",
        type_key="category",
        frame=_page_frame,
        module=dots_mocr,
        owed_rows=lambda scan, run: dots_mocr.live_analyze_jobs(scan),
    ),
    "mistral_ocr": EngineSpec(
        name="mistral_ocr",
        key_field="extract_key",
        units_key="blocks",
        text_key=mistral_ocr.BLOCK_TEXT_KEY,
        type_key="type",
        frame=_mistral_frame,
        module=mistral_ocr,
        owed_rows=functools.partial(_extract_owed_rows, mistral_ocr),
    ),
    "surya": EngineSpec(
        name="surya",
        key_field="surya_key",
        units_key="blocks",
        # The block's flattened text, not its ``html``: the unit shape
        # is one shape for every engine, and the markup stays in the
        # volume document for a reader of the tables.
        text_key="text",
        type_key="label",
        frame=_page_frame,
        module=surya,
        owed_rows=functools.partial(_extract_owed_rows, surya),
    ),
}

#: The engine a reader gets when nobody named one (#381): the first
#: entry of :data:`ENGINES`. dots.mocr is the read the pipeline pays
#: for on every volume, so it is the one that is there.
DEFAULT_ENGINE = next(iter(ENGINES))


# ---------------------------------------------------------------------------
# The keys and the ledger
# ---------------------------------------------------------------------------


def engine_key(opinion: Opinion, engine: str) -> str:
    """Return the S3 key of one engine's document for ``opinion``.

    :param opinion: The row.
    :param engine: A name of :data:`ENGINES`, or ``"manifest"``.
    :returns: The key.
    :rtype: str
    """
    prefix = s3_sync.s3_processing_prefix(opinion.scan)
    name = MANIFEST if engine == "manifest" else f"{engine}.json"
    return f"{prefix}{opinion.glue_prefix}{name}"


def is_written(opinion: Opinion) -> bool:
    """Return whether the OCR glue of the live revision exists.

    **The one rule** for "the OCR glue exists", read off the row and
    never off the bucket.

    :param opinion: The row.
    :returns: Whether the stamp names the live revision.
    :rtype: bool
    """
    return (
        opinion.ocr_glue_revision is not None
        and opinion.ocr_glue_revision == opinion.glue_revision
    )


def due():
    """Return the rows that owe their OCR glue, as a queryset.

    A row whose stamp is not the live revision, that is not ``ERROR``,
    that has attempts left, and whose scan is in
    ``REDACTION_REVIEW_DONE``: a volume an admin sent back writes no
    glue while it is back.

    :returns: The queryset, unordered.
    """
    return (
        Opinion.objects.filter(
            scan__status=Status.REDACTION_REVIEW_DONE,
            ocr_glue_attempts__lt=MAX_ATTEMPTS,
        )
        .exclude(status=OpinionReviewStatus.ERROR)
        .filter(
            Q(ocr_glue_revision__isnull=True)
            | ~Q(ocr_glue_revision=F("glue_revision"))
        )
    )


def engines_owed(scan: Scan, run) -> list[str]:
    """Return the engines ``run`` does not have yet but will get.

    An engine whose key on the run is blank while a read of it is on
    its way: a live volume run, or the rows of the run's edited pages,
    with at least one row that is not dead (``DEAD_JOB_STATUSES``). A
    dead run brings no read, and only a person restarts it, so it does
    not hold the glue. An engine nobody asked to read is not owed.

    :param scan: The scan.
    :param run: The final apply run.
    :returns: Engine names, in :data:`ENGINES` order.
    :rtype: list[str]
    """
    owed = []
    for spec in ENGINES.values():
        if spec.document_key(run):
            continue
        rows = spec.owed_rows(scan, run)
        if any(row.status not in DEAD_JOB_STATUSES for row in rows):
            owed.append(spec.name)
    return owed


# ---------------------------------------------------------------------------
# The inputs of one scan
# ---------------------------------------------------------------------------


@dataclass
class ScanInputs:
    """What every row of one scan is cut from, read once per tick.

    :param run: The final apply run.
    :param documents: ``{engine: document}`` for the engines the run
        has.
    :param redactions: ``{page_index: [rect]}`` in points, the boxes a
        reader paints.
    :param renders: ``{page_index: (img_width, img_height)}`` of the
        live detections, for the page size in points.
    :param printed: ``{page_index: value}``, the approved page number
        of every final page that has one, off the run's printed-page
        map (#396).
    """

    run: object
    documents: dict[str, dict] = field(default_factory=dict)
    redactions: dict[int, list[dict]] = field(default_factory=dict)
    renders: dict[int, tuple[int, int]] = field(default_factory=dict)
    printed: dict[int, str] = field(default_factory=dict)


def load_inputs(scan: Scan) -> ScanInputs:
    """Read what one scan's rows are cut from, or say why not.

    Every check here is a fact about the scan, so a refusal is
    :class:`ScanHeld` and spends nothing. The documents come through
    ``apply.local_copy``, so a second tick over the same scan reads
    the mirror and pulls nothing.

    :param scan: The scan.
    :returns: The inputs.
    :rtype: ScanInputs
    :raises ScanHeld: When the corrected volume is not built, the
        redactions are not measured against it, an engine is owed, or
        a document does not pull, the printed-page map included.
    """
    run = review_states.final_run(scan)
    if run is None:
        raise ScanHeld("the corrected volume is not built")
    if not yolo.redactions_current(yolo.live_detect_jobs(scan), run):
        raise ScanHeld(f"the redactions are not measured against {run.label}")
    owed = engines_owed(scan, run)
    if owed:
        raise ScanHeld(f"the run owes a read from {', '.join(owed)}")

    documents: dict[str, dict] = {}
    for spec in ENGINES.values():
        key = spec.document_key(run)
        if not key:
            continue
        try:
            path = apply.local_copy(scan, key)
            document = json.loads(path.read_text())
        except (apply.ApplyError, OSError, ValueError) as exc:
            raise ScanHeld(f"the {spec.name} document did not load: {exc}")
        if not isinstance(document, dict) or "pages" not in document:
            raise ScanHeld(f"the object at {key} is not a volume document")
        documents[spec.name] = document
    if not documents:
        raise ScanHeld("the run has no engine document")

    inputs = ScanInputs(run=run, documents=documents)
    inputs.printed = _printed_numbers(scan, run)
    for entry in redactions.visible_by_page(scan):
        inputs.redactions[entry["page_index"]] = entry["rects"]
    # The run's space alone: a human row ``detections.relocate_rows``
    # could not place keeps its old index and its old run.
    for page_index, width, height in (
        Detection.objects.live()
        .filter(scan=scan, apply_run=run, img_width__gt=0, img_height__gt=0)
        .values_list("page_index", "img_width", "img_height")
        .distinct()
    ):
        inputs.renders.setdefault(page_index, (width, height))
    return inputs


def _printed_numbers(scan: Scan, run) -> dict[int, str]:
    """Read the approved page number of every final page (#396).

    The run's printed-page map, through ``apply.local_copy`` like the
    engine documents, so a second tick over the scan pulls nothing.
    The map is the one review 1 approved: the model's reading with the
    curator's own numbers over it (``apply.printed_pages``). A page
    with no number is absent, so a reader's ``get`` answers None.

    :param scan: The scan.
    :param run: The final apply run.
    :returns: ``{page_index: value}``.
    :rtype: dict[int, str]
    :raises ScanHeld: When the map does not load, a fact about the
        scan.
    """
    key = run.printed_pages_key
    try:
        document = json.loads(apply.local_copy(scan, key).read_text())
    except (apply.ApplyError, OSError, ValueError) as exc:
        raise ScanHeld(f"the printed-page map did not load: {exc}")
    if not isinstance(document, dict) or "pages" not in document:
        raise ScanHeld(f"the object at {key} is not a printed-page map")
    printed: dict[int, str] = {}
    for page in document["pages"] or []:
        if not isinstance(page, dict) or not page.get("printed"):
            continue
        final = page.get("final_page")
        if isinstance(final, int) and final > 0:
            printed[final - 1] = str(page["printed"])
    return printed


# ---------------------------------------------------------------------------
# The geometry
# ---------------------------------------------------------------------------


def page_size_pt(
    inputs: ScanInputs, page_index: int, dots_page: dict | None
) -> tuple[float, float] | None:
    """Return one page's size in points, or None.

    One rule, two sources. First the live ``Detection`` rows of the
    page, at ``yolo.DPI``: the rule ``boundaries.outside_rects`` uses,
    and every page with a redaction box has a detection. Else the
    dots.mocr render of the page, at ``dots_mocr.DPI``, or at 72 on a
    ``render_fallback`` page (``text_fit.PageCells``).

    :param inputs: The scan's inputs.
    :param page_index: The final page.
    :param dots_page: The dots.mocr page dict, when the run has one.
    :returns: ``(width, height)`` in points.
    :rtype: tuple[float, float] | None
    """
    render = inputs.renders.get(page_index)
    if render:
        width, height = render
        return boundaries.to_points(width, height, width, height)
    frame = _page_frame(dots_page or {}, {})
    if frame is None:
        return None
    dpi = 72.0 if (dots_page or {}).get("render_fallback") else dots_mocr.DPI
    scale = POINTS_PER_INCH / dpi
    return frame[0] * scale, frame[1] * scale


def as_box(value) -> list[float] | None:
    """Return a four-number box with area, or None.

    Public, because the ensemble (#365) reads the same boxes out of
    the documents this module writes. One copy of the box rules.
    """
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return None
    if not all(_number(v) for v in value):
        return None
    x0, y0, x1, y1 = (float(v) for v in value)
    if x1 <= x0 or y1 <= y0:
        return None
    return [x0, y0, x1, y1]


def _number(value) -> bool:
    """Return whether ``value`` is a real number."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _positive(value) -> bool:
    """Return whether ``value`` is a number above zero."""
    return _number(value) and value > 0


def intersection(a: list[float], b: list[float]) -> float:
    """Return the area two boxes share.

    Public, for the ensemble's own geometry (#365).
    """
    width = min(a[2], b[2]) - max(a[0], b[0])
    height = min(a[3], b[3]) - max(a[1], b[1])
    if width <= 0 or height <= 0:
        return 0.0
    return width * height


def covered_share(
    box: list[float], rects: list[dict]
) -> tuple[float, dict | None]:
    """Return the largest share of ``box`` one of ``rects`` covers.

    The maximum over the boxes, not the sum: the rule of the
    experiment's ``covered`` (#317). Two boxes that each touch a tenth
    do not make an exclusion between them.

    :param box: ``[x0, y0, x1, y1]`` in points.
    :param rects: Dicts with ``x0``, ``y0``, ``x1``, ``y1`` in points.
    :returns: ``(share, the rect)``, or ``(0.0, None)``.
    :rtype: tuple[float, dict | None]
    """
    area = (box[2] - box[0]) * (box[3] - box[1])
    if area <= 0:
        return 0.0, None
    best, hit = 0.0, None
    for rect in rects:
        other = as_box([rect["x0"], rect["y0"], rect["x1"], rect["y1"]])
        if other is None:
            continue
        share = intersection(box, other) / area
        if share > best:
            best, hit = share, rect
    return best, hit


def verdict(
    box_pt: list[float] | None,
    rects: list[dict],
    masks: list[dict],
    text: str = "",
    printed: str | None = None,
    height_pt: float | None = None,
) -> tuple[dict | None, float]:
    """Return one unit's ``(exclusion, share)``.

    A redaction that covers :data:`EXCLUDE_SHARE` or more of the unit
    names itself; else a neighbour's mask that does; else the printed
    page number, when the unit sits in the head or the foot band and
    one of its lines ends in the approved number of its page (#396);
    else nothing. The share is the larger of the two boxes' shares,
    so a partial verdict is read off the file as ``share <
    FULL_SHARE``; a page-number unit is taken whole and carries 1.0.

    A unit with no box, or on a page with no size, cannot be judged.
    It carries the third verdict, :data:`UNJUDGED`, so a reader tells
    "judged and clean" from "not judged" and :func:`kept_units` leaves
    it out: the rule is that no reader sees the text under a
    redaction, and a unit nobody measured may be under one.

    :param box_pt: The unit's box in points, or None for a unit with
        no box or on a page with no size.
    :param rects: The redaction boxes of the page, in points.
    :param masks: The outside masks of the page, in points.
    :param text: The unit's text, as the engine wrote it.
    :param printed: The approved page number of the page, or None for
        a page with none.
    :param height_pt: The page's height in points, the space of
        ``box_pt``; None leaves the band unread.
    :returns: The verdict.
    :rtype: tuple[dict | None, float]
    """
    if box_pt is None:
        return {"reason": UNJUDGED}, 0.0
    red_share, hit = covered_share(box_pt, rects)
    out_share, _ = covered_share(box_pt, masks)
    share = max(red_share, out_share)
    if (
        hit is not None
        and red_share >= EXCLUDE_SHARE
        and red_share >= out_share
    ):
        exclusion = {
            "reason": "redaction",
            "rect_type": hit.get("rect_type") or "",
            "redaction_id": hit.get("id"),
            "fill": hit.get("fill") or "",
        }
    elif out_share >= EXCLUDE_SHARE:
        exclusion = {"reason": "outside"}
    elif is_page_number(box_pt, text, printed, height_pt):
        exclusion = {"reason": PAGE_NUMBER, "printed": printed}
        share = 1.0
    else:
        exclusion = None
    return exclusion, round(share, 4)


def is_page_number(
    box_pt: list[float],
    text: str,
    printed: str | None,
    height_pt: float | None,
) -> bool:
    """Say whether a unit is the printed page number of its page (#396).

    Both conditions, and the one rule for them: the box sits in the
    head or the foot band (``page_numbers.band_of``), and a line of
    the text ends in the approved value
    (``page_numbers.carries_number``).

    :param box_pt: The unit's box in points.
    :param text: The unit's text.
    :param printed: The approved number of the page, or None.
    :param height_pt: The page's height in points, or None.
    :returns: Whether the unit is the page number.
    :rtype: bool
    """
    if not printed or not height_pt:
        return False
    if page_numbers.band_of(box_pt, height_pt) is None:
        return False
    return page_numbers.carries_number(text, printed)


def kept_units(page: dict) -> list[dict]:
    """Return the units of a glued page that carry no exclusion.

    **The one reader** of the verdict for a consumer that wants the
    clean text alone. The ensemble does not call it: it aligns every
    unit first and drops the aligned group afterwards (#317).

    :param page: One page of a document this module wrote.
    :returns: The units, in the engine's order.
    :rtype: list[dict]
    """
    return [
        unit
        for unit in page.get("units") or []
        if unit.get("exclusion") is None
    ]


# ---------------------------------------------------------------------------
# The document
# ---------------------------------------------------------------------------


def build_document(
    opinion: Opinion,
    inputs: ScanInputs,
    spec: EngineSpec,
    masks: dict[int, list[dict]],
) -> dict:
    """Cut one engine's document to the opinion, with the verdicts.

    :param opinion: The row.
    :param inputs: The scan's inputs.
    :param spec: The engine.
    :param masks: ``{page_index: [mask]}`` in points, the neighbours'
        text on the first and the last page.
    :returns: The document.
    :rtype: dict
    :raises OpinionGlueError: When the volume document lacks a page of
        the opinion.
    """
    document = inputs.documents[spec.name]
    by_index = {
        page.get("page_index"): page
        for page in document.get("pages") or []
        if isinstance(page, dict)
    }
    dots_by_index = {
        page.get("page_index"): page
        for page in (inputs.documents.get("dots_mocr") or {}).get("pages")
        or []
        if isinstance(page, dict)
    }
    pages: list[dict] = []
    failed: list[int] = []
    counts = {
        "units": 0,
        "excluded": 0,
        "partial": 0,
        "unjudged": 0,
        "page_number": 0,
    }
    for offset in range(opinion.page_count):
        page_index = opinion.start_page_index + offset
        page = by_index.get(page_index)
        if page is None:
            raise OpinionGlueError(
                f"the {spec.name} document of {inputs.run.label} has no "
                f"page {page_index + 1}"
            )
        size = page_size_pt(inputs, page_index, dots_by_index.get(page_index))
        frame = spec.frame(page, document)
        entry = {
            "page_in_opinion": offset,
            "page_index": page_index,
            "pdf_page": page_index + 1,
            "source": page.get("source"),
            "frame": None,
            "units": [],
        }
        if size and frame:
            entry["frame"] = {
                "width_pt": round(size[0], 2),
                "height_pt": round(size[1], 2),
                "render_width": frame[0],
                "render_height": frame[1],
            }
        if "error" in page:
            entry["error"] = page["error"]
            failed.append(page_index)
            pages.append(entry)
            continue
        rects = inputs.redactions.get(page_index, [])
        page_masks = masks.get(page_index, [])
        printed = inputs.printed.get(page_index)
        for index, unit in enumerate(page.get(spec.units_key) or []):
            if not isinstance(unit, dict):
                continue
            box = as_box(unit.get("bbox"))
            box_pt = None
            if box and size and frame:
                sx, sy = size[0] / frame[0], size[1] / frame[1]
                box_pt = [
                    round(box[0] * sx, 2),
                    round(box[1] * sy, 2),
                    round(box[2] * sx, 2),
                    round(box[3] * sy, 2),
                ]
            text = unit.get(spec.text_key)
            if not isinstance(text, str):
                text = ""
            exclusion, share = verdict(
                box_pt,
                rects,
                page_masks,
                text,
                printed,
                size[1] if size else None,
            )
            counts["units"] += 1
            if exclusion is not None and exclusion["reason"] == UNJUDGED:
                counts["unjudged"] += 1
            elif exclusion is not None:
                counts["excluded"] += 1
                if exclusion["reason"] == PAGE_NUMBER:
                    counts["page_number"] += 1
                if share < FULL_SHARE:
                    counts["partial"] += 1
            entry["units"].append(
                {
                    "id": index,
                    "type": unit.get(spec.type_key) or "",
                    "text": text,
                    "bbox": unit.get("bbox"),
                    "box_pt": box_pt,
                    "exclusion": exclusion,
                    "share": share,
                }
            )
        pages.append(entry)

    return {
        "schema_version": SCHEMA_VERSION,
        "engine": spec.name,
        "scan_pk": opinion.scan_id,
        "opinion": {
            "first_printed_page": opinion.first_printed_page,
            "index_in_page": opinion.index_in_page,
            "last_printed_page": opinion.last_printed_page,
            "page_count": opinion.page_count,
            "glue_revision": opinion.glue_revision,
        },
        "apply_run": inputs.run.label,
        "source_fingerprint": opinion.source_fingerprint or "",
        "source": {
            "key": spec.document_key(inputs.run),
            "run": document.get("run"),
        },
        "exclude_share": EXCLUDE_SHARE,
        "full_share": FULL_SHARE,
        "generated_at": timezone.now().isoformat(),
        "pages": pages,
        "failed_pages": failed,
        "counts": counts,
    }


def write(opinion: Opinion, inputs: ScanInputs) -> list[str]:
    """Write one opinion's documents and stamp the row.

    One file per engine the run has, then the manifest, then the stamp
    by compare-and-swap on the revision: a revision that moved during
    the write wins, and the glue is due again.

    :param opinion: The row.
    :param inputs: The scan's inputs.
    :returns: The engines written.
    :rtype: list[str]
    :raises OpinionGlueError: On a fact about the row.
    """
    run = inputs.run
    if opinion.apply_run_id != run.pk:
        raise OpinionGlueError(
            f"the opinion is in the space of another run than {run.label}; "
            "approve the redaction review again"
        )
    if (
        opinion.page_count
        != opinion.end_page_index - opinion.start_page_index + 1
    ):
        raise OpinionGlueError("the page count does not match the indexes")
    boundary = opinion.boundary
    if boundary is None:
        raise OpinionGlueError(
            "the boundary of this opinion is gone; approve the redaction "
            "review again"
        )
    masks: dict[int, list[dict]] = {}
    for rect in boundaries.outside_rects(opinion.scan, [boundary]).get(
        boundary.pk, []
    ):
        masks.setdefault(rect["page_index"], []).append(rect)

    written: dict[str, dict] = {}
    for spec in ENGINES.values():
        if spec.name not in inputs.documents:
            continue
        document = build_document(opinion, inputs, spec, masks)
        key = engine_key(opinion, spec.name)
        if not s3_sync.upload_json_object(key, document):
            raise OpinionGlueError(
                f"the document could not be uploaded to {key}"
            )
        written[spec.name] = {
            "key": key,
            "source_key": document["source"]["key"],
            "source_run": document["source"]["run"],
            "counts": document["counts"],
        }
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "scan_pk": opinion.scan_id,
        "opinion": {
            "first_printed_page": opinion.first_printed_page,
            "index_in_page": opinion.index_in_page,
        },
        "glue_revision": opinion.glue_revision,
        "apply_run": run.label,
        "engines": written,
        "generated_at": timezone.now().isoformat(),
    }
    manifest_key = engine_key(opinion, "manifest")
    if not s3_sync.upload_json_object(manifest_key, manifest):
        raise OpinionGlueError(
            f"the manifest could not be uploaded to {manifest_key}"
        )
    stamped = Opinion.objects.filter(
        pk=opinion.pk, glue_revision=opinion.glue_revision
    ).update(
        ocr_glue_revision=opinion.glue_revision,
        ocr_glue_attempts=0,
        # How many engines this revision holds (#365). The ensemble
        # gate is a query over it, so it is written with the stamp it
        # describes and never read back off the manifest.
        ocr_engine_count=len(written),
    )
    if stamped:
        # This module's own message alone: another work's failure on
        # the same row is not answered by this success.
        Opinion.objects.filter(
            pk=opinion.pk, error_message__startswith=MESSAGE_PREFIX
        ).update(error_message="")
    return list(written)


def record_failure(opinion: Opinion, message: str) -> None:
    """Spend one attempt on the row, and end it at the cap.

    :param opinion: The row.
    :param message: What failed, for ``error_message``.
    :return: None.
    """
    Opinion.objects.filter(pk=opinion.pk).update(
        ocr_glue_attempts=F("ocr_glue_attempts") + 1,
        error_message=f"{MESSAGE_PREFIX}{message}",
    )
    ended = (
        Opinion.objects.filter(
            pk=opinion.pk, ocr_glue_attempts__gte=MAX_ATTEMPTS
        )
        .exclude(status=OpinionReviewStatus.TEXT_REVIEW_DONE)
        .update(status=OpinionReviewStatus.ERROR)
    )
    if ended:
        logger.error(
            "%s of scan %s: the OCR glue failed %d times; the row is ERROR "
            "until the next approval. Last: %s",
            opinion,
            opinion.scan_id,
            MAX_ATTEMPTS,
            message,
        )
    else:
        logger.warning(
            "%s of scan %s: the OCR glue failed: %s",
            opinion,
            opinion.scan_id,
            message,
        )


# ---------------------------------------------------------------------------
# The pass
# ---------------------------------------------------------------------------


def glue_due(limit: int = OPINIONS_PER_TICK) -> int:
    """Write the OCR glue of up to ``limit`` rows of one scan.

    The tenth pass of the collect tick. The due scans are walked newest
    first (the rule of ``apply.queue_ready_scans``), and the first one
    whose inputs load is glued, its rows in reading order. A held scan
    is logged and passed over, so a volume that waits for its Mistral
    batch holds no other volume. One scan is glued per tick, so the
    engine documents are parsed once.

    :param limit: The rows to write.
    :returns: How many rows were written.
    :rtype: int
    """
    if not s3_sync.s3_active():
        return 0
    scan_ids = list(
        due().order_by("-scan_id").values_list("scan_id", flat=True).distinct()
    )
    for scan_id in scan_ids:
        scan = Scan.objects.get(pk=scan_id)
        try:
            inputs = load_inputs(scan)
        except ScanHeld as exc:
            logger.info(
                "scan %s: its opinions owe their OCR glue and wait: %s",
                scan.pk,
                exc,
            )
            continue
        return _glue_scan(scan, inputs, limit)
    return 0


def _glue_scan(scan: Scan, inputs: ScanInputs, limit: int) -> int:
    """Write up to ``limit`` due rows of one scan whose inputs loaded.

    :param scan: The scan.
    :param inputs: :func:`load_inputs`.
    :param limit: The rows to write.
    :returns: How many rows were written.
    :rtype: int
    """
    rows = list(
        due()
        .filter(scan=scan)
        .select_related("boundary", "scan")
        .order_by("first_printed_page", "index_in_page")[:limit]
    )
    started = time.monotonic()
    written = 0
    for opinion in rows:
        try:
            engines = write(opinion, inputs)
        except OpinionGlueError as exc:
            record_failure(opinion, str(exc))
            continue
        except Exception:
            logger.exception(
                "%s of scan %s: the OCR glue raised", opinion, scan.pk
            )
            record_failure(opinion, "the OCR glue raised; see the log")
            continue
        written += 1
        logger.info(
            "%s of scan %s: OCR glue written at r%d (%s)",
            opinion,
            scan.pk,
            opinion.glue_revision,
            ", ".join(engines),
        )
    logger.info(
        "scan %s: %d of %d due opinion(s) glued in %.1fs",
        scan.pk,
        written,
        len(rows),
        time.monotonic() - started,
    )
    return written


def reglue(scan: Scan) -> int:
    """Raise the revision of every row of ``scan`` that is not approved.

    The body of ``reglue_opinion_ocr``: the pass finds the rows due on
    the next tick. A ``TEXT_REVIEW_DONE`` row keeps its glues.

    :param scan: The scan.
    :returns: How many rows moved.
    :rtype: int
    """
    return (
        Opinion.objects.filter(scan=scan)
        .exclude(status=OpinionReviewStatus.TEXT_REVIEW_DONE)
        .update(glue_revision=F("glue_revision") + 1, ocr_glue_attempts=0)
    )
