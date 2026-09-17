"""The redacted PDF of each opinion (#336, part 3).

The ``Opinion`` rows exist before any PDF does (part 1), and each row
carries its own status. So the PDF is one pass over the rows and not a
``Scan.queued_action``: the daemon runs :func:`run_tick` on its own
schedule (``build_opinion_pdfs``, last in ``run_daemon``), the tick
finds one row that owes its PDF, writes it, and stamps the row. One PDF
per tick (:data:`PDFS_PER_TICK`), the rule of the Mistral wave
(``mistral_ocr.MAX_SUBMITS_PER_TICK``), because the tick blocks the
serial scheduler for the whole write, and every other task
(``process_next_scan``, the two job waves) waits for it. After the
pulls that is a second or two of re-encoding. The first tick of a
volume pulls the corrected bitonal copy, and the first tick that needs
a picture pulls a shard of about 200 MB, and those pulls are inside the
same loop: minutes, once per volume, the same hazard the Mistral wave
carries for its render.

**The trigger is a fact on the row.** A row owes its PDF when
``redacted_pdf_revision`` is not ``glue_revision`` (:func:`is_written`,
:func:`due`). Part 1 writes no chain, no stamp and no queue for this
pass; it raises ``glue_revision`` when it re-derives a row no human
approved, and that alone makes the PDF due again. The file at the old
revision stays in the bucket, the rule of the apply's ``a{n}`` outputs:
disposable, swept by the admin deletion, and a link a reviewer holds
keeps working until then. Nothing here writes a status on the scan, and
:data:`OPINION_PDF_STATUSES` is the one place that says which scan
statuses the pass reads.

**Finish the volume on disk first.** :func:`next_due` prefers a scan
whose local mirror exists, then the newest scan. The inputs of a volume
are gigabytes (the bitonal copy plus every shard that holds a picture),
and a newer approval that preempted the volume in progress would hold
that tree until the pass came back to it.

**blackletter cuts the pages.** ``blackletter.api.generate`` paints the
rects, applies them and repaints the fill, once per opinion, over a
**small source** that holds the opinion's pages alone
(:func:`_small_source`). The volume never enters the call, so the
payload is built in the small source's space (:func:`payload`), the
dict carries ``filename`` and no printed page so the stored name is
internal (#165), and ``full_redacted=False`` skips the copy of the
source blackletter would otherwise write (blackletter#81). The
pictures come through ``image_for`` (:func:`image_source`): a bitonal
conversion destroys a photograph, so each ``IMAGE`` detection is
rendered from the shard that holds the page and handed to blackletter
as JPEG bytes.

**The inputs stay, the outputs go.** The bitonal copy and the shards
are pulled to the scan's local mirror and never deleted inside a tick:
three hundred ticks over one volume pull the copy once. The tick that
leaves no row of the scan owed releases the tree, and
:func:`release_mirrors` releases every such tree when the daemon
starts. The small source and the opinion file live in a scratch
directory under the mirror, removed in the ``finally`` of the tick.

**A fault is the fault of one opinion, and only one kind is free.** An
:class:`OpinionPdfError` is a fact the rows explain (a null boundary, a
page count that disagrees, the run moved, blackletter refused the
payload), and it will fail again. An unexpected exception is not
transient either: a fault in the cut, in the payload or in PyMuPDF is
deterministic. Both count on ``pdf_attempts``, and at
:data:`MAX_ATTEMPTS` the row is ``ERROR`` with ``error_message``, loud
then quiet (the rule of ``ApplyRun.attempts``); the next approval
brings it back through part 1. A :class:`TransientFault` alone is the
network (a pull that fails, a PUT that answers false), and it costs no
attempt, the rule of a defer in the jobs layer.

Every fault stamps ``pdf_attempted_at``, and the row is not due again
before :func:`retry_after`, so a failed row never holds the head of
the queue and three counted faults span three cooldowns and not
fifteen seconds. A picture that cannot be read is no fault at all: the
callable answers ``None`` and the page keeps its bitonal pixels there.

**The known limit.** A transient fault that never passes -- a
permission fault on one key -- retries every cooldown with no end and
never reaches ``ERROR``. Such a row is still *owed*, so it also holds
its volume's whole local tree until ``cleanup_processing_tmp`` sweeps
it at 24 hours, after which the pass pulls it again: the right trade
against re-pulling the volume every cooldown, but a real cost. The
jobs layer bounds its defers with a deadline (``jobs.check_deadline``);
a cap here is a later change, once the first weeks say whether it is
needed.
"""

from __future__ import annotations

