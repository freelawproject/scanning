"""The Mistral OCR stage (issue #191): rows, render, submit, poll, harvest.

One :class:`~scanning.models.ExternalJob` row per **original** shard at
``EXTRACT``/``MISTRAL_OCR``/``MISTRAL``, and one Mistral batch job per
row. The daemon does the intermediate step Mistral needs: it renders
every page of the shard the way every engine of the ai-research
ensemble saw it (``pipeline/core/render.py`` on the ``extraction_align``
branch: 1700x2200 RGB), uploads each page as an ``ocr`` file, uploads
a JSONL manifest naming them, and creates the batch. The confirm tick
polls the batch, and on ``SUCCESS`` downloads the output and stores it,
**whole**, at the row's ``result_key``.

This module is the Mistral entry of :func:`jobs._providers`. It uses
the lifecycle primitives of :mod:`scanning.jobs` -- the claim, the
compare-and-swap writes, the retry ledger -- rather than copying them,
which is what the provider table exists for. It reads the original
scan and nothing else: no redaction, no detection, no review state.
Where the stage is called from is a separate question, answered by
the button today and by a trigger later. What is specific to Mistral,
and must not be broken:

- **The harvest stores every byte Mistral returned, and nothing else
  transforms it.** The result document holds the output lines and the
  error lines as they came, plus the batch job object. ``markdown``,
  ``blocks``, ``images``, ``tables``, ``usage_info`` and whatever a
  later model adds all land in S3. The glue (a follow-up) is where
  ``parse()`` and every other transform run, so a better transform is a
  re-glue at no API cost, never a re-paid read.
- **The render is the branch's, line for line.** RGB, ``zoom = 1700 /
  page width``, a resize to exactly 1700x2200. Every engine of the
  ensemble saw that image, and every bbox of every engine lives in
  that pixel space.
- **The deadline is Mistral's own timeout**, stamped once at the first
  claim and never restamped on ``RUNNING``: a batch is queued and run
  inside one budget Mistral enforces.
- **The wave blocks the serial scheduler, so it takes one shard per
  tick** (``MAX_SUBMITS_PER_TICK``). A full shard is minutes of render
  and sequential upload; the in-flight cap (``MAX_CONCURRENCY``) is a
  separate number, so a volume keeps draining while its batches wait.
- **A job nothing will read is cancelled, and its files deleted.**
  Every page image, the manifest and the two output files live at
  Mistral until we delete them, so every path that writes a row off
  deletes what it uploaded.
- **A result with a hole is never carried.** The stable-hole rule of
  #238 trusts a deterministic worker; a Mistral batch line can fail
  from a transient fault, so the carry re-reads the shard instead.
- **The source is the original scan, never the bitonal copy.** The
  ensemble's tests all ran on non-bitonal images, and the shards are
  cut from the original. ``SOURCE`` is a constant with no per-row
  override, because the bitonal copy is one merged file with no shard
  set to read.
"""

from __future__ import annotations

import io
import json
import logging
import tempfile
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

import fitz
from botocore.exceptions import BotoCoreError, ClientError
from django.conf import settings
from django.utils import timezone
from PIL import Image

from scanning import jobs, mistral_client, s3_sync
from scanning.models import (
    ExternalJob,
    JobEngine,
    JobProvider,
    JobStage,
)

logger = logging.getLogger(__name__)

#: The action the result envelope names, so ``jobs.check_result_envelope``
#: can refuse an object of another stage at this key.
ACTION = "extract"

#: The canonical render of the ai-research ensemble
#: (``pipeline/core/config.py``): every engine saw this size, and every
#: bbox of every engine lives in this pixel space. US letter at 200 dpi,
#: the resolution the rest of the corpus is measured at.
RENDER_W = 1700
RENDER_H = 2200

#: What the pages are rendered from. See the module docstring.
SOURCE = "original"

#: What every manifest line asks for, as the branch did.
INCLUDE_BLOCKS = True
TABLE_FORMAT = "html"

