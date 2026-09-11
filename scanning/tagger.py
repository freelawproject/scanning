"""The case-law block tagger stage: one RunPod job per volume.

After review 2 a volume's opinions are known -- a person walked every
caption to its key icon -- and the redaction geometry says what a
reader of the finished volume will see. This stage sends exactly that
text to the ``caselaw-block-tagger`` worker
(``scanning/runpod-caselaw-tagger/``) and stores what it says: for every
opinion, the character spans of the caption fields and the opinion
skeleton (party, docket number, court, judges, disposition, ...).

The pieces, in the shape of ``dots_mocr.py`` and ``yolo.py``:

- :func:`ensure_tag_jobs` builds the input (``tagger_input.convert``
  over the glued OCR document and the reviewed detections), writes it
  and its map to the bucket, and creates **one** row at
  ``TAG``/``CASELAW_TAGGER``/``RUNPOD`` through ``jobs.ensure_run_jobs``.
  The input object is addressed by the digest of its text, so a second
  call over unchanged inputs finds the same key and the same identity
  and reuses the run; a converter change, a new OCR run or a different
  set of reviewed boxes makes a new digest and a new run.
- The daemon's submit wave presigns the row's input for a GET and its
  result key for a PUT and posts :func:`build_payload`; the poll, the
  deadlines, the retries and the cancel are the shared machinery.
- :func:`finish_ready_runs`, on the collect tick, reads the result
  envelope and writes ``r{run}-volume.json`` beside the input: the
  spans per opinion, with the map's key, so the assembly step has
  everything it needs under one prefix. The rows go to ``CONSUMED``;
  the result object is kept.

The review-2 rows are the layout (#240). :func:`reviewed_detections`
reads the ``Detection`` rows a curator left live, :func:`redaction_rects`
the ``Redaction`` rows a reader paints (PR B) and
:func:`opinion_boundaries` the ``OpinionBoundary`` rows that stand
(PR C), and every read is in the one space :func:`measured_run` names
(the #269 rule), so a volume with a deleted page is tagged as the
corrected volume. Without a boundary the converter cuts at the
reviewed key icons; without a redaction nothing is excluded by
geometry.

Nothing enqueues this stage on its own yet. The ``enqueue_caselaw_tagger``
command is the one caller of :func:`ensure_tag_jobs`, a staff decision,
pinned by ``TestKnownEnqueuePaths``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from django.conf import settings
from django.utils import timezone

from scanning import dots_mocr, jobs, runpod_client, s3_sync, tagger_input
from scanning.models import (
    DEAD_JOB_STATUSES,
    IN_FLIGHT_JOB_STATUSES,
    ApplyRun,
    Detection,
    ExternalJob,
    JobEngine,
    JobProvider,
    JobStage,
    JobStatus,
    Redaction,
    Scan,
    Status,
)

logger = logging.getLogger(__name__)

#: The handler action, the one the worker answers.
ACTION = "tag"

STAGE = JobStage.TAG
ENGINE = JobEngine.CASELAW_TAGGER

#: The statuses a volume is tagged in: review 2 is closed, so the boxes
#: and the pairing are final. A tuple, like ``dots_mocr.APPLY_STATUSES``,
#: so the command and a future tick pass offer the same set.
TAG_STATUSES = (Status.REDACTION_REVIEW_DONE,)

#: How many times the glue is retried before it is left alone, the
#: loud-then-quiet shape of the dots.mocr glue.
GLUE_MAX_ATTEMPTS = 3


class TaggerInputError(Exception):
    """The input for a volume could not be built."""


class TaggerGlueError(Exception):
    """A completed run could not be written into its volume document."""


# ── Switches and keys ───────────────────────────────────────────────
def enabled() -> bool:
    """Return whether tagger jobs may be dispatched.

    Both switches, like the other engines: ``TAGGER_ENABLED`` is the
    operator's decision to spend money on this stage, and
    :func:`runpod_client.enabled` covers the account credentials and
    this engine's own endpoint id.

    :returns: Whether the stage should run.
    :rtype: bool
    """
    return bool(
        settings.TAGGER_ENABLED
        and runpod_client.enabled(settings.RUNPOD_TAGGER_ENDPOINT_ID)
    )


def stage_prefix(scan: Scan) -> str:
    """Return the prefix every object of this stage lives under.

    :param scan: The scan.
    :returns: ``{processing_prefix}jobs/tag/caselaw_tagger/``.
    :rtype: str
    """
    return (
        f"{s3_sync.s3_processing_prefix(scan)}{s3_sync.JOB_RESULTS_SUBDIR}"
        f"{STAGE}/{ENGINE}/"
    )


def input_key(scan: Scan, digest: str) -> str:
    """Return the key of the input document with this text digest.

    Addressed by content, not by run: the row's identity compares the
    key, and a key that carried the run number would read every call
    as new work.

    :param scan: The scan.
    :param digest: From :func:`tagger_input.text_digest`.
    :returns: The key.
    :rtype: str
    """
    return f"{stage_prefix(scan)}input-{digest[:16]}.json"


def map_key_for(input_key_: str) -> str:
    """Return the map's key for an input key: the same name with
    ``.map.json``, so a reader derives one from the other.

    :param input_key_: An input document's key.
    :returns: Its map's key.
    :rtype: str
    """
    return input_key_[: -len(".json")] + ".map.json"


def glued_result_key(scan: Scan, run: int) -> str:
    """Return the key of a run's volume document.

    :param scan: The scan.
    :param run: The run number.
    :returns: ``{stage_prefix}r{run}-volume.json``.
    :rtype: str
    """
    return f"{stage_prefix(scan)}r{run}-volume.json"


def glued_volume_key(scan: Scan) -> str | None:
    """Return the live run's volume document key, if the run is glued.

    :param scan: The scan.
    :returns: The key, or None when no run is complete.
    :rtype: str | None
    """
    rows = live_tag_jobs(scan)
    if not rows or any(row.status != JobStatus.CONSUMED for row in rows):
        return None
    return glued_result_key(scan, rows[0].run)


def build_payload(job: ExternalJob, input_url: str, output_url: str) -> dict:
    """Return the RunPod ``input`` payload for one volume.

    :param job: The claimed row. Its ``result_key`` is already set.
    :param input_url: Presigned GET of the input document.
    :param output_url: Presigned PUT for the result, signed with
        ``runpod_client.RESULT_CONTENT_TYPE``.
    :returns: The ``input`` dict to POST.
    :rtype: dict
    """
    return {
        "action": ACTION,
        "scan_pk": job.scan_id,
        "input_url": input_url,
        "result_url": output_url,
        "result_key": job.result_key,
    }


# ── The inputs ──────────────────────────────────────────────────────
def reviewed_detections(scan: Scan):
    """Return the boxes review 2 left standing.

    ``live()`` reads the derived ``active`` flag the curator's decisions
    write (#240): an approval sets confidence 1.0, a deactivation clears
    ``active``, a hand-drawn box is a row of its own until withdrawn. So
    one filter is the reviewed set.

    :param scan: The scan.
    :returns: The rows, in page and reading order.
    """
    return (
        Detection.objects.filter(scan=scan)
        .live()
        .order_by("page_index", "y0", "x0")
    )


def measured_run(scan: Scan) -> ApplyRun | None:
    """Return the apply run the review-2 rows are measured against.

    ``detections.measured_run``, the #269 rule: the standing run when
    the compute measured against it, else None for the original's
    space. Every read here -- the OCR document, the printed pages, the
    boxes, the redactions and the boundaries -- is in that one space,
    so a volume with a deleted page is tagged as the corrected volume
    and every ``page_index`` on the map names a page of it.

    :param scan: The scan.
    :returns: The run, or None.
    """
    from scanning import detections

    return detections.measured_run(scan)


def _in_space(row, run: ApplyRun | None) -> bool:
    """Whether a review-2 row is drawn in the space ``run`` names.

    A human row whose ``apply_run`` is not the measured run is the
    ``stale_*`` finding of review 2 (#240): its page index counts in
    another volume, so it is left out here rather than placed on the
    wrong page.
    """
    return row.apply_run_id == (run.pk if run is not None else None)


def redaction_rects(
    scan: Scan, run: ApplyRun | None = None
) -> dict[int, list[tuple[float, float, float, float]]]:
    """Return the boxes the final PDF paints over, per page, in the frame.

    The ``Redaction`` rows a reader paints (``visible()``: a computed
    row under no standing dismissal, a curator's addition not
    withdrawn; #240 PR B) -- the headnotes, the running heads, the
    tables before the first opinion, and the white margin strips,
    which cover text too. The rows are in PDF points and the cells in
    the 200 dpi frame, so each box goes through
    ``tagger_input.points_to_frame``. A cell whose centre lies in one
    is not sent (``tagger_input.apply_boxes``).

    :param scan: The scan.
    :param run: The measured run (:func:`measured_run`), for the space.
    :returns: ``{page_index: [(x0, y0, x1, y1), ...]}``; empty when
        nothing is painted.
    :rtype: dict[int, list[tuple[float, float, float, float]]]
    """
    out: dict[int, list[tuple[float, float, float, float]]] = {}
    skipped = 0
    for row in (
        Redaction.objects.visible()
        .filter(scan=scan)
        .order_by("page_index", "y0", "x0")
    ):
        if None in (row.x0, row.y0, row.x1, row.y1):
            continue
        if not _in_space(row, run):
            skipped += 1
            continue
        x0, y0 = tagger_input.points_to_frame(row.x0, row.y0)
        x1, y1 = tagger_input.points_to_frame(row.x1, row.y1)
        out.setdefault(row.page_index, []).append((x0, y0, x1, y1))
    if skipped:
        logger.warning(
            "scan %s: %d redaction row(s) are in another space than the "
            "measured run and were not applied to the tagger input",
            scan.pk,
            skipped,
        )
    return out


def printed_pages(
    scan: Scan, run: ApplyRun | None = None
) -> dict[int, str | None]:
    """Return the printed page number per page index, in the space read.

    With a measured run it is the run's ``printed_pages.json`` (#224:
    the curator's numbers over the model's, one entry per final page).
    Without one it is ``Scan.ocr_results``, the reading of #228 with
    the curator's corrections applied (#214): one entry per PDF page
    with the number read in ``detected`` (a range such as ``913-925``
    for a compressed page, None where nothing was read). The map
    carries these so the assembly step and a reviewer can name a page
    as the book does; the converter never reads the running head
    itself.

    :param scan: The scan.
    :param run: The measured run (:func:`measured_run`), for the space.
    :returns: ``{page_index: printed}``.
    :rtype: dict[int, str | None]
    """
    out: dict[int, str | None] = {}
    if run is not None:
        from scanning import apply

        for entry in apply.load_printed_pages(scan, run).get("pages", []):
            printed = entry.get("printed")
            out[int(entry["final_page"]) - 1] = (
                str(printed) if printed not in (None, "") else None
            )
        return out
    for entry in scan.ocr_results or []:
        if not isinstance(entry, dict) or not entry.get("pdf_page"):
            continue
        detected = entry.get("detected")
        out[int(entry["pdf_page"]) - 1] = (
            str(detected) if detected not in (None, "") else None
        )
    return out


def opinion_boundaries(
    scan: Scan, run: ApplyRun | None = None
) -> list[tuple[tagger_input.Anchor, tagger_input.Anchor]] | None:
    """Return the reviewed opinion boundaries as anchors in the frame.

    ``boundaries.standing`` (#240 PR C) is the one read of the
    boundaries every consumer shares: the computed rows plus the
    curator's additions, in reading order. A dismissed computed row is
    left out here -- the curator said that caption opens nothing --
    and so is a row drawn in another space (:func:`_in_space`). The
    start is the caption's top-left corner and the end the key icon's
    bottom-right, in PDF points on the row, in the 200 dpi frame here.

    :param scan: The scan.
    :param run: The measured run (:func:`measured_run`), for the space.
    :returns: ``[(start, end), ...]`` or None when no boundary stands,
        which sends the converter to the key icons.
    :rtype: list[tuple[Anchor, Anchor]] | None
    """
    from scanning import boundaries

    out = []
    skipped = 0
    for row in boundaries.standing(scan):
        if row.is_dismissed:
            continue
        if not _in_space(row, run):
            skipped += 1
            continue
        sx, sy = tagger_input.points_to_frame(row.start_x, row.start_y)
        ex, ey = tagger_input.points_to_frame(row.end_x, row.end_y)
        out.append(
            ((row.start_page_index, sx, sy), (row.end_page_index, ex, ey))
        )
    if skipped:
        logger.warning(
            "scan %s: %d opinion boundary row(s) are in another space than "
            "the measured run and were not used for the tagger input",
            scan.pk,
            skipped,
        )
    return out or None


@dataclass
class PreparedInput:
    """What :func:`prepare_input` hands to the row creator."""

    input_document: dict
    map_document: dict
    digest: str
    stats: dict
    ocr_key: str
    ocr_run: int | None
    apply_run: ApplyRun | None


def prepare_input(scan: Scan) -> PreparedInput:
    """Build a volume's input from the OCR document, the reviewed
    detections, the redactions and the opinion boundaries, without
    writing anything.

    Every read is in one space, the one :func:`measured_run` names:
    with a measured run the run's glued OCR volume (``ApplyRun.ocr_key``,
    the corrected volume's pages) and its printed pages; without one
    the volume's own glued document (``dots_mocr.glued_volume_key``,
    the original's pages) and ``Scan.ocr_results``. The rule of
    ``text_fit.load_cells`` and ``services.geometry_pdf_path``.

    :param scan: The scan.
    :returns: The input, its map, the digest that addresses them, and
        the converter's counts.
    :rtype: PreparedInput
    :raises TaggerInputError: If the volume has no glued OCR document,
        or the conversion finds no opinion to send.
    """
    run = measured_run(scan)
    if run is not None:
        from scanning import apply

        ocr_key = run.ocr_key
        try:
            document = apply.load_ocr_document(scan, run)
            printed = printed_pages(scan, run)
        except apply.ApplyError as exc:
            raise TaggerInputError(str(exc)) from exc
    else:
        ocr_key = dots_mocr.glued_volume_key(scan)
        if ocr_key is None:
            raise TaggerInputError(
                f"scan {scan.pk} has no glued dots.mocr volume; the OCR "
                "run must finish and glue before it can be tagged"
            )
        document = s3_sync.download_json_object(ocr_key)
        printed = printed_pages(scan)
    input_document, map_document, stats = tagger_input.convert(
        document,
        reviewed_detections(scan),
        scan_pk=scan.pk,
        reporter=scan.reporter.short_name if scan.reporter_id else None,
        reporter_volume=scan.volume,
        printed_pages=printed,
        redaction_rects=redaction_rects(scan, run),
        boundaries=opinion_boundaries(scan, run),
    )
    # The space the map's page indexes count in, for the assembly step.
    map_document["ocr_key"] = ocr_key
    map_document["apply_run"] = run.pk if run is not None else None
    if not input_document["sequences"]:
        raise TaggerInputError(
            f"scan {scan.pk}: the conversion found no opinion text to "
            f"tag ({stats})"
        )
    return PreparedInput(
        input_document=input_document,
        map_document=map_document,
        digest=tagger_input.text_digest(input_document),
        stats=stats,
        ocr_key=ocr_key,
        ocr_run=document.get("run"),
        apply_run=run,
    )


def ensure_tag_jobs(
    scan: Scan, *, force_new_run: bool = False
) -> list[ExternalJob]:
    """Return the live tagger row for ``scan``, creating it if the
    current run does not describe today's input.

    Builds the input, writes it and its map to the bucket unless an
    object with that digest is already there, and creates one row
    whose identity is the digest plus the sources it was built from.
    Idempotent through ``jobs.ensure_run_jobs``: the same inputs give
    the same key and identity, so a second call reuses the run; a run
    holding a dead row is replaced, and a prior result for the same
    identity is carried (``reuse_results``), since the result objects
    are kept.

    :param scan: The scan to tag.
    :param force_new_run: Start a new run even when the live one still
        describes today's input.
    :returns: The live run's rows (one).
    :rtype: list[ExternalJob]
    :raises TaggerInputError: If the input cannot be built or written.
    """
    if not s3_sync.s3_active():
        raise TaggerInputError(
            "the tagger needs S3: the worker reads its input from the "
            "bucket through a presigned URL"
        )
    prepared = prepare_input(scan)
    key = input_key(scan, prepared.digest)
    if not s3_sync.object_exists(key):
        if not s3_sync.upload_json_object(key, prepared.input_document):
            raise TaggerInputError(f"scan {scan.pk}: could not write {key}")
        if not s3_sync.upload_json_object(
            map_key_for(key), prepared.map_document
        ):
            raise TaggerInputError(
                f"scan {scan.pk}: could not write {map_key_for(key)}"
            )
        logger.info(
            "scan %s: wrote tagger input %s (%d opinions, %d chars; %s)",
            scan.pk,
            key,
            prepared.stats["opinions"],
            prepared.stats["chars"],
            {
                k: prepared.stats[k]
                for k in (
                    "footnote_cells",
                    "redacted_cells",
                    "key_icons",
                    "images",
                    "blocks_outside_boundaries",
                )
            },
        )
    sequence_count = len(prepared.input_document["sequences"])
    # ``page_count`` is what the running-job deadline multiplies by
    # (``jobs.runpod_execution_deadline``). Pages the sent text covers,
    # not cases: the model's time scales with the text, and a case may
    # be one page or two hundred.
    pages_sent = {
        block["page_index"]
        for entry in prepared.map_document["sequences"]
        for block in entry["blocks"]
    }
    identity = {
        "digest": prepared.digest,
        "sequence_count": sequence_count,
        "page_count": len(pages_sent),
        "chars": prepared.stats["chars"],
        "ocr_key": prepared.ocr_key,
        "ocr_run": prepared.ocr_run,
        "apply_run": prepared.apply_run.pk if prepared.apply_run else None,
        "converter": tagger_input.CONVERTER_VERSION,
    }
    return jobs.ensure_run_jobs(
        scan,
        [(key, identity)],
        stage=STAGE,
        engine=ENGINE,
        provider=JobProvider.RUNPOD,
        fingerprint=scan.source_fingerprint or "",
        reuse_results=True,
        force_new_run=force_new_run,
    )


def live_tag_jobs(scan: Scan) -> list[ExternalJob]:
    """Return the scan's live tagger run.

    :param scan: The scan, or its pk.
    :returns: The rows (one), or an empty list.
    :rtype: list[ExternalJob]
    """
    return jobs.live_run(scan, STAGE, ENGINE)


def run_summary(scan: Scan) -> dict | None:
    """Describe the live run for a page or a command.

    :param scan: The scan.
    :returns: See :func:`jobs.run_summary`.
    :rtype: dict | None
    """
    return jobs.run_summary(scan, STAGE, ENGINE)


# ── The glue ────────────────────────────────────────────────────────
def glue_run(scan: Scan, rows: list[ExternalJob]) -> str:
    """Write a completed run's result into its volume document.

    :param scan: The scan.
    :param rows: The live run's rows, all ``COMPLETED``.
    :returns: The key written.
    :rtype: str
    :raises TaggerGlueError: If the result object is missing, is not
        this attempt's envelope, or the write fails.
    """
    row = rows[0]
    if not row.result_key:
        raise TaggerGlueError(
            f"scan {scan.pk} run {row.run} has no result key"
        )
    envelope = s3_sync.download_json_object(row.result_key)
    payload = jobs.check_result_envelope(
        scan, row, envelope, ACTION, TaggerGlueError
    )
    sequences = payload.get("sequences")
    if not isinstance(sequences, list):
        raise TaggerGlueError(
            f"scan {scan.pk} run {row.run}: the result carries no sequences"
        )
    identity = row.input_manifest or {}
    volume = {
        "schema_version": 1,
        "engine": str(ENGINE),
        "action": ACTION,
        "scan_pk": scan.pk,
        "run": row.run,
        "input_key": row.input_key,
        "map_key": map_key_for(row.input_key),
        "ocr_run": identity.get("ocr_run"),
        "ocr_key": identity.get("ocr_key"),
        "converter": identity.get("converter"),
        "model": payload.get("model"),
        "max_tokens": payload.get("max_tokens"),
        "sequence_count": payload.get("sequence_count", len(sequences)),
        "failed_sequences": payload.get("failed_sequences", []),
        "sequences": sequences,
        "generated_at": timezone.now().isoformat(),
    }
    key = glued_result_key(scan, row.run)
    if not s3_sync.upload_json_object(key, volume):
        raise TaggerGlueError(
            f"scan {scan.pk} run {row.run}: could not write {key}"
        )
    logger.info(
        "scan %s tagger run %d glued: %d opinions, %d spans -> %s",
        scan.pk,
        row.run,
        len(sequences),
        sum(len(entry.get("spans") or []) for entry in sequences),
        key,
    )
    return key


def _glue_state(rows: list[ExternalJob]) -> dict:
    meta = rows[0].provider_meta or {}
    return dict(meta.get("glue") or {})


def _record_glue_failure(
    scan: Scan, rows: list[ExternalJob], exc: Exception
) -> None:
    """Count a glue failure on the run, loud at the crossing into "out
    of tries" and quiet after, the dots.mocr glue's shape."""
    state = _glue_state(rows)
    state["attempts"] = int(state.get("attempts") or 0) + 1
    state["last_error"] = str(exc)[:500]
    state["last_attempt_at"] = timezone.now().isoformat()
    head = rows[0]
    meta = dict(head.provider_meta or {})
    meta["glue"] = state
    ExternalJob.objects.filter(pk=head.pk).update(provider_meta=meta)
    if state["attempts"] >= GLUE_MAX_ATTEMPTS:
        logger.error(
            "scan %s tagger run %d: glue failed %d times, giving up: %s",
            scan.pk,
            head.run,
            state["attempts"],
            exc,
        )
    else:
        logger.warning(
            "scan %s tagger run %d: glue failed (attempt %d): %s",
            scan.pk,
            head.run,
            state["attempts"],
            exc,
        )


def finish_ready_runs() -> int:
    """Glue every finished tagger run into its volume document.

    A run is finished when its row is ``COMPLETED``; a dead row means
    the run will never finish and is left to ``run_summary`` and the
    command. Writes no scan status: the volume document is the output,
    and what reads it is the assembly step, not the review flow.

    :returns: How many runs were glued and consumed.
    :rtype: int
    """
    if not s3_sync.s3_active():
        return 0
    scan_ids = (
        Scan.objects.filter(
            jobs__stage=STAGE,
            jobs__engine=ENGINE,
            jobs__provider=JobProvider.RUNPOD,
            jobs__status=JobStatus.COMPLETED,
            jobs__apply_run__isnull=True,
        )
        .values_list("pk", flat=True)
        .distinct()
    )
    unfinished = {JobStatus.PENDING} | IN_FLIGHT_JOB_STATUSES
    glued = 0
    for scan in Scan.objects.filter(pk__in=list(scan_ids)).select_related(
        "reporter"
    ):
        rows = live_tag_jobs(scan)
        if not rows:
            continue
        if any(row.status in unfinished for row in rows):
            continue
        if any(row.status in DEAD_JOB_STATUSES for row in rows):
            continue
        if not any(row.status == JobStatus.COMPLETED for row in rows):
            continue
        if int(_glue_state(rows).get("attempts") or 0) >= GLUE_MAX_ATTEMPTS:
            continue
        try:
            glue_run(scan, rows)
        except Exception as exc:
            _record_glue_failure(scan, rows, exc)
            continue
        ExternalJob.objects.filter(
            pk__in=[row.pk for row in rows], status=JobStatus.COMPLETED
        ).update(status=JobStatus.CONSUMED, consumed_at=timezone.now())
        glued += 1
    return glued