import logging
import shutil
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path

import fitz
from django.conf import settings
from django.db.models import F, QuerySet
from django.utils import timezone

from scanning import apply, boundaries, review_states, s3_sync, sharding
from scanning.models import (
    Detection,
    Opinion,
    OpinionReviewStatus,
    PageEdit,
    Redaction,
    Scan,
    Status,
)

logger = logging.getLogger(__name__)

#: How many PDFs one tick writes. One, because the tick blocks the
#: serial scheduler for the whole write, and every other task waits.
PDFS_PER_TICK = 1

#: Counted faults a row may spend at one revision before it is ``ERROR``.
MAX_ATTEMPTS = 3

#: The scan statuses whose rows the pass writes. One value today: the
#: approval of review 2 wrote the rows. Review 3 (#334) moves a scan
#: past it, and an unwritten PDF must stay due there too, so this set is
#: the one place to extend, the rule of ``REDACTION_COMPUTE_STATUSES``.
OPINION_PDF_STATUSES = (Status.REDACTION_REVIEW_DONE,)

#: The name of the file under ``Opinion.glue_prefix``. Internal on
#: purpose: the printed range is the download name alone (#165).
REDACTED_NAME = "redacted.pdf"

#: The render of a picture handed to blackletter: 150 dpi, the density
#: the legacy stamp used, as JPEG. PNG is lossless over the grain of a
#: scanned photograph and stores the grain, at about four times the
#: bytes (blackletter#81).
IMAGE_DPI = 150
IMAGE_JPEG_QUALITY = 85

#: The label of a detection that marks a picture.
IMAGE_LABEL = "IMAGE"


def retry_after() -> timedelta:
    """Return how long a failed row stays out of :func:`due`.

    A counted fault will fail again, so retrying it every tick would
    spend the cap in seconds; a transient fault is the network, and the
    network needs minutes. Read from the settings at every call, not
    bound at import, so an operator can shorten it during an incident
    without a deploy, and ``override_settings`` reaches it in a test.

    :returns: The cooldown.
    :rtype: timedelta
    """
    return timedelta(seconds=settings.OPINION_PDF_RETRY_AFTER_SECONDS)


class OpinionPdfError(Exception):
    """One opinion's PDF cannot be written from what the rows say.

    A fact of the rows, so it will fail again: it counts on the row. The
    message is what the row's ``error_message`` holds at the cap.
    """


class TransientFault(Exception):
    """One opinion's PDF was not written because of a fault that passes.

    A pull or a PUT that failed, and nothing else: an unexpected
    exception is unknown rather than transient, and counts. This one
    counts no attempt; the row waits :func:`retry_after` and is due
    again.
    """


# ---------------------------------------------------------------------------
# The ledger
# ---------------------------------------------------------------------------


def key(opinion: Opinion) -> str:
    """Return the S3 key of the opinion's redacted PDF at the live revision.

    The one rule for the key: the scan's processing prefix, the
    opinion's glue prefix (``jobs/opinions/{first}.{index}/r{n}/``, the
    invariant identity since #350), the internal
    name. Under ``jobs/`` so the generic sync never carries it and the
    admin deletion sweeps it.

    :param opinion: The opinion.
    :returns: The key.
    :rtype: str
    """
    prefix = s3_sync.s3_processing_prefix(opinion.scan)
    return f"{prefix}{opinion.glue_prefix}{REDACTED_NAME}"


def download_name(opinion: Opinion) -> str:
    """Return the readable name the browser saves the PDF under.

    ``{reporter}.{volume}.{first:04d}-{last:04d}.pdf``, the name the
    legacy step stored the file under, with ``.{index}`` before ``.pdf``
    for a second opinion of the same printed page. A download name and
    nowhere else (#165): the stored key is internal, so a boundary that
    moves leaves no stale file beside the new one.

    :param opinion: The opinion.
    :returns: The file name.
    :rtype: str
    """
    scan = opinion.scan
    parts = []
    short = (
        getattr(scan.reporter, "short_name", "") if scan.reporter_id else ""
    )
    if short:
        parts.append(short)
    if scan.volume:
        parts.append(str(scan.volume))
    parts.append(
        f"{opinion.first_printed_page:04d}-{opinion.last_printed_page:04d}"
    )
    if opinion.index_in_page:
        parts.append(str(opinion.index_in_page))
    return ".".join(parts) + ".pdf"