#: Batch jobs in flight at once, not pages. A debug guard on blast
#: radius, not a cost control: Mistral bills per page whatever the
#: parallelism, and the batch API is built for a deep queue. A batch
#: waits at Mistral for hours, so this is the throughput limit of a
#: volume: with one submit per tick and N in flight, a volume of more
#: than N shards needs a second round of that latency. Sixteen covers
#: a normal volume (1100 pages at 100 a shard is 11-14 shards) in one
#: round. It must stay well above ``MAX_SUBMITS_PER_TICK``.
MAX_CONCURRENCY = 16

#: Shards rendered, uploaded and submitted on one submit tick. **One,
#: because the wave blocks the daemon's serial scheduler** (#156) for
#: as long as the slowest shard takes: a 100-page shard is 100 renders
#: (about 160 ms each for a synthetic page, more for a 200 dpi scan)
#: and 100 uploads of 2-4 MB in sequence -- minutes, not seconds --
#: and every poll, the glue and both applies wait behind it. One shard
#: per tick caps that wait at one shard. Raising it, or parallel
#: uploads inside a shard, or a render off the tick (as #196 did for
#: its geometry), waits for a measurement on a real volume.
MAX_SUBMITS_PER_TICK = 1

#: Submissions a row gets before it is failed. Two, because a lost
#: create may have made a job nothing we hold names, and every attempt
#: uploads the shard's pages again.
MAX_ATTEMPTS = 2

#: Mistral's own budget for one batch job (its ``timeout_hours``, and
#: the SDK default). A job past it ends ``TIMEOUT_EXCEEDED``.
BATCH_TIMEOUT_HOURS = 24

#: How long after Mistral's own timeout the row is written off. Mistral
#: is the one that ends the job; this only catches a poll that never
#: sees it end.
DEADLINE_SLACK = timedelta(hours=1)

#: ``input_manifest`` keys a row may carry to override a constant for a
#: one-off experiment.
TUNING_KEYS = ("model",)

#: File purposes at Mistral: a page image and a batch manifest.
PAGE_FILE_PURPOSE = "ocr"
MANIFEST_FILE_PURPOSE = "batch"

#: Prefix of the scratch directory one submission renders in. The
#: directory holds the downloaded shard PDF (up to ``SHARD_TARGET_BYTES``)
#: while its pages are rendered and uploaded; the PNGs never touch the
#: disk. A normal exit and an exception both remove it; a SIGKILL
#: orphans it, and ``cleanup_processing_tmp`` reclaims it by this prefix
#: (#215), as it does the other stages' scratch dirs.
RENDER_TMP_PREFIX = "mistralocr-"


# ── switches and rows ───────────────────────────────────────────────
def enabled() -> bool:
    """Return whether Mistral jobs may be dispatched.

    :returns: Whether the stage should run.
    :rtype: bool
    """
    return mistral_client.enabled()


def model_for(job: ExternalJob) -> str:
    """Return the model one row's requests name.

    :param job: The row.
    :returns: The row's override, else ``settings.MISTRAL_MODEL``.
    :rtype: str
    """
    override = (job.input_manifest or {}).get("model")
    return str(override) if override else str(settings.MISTRAL_MODEL)


def ensure_extract_jobs(
    scan, manifest: dict, *, force_new_run: bool = False
) -> list[ExternalJob]:
    """Return the live Mistral jobs for ``scan``, creating them if the
    current run does not describe today's shard set.

    Idempotent, so a second press of the button is a no-op rather than
    a second run over pages already read. A run holding a dead row is
    replaced. A replacement run carries every shard whose identity is
    unchanged and whose result object is still on S3, so a re-cut that
    moved a few page ranges re-pays only the shards that moved.

    A result with a hole is never carried, stable or not
    (``carry_stable_holes=False``). The stable-hole rule of #238 trusts
    a deterministic worker to give the same answer twice; a Mistral
    batch line can fail from a transient fault at Mistral, so two
    unlucky runs must not freeze a page as unread for good.

    :param scan: The scan to read.
    :param manifest: The committed shard manifest.
    :param force_new_run: Replace a whole, reusable live run.
    :returns: The live run's rows, ordered by shard index.
    :rtype: list[ExternalJob]
    """
    return jobs.ensure_shard_jobs(
        scan,
        manifest,
        stage=JobStage.EXTRACT,
        engine=JobEngine.MISTRAL_OCR,
        provider=JobProvider.MISTRAL,
        reuse_results=True,
        force_new_run=force_new_run,
        carry_stable_holes=False,
    )


