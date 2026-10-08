"""The pictures of an opinion's text, cut from the original scan (#463).

An opinion can print a picture: a photograph, a map, a diagram. The
engines draw a box for it, and the ensemble writes it as a ``figure``
group with no text (``ensemble.figures_of``). The final XML embeds the
picture, so this pass cuts each one out of the original as uploaded,
at the place and the size it has on the page, and stores it as a JPEG.

**The daemon cuts, never a web pod.** The cut reads the original
shards, and a web pod never pulls one. So this is a task of
``run_daemon`` beside the PDF pass, one opinion per tick, and it reuses
the PDF pass's own source of pictures (``opinion_pdf.image_source``):
the page map gives the source page, the committed shard manifest the
shard, and the box is scaled from the volume page to the shard page.

**The ledger is a digest, not a revision.** The ensemble stamps
``Opinion.figure_digest`` over the run and the place of every picture
(``ensemble.figure_digest``), and this pass stamps
``figures_cut_digest`` when every picture of that digest is stored.
:func:`is_written` is the one rule: no picture, or the two digests
alike. A re-glue or an edit that leaves the pictures where they were
keeps the digest, so the cut is not paid again; that is why the
objects live under the opinion's prefix and not under a revision.

**No redacted pixel leaves the cut.** A picture a redaction covers by
``opinion_ocr.EXCLUDE_SHARE`` or more is dropped by the ensemble, but a
smaller box (a name inside a map, a margin redaction over a corner)
leaves the picture in the text. The cut is taken from the unredacted
original, so every redaction the PDF paints, and every mask over the
neighbour opinions, is painted on it before it is stored
(:func:`paint`).

**A fault is the PDF pass's.** Every fault stamps
``figures_attempted_at``, and the row waits ``opinion_pdf.retry_after``
before it is due again, so a stuck row never holds the pass and a
bucket blip does not spend the cap in seconds. A fault of the bucket
costs no attempt; a fault the rows explain, or an unexpected one,
spends one of :data:`MAX_ATTEMPTS`, and at the cap a row that is not
approved is ``ERROR``.
"""

from __future__ import annotations

import logging

import fitz
from django.db.models import F, QuerySet
from django.utils import timezone

from scanning import boundaries, ensemble, opinion_pdf, redactions, s3_sync
from scanning.models import Opinion, OpinionReviewStatus

logger = logging.getLogger(__name__)

#: How many opinions one tick cuts. A cut pulls a shard of the original
#: the first time a volume needs one, inside the serial loop, the
#: hazard of the PDF pass.
FIGURES_PER_TICK = 1

#: Failed cuts of one row at one digest before the row is ERROR.
MAX_ATTEMPTS = 3

#: The content type of a cut, the format ``image_source`` answers.
CONTENT_TYPE = "image/jpeg"


class FigureError(Exception):
    """A fact about the row stopped the cut: it spends an attempt."""


class TransientFault(Exception):
    """The bucket stopped the cut: it spends nothing."""


def _box_name(box: list[float]) -> str:
    """Return the part of a key that names one box, to a tenth of a point."""
    return "_".join(f"{value:.1f}" for value in box)


def key(opinion: Opinion, run_label: str, figure: dict) -> str:
    """Return the S3 key of one picture of an opinion.

    **The one rule for the key**: the opinion's prefix (its invariant
    identity), the apply run whose page space the box is in, the page
    and the box. A run is in the key because another run can put
    another page at the same index.

    :param opinion: The opinion.
    :param run_label: The label of the apply run, the document's
        ``apply_run``.
    :param figure: One entry of ``ensemble.figures_of``.
    :returns: The key.
    :rtype: str
    """
    prefix = s3_sync.s3_processing_prefix(opinion.scan)
    return (
        f"{prefix}{opinion.object_prefix}figures/{run_label or 'original'}/"
        f"p{figure['page_index']}_{_box_name(figure['box_pt'])}.jpg"
    )