def is_written(opinion: Opinion) -> bool:
    """Return whether the PDF exists at the opinion's live revision.

    The one rule for "the PDF exists": the stamp equals the revision. A
    null stamp never equals, and a stamp of an older revision is a PDF
    of another set of glues.

    :param opinion: The opinion.
    :returns: Whether the PDF is written.
    :rtype: bool
    """
    return (
        opinion.redacted_pdf_revision is not None
        and opinion.redacted_pdf_revision == opinion.glue_revision
    )


def owed() -> QuerySet:
    """Return the rows that still owe a PDF, newest scan first.

    The ledger alone: the stamp is not the revision, the row is not
    ``ERROR``, its counted faults at this revision are under the cap,
    and its scan is in :data:`OPINION_PDF_STATUSES`. The scan's status
    is joined so a volume an admin sent back writes no PDF while it is
    back. Newest scan first for the reason ``apply.queue_ready_scans``
    gives: the volume a volunteer approved today goes before the
    backlog. Inside a scan, reading order.

    **Not the same question as** :func:`due`, which subtracts the
    cooldown. "Does this volume still owe a PDF" must not answer no
    for the minutes a transient fault holds its last row, or
    :func:`_release_if_done` would free the tree and the pass would
    pull the whole volume again to write that one opinion.

    The reporter is joined because :func:`key` reads it through the
    processing prefix; the boundary because the masks read its anchors.

    :returns: The queryset, ordered.
    :rtype: QuerySet
    """
    return (
        Opinion.objects.filter(
            scan__status__in=OPINION_PDF_STATUSES,
            pdf_attempts__lt=MAX_ATTEMPTS,
        )
        .exclude(status=OpinionReviewStatus.ERROR)
        .exclude(redacted_pdf_revision=F("glue_revision"))
        .select_related("scan", "scan__reporter", "boundary")
        .order_by("-scan_id", "first_printed_page", "index_in_page")
    )


def due() -> QuerySet:
    """Return the rows a tick may write now, newest scan first.

    :func:`owed` less the rows under their cooldown: a row whose last
    fault is younger than :func:`retry_after` waits. :func:`next_due`
    puts the volume on disk ahead of the order.

    :returns: The queryset, ordered.
    :rtype: QuerySet
    """
    return owed().exclude(pdf_attempted_at__gt=timezone.now() - retry_after())


def _has_mirror(scan: Scan) -> bool:
    """Return whether the scan's local tree exists on this daemon.

    :param scan: The scan.
    :returns: Whether ``Scan.output_dir`` is a directory.
    :rtype: bool
    """
    return Path(scan.output_dir).is_dir()


def next_due() -> Opinion | None:
    """Return the row the next tick writes, or None.

    The first due row of the first due scan whose mirror is on disk,
    in the order of :func:`due`; the first due row of all when no due
    scan has one. Finishing the volume on disk bounds the disk to one
    tree in progress, where the newest-first order alone would hold
    the tree of every volume a newer approval preempted. The cost is
    one query for the scan ids and one ``is_dir`` per candidate scan.

    The ids are deduplicated here and not with ``distinct()``: Django
    puts every ``order_by`` column into a ``SELECT DISTINCT``, so the
    database would distinct the triple rather than the scan, and the
    walk below would run one query per due *row* -- about 1500 of them
    for a backlog of five volumes, inside the serial loop.
    ``dict.fromkeys`` keeps the order of the rows, which is already
    newest scan first.

    :returns: The row, or None.
    :rtype: Opinion | None
    """
    rows = due()
    first = rows.first()
    if first is None:
        return None
    if _has_mirror(first.scan):
        return first
    for scan_id in dict.fromkeys(rows.values_list("scan_id", flat=True)):
        if scan_id == first.scan_id:
            continue
        candidate = rows.filter(scan_id=scan_id).first()
        if candidate is not None and _has_mirror(candidate.scan):
            return candidate
    return first


def _stamp(opinion: Opinion) -> bool:
    """Record that the PDF at the opinion's revision is written.

    A compare-and-swap on the revision: a bump that landed during the
    write wins, the stamp stays behind, and the PDF is due again.

    :param opinion: The opinion, as read at the start of the tick.
    :returns: Whether the write won.
    :rtype: bool
    """
    return bool(
        Opinion.objects.filter(
            pk=opinion.pk, glue_revision=opinion.glue_revision
        ).update(
            redacted_pdf_revision=opinion.glue_revision,
            pdf_attempts=0,
            pdf_attempted_at=None,
        )
    )