def live_extract_jobs(scan) -> list[ExternalJob]:
    """Return the current run's Mistral rows for ``scan``, in page order.

    :param scan: The scan, or its pk.
    :returns: The rows, or an empty list.
    :rtype: list[ExternalJob]
    """
    return jobs.live_run(scan, JobStage.EXTRACT, JobEngine.MISTRAL_OCR)


def run_summary(scan) -> dict | None:
    """Describe a scan's live Mistral run for the process page.

    :param scan: The scan (or its pk) to describe.
    :returns: See :func:`jobs.run_summary`, or ``None``.
    :rtype: dict | None
    """
    return jobs.run_summary(scan, JobStage.EXTRACT, JobEngine.MISTRAL_OCR)


def custom_id(page_no: int) -> str:
    """Return the manifest line id of one page of a shard.

    :param page_no: The page, counted from zero inside the shard, as
        the dots.mocr worker counts.
    :returns: ``"p{page_no}"``.
    :rtype: str
    """
    return f"p{page_no}"


def page_no_of(line_id) -> int | None:
    """Return the shard-local page a manifest line id names.

    :param line_id: A ``custom_id`` from an output or error line.
    :returns: The page, or ``None`` for an id this stage did not mint.
    :rtype: int | None
    """
    if not isinstance(line_id, str) or not line_id.startswith("p"):
        return None
    try:
        return int(line_id[1:])
    except ValueError:
        return None


# ── the render ──────────────────────────────────────────────────────
def render_page(page: fitz.Page) -> bytes:
    """Render one page as the ensemble's canonical PNG.

    ``pipeline/core/render.py`` line for line: zoom the page to
    ``RENDER_W`` wide, render RGB with no alpha, resize to exactly
    ``RENDER_W`` x ``RENDER_H`` when the page is another size.

    :param page: The fitz page.
    :returns: PNG bytes.
    :rtype: bytes
    """
    zoom = RENDER_W / page.rect.width
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
    img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    if img.size != (RENDER_W, RENDER_H):
        img = img.resize((RENDER_W, RENDER_H))
    buffer = io.BytesIO()
    img.save(buffer, format="PNG")
    return buffer.getvalue()


def render_shard_pages(pdf_path: Path) -> Iterator[tuple[int, bytes]]:
    """Render every page of one shard, in order.

    :param pdf_path: The downloaded shard.
    :returns: ``(page_no, png)`` per page, ``page_no`` counted from
        zero inside the shard.
    :rtype: Iterator[tuple[int, bytes]]
    """
    with fitz.open(str(pdf_path)) as doc:
        for page_no, page in enumerate(doc):
            yield page_no, render_page(page)


# ── the deadline ────────────────────────────────────────────────────
def claim_deadline(job: ExternalJob, submitted_at) -> dict:
    """Return the deadline a first claim stamps, and nothing after.

    Mistral's own ``timeout_hours`` ends a job that runs long, so the
    row's deadline is that budget plus slack for a poll to see it end.
    Stamped once: a re-claim after a defer writes nothing, as the
    RunPod rule does, or a rate-limited endpoint would push the ceiling
    out on every tick.

    :param job: The row being submitted.
    :param submitted_at: Submission timestamp.
    :returns: ``{"deadline": ...}`` on the first claim, else ``{}``.
    :rtype: dict
    """
    if job.deadline is not None:
        return {}
    return {
        "deadline": submitted_at
        + timedelta(hours=BATCH_TIMEOUT_HOURS)
        + DEADLINE_SLACK
    }