def is_written(opinion: Opinion) -> bool:
    """Return whether every picture of the opinion's text is cut.

    **The one rule** the readiness of the text review reads
    (``opinions.text_review_ready``), off the row and never off the
    bucket.

    :param opinion: The row.
    :returns: Whether the text holds no picture, or every one is cut.
    :rtype: bool
    """
    return not opinion.figure_digest or (
        opinion.figures_cut_digest == opinion.figure_digest
    )


def owed() -> QuerySet:
    """Return the rows whose pictures are not all cut, newest scan first.

    The text of the live revision is written, so the digest describes
    it; the row is neither approved (its text holds its pictures
    already) nor ``ERROR``; the faults at this digest are under the
    cap; and the scan is in ``opinion_pdf.OPINION_PDF_STATUSES``, the
    one table of the statuses the opinion passes read.

    :returns: The queryset, ordered.
    :rtype: QuerySet
    """
    return (
        Opinion.objects.filter(
            scan__status__in=opinion_pdf.OPINION_PDF_STATUSES,
            figure_attempts__lt=MAX_ATTEMPTS,
            ocr_glue_revision=F("glue_revision"),
            ensemble_revision=F("glue_revision"),
        )
        .exclude(figure_digest="")
        .exclude(figures_cut_digest=F("figure_digest"))
        .exclude(
            status__in=(
                OpinionReviewStatus.TEXT_REVIEW_DONE,
                OpinionReviewStatus.ERROR,
            )
        )
        .select_related("scan", "scan__reporter", "apply_run", "boundary")
        .order_by("-scan_id", "first_printed_page", "index_in_page")
    )


def due() -> QuerySet:
    """Return the rows a tick may cut now, newest scan first.

    :func:`owed` less the rows under their cooldown: a row whose last
    fault is younger than ``opinion_pdf.retry_after`` waits, the rule
    of the PDF pass.

    :returns: The queryset, ordered.
    :rtype: QuerySet
    """
    return owed().exclude(
        figures_attempted_at__gt=timezone.now() - opinion_pdf.retry_after()
    )


def covers(opinion: Opinion) -> dict[int, list[dict]]:
    """Return what must never show on a picture, by volume page.

    The visible redactions of the scan, each with its ``fill``, and the
    masks over the neighbour opinions (``boundaries.outside_rects``),
    white, the two sets the OCR glue judges a unit by. Points of the
    volume page, the space of the ensemble's boxes.

    :param opinion: The row, with ``boundary``.
    :returns: ``{page_index: [{"x0", "y0", "x1", "y1", "fill"}]}``.
    :rtype: dict[int, list[dict]]
    :raises FigureError: When the opinion's boundary is gone.
    """
    if opinion.boundary is None:
        raise FigureError("the boundary of this opinion is gone")
    found: dict[int, list[dict]] = {}
    for entry in redactions.visible_by_page(opinion.scan):
        found.setdefault(entry["page_index"], []).extend(entry["rects"])
    for rect in boundaries.outside_rects(opinion.scan, [opinion.boundary]).get(
        opinion.boundary.pk, []
    ):
        found.setdefault(rect["page_index"], []).append(
            {**rect, "fill": "white"}
        )
    return found