def _fail(opinion: Opinion, message: str, counted: bool) -> None:
    """Record one failed tick on the row, and close it at the cap.

    Every fault stamps ``pdf_attempted_at`` and stores the message, so
    the row leaves :func:`due` for :func:`retry_after` and the admin
    can read what happened. A counted fault (an
    :class:`OpinionPdfError`, or an unexpected exception) also spends
    one of :data:`MAX_ATTEMPTS`; a :class:`TransientFault` does not.
    The writes are scoped to the revision, so a bump that landed during
    the tick spends nothing of the new set. At the cap a row still in
    ``PROCESSING`` goes to ``ERROR`` (terminal; part 1 sets it back at
    the next approval). A row a human already reviewed keeps its status:
    it leaves :func:`due` through the count alone.

    :param opinion: The opinion, as read at the start of the tick.
    :param message: What failed.
    :param counted: Whether the fault is one the rows explain.
    :return: None.
    """
    values = {
        "pdf_attempted_at": timezone.now(),
        "error_message": message[:2000],
    }
    if counted:
        values["pdf_attempts"] = F("pdf_attempts") + 1
    Opinion.objects.filter(
        pk=opinion.pk, glue_revision=opinion.glue_revision
    ).update(**values)
    if not counted:
        logger.warning(
            "opinion %s (scan %s): the redacted PDF hit a transient fault; "
            "it is due again in %s: %s",
            opinion.pk,
            opinion.scan_id,
            retry_after(),
            message,
        )
        return
    row = (
        Opinion.objects.filter(pk=opinion.pk)
        .values("pdf_attempts", "glue_revision")
        .first()
    )
    if row is None or row["glue_revision"] != opinion.glue_revision:
        return
    if row["pdf_attempts"] >= MAX_ATTEMPTS:
        closed = Opinion.objects.filter(
            pk=opinion.pk, status=OpinionReviewStatus.PROCESSING
        ).update(status=OpinionReviewStatus.ERROR)
        logger.error(
            "opinion %s (scan %s): the redacted PDF failed %d times at "
            "revision %d; %s: %s",
            opinion.pk,
            opinion.scan_id,
            row["pdf_attempts"],
            opinion.glue_revision,
            "the row is ERROR" if closed else "the row keeps its status",
            message,
        )
    else:
        logger.warning(
            "opinion %s (scan %s): the redacted PDF failed (attempt %d of "
            "%d): %s",
            opinion.pk,
            opinion.scan_id,
            row["pdf_attempts"],
            MAX_ATTEMPTS,
            message,
        )


# ---------------------------------------------------------------------------
# The payload, in the small source's space
# ---------------------------------------------------------------------------


def _scratch_dir(opinion: Opinion) -> Path:
    """Return the scratch directory of one tick, under the scan's mirror.

    Under ``jobs/`` so the generic sync never pushes it, and named by
    the opinion so two ticks never share it.

    :param opinion: The opinion.
    :returns: The directory, not created.
    :rtype: Path
    """
    return (
        Path(opinion.scan.output_dir)
        / "jobs"
        / "opinions"
        / f"{opinion.first_printed_page}.{opinion.index_in_page}"
    )


def _small_source(
    volume: fitz.Document, opinion: Opinion, path: Path
) -> fitz.Document:
    """Cut the opinion's pages out of the volume into a document of their own.

    The source ``generate`` is given: indexes 0 to ``page_count - 1``.
    Saved under the opinion's name, because blackletter prefixes every
    log line with the source's name, and one opinion per tick
    interleaves the lines of many scans in one pod log.

    :param volume: The run's bitonal copy, open.
    :param opinion: The opinion.
    :param path: Where to save the small source.
    :returns: The small source, open. The caller closes it.
    :rtype: fitz.Document
    :raises OpinionPdfError: If the volume has fewer pages than the
        opinion names, or the cut has another count than the row.
    """
    if opinion.start_page_index > opinion.end_page_index:
        raise OpinionPdfError(
            f"The opinion starts on page {opinion.start_page_index + 1} and "
            f"ends on page {opinion.end_page_index + 1}, which is before it."
        )
    if opinion.end_page_index >= volume.page_count:
        raise OpinionPdfError(
            f"The corrected volume has {volume.page_count} pages and the "
            f"opinion ends on page {opinion.end_page_index + 1}."
        )
    small = fitz.open()
    small.insert_pdf(
        volume,
        from_page=opinion.start_page_index,
        to_page=opinion.end_page_index,
    )
    if small.page_count != opinion.page_count:
        count = small.page_count
        small.close()
        raise OpinionPdfError(
            f"The cut holds {count} pages and the opinion says "
            f"{opinion.page_count}."
        )
    small.save(str(path), garbage=3, deflate=True)
    return small