# ── the submit wave ─────────────────────────────────────────────────
@dataclass
class _Submission:
    """What one thread hands back after a shard is submitted.

    :ivar job_id: The batch job id.
    :ivar files: Every file id uploaded: the pages, then the manifest.
    :ivar page_count: Pages rendered and uploaded.
    :ivar duration_ms: Wall clock of download, render, upload, create.
    """

    job_id: str
    files: list[str] = field(default_factory=list)
    page_count: int = 0
    duration_ms: int = 0


def _delete_files(file_ids: list[str]) -> None:
    """Delete some files at Mistral, best effort.

    :param file_ids: The ids.
    :return: None.
    """
    for file_id in file_ids:
        if file_id:
            mistral_client.delete_file(str(file_id))


def _manifest_line(page_no: int, file_id: str, model: str) -> str:
    """Return one JSONL line of a batch manifest.

    The body is what the branch sent: the model, the uploaded page as a
    ``file`` document, blocks on, tables as HTML.

    :param page_no: Shard-local page.
    :param file_id: The uploaded page image.
    :param model: The model to name.
    :returns: One JSON object, on one line.
    :rtype: str
    """
    return json.dumps(
        {
            "custom_id": custom_id(page_no),
            "body": {
                "model": model,
                "document": {"type": "file", "file_id": file_id},
                "include_blocks": INCLUDE_BLOCKS,
                "table_format": TABLE_FORMAT,
            },
        }
    )


def _prepare_and_submit(job: ExternalJob) -> _Submission:
    """Render, upload and submit one shard. Runs on a worker thread.

    No database access here: the row's fields were read on the main
    thread, and every write happens there afterwards. On any failure
    the files uploaded so far are deleted, because nothing will name
    them: a failed create left no job, and a lost create left a job
    nothing we hold names.

    :param job: The claimed row.
    :returns: The submission.
    :raises Exception: Whatever the download, the render or the API
        raised; classified by :func:`_apply_outcome`.
    """
    started = time.monotonic()
    model = model_for(job)
    files: list[str] = []
    try:
        # Named after the scan and the shard, as the other stages name
        # theirs: an orphan of a SIGKILL holds a shard PDF of up to
        # SHARD_TARGET_BYTES, and the #215 sweep reclaims it by prefix,
        # so whoever finds one first can tell whose it is.
        with tempfile.TemporaryDirectory(
            prefix=f"{RENDER_TMP_PREFIX}{job.scan_id}-s{job.shard_index}-"
        ) as tmp:
            pdf_path = Path(tmp) / "shard.pdf"
            s3_sync.download_object(job.input_key, pdf_path)
            lines = []
            for page_no, png in render_shard_pages(pdf_path):
                file_id = mistral_client.upload_file(
                    f"{custom_id(page_no)}.png", png, PAGE_FILE_PURPOSE
                )
                files.append(file_id)
                lines.append(_manifest_line(page_no, file_id, model))
        manifest_name = (
            f"scan{job.scan_id}-r{job.run}-s{job.shard_index}"
            f"-a{job.attempt}.jsonl"
        )
        manifest_id = mistral_client.upload_file(
            manifest_name,
            "\n".join(lines).encode("utf-8"),
            MANIFEST_FILE_PURPOSE,
        )
        files.append(manifest_id)
        job_id = mistral_client.create_batch(
            manifest_id,
            metadata={
                "scan": str(job.scan_id),
                "job": str(job.pk),
                "shard": str(job.shard_index),
            },
            timeout_hours=BATCH_TIMEOUT_HOURS,
        )
    except Exception:
        _delete_files(files)
        raise
    return _Submission(
        job_id=job_id,
        files=files,
        page_count=len(lines),
        duration_ms=int((time.monotonic() - started) * 1000),
    )