def paint(data: bytes, box: list[float], rects: list[dict]) -> bytes:
    """Paint the rects of a page over one cut picture.

    The cut is the clip of ``box`` (points of the volume page), so a
    rect in the same points lands at its offset in the clip, scaled to
    the pixels. A rect that misses the box paints nothing.

    :param data: The JPEG of the cut.
    :param box: The picture's box, in points.
    :param rects: The rects of its page, in points, each with ``fill``.
    :returns: The JPEG, painted; the bytes given when no rect touches it.
    :rtype: bytes
    """
    touching = [
        rect
        for rect in rects
        if rect["x0"] < box[2]
        and rect["x1"] > box[0]
        and rect["y0"] < box[3]
        and rect["y1"] > box[1]
    ]
    if not touching:
        return data
    pix = fitz.Pixmap(data)
    sx = pix.width / max(box[2] - box[0], 1e-6)
    sy = pix.height / max(box[3] - box[1], 1e-6)
    for rect in touching:
        # Outwards to the pixel on every side: a redaction never leaves
        # a sliver of what it covers.
        area = (
            fitz.IRect(
                int((max(rect["x0"], box[0]) - box[0]) * sx),
                int((max(rect["y0"], box[1]) - box[1]) * sy),
                int((min(rect["x1"], box[2]) - box[0]) * sx + 0.999),
                int((min(rect["y1"], box[3]) - box[1]) * sy + 0.999),
            )
            & pix.irect
        )
        if area.is_empty:
            continue
        shade = 0 if rect.get("fill") == "black" else 255
        pix.set_rect(area, (shade,) * pix.n)
    return pix.tobytes("jpeg", jpg_quality=opinion_pdf.IMAGE_JPEG_QUALITY)


def cut_one(opinion: Opinion) -> int:
    """Cut every picture of one opinion's text, store it and stamp the row.

    The document is the stamped one, the text a reviewer sees. Its
    digest must be the row's: an ensemble that wrote again during the
    tick wins, and the row is due again at the new digest. Every
    picture is stored before the stamp, so a stamped row has them all.

    :param opinion: The row, with ``scan`` and ``apply_run``.
    :returns: How many pictures were cut.
    :rtype: int
    :raises FigureError: When the run is gone or a picture has no
        source.
    :raises TransientFault: When a read or an upload failed.
    """
    run = opinion.apply_run
    if run is None:
        raise FigureError("the opinion has no apply run")
    try:
        document = ensemble.read_document(opinion)
    except ensemble.TransientFault as exc:
        raise TransientFault(str(exc)) from exc
    except ensemble.EnsembleError as exc:
        raise FigureError(str(exc)) from exc
    digest = ensemble.figure_digest(document)
    if digest != opinion.figure_digest:
        # The ensemble wrote another text since the row was read.
        logger.info(
            "%s of scan %s: the pictures moved during the cut; it is due "
            "again",
            opinion,
            opinion.scan_id,
        )
        return 0
    figures = ensemble.figures_of(document)
    for figure in figures:
        # A page the map gives no source is a fact of the run, not of
        # the bucket: the cut would fail the same way every time.
        if opinion_pdf._source_of(run, figure["page_index"]) is None:
            raise FigureError(
                f"the page map names no source for page "
                f"{figure['page_index'] + 1}"
            )
    hidden = covers(opinion)
    pages = {
        page.get("page_in_opinion"): page.get("frame") or {}
        for page in document.get("pages") or []
    }

    def page_rect(page_in_opinion: int) -> fitz.Rect:
        frame = pages.get(page_in_opinion) or {}
        return fitz.Rect(
            0, 0, frame.get("width_pt") or 0, frame.get("height_pt") or 0
        )

    wanted = {str(figure["page_in_opinion"]): [] for figure in figures}
    with opinion_pdf.image_source(
        opinion, run, page_rect, wanted
    ) as image_for:
        for figure in figures:
            data = image_for(
                figure["page_in_opinion"], fitz.Rect(*figure["box_pt"])
            )
            if not data:
                # A shard that did not pull or open, or a clip that did
                # not render: ``image_source`` logged which. It counts,
                # spaced by the cooldown, so a blip of the bucket must
                # last three cooldowns to end the row.
                raise FigureError(
                    f"the picture on page {figure['page_index'] + 1} did "
                    "not render from the original"
                )
            data = paint(
                data, figure["box_pt"], hidden.get(figure["page_index"], [])
            )
            target = key(opinion, document.get("apply_run") or "", figure)
            if not s3_sync.upload_bytes_object(target, data, CONTENT_TYPE):
                raise TransientFault(f"the upload to {target} failed")
    stamped = Opinion.objects.filter(
        pk=opinion.pk, figure_digest=digest
    ).update(
        figures_cut_digest=digest,
        figure_attempts=0,
        figures_attempted_at=None,
    )
    if stamped:
        logger.info(
            "%s of scan %s: %d picture(s) cut",
            opinion,
            opinion.scan_id,
            len(figures),
        )
    return len(figures)