def _rect(x0: float, y0: float, x1: float, y1: float) -> dict:
    """One rect in the shape of the payload, rounded like the viewer's."""
    return {
        "x0": round(x0, 1),
        "y0": round(y0, 1),
        "x1": round(x1, 1),
        "y1": round(y1, 1),
    }


def _redaction_pages(opinion: Opinion) -> dict[str, list[dict]]:
    """Return the ``pages`` of the payload: the boxes to paint, remapped.

    The visible redaction rows of the opinion's pages
    (``Redaction.objects.visible()``: a computed row under no standing
    dismissal, and a human add not withdrawn), in PDF points, keyed by
    the index in the small source. The row's ``fill`` decides the
    colour and its ``rect_type`` is the payload's ``type``; a margin
    row is white like every white row.

    :param opinion: The opinion.
    :returns: ``{"<small index>": [rect, ...]}``.
    :rtype: dict[str, list[dict]]
    """
    pages: dict[str, list[dict]] = {}
    rows = (
        Redaction.objects.visible()
        .filter(
            scan=opinion.scan,
            page_index__gte=opinion.start_page_index,
            page_index__lte=opinion.end_page_index,
        )
        .order_by("page_index", "y0", "x0")
    )
    for row in rows:
        small_index = str(row.page_index - opinion.start_page_index)
        pages.setdefault(small_index, []).append(
            {
                **_rect(row.x0, row.y0, row.x1, row.y1),
                "fill": row.fill,
                "type": row.rect_type,
            }
        )
    return pages


def _image_rects(
    opinion: Opinion, small: fitz.Document
) -> dict[str, list[dict]]:
    """Return the ``images`` of the payload: where a picture belongs.

    One rect per live ``IMAGE`` detection on the opinion's pages,
    converted from the render's pixels with ``boundaries.to_points``
    against the small source's own page size, so the scale is exact.
    Keyed by the index in the small source.

    :param opinion: The opinion.
    :param small: The small source, open, for the page sizes.
    :returns: ``{"<small index>": [rect, ...]}``, empty when no page
        holds a picture.
    :rtype: dict[str, list[dict]]
    """
    images: dict[str, list[dict]] = {}
    rows = Detection.objects.live().filter(
        scan=opinion.scan,
        label=IMAGE_LABEL,
        page_index__gte=opinion.start_page_index,
        page_index__lte=opinion.end_page_index,
    )
    for row in rows:
        index = row.page_index - opinion.start_page_index
        page_rect = small[index].rect
        # The fields as they are: a zero render size makes ``to_points``
        # fall back to the render density, and a ``1`` would make the
        # scale the page width.
        x0, y0 = boundaries.to_points(
            row.x0,
            row.y0,
            row.img_width,
            row.img_height,
            page_rect.width,
            page_rect.height,
        )
        x1, y1 = boundaries.to_points(
            row.x1,
            row.y1,
            row.img_width,
            row.img_height,
            page_rect.width,
            page_rect.height,
        )
        if x1 <= x0 or y1 <= y0:
            continue
        images.setdefault(str(index), []).append(_rect(x0, y0, x1, y1))
    return images


def _masks(opinion: Opinion, volume: fitz.Document) -> list[dict]:
    """Return the masks over the neighbour opinions, remapped.

    ``boundaries.outside_rects`` with the volume, so each mask grows
    over the ink that continues past its side edges, which the viewer's
    unwidened masks do not. The page indexes come back in the volume's
    space and go out in the small source's.

    :param opinion: The opinion.
    :param volume: The run's bitonal copy, open.
    :returns: The ``outside_rects`` of the one dict.
    :rtype: list[dict]
    :raises OpinionPdfError: If the opinion has no boundary to read the
        anchors from.
    """
    boundary = opinion.boundary
    if boundary is None:
        raise OpinionPdfError(
            "The boundary of this opinion is gone. Approve the redaction "
            "review again."
        )
    rects = boundaries.outside_rects(
        opinion.scan, [boundary], document=volume
    ).get(boundary.pk, [])
    masks = []
    for rect in rects:
        masks.append(
            {
                **rect,
                "page_index": rect["page_index"] - opinion.start_page_index,
            }
        )
    return masks