def _apply_outcome(
    job: ExternalJob,
    exc: Exception | None,
    submission: _Submission | None,
    now,
) -> str:
    """Record what one shard's submission came to.

    Unlike doctor's, a success is not a completion: it is a batch id,
    and the row stays in flight until a poll says otherwise. The
    failures differ in what they cost the row:

    - **Rate limited.** Nothing is wrong with the job, so it goes back
      to PENDING with its attempt intact.
    - **A transient fault** -- a 5xx, a lost answer, an S3 download
      that failed. Another attempt is spent. There is no "unanswered"
      branch here: a create that did not answer minted no id we can
      find, so waiting on it would wait on nothing.
    - **Refused for good.** A 4xx from Mistral, or a shard that will
      not render: the row is failed.

    :param job: The SUBMITTED row.
    :param exc: The exception raised by the thread, if any.
    :param submission: The submission on success.
    :param now: Current time.
    :returns: One of ``"submitted"``, ``"deferred"``, ``"retried"``,
        ``"failed"``, ``"skipped"``.
    :rtype: str
    """
    if exc is None and submission is not None:
        meta = dict(job.provider_meta or {})
        meta["files"] = list(submission.files)
        meta["submission"] = {
            "page_count": submission.page_count,
            "duration_ms": submission.duration_ms,
            "model": model_for(job),
        }
        written = jobs._write(
            job, external_id=submission.job_id, provider_meta=meta
        )
        if not written:
            # The row was cancelled while the thread was working, so
            # the id has nowhere to live. Cancel from here or nothing
            # ever will, and delete what was uploaded for it.
            logger.warning(
                "job %s was claimed by another writer while Mistral batch "
                "%s was being submitted; cancelling it",
                job.pk,
                submission.job_id,
            )
            cancel_job(job, submission.job_id, files=submission.files)
            return "skipped"
        logger.info(
            "job %s (scan %s shard %s): %d page(s) submitted as Mistral "
            "batch %s in %d ms",
            job.pk,
            job.scan_id,
            job.shard_index,
            submission.page_count,
            submission.job_id,
            submission.duration_ms,
        )
        return "submitted"

    if isinstance(exc, mistral_client.MistralBusy):
        return jobs._defer(job, exc.error_code, str(exc), now)
    if isinstance(exc, mistral_client.MistralTransientError):
        return jobs._retry_or_fail(job, exc.error_code, str(exc), now)
    if isinstance(exc, mistral_client.MistralError):
        failed = jobs._fail(job, exc.error_code, str(exc))
        return "failed" if failed else "skipped"
    if isinstance(exc, (BotoCoreError, ClientError)):
        # The shard did not come down. Ours, not the job's: retry.
        return jobs._retry_or_fail(
            job, "INPUT_DOWNLOAD_FAILED", str(exc)[:500], now
        )
    logger.exception(
        "unexpected error submitting job %s to Mistral", job.pk, exc_info=exc
    )
    failed = jobs._fail(job, "SUBMIT_FAILED", str(exc)[:500])
    return "failed" if failed else "skipped"


def submit_wave(summary: jobs.SubmitSummary, limit: int | None) -> None:
    """Render, upload and submit one wave of pending shards.

    The provider table's ``submit_wave`` for Mistral. The threads
    download, render and talk to Mistral and nothing else; every
    database write happens on this thread, before and after the
    fan-out, as doctor's wave does. Rows are claimed only as far as the
    pool can start them at once, so a SUBMITTED row is genuinely in
    flight.

    Two caps, on purpose. :data:`MAX_CONCURRENCY` bounds the batches in
    flight at Mistral; :data:`MAX_SUBMITS_PER_TICK` bounds how many
    shards this tick renders and uploads, which is what blocks the
    serial scheduler. The first keeps a volume draining while batches
    wait for hours; the second keeps every other daemon task waiting
    for one shard at most.

    :param summary: Counts to update.
    :param limit: In-flight override; defaults to
        :data:`MAX_CONCURRENCY`. The per-tick cap is not overridden.
    :return: None.
    """
    claimed = jobs.claim_for_wave(
        ExternalJob.objects.filter(
            provider=JobProvider.MISTRAL,
            stage=JobStage.EXTRACT,
            engine=JobEngine.MISTRAL_OCR,
        ),
        int(limit or MAX_CONCURRENCY),
        "Mistral OCR",
        summary,
        per_tick=MAX_SUBMITS_PER_TICK,
        # DEBUG for the "cap reached" line: a batch waits at Mistral
        # for hours, so at INFO a full cap would write one line per
        # tick (every 5 s) for the whole wait.
        level=logging.DEBUG,
    )
    if not claimed:
        return

    logger.info(
        "rendering and submitting %d shard(s) to Mistral", len(claimed)
    )
    with ThreadPoolExecutor(max_workers=len(claimed)) as pool:
        futures = [
            (job, pool.submit(_prepare_and_submit, job)) for job, _ in claimed
        ]
    for job, future in futures:
        exc: Exception | None = None
        submission = None
        try:
            submission = future.result()
        except Exception as caught:  # noqa: BLE001 - classified above
            exc = caught
        result = _apply_outcome(job, exc, submission, timezone.now())
        setattr(summary, result, getattr(summary, result) + 1)