def _fail(opinion: Opinion, message: str, counted: bool = True) -> None:
    """Record one failed cut on the row, and end it at the cap.

    Every fault stamps ``figures_attempted_at``, so the row leaves
    :func:`due` for ``opinion_pdf.retry_after``. A counted fault also
    spends one of :data:`MAX_ATTEMPTS`; a fault of the bucket does not.
    Scoped to the digest the tick read, so a new text spends nothing of
    the new set. At the cap a row that is not approved goes to
    ``ERROR`` with the message; below it the row's ``error_message``,
    which another pass may own, is left alone.

    :param opinion: The row, as read at the start of the tick.
    :param message: What failed.
    :param counted: Whether the fault spends an attempt.
    :return: None.
    """
    values = {"figures_attempted_at": timezone.now()}
    if counted:
        values["figure_attempts"] = F("figure_attempts") + 1
    Opinion.objects.filter(
        pk=opinion.pk, figure_digest=opinion.figure_digest
    ).update(**values)
    if not counted:
        logger.warning(
            "%s of scan %s: the pictures wait for the bucket; due again in "
            "%s: %s",
            opinion,
            opinion.scan_id,
            opinion_pdf.retry_after(),
            message,
        )
        return
    attempts = (
        Opinion.objects.filter(pk=opinion.pk)
        .values_list("figure_attempts", flat=True)
        .first()
    )
    if attempts is not None and attempts >= MAX_ATTEMPTS:
        Opinion.objects.filter(pk=opinion.pk).exclude(
            status__in=(
                OpinionReviewStatus.TEXT_REVIEW_DONE,
                OpinionReviewStatus.ERROR,
            )
        ).update(
            status=OpinionReviewStatus.ERROR,
            error_message=f"Pictures: {message}"[:2000],
        )
        logger.error(
            "%s of scan %s: the pictures failed %d times: %s",
            opinion,
            opinion.scan_id,
            attempts,
            message,
        )
    else:
        logger.warning(
            "%s of scan %s: the pictures failed (attempt %s of %d): %s",
            opinion,
            opinion.scan_id,
            attempts,
            MAX_ATTEMPTS,
            message,
        )


def run_tick() -> int:
    """Cut the pictures of up to :data:`FIGURES_PER_TICK` opinions.

    The body of the ``cut_opinion_figures`` command. A fault never
    raises out of the tick, and every fault puts the row under its
    cooldown, so the next tick takes another row. After each row, the
    scan's local mirror is released when neither this pass nor the PDF
    pass owes it anything: the cut pulls the shards into the same tree
    the PDF pass keeps.

    :returns: How many opinions were cut.
    :rtype: int
    """
    done = 0
    for opinion in due()[:FIGURES_PER_TICK]:
        try:
            cut_one(opinion)
            done += 1
        except TransientFault as exc:
            _fail(opinion, str(exc), counted=False)
        except FigureError as exc:
            _fail(opinion, str(exc))
        except Exception as exc:
            logger.exception(
                "%s of scan %s: the cut of the pictures raised",
                opinion,
                opinion.scan_id,
            )
            _fail(opinion, f"{type(exc).__name__}: {exc}")
        if not (
            owed().filter(scan=opinion.scan).exists()
            or opinion_pdf.owed().filter(scan=opinion.scan).exists()
        ):
            s3_sync.release_local_processing(opinion.scan)
    return done