def payload(
    opinion: Opinion, volume: fitz.Document, small: fitz.Document
) -> dict:
    """Build the payload ``generate`` reads, for one opinion.

    Every index is in the small source's space (the final index minus
    ``start_page_index``). One dict in ``opinions``: the whole small
    source, the masks, and ``filename`` with no printed page, so the
    file lands under the internal name. ``images`` is a top-level key
    beside ``pages`` (blackletter#81), present only when a page holds
    a picture.

    :param opinion: The opinion.
    :param volume: The run's bitonal copy, open, for the masks.
    :param small: The small source, open, for the page sizes.
    :returns: The payload.
    :rtype: dict
    :raises OpinionPdfError: From :func:`_masks`.
    """
    data = {
        "opinions": [
            {
                "caption_page": 0,
                "end_page": opinion.page_count - 1,
                "outside_rects": _masks(opinion, volume),
                "filename": REDACTED_NAME,
            }
        ],
        "pages": _redaction_pages(opinion),
    }
    images = _image_rects(opinion, small)
    if images:
        data["images"] = images
    return data


# ---------------------------------------------------------------------------
# The pictures
# ---------------------------------------------------------------------------


def _source_of(run, final_index: int) -> dict | None:
    """Return the page map's source of one final page.

    :param run: The apply run.
    :param final_index: The 0-based page in the run's space.
    :returns: The ``source`` entry, or None when the map has no such
        page.
    :rtype: dict | None
    """
    pages = (run.page_map or {}).get("pages") or []
    if 0 <= final_index < len(pages):
        return pages[final_index].get("source")
    if not pages:
        # A run with no stored map is the original, page for page.
        return {"kind": "original", "pdf_page": final_index + 1}
    return None


def _shard_of(
    scan: Scan, manifest: dict, pdf_page: int
) -> tuple[str, int] | None:
    """Return the shard key and the page inside it for one original page.

    :param scan: The scan.
    :param manifest: The committed shard manifest.
    :param pdf_page: The 1-based page of the original.
    :returns: ``(key, page in shard)``, or None when no shard holds it.
    :rtype: tuple[str, int] | None
    """
    index = pdf_page - 1
    for entry in manifest.get("shards", []):
        if entry["from_page"] <= index <= entry["to_page"]:
            return (
                f"{s3_sync.shards_prefix(scan)}{entry['name']}",
                index - entry["from_page"],
            )
    return None


@contextmanager
def image_source(
    opinion: Opinion, run, small: fitz.Document, images: dict
) -> Iterator[Callable[[int, fitz.Rect], bytes | None] | None]:
    """Yield the ``image_for`` callable of one ``generate`` call.

    Before it yields, it maps every small index in ``images`` to the
    source page that holds the picture in full quality: the page map
    names an original page or a page of an edit's one-page shard, the
    committed shard manifest names the original's shard, and each
    shard is pulled once (``apply.local_copy``) and opened once. The
    callable then renders the clip and answers JPEG bytes. It is
    called per rect and opens nothing.

    The rect is in the small source's space, cut from the bitonal
    copy, whose page box may differ from the shard page's. So the
    callable scales the rect by the ratio of the two page boxes before
    it clips: a picture from the wrong part of the page is the one
    thing the output must never show.

    Every fault answers ``None``, logged: a shard that does not pull or
    open, a page past the end, a clip that fails. A raise inside the
    callable would fail the opinion for a lost picture, and the picture
    is the lesser deliverable. With no ``images`` the callable is None,
    so blackletter reads no map.

    :param opinion: The opinion.
    :param run: The apply run whose space the opinion is in.
    :param small: The small source, open.
    :param images: The ``images`` of the payload.
    :yields: The callable, or None.
    """
    if not images:
        yield None
        return
    scan = opinion.scan
    manifest = None
    needs_manifest = False
    plan: dict[int, tuple[str, int, PageEdit | None]] = {}
    for small_key in images:
        small_index = int(small_key)
        source = _source_of(run, opinion.start_page_index + small_index)
        if source is None:
            continue
        if source["kind"] == "original":
            needs_manifest = True
            plan[small_index] = ("original", source["pdf_page"], None)
        else:
            edit = PageEdit.objects.filter(pk=source["edit_id"]).first()
            if edit is not None:
                plan[small_index] = ("edit", source["page"], edit)
    if needs_manifest:
        manifest, reason = sharding.committed_manifest(scan)
        if manifest is None:
            logger.warning(
                "opinion %s (scan %s): no shard manifest, the pictures stay "
                "bitonal: %s",
                opinion.pk,
                scan.pk,
                reason,
            )

    docs: dict[str, fitz.Document | None] = {}
    cache: dict[tuple, bytes | None] = {}

    def _open(shard_key: str) -> fitz.Document | None:
        if shard_key in docs:
            return docs[shard_key]
        try:
            path = apply.local_copy(scan, shard_key)
            docs[shard_key] = fitz.open(str(path))
        except Exception:
            logger.warning(
                "opinion %s (scan %s): could not open the shard %s",
                opinion.pk,
                scan.pk,
                shard_key,
                exc_info=True,
            )
            docs[shard_key] = None
        return docs[shard_key]

    def _locate(small_index: int) -> tuple[str, int] | None:
        entry = plan.get(small_index)
        if entry is None:
            return None
        kind, page, edit = entry
        if kind == "original":
            if manifest is None:
                return None
            return _shard_of(scan, manifest, page)
        if edit is None:
            return None
        return apply.page_shard_key(scan, edit), page

    def image_for(page_index: int, rect: fitz.Rect) -> bytes | None:
        cache_key = (
            page_index,
            round(rect.x0, 1),
            round(rect.y0, 1),
            round(rect.x1, 1),
            round(rect.y1, 1),
        )
        if cache_key in cache:
            return cache[cache_key]
        data = None
        located = _locate(page_index)
        if located is not None:
            shard_key, page_in_shard = located
            doc = _open(shard_key)
            if doc is not None and 0 <= page_in_shard < doc.page_count:
                try:
                    source_page = doc[page_in_shard]
                    small_rect = small[page_index].rect
                    sx = source_page.rect.width / (small_rect.width or 1.0)
                    sy = source_page.rect.height / (small_rect.height or 1.0)
                    clip = fitz.Rect(
                        rect.x0 * sx, rect.y0 * sy, rect.x1 * sx, rect.y1 * sy
                    )
                    pix = source_page.get_pixmap(
                        clip=clip,
                        dpi=IMAGE_DPI,
                        colorspace=fitz.csRGB,
                        alpha=False,
                    )
                    if pix.width and pix.height:
                        data = pix.tobytes(
                            "jpeg", jpg_quality=IMAGE_JPEG_QUALITY
                        )
                except Exception:
                    logger.warning(
                        "opinion %s (scan %s): could not render the picture "
                        "on page %d at %s",
                        opinion.pk,
                        scan.pk,
                        page_index,
                        (rect.x0, rect.y0, rect.x1, rect.y1),
                        exc_info=True,
                    )
            else:
                logger.warning(
                    "opinion %s (scan %s): no source page for the picture on "
                    "page %d; it stays bitonal",
                    opinion.pk,
                    scan.pk,
                    page_index,
                )
        else:
            logger.warning(
                "opinion %s (scan %s): the page map names no source for page "
                "%d; the picture stays bitonal",
                opinion.pk,
                scan.pk,
                page_index,
            )
        cache[cache_key] = data
        return data

    try:
        yield image_for
    finally:
        for doc in docs.values():
            if doc is not None:
                doc.close()