# ── the sweep ───────────────────────────────────────────────────────
def sweep_job(job: ExternalJob, now, summary: jobs.SweepSummary) -> None:
    """Poll one batch and apply what it said.

    The provider table's ``sweep_job`` for Mistral. A row with no
    ``external_id`` is a claim the daemon lost mid-submission: the
    scheduler is serial, so no wave is running while this sweeps, and
    a claimed row with no id will never get one. It is retried at once
    rather than waited on for a day.

    :param job: An in-flight row.
    :param now: Comparison time.
    :param summary: Counts to update.
    :return: None.
    """
    if not job.external_id:
        jobs.count_sweep_outcome(
            summary,
            jobs._retry_or_fail(
                job,
                "LOST_CLAIM",
                "claimed, but the daemon stopped before the batch was created",
                now,
            ),
        )
        return

    outcome = mistral_client.poll_batch(
        job.external_id, label=f"scan {job.scan_id} shard {job.shard_index}"
    )
    jobs.apply_poll_outcome(
        job,
        outcome,
        now,
        summary,
        on_complete=_harvest_outcome,
        on_progress=_record_progress,
    )


def _harvest_outcome(job: ExternalJob, outcome, now) -> str:
    """Apply a finished batch: nothing wrote our result object but us.

    :param job: The row Mistral reports finished.
    :param outcome: The poll result.
    :param now: Completion timestamp.
    :returns: The outcome label to count. A failed harvest counts as a
        check error, like an S3 blip: the output is still at Mistral
        and the next tick downloads it again.
    :rtype: str
    """
    return "completed" if harvest(job, outcome, now) else "errors"


def _record_progress(job: ExternalJob, outcome, now) -> bool:
    """Store a running batch's line counts on the row.

    :param job: The in-flight row.
    :param outcome: The poll result.
    :param now: Current time.
    :returns: Whether the write won its compare-and-swap.
    :rtype: bool
    """
    meta = dict(job.provider_meta or {})
    meta["progress"] = {
        "status": outcome.provider_status,
        "total": outcome.total,
        "succeeded": outcome.succeeded,
        "failed": outcome.failed,
    }
    return jobs._write(
        job,
        status=outcome.status,
        last_polled_at=now,
        provider_meta=meta,
    )