# ---------------------------------------------------------------------------
# The write
# ---------------------------------------------------------------------------


def write_one(opinion: Opinion) -> dict:
    """Write the redacted PDF of one opinion, and stamp the row.

    The whole of one tick's work on one row, in order: the run and the
    bitonal copy, the small source, the payload, ``generate``, the
    upload, the stamp. The scratch directory goes in the ``finally``;
    the bitonal copy and the shards stay in the mirror for the next
    tick.

    :param opinion: The opinion, from :func:`due`.
    :returns: A summary: ``pages``, ``images``, ``seconds``.
    :rtype: dict
    :raises OpinionPdfError: For a fault the rows explain.
    :raises TransientFault: For a pull or a PUT that failed.
    :raises Exception: For any other fault of the write.
    """
    from blackletter.api import generate

    scan = opinion.scan
    started = time.monotonic()
    run = review_states.final_run(scan)
    if run is None:
        raise OpinionPdfError("The corrected volume is not built.")
    if opinion.apply_run_id != run.pk:
        raise OpinionPdfError(
            "The corrected volume changed under this opinion. Approve the "
            "redaction review again."
        )
    try:
        bitonal = apply.local_copy(scan, run.bitonal_key)
    except apply.ApplyError as exc:
        raise TransientFault(str(exc)) from exc
    scratch = _scratch_dir(opinion)
    shutil.rmtree(scratch, ignore_errors=True)
    scratch.mkdir(parents=True, exist_ok=True)
    try:
        with fitz.open(str(bitonal)) as volume:
            source_path = scratch / (
                f"{opinion.first_printed_page}."
                f"{opinion.index_in_page}.r{opinion.glue_revision}.pdf"
            )
            small = _small_source(volume, opinion, source_path)
            try:
                data = payload(opinion, volume, small)
                images = data.get("images") or {}
                with image_source(opinion, run, small, images) as image_for:
                    result = generate(
                        pdf_path=source_path,
                        redactions=data,
                        output_dir=scratch,
                        full_redacted=False,
                        unredacted=False,
                        llm=False,
                        image_for=image_for,
                    )
            finally:
                small.close()
        if result.get("failed"):
            raise OpinionPdfError(
                "blackletter could not write the file: "
                f"{result['failed'][0].get('error', 'unknown fault')}"
            )
        written = result["files"][0]
        if written is None:
            raise OpinionPdfError("blackletter wrote no file.")
        with fitz.open(str(written)) as out:
            if out.page_count != opinion.page_count:
                raise OpinionPdfError(
                    f"The file holds {out.page_count} pages and the opinion "
                    f"says {opinion.page_count}."
                )
        target = key(opinion)
        if not s3_sync.upload_file_object(
            target, Path(written), "application/pdf"
        ):
            raise TransientFault(f"The upload to {target} failed.")
        if not _stamp(opinion):
            logger.info(
                "opinion %s (scan %s): the revision moved during the write; "
                "the PDF is due again",
                opinion.pk,
                scan.pk,
            )
        summary = {
            "pages": opinion.page_count,
            "images": sum(len(v) for v in images.values()),
            "seconds": round(time.monotonic() - started, 1),
        }
        logger.info(
            "opinion %s (scan %s): redacted PDF written at %s: %d pages, %d "
            "pictures, %.1fs",
            opinion.pk,
            scan.pk,
            target,
            summary["pages"],
            summary["images"],
            summary["seconds"],
        )
        return summary
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def _release_if_done(scan: Scan) -> bool:
    """Release the scan's local mirror when no row of it owes a PDF.

    :func:`owed` and not :func:`due`: a row under its cooldown still
    owes its PDF. Reading the cooldown here would free the tree of a
    volume whose last opinion hit one failed PUT, and the pass would
    pull the whole volume again minutes later to write that one
    opinion -- every cooldown, for a fault that does not pass.

    :param scan: The scan.
    :returns: Whether a tree was removed.
    :rtype: bool
    """
    if owed().filter(scan=scan).exists():
        return False
    return s3_sync.release_local_processing(scan)


def run_tick() -> int:
    """Write up to :data:`PDFS_PER_TICK` PDFs, one row each, and count them.

    The body of the ``build_opinion_pdfs`` command. A fault of the
    write is the fault of that row (:func:`_fail`) and never raises out
    of the tick: a :class:`TransientFault` costs no attempt, an
    :class:`OpinionPdfError` and any other exception count. After each
    row, the scan's mirror is released when nothing of it is owed.

    :returns: How many PDFs were written.
    :rtype: int
    """
    written = 0
    for _ in range(PDFS_PER_TICK):
        opinion = next_due()
        if opinion is None:
            break
        try:
            write_one(opinion)
            written += 1
        except OpinionPdfError as exc:
            _fail(opinion, str(exc), counted=True)
        except TransientFault as exc:
            _fail(opinion, str(exc), counted=False)
        except Exception as exc:
            # Unknown, not transient: a fault in the cut, in the
            # payload or in PyMuPDF is deterministic, so it must reach
            # ERROR and stop rather than log a traceback every
            # cooldown with no end.
            logger.exception(
                "opinion %s (scan %s): the redacted PDF raised",
                opinion.pk,
                opinion.scan_id,
            )
            _fail(opinion, f"{type(exc).__name__}: {exc}", counted=True)
        _release_if_done(opinion.scan)
    return written


def release_mirrors() -> int:
    """Release the local mirror of every scan the pass may read.

    Run once when the daemon starts. No worker runs at that moment, the
    apply and the compute release their own trees at their end, and
    every reader pulls what is absent, so a mirror a crash left behind
    goes here at the price of one pull per volume that was in
    progress. Skipped under ``DEVELOPMENT`` and without S3, where the
    local files are the developer's or the only copy
    (``release_local_processing`` refuses both).

    :returns: How many trees were removed.
    :rtype: int
    """
    if settings.DEVELOPMENT or not s3_sync.s3_active():
        return 0
    root = Path(settings.PROCESSING_TMP_DIR)
    if not root.is_dir():
        return 0
    pks = []
    for child in root.iterdir():
        if child.is_dir() and child.name.isdigit():
            pks.append(int(child.name))
    if not pks:
        return 0
    removed = 0
    for scan in Scan.objects.filter(
        pk__in=pks, status__in=OPINION_PDF_STATUSES
    ):
        if s3_sync.release_local_processing(scan):
            removed += 1
    if removed:
        logger.info("opinion PDFs: released %d mirror(s) at start", removed)
    return removed