def harvest(
    job: ExternalJob, outcome: mistral_client.BatchOutcome, now
) -> bool:
    """Store a finished batch's output at the row's key, whole.

    The output file and the error file are downloaded and kept line
    for line. The only thing read out of a line is its ``custom_id``,
    to order the pages and to name the holes: a line with ``error``,
    an empty ``response.body``, or a page no line names at all is a
    hole, and its shard-local page joins ``failed_pages`` -- the list
    ``jobs.has_unread_pages`` reads, so a result with a hole is never
    carried into a later run. Nothing is parsed, reduced or renamed.

    The document is written in the result-envelope shape every other
    stage uses, so the glue reuses ``jobs.check_result_envelope``.
    The row is completed only after the PUT landed; a failed PUT leaves
    the row in flight, and the next tick downloads the output again.
    Then the page files, the manifest and the two output files are
    deleted at Mistral: the object in S3 is the copy that matters.

    :param job: The in-flight row Mistral reports ``SUCCESS`` for.
    :param outcome: The poll answer.
    :param now: Completion timestamp.
    :returns: Whether the row was completed.
    :rtype: bool
    """
    try:
        output = (
            mistral_client.download_lines(outcome.output_file)
            if outcome.output_file
            else []
        )
        errors = (
            mistral_client.download_lines(outcome.error_file)
            if outcome.error_file
            else []
        )
    except mistral_client.MistralError as exc:
        logger.warning(
            "job %s (scan %s shard %s): could not download the batch "
            "output; trying again next tick: %s",
            job.pk,
            job.scan_id,
            job.shard_index,
            exc,
        )
        return False

    page_count = int((job.input_manifest or {}).get("page_count") or 0)
    answered: set[int] = set()
    failed: set[int] = set()
    for line in output:
        page_no = page_no_of(line.get("custom_id"))
        if page_no is None:
            continue
        answered.add(page_no)
        response = line.get("response") or {}
        body = (
            response.get("body") if isinstance(response, dict) else None
        ) or line.get("body")
        if line.get("error") or not body:
            failed.add(page_no)
    for line in errors:
        page_no = page_no_of(line.get("custom_id"))
        if page_no is not None:
            failed.add(page_no)
    for page_no in range(page_count):
        if page_no not in answered:
            failed.add(page_no)
    failed_pages = sorted(failed)

    document = {
        "schema_version": jobs.RESULT_SCHEMA_VERSION,
        "action": ACTION,
        "scan_pk": job.scan_id,
        "result_key": job.result_key,
        "payload": {
            # Verbatim: what Mistral wrote, line for line.
            "output": output,
            "errors": errors,
            "batch": outcome.job,
            # What was sent, so a reader knows the space the bboxes
            # are in and which copy of the volume was read.
            "model": model_for(job),
            "render": {
                "width": RENDER_W,
                "height": RENDER_H,
                "source": SOURCE,
            },
            "page_count": page_count,
            "failed_pages": failed_pages,
        },
    }
    if not s3_sync.upload_json_object(job.result_key, document):
        logger.warning(
            "job %s (scan %s shard %s): could not store the batch output "
            "at %s; trying again next tick",
            job.pk,
            job.scan_id,
            job.shard_index,
            job.result_key,
        )
        return False

    summary = {
        "page_count": page_count,
        "failed_pages": failed_pages,
        "succeeded_requests": outcome.succeeded,
        "failed_requests": outcome.failed,
        "duration_ms": int((now - job.submitted_at).total_seconds() * 1000)
        if job.submitted_at
        else None,
    }
    files = [str(f) for f in (job.provider_meta or {}).get("files") or []]
    outputs = [f for f in (outcome.output_file, outcome.error_file) if f]
    # The files are about to be deleted, so the row stops naming them.
    job.provider_meta = {
        key: value
        for key, value in (job.provider_meta or {}).items()
        if key != "files"
    }
    if not jobs._complete(job, summary, now):
        # Another writer took the row. Its cancel deletes the files the
        # row names -- the pages and the manifest -- but nothing names
        # the two output files except this call, so delete them here.
        _delete_files(outputs)
        return False
    _delete_files(files + outputs)
    return True


# ── the cancel ──────────────────────────────────────────────────────
def cancel_job(
    job: ExternalJob, job_id: str, files: list[str] | None = None
) -> None:
    """Cancel one batch and delete what was uploaded for it.

    The provider table's ``cancel`` for Mistral, called from every
    path that writes a row off. Best effort and never raises.

    :param job: The row being written off.
    :param job_id: The batch job id.
    :param files: The file ids to delete; defaults to the row's own
        ``provider_meta["files"]``.
    :return: None.
    """
    if job_id:
        mistral_client.cancel_batch(job_id)
    if files is None:
        files = [str(f) for f in (job.provider_meta or {}).get("files") or []]
    _delete_files(files)
