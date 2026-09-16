"""The Mistral OCR stage: rows, render, submit, poll, harvest, glue.

Issues #191 (the job) and #245 (the glue).

One :class:`~scanning.models.ExternalJob` row per shard at
``EXTRACT``/``MISTRAL_OCR``/``MISTRAL``, and one Mistral batch job per
row. The daemon does the intermediate step Mistral needs: it renders
every page of the shard the way every engine of the ai-research
ensemble saw it (``pipeline/core/render.py`` on the ``extraction_align``
branch: 1700x2200 RGB), uploads each page as an ``ocr`` file, uploads
a JSONL manifest naming them, and creates the batch. The confirm tick
polls the batch, and on ``SUCCESS`` downloads the output and stores it,
**whole**, at the row's ``result_key``.

**The read is over the original shards, and the redaction is a
transform on the text it returns.** A legal review removed the
requirement that the page be redacted before the read (2026-09-15),
so the source is the shard set ``sharding.ensure_shards`` already cut
-- the one dots.mocr and YOLO read -- and this stage waits on no
redacted volume and cuts no second set. That is also what makes a late change cheap. A page is addressed
by ``source_fingerprint`` plus a page range, nothing downstream moves
it, and so a missed redaction, a moved boundary and a re-cut opinion
split all cost a re-glue rather than a re-paid read. The boxes come
off the **text** in the glue; nothing paints a box on the page this
stage renders.

This module is the Mistral entry of :func:`jobs._providers`. It uses
the lifecycle primitives of :mod:`scanning.jobs` -- the claim, the
compare-and-swap writes, the retry ledger -- rather than copying them,
which is what the provider table exists for. What is specific to
Mistral, and must not be broken:

- **The harvest stores every byte Mistral returned, and nothing else
  transforms it.** The result document holds the output lines and the
  error lines as they came, plus the batch job object. ``markdown``,
  ``blocks``, ``images``, ``tables``, ``usage_info`` and whatever a
  later model adds all land in S3. :func:`parse_payload` is the one
  transform, and both glues call it, so a better transform is a re-glue
  (``reglue_mistral_ocr``) at no API cost, never a re-paid read.
- **The two glues run on the collect tick, and never in
  ``apply.glues_due``** (#245). The apply's own trigger takes a scan in
  ``PAGE_COMPLETENESS_REVIEW_DONE`` alone, and this read starts later
  than that status: by hand today, and after the second review once
  #336 lands. An arm there would miss the normal case for good.
- **No review state waits for an output of this stage.**
  ``ApplyRun.extract_key`` is outside ``is_complete``, and the rows of
  a corrected volume are created outside ``apply._ensure_rows``: an
  environment that does not pay for Mistral must build its corrected
  volumes and open its reviews exactly as it does today.
- **The corrected volume's rows are created only for a volume somebody
  chose to read.** :func:`finish_ready_applies` takes a scan whose
  live volume run is glued, which only a person starts, so the pass
  mints no paid work of its own.
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
  deletes what it uploaded, and :func:`harvest` deletes all four once
  the result is in S3. The pages are unredacted, so this delete is
  the only thing that limits how long a third party holds them.
- **A result with a hole is never carried.** The stable-hole rule of
  #238 trusts a deterministic worker; a Mistral batch line can fail
  from a transient fault, so the carry re-reads the shard instead.
- **The source is the original, never the bitonal copy.** The
  ensemble's tests all ran on non-bitonal images, so the render must
  read a greyscale document, and the original shards are the only
  greyscale copy the pipeline keeps. :data:`SOURCE` records it on
  every stored result. It has no per-row override.
"""

from __future__ import annotations

import io
import json
import logging
import re
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
    JobStatus,
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

#: What the render reads, recorded on every stored result so a reader
#: knows which copy of the volume was read. ``input_key`` names a
#: shard of the original, and the original never changes, which is
#: what lets a later run carry a paid result for good.
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

#: How long one row waits between two polls of its batch. The sweep
#: visits every in-flight row on every collect tick, and a batch waits
#: at Mistral for hours, so without this a full cap would ask Mistral
#: about sixteen batches four times a minute for a day. The deadline is
#: judged on every tick whatever this says (``jobs.check_deadline``),
#: so a skipped poll delays no write-off.
POLL_INTERVAL = timedelta(minutes=2)

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
    scan, manifest: dict, *, force_new_run: bool = False, apply_run=None
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
    :param apply_run: The apply run whose one-page shards are read
        (#245), or None for the volume's own shard set.
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
        apply_run=apply_run,
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

    A row polled inside :data:`POLL_INTERVAL` is left alone, and its
    deadline is judged all the same.

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

    if not _poll_due(job, now):
        summary.pending += 1
        jobs.check_deadline(job, now, summary)
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


def _poll_due(job: ExternalJob, now) -> bool:
    """Return whether this row's batch may be polled again.

    :param job: An in-flight row.
    :param now: Comparison time.
    :returns: Whether :data:`POLL_INTERVAL` has passed. A row never
        polled is always due.
    :rtype: bool
    """
    last = job.last_polled_at
    return last is None or (now - last) >= POLL_INTERVAL


def _harvest_outcome(job: ExternalJob, outcome, now) -> str:
    """Apply a finished batch: nothing wrote our result object but us.

    **Three answers, because a failed download is three things.**
    ``jobs.apply_poll_outcome`` returns inside its completion branch,
    before the deadline check, so a finished batch this function never
    settles is a row nothing else will ever end.

    - **A transient fault** -- a 5xx, a rate limit, a lost answer.
      The output is still at Mistral, so the row stays in flight and
      the next tick downloads it again.
    - **The output file is gone** (404), or Mistral refuses the
      download for good. Waiting cannot fix either, so the shard is
      retried: it costs an attempt, and the attempt ladder ends it.
    - **Neither, but the row is past its deadline.** A PUT to S3 that
      keeps failing holds the row exactly as a missing file would, so
      the deadline is the escape from both.

    :param job: The row Mistral reports finished.
    :param outcome: The poll result.
    :param now: Completion timestamp.
    :returns: The outcome label to count.
    :rtype: str
    """
    try:
        stored = harvest(job, outcome, now)
    except mistral_client.MistralTransientError as exc:
        logger.warning(
            "job %s (scan %s shard %s): could not download the batch "
            "output; trying again next tick: %s",
            job.pk,
            job.scan_id,
            job.shard_index,
            exc,
        )
        stored = False
    except mistral_client.MistralError as exc:
        logger.warning(
            "job %s (scan %s shard %s): the batch output cannot be "
            "downloaded (%s); retrying the shard: %s",
            job.pk,
            job.scan_id,
            job.shard_index,
            exc.error_code,
            exc,
        )
        return jobs._retry_or_fail(job, exc.error_code, str(exc), now)
    if stored:
        return "completed"
    if job.is_overdue(now):
        return jobs._retry_or_fail(
            job,
            "DEADLINE_EXCEEDED",
            f"the batch finished, but its output was still unstored at "
            f"{job.deadline}",
            now,
        )
    return "errors"


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
    the row in flight, and the next tick downloads the output again,
    until the deadline ends it (:func:`_harvest_outcome`).
    Then the page files, the manifest and the two output files are
    deleted at Mistral: the object in S3 is the copy that matters.

    :param job: The in-flight row Mistral reports ``SUCCESS`` for.
    :param outcome: The poll answer.
    :param now: Completion timestamp.
    :returns: Whether the row was completed. ``False`` means the S3
        PUT did not land, or another writer took the row.
    :rtype: bool
    :raises MistralError: If a file cannot be downloaded.
        :func:`_harvest_outcome` decides what each failure costs.
    """
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


# ── the glue ────────────────────────────────────────────────────────
#: Version of the volume document this module writes. A reader checks
#: it before it trusts the page shape.
GLUE_SCHEMA_VERSION = 1

#: How many times a run's glue may fail before the pass leaves it
#: alone. The per-shard results are kept, so a retry costs one small
#: download and no API payment.
GLUE_MAX_ATTEMPTS = 3

#: Where the leaked coordinate markers come off the block text
#: (``pipeline/core/markup.py`` of ai-research, line for line). Mistral
#: sometimes writes its own box into the text as
#: ``[BBOX]x0,y0,x1,y1[/BBOX]``, and sometimes leaves the closing tag
#: out. The inner run therefore takes coordinate characters only --
#: digits, dots, commas, spaces -- because a permissive inner match
#: would swallow the sentence after a marker with no closing tag. The
#: block already carries the box as a field, so the marker is the same
#: fact restated.
_BBOX_MARKER = re.compile(
    r"\[\s*BBOX\s*\][\d.,\t ]*(?:\[\s*/\s*BBOX\s*\])?", re.I
)

#: The block field the glue writes, and the two it reads. The name is
#: internal: this repository is the only reader of a glued document.
#: PR #247's fixture writes ``content`` and the ai-research loader reads
#: ``text``, so the parse takes either and writes one.
BLOCK_TEXT_KEY = "content"
BLOCK_TEXT_FIELDS = ("content", "text")


class MistralGlueError(Exception):
    """A Mistral run could not be glued into a volume document."""


def glued_result_key(scan, run: int) -> str:
    """Return the S3 key one run's glued volume document lives at.

    This stage's name for :func:`jobs.volume_result_key`, which holds
    the rule and the reasons.

    :param scan: The scan the run belongs to.
    :param run: The run number.
    :returns: Key of the form ``{processing_prefix}jobs/extract/
        mistral_ocr/r{run}-volume.json``.
    :rtype: str
    """
    return jobs.volume_result_key(
        scan, JobStage.EXTRACT, JobEngine.MISTRAL_OCR, run
    )


def glued_volume_key(scan) -> str | None:
    """Return the key of a scan's glued Mistral document, or nothing.

    The live run is glued when every one of its rows is ``CONSUMED``:
    the glue writes the document and flips the rows in one pass. The
    twin of ``dots_mocr.glued_volume_key``.

    :param scan: The scan (or its pk) to look up.
    :returns: The key, or None when no run is glued.
    :rtype: str | None
    """
    rows = live_extract_jobs(scan)
    if rows and all(row.status == JobStatus.CONSUMED for row in rows):
        return glued_result_key(scan, rows[0].run)
    return None


def _check_envelope(scan, job: ExternalJob, envelope) -> dict:
    """Return an envelope's payload, or refuse the envelope.

    :param scan: The scan being glued.
    :param job: The row whose result the envelope claims to be.
    :param envelope: The parsed JSON found at ``job.result_key``.
    :returns: ``envelope["payload"]``.
    :rtype: dict
    :raises MistralGlueError: If the envelope is not one this attempt
        should have produced.
    """
    return jobs.check_result_envelope(
        scan, job, envelope, ACTION, MistralGlueError
    )


def _block_text(block: dict) -> str:
    """Return one block's text, with the coordinate markers removed.

    :param block: A block as Mistral wrote it.
    :returns: The text, or an empty string.
    :rtype: str
    """
    for name in BLOCK_TEXT_FIELDS:
        value = block.get(name)
        if isinstance(value, str):
            return _BBOX_MARKER.sub("", value).lstrip()
    return ""


def _blocks(page: dict) -> list[dict]:
    """Return one page's blocks in this module's own shape.

    ``{"id", "type", "bbox", "content"}`` per block, in the order
    Mistral wrote them. The box is kept exactly as it came: every
    engine of the ai-research ensemble measures in the 1700x2200 render
    space, and a second convention here would have to be undone by
    every reader.

    :param page: One page of a response body.
    :returns: The blocks, empty when the answer has none.
    :rtype: list[dict]
    """
    blocks = page.get("blocks")
    if not isinstance(blocks, list):
        return []
    return [
        {
            "id": index,
            "type": block.get("type") or "",
            "bbox": block.get("bbox"),
            BLOCK_TEXT_KEY: _block_text(block),
        }
        for index, block in enumerate(blocks)
        if isinstance(block, dict)
    ]


def _page_of_line(line: dict) -> dict | None:
    """Return the page one output line answered, or nothing.

    One request carries one page image, so one answer carries one page.
    A line with an error, with no body, or with no page in its body is
    a hole, and the caller keeps the page's slot instead.

    :param line: One output line, as Mistral wrote it.
    :returns: The page dict of this module's shape, or None.
    :rtype: dict | None
    """
    if line.get("error"):
        return None
    response = line.get("response")
    body = (
        response.get("body") if isinstance(response, dict) else None
    ) or line.get("body")
    if not isinstance(body, dict):
        return None
    pages = body.get("pages")
    if not isinstance(pages, list) or not pages:
        return None
    page = pages[0]
    if not isinstance(page, dict):
        return None
    read = {
        "md": page.get("markdown") or "",
        "blocks": _blocks(page),
    }
    if isinstance(page.get("dimensions"), dict):
        read["dimensions"] = page["dimensions"]
    return read


def parse_payload(payload: dict) -> dict[int, dict]:
    """Turn one stored result into a page dict per page of its shard.

    **The one transform of a Mistral result.** The harvest stores the
    output lines verbatim (:func:`harvest`), and this is where they
    become pages: the body of each line, the page markdown, the blocks
    with their boxes and their text cleaned of the coordinate markers.
    Both glues call it -- the volume glue over a shard result, the
    apply glue over a one-page result -- so a better transform is a
    re-glue at no API cost, and it is right in both documents at once.

    Every page of the shard comes back, keyed by its shard-local page.
    A page Mistral could not read keeps its slot and carries ``error``
    instead of text, as the dots.mocr glue does: a hole must be visible
    to a reader rather than shift the pages after it.

    :param payload: The ``payload`` of a stored result envelope.
    :returns: ``{shard-local page: page dict}``, one entry per page.
    :rtype: dict[int, dict]
    """
    page_count = int(payload.get("page_count") or 0)
    lines = payload.get("output")
    read: dict[int, dict] = {}
    for line in lines if isinstance(lines, list) else []:
        if not isinstance(line, dict):
            continue
        page_no = page_no_of(line.get("custom_id"))
        if page_no is None:
            continue
        page = _page_of_line(line)
        if page is not None:
            read[page_no] = page
    pages: dict[int, dict] = {}
    for page_no in range(page_count):
        page = read.get(page_no)
        if page is None:
            page = {
                "md": "",
                "blocks": [],
                "error": "not read: no answer for this page",
            }
        pages[page_no] = {"page_no": page_no, **page}
    return pages


def _failed_pages(pages: list[dict], key: str) -> list[int]:
    """Return the pages that carry no reading, by one page-number field.

    One list, not the four of ``dots_mocr.PAGE_LISTS``: the other three
    name faults of the dots.mocr worker (a filtered answer, a retry
    rung, a repaired layout), and this stage has none of them. A list
    of zeros would read as "none" where the truth is "not a question
    here".

    :param pages: Page dicts, shard-local or volume-level.
    :param key: The page-number field to list: ``"page_no"`` for a
        shard's pages, ``"page_index"`` for the volume document.
    :returns: The page numbers, in order.
    :rtype: list[int]
    """
    return sorted(page[key] for page in pages if "error" in page)


def merge_extract_results(scan, extract_jobs: list[ExternalJob]) -> str:
    """Glue one run's shard results into a volume document on S3.

    Glues in strict shard order and asserts the page arithmetic:
    ``page_no`` counts from zero inside a shard, so a page's volume
    index is the shard's ``from_page`` plus its ``page_no``, and its
    1-based ``pdf_page`` is that plus one. The volume document is in
    the **original's** page space, as every other volume document is;
    the corrected volume's own space is the apply glue
    (:func:`glue_apply_run`).

    Idempotent: it rebuilds from the result objects every time, so a
    daemon killed between the upload and the CONSUMED write just glues
    again. The results are kept and nothing here deletes one.

    It writes no scan status. The stages that read a volume own no
    review state (#190, #195), and this one runs later than both.

    :param scan: The scan whose run finished.
    :param extract_jobs: The live run's rows, ordered by shard index.
    :returns: The S3 key the document was uploaded to.
    :rtype: str
    :raises MistralGlueError: If a result is missing, malformed, or the
        page arithmetic does not add up to the volume.
    """
    if not extract_jobs:
        raise MistralGlueError(f"scan {scan.pk} has no Mistral jobs")

    expected_total = (extract_jobs[0].input_manifest or {}).get(
        "source_page_count"
    )
    run = extract_jobs[0].run
    started = time.monotonic()

    pages: list[dict] = []
    shards: list[dict] = []
    next_page = 0
    for index, job in enumerate(extract_jobs):
        if job.shard_index != index:
            raise MistralGlueError(
                f"scan {scan.pk} shard sequence breaks at position "
                f"{index}: job {job.pk} covers shard {job.shard_index}"
            )
        if not job.result_key:
            raise MistralGlueError(
                f"scan {scan.pk} shard {index} has no result key"
            )
        manifest = job.input_manifest or {}
        from_page = manifest.get("from_page")
        page_count = manifest.get("page_count")
        if from_page != next_page or not isinstance(page_count, int):
            raise MistralGlueError(
                f"scan {scan.pk} shard {index} covers pages from "
                f"{from_page}, expected {next_page}"
            )
        payload = _check_envelope(
            scan, job, s3_sync.download_json_object(job.result_key)
        )
        shard_pages = parse_payload(payload)
        if sorted(shard_pages) != list(range(page_count)):
            raise MistralGlueError(
                f"scan {scan.pk} shard {index} answered page(s) "
                f"{sorted(shard_pages)}, the shard has {page_count}"
            )
        for page_no in range(page_count):
            page_index = from_page + page_no
            pages.append(
                {
                    "page_index": page_index,
                    "pdf_page": page_index + 1,
                    "shard_index": index,
                    **shard_pages[page_no],
                }
            )
        entry = {
            "name": manifest.get("name"),
            "index": index,
            "from_page": from_page,
            "to_page": manifest.get("to_page"),
            "page_count": page_count,
            "attempt": job.attempt,
            "result_key": job.result_key,
            "model": payload.get("model"),
        }
        tuning = {key: manifest[key] for key in TUNING_KEYS if key in manifest}
        if tuning:
            entry["tuning"] = tuning
        shards.append(entry)
        next_page += page_count

    if expected_total is not None and len(pages) != expected_total:
        raise MistralGlueError(
            f"scan {scan.pk} glued to {len(pages)} page(s), the original "
            f"has {expected_total}"
        )

    document = {
        "schema_version": GLUE_SCHEMA_VERSION,
        "engine": str(JobEngine.MISTRAL_OCR),
        "action": ACTION,
        "scan_pk": scan.pk,
        "run": run,
        "source_page_count": expected_total,
        "model": shards[0].get("model"),
        # The space every box of every block lives in, and the copy of
        # the volume that was read.
        "render": {
            "width": RENDER_W,
            "height": RENDER_H,
            "source": SOURCE,
        },
        "generated_at": timezone.now().isoformat(),
        "shards": shards,
        "pages": pages,
        "failed_pages": _failed_pages(pages, "page_index"),
    }
    key = glued_result_key(scan, run)
    if not s3_sync.upload_json_object(key, document):
        raise MistralGlueError(
            f"scan {scan.pk}: the glued document could not be uploaded "
            f"to {key}"
        )
    logger.info(
        "Glued %d Mistral shard(s) for scan %s into %s (%d page(s), "
        "%d unread) in %.1fs",
        len(extract_jobs),
        scan.pk,
        key,
        len(pages),
        len(document["failed_pages"]),
        time.monotonic() - started,
    )
    return key


def _glue_state(extract_jobs: list[ExternalJob], name: str) -> dict:
    """Return one glue's bookkeeping for a run.

    Kept on the first row's ``provider_meta`` rather than a field: the
    counter describes the run, any row of it can carry that, and
    ``input_manifest`` is off limits (``jobs._still_describes`` compares
    it exactly, so an added key would read as a stale run). A new run
    starts with clean rows, which is what gives a re-read fresh tries.

    :param extract_jobs: The live run's rows, ordered by shard index.
    :param name: ``"glue"`` for the volume document, or the apply run's
        label for the corrected volume's.
    :returns: ``attempts``, ``last_error``, ``last_attempt_at``; empty
        when the glue has never failed.
    :rtype: dict
    """
    meta = extract_jobs[0].provider_meta or {}
    return dict((meta.get("glue") or {}).get(name) or {})


def _record_glue_failure(
    scan, extract_jobs: list[ExternalJob], name: str, exc
) -> None:
    """Count one glue failure, and give up loudly on the last one.

    The result objects stay in S3, so a retry costs one small download
    and no API payment. The crossing into "out of tries" is the one
    ERROR-level event; the way back after a fix is a person clearing
    ``provider_meta["glue"]`` on the named row.

    :param scan: The scan whose glue failed.
    :param extract_jobs: The live run's rows, ordered by shard index.
    :param name: ``"glue"``, or the apply run's label.
    :param exc: What the glue raised.
    :return: None.
    """
    head = extract_jobs[0]
    meta = dict(head.provider_meta or {})
    glue = dict(meta.get("glue") or {})
    state = dict(glue.get(name) or {})
    attempts = int(state.get("attempts") or 0) + 1
    state.update(
        {
            "attempts": attempts,
            "last_error": str(exc)[:500],
            "last_attempt_at": timezone.now().isoformat(),
        }
    )
    glue[name] = state
    meta["glue"] = glue
    head.provider_meta = meta
    head.save(update_fields=["provider_meta"])
    if attempts >= GLUE_MAX_ATTEMPTS:
        logger.exception(
            "Gluing the Mistral results (%s) for scan %s failed; giving up "
            "after %d attempt(s). The shard results stay in S3; clear "
            "provider_meta['glue'] on job %s to retry.",
            name,
            scan.pk,
            attempts,
            head.pk,
        )
    else:
        logger.warning(
            "Gluing the Mistral results (%s) for scan %s failed (attempt "
            "%d of %d): %s",
            name,
            scan.pk,
            attempts,
            GLUE_MAX_ATTEMPTS,
            exc,
        )


def _volume_glue_attempts(extract_jobs: list[ExternalJob]) -> int:
    """Return how many times this run's volume glue has failed.

    :param extract_jobs: The live run's rows, ordered by shard index.
    :returns: The stored attempt count, 0 when none.
    :rtype: int
    """
    return int(_glue_state(extract_jobs, "glue").get("attempts") or 0)


def finish_ready_runs() -> int:
    """Glue every finished Mistral run into its volume document.

    Runs on the collect tick, next to ``dots_mocr.finish_ready_runs``
    and ``yolo.finish_ready_runs``, and on the same candidate rule
    (:func:`jobs.ready_volume_runs`). A glued run is all ``CONSUMED``,
    which is the idempotence marker, because this pass writes no scan
    status.

    It asks :func:`enabled` nothing, on purpose: the results are paid
    for and stored, so a key taken out of the environment after the
    read must not leave them unglued. What the key gates is spending,
    and this pass spends nothing.

    :returns: How many runs were glued and consumed.
    :rtype: int
    """
    if not s3_sync.s3_active():
        return 0

    glued = 0
    for scan, rows in jobs.ready_volume_runs(
        JobStage.EXTRACT,
        JobEngine.MISTRAL_OCR,
        JobProvider.MISTRAL,
        live_extract_jobs,
        _volume_glue_attempts,
        GLUE_MAX_ATTEMPTS,
    ):
        try:
            merge_extract_results(scan, rows)
        except Exception as exc:
            _record_glue_failure(scan, rows, "glue", exc)
            continue
        jobs.consume_run(rows)
        glued += 1

    return glued


# ── the corrected volume (#224, #245) ───────────────────────────────
def ensure_extract_apply_jobs(scan, run) -> list[ExternalJob]:
    """Return the Mistral rows of one apply run, creating them if none.

    The pages a curator inserted, replaced or turned have no address in
    the original, so the volume read does not cover them: they are
    one-page shards of their own, under ``jobs/apply/pages/e{pk}.pdf``
    (#224). This is where they are read.

    Created here rather than in ``apply._ensure_rows`` for two reasons.
    The read starts long after the build -- by hand today, and after
    the second review once #336 lands -- so the build usually has no
    Mistral run to join. And ``_ensure_rows`` refuses the whole
    corrected volume when a stage it needs is off
    (``apply.GateClosedError``), which must never happen for a stage
    an environment may simply not pay for.

    Idempotent, like every ``ensure_*`` of this module: a second call
    hands back the live rows, and the carry gives a replacement run the
    results already paid for.

    :param scan: The scan.
    :param run: A built, standing ``ApplyRun``.
    :returns: The rows, or an empty list for a run with no edited page.
    :rtype: list[ExternalJob]
    """
    from scanning import apply

    manifest = apply.stored_shard_manifest(scan, run)
    if not manifest["shards"]:
        return []
    return ensure_extract_jobs(scan, manifest, apply_run=run)


def apply_jobs(scan, run) -> list[ExternalJob]:
    """Return one apply run's live Mistral rows, in shard order.

    :param scan: The scan.
    :param run: The apply run.
    :returns: The rows, or an empty list.
    :rtype: list[ExternalJob]
    """
    return jobs.live_run(
        scan, JobStage.EXTRACT, JobEngine.MISTRAL_OCR, apply_run=run
    )


def apply_glue_due(run, rows: list[ExternalJob], volume_run: int) -> bool:
    """Return whether the corrected volume's Mistral document can be
    written now.

    The one rule, called by the pass that writes it and by the command
    that writes it again. Four things must hold: the run is built; the
    volume run is glued (the caller's own test, passed in as its run
    number); no row of this run's own read is unstarted or dead, the
    way ``apply.glues_due`` judges a stage; and the document that
    stands is not this volume run's already.

    :param run: The standing ``ApplyRun``.
    :param rows: The run's Mistral rows (:func:`apply_jobs`).
    :param volume_run: The glued volume run's number.
    :returns: Whether to write the document.
    :rtype: bool
    """
    from scanning import apply

    if not run.is_built:
        return False
    if any(row.status in apply.BLOCKING_JOB_STATUSES for row in rows):
        return False
    return not (run.extract_key and run.extract_run == volume_run)


def glue_apply_run(scan, run, rows: list[ExternalJob], volume_run: int) -> str:
    """Write the corrected volume's Mistral document, and name it on the
    run.

    The walk of ``apply._glue_ocr``, over the same page map: a kept
    page comes from the volume document, an edited page from that
    edit's own one-page result, every page is renumbered to its final
    page, and a page the map does not name -- a page a curator deleted
    -- is simply not walked. So the document is in the corrected
    volume's page space, and the deleted pages are gone from it.

    A run with no structural edit aliases the volume document rather
    than copying it, as the OCR glue does for the same case.

    :param scan: The scan.
    :param run: The built, standing run.
    :param rows: The run's Mistral rows.
    :param volume_run: The glued volume run's number.
    :returns: The key the document lives at.
    :rtype: str
    :raises MistralGlueError: If an input is missing or the upload
        failed.
    """
    from scanning import apply

    volume_key = glued_result_key(scan, volume_run)
    volume = s3_sync.download_json_object(volume_key)
    if not isinstance(volume, dict) or not volume.get("pages"):
        raise MistralGlueError(
            f"scan {scan.pk}: the glued Mistral document at {volume_key} "
            f"has no pages"
        )
    page_map = run.page_map or {}
    if apply.is_identity_map(page_map):
        return volume_key

    by_pdf_page = {page["pdf_page"]: page for page in volume["pages"]}
    read = apply._rows_by_edit(rows, JobStage.EXTRACT)
    edit_pages: dict[int, dict[int, dict]] = {}
    for edit_id, row in read.items():
        payload = apply._result_payload(scan, row, ACTION)
        edit_pages[edit_id] = parse_payload(payload)

    pages = []
    for entry in page_map["pages"]:
        source = entry["source"]
        if source["kind"] == "original":
            page = by_pdf_page.get(source["pdf_page"])
            if page is None:
                raise MistralGlueError(
                    f"the Mistral volume document of scan {scan.pk} has no "
                    f"page {source['pdf_page']}"
                )
            page = dict(page)
        else:
            page = edit_pages.get(source["edit_id"], {}).get(source["page"])
            page = (
                dict(page)
                if page is not None
                else {
                    "md": "",
                    "blocks": [],
                    "error": "not read: no result for this page",
                }
            )
        page.pop("shard_index", None)
        page.pop("page_no", None)
        page["page_index"] = entry["final_page"] - 1
        page["pdf_page"] = entry["final_page"]
        page["source"] = source
        pages.append(page)

    document = {
        "schema_version": GLUE_SCHEMA_VERSION,
        "engine": str(JobEngine.MISTRAL_OCR),
        "action": ACTION,
        "scan_pk": scan.pk,
        "run": volume_run,
        "apply_run": run.label,
        "source_page_count": page_map["final_page_count"],
        "source_fingerprint": run.source_fingerprint,
        "model": volume.get("model"),
        "render": volume.get(
            "render",
            {"width": RENDER_W, "height": RENDER_H, "source": SOURCE},
        ),
        "generated_at": timezone.now().isoformat(),
        "pages": pages,
        "failed_pages": _failed_pages(pages, "page_index"),
    }
    key = f"{apply.run_prefix(scan, run)}extract-volume.json"
    if not s3_sync.upload_json_object(key, document):
        raise MistralGlueError(
            f"scan {scan.pk}: the corrected volume's Mistral document "
            f"could not be uploaded to {key}"
        )
    return key


def _glue_one_apply(scan, run, volume_rows: list[ExternalJob]) -> bool:
    """Create the rows of one standing run, and glue it when it is due.

    :param scan: The scan.
    :param run: The standing run.
    :param volume_rows: The glued volume run's rows.
    :returns: Whether the document was written.
    :rtype: bool
    """
    from scanning.models import ApplyRun

    volume_run = volume_rows[0].run
    rows = apply_jobs(scan, run)
    if not rows:
        rows = ensure_extract_apply_jobs(scan, run)
    if not apply_glue_due(run, rows, volume_run):
        return False
    key = glue_apply_run(scan, run, rows, volume_run)
    ApplyRun.objects.filter(pk=run.pk).update(
        extract_key=key, extract_run=volume_run
    )
    run.extract_key, run.extract_run = key, volume_run
    jobs.consume_run(rows)
    logger.info(
        "Glued the Mistral read of scan %s into %s for apply run %s "
        "(volume run %s, %d edited page(s))",
        scan.pk,
        key,
        run.label,
        volume_run,
        len(rows),
    )
    return True


def _apply_candidates() -> dict[int, int]:
    """Return the scans that may owe a corrected volume, in three queries.

    The pre-check of :func:`finish_ready_applies`, over the whole
    corpus at once rather than scan by scan. Every volume ever read
    keeps a glued run for good, so a per-scan check would cost two
    queries for each of them on every tick, growing with the corpus --
    the fault ``apply._candidate_scan_ids`` was written to avoid.

    Each question is one query: the live volume run per scan, the runs
    that are not glued yet, and the standing built apply runs. A run
    whose document already names the live volume run drops out here,
    and :func:`finish_ready_applies` then judges the rest exactly.

    :returns: ``{scan pk: (the standing run, the glued volume run's
        number)}``.
    :rtype: dict[int, tuple]
    """
    from django.db.models import Max

    from scanning.models import ApplyRun

    rows = ExternalJob.objects.filter(
        stage=JobStage.EXTRACT,
        engine=JobEngine.MISTRAL_OCR,
        provider=JobProvider.MISTRAL,
        apply_run__isnull=True,
    )
    live = {
        entry["scan_id"]: entry["run"]
        for entry in rows.values("scan_id").annotate(run=Max("run"))
    }
    if not live:
        return {}
    # A run with a row of any other status is not glued: the glue
    # writes the document and flips every row in one pass.
    for scan_id, run in rows.exclude(status=JobStatus.CONSUMED).values_list(
        "scan_id", "run"
    ):
        if live.get(scan_id) == run:
            live.pop(scan_id, None)
    if not live:
        return {}
    candidates = {}
    for run in ApplyRun.objects.filter(
        scan_id__in=list(live),
        superseded_at__isnull=True,
        built_at__isnull=False,
    ):
        volume_run = live[run.scan_id]
        if run.extract_key and run.extract_run == volume_run:
            continue
        candidates[run.scan_id] = (run, volume_run)
    return candidates


def finish_ready_applies() -> int:
    """Read the edited pages of every corrected volume, and glue them.

    The second pass of this stage on the collect tick. For every scan
    whose live Mistral volume run is glued and whose standing apply run
    is built, it creates the run's own one-page rows if it has none,
    and writes the corrected volume's document once every row has
    answered.

    **A volume nobody read with Mistral pays nothing here.** The
    candidate is the glued volume run, which only a person starts
    (``views_process.start_mistral_ocr``), so this pass creates a paid
    row only for a volume somebody chose to read.

    Not part of ``apply.glues_due`` on purpose (#245). That trigger
    takes a scan in ``PAGE_COMPLETENESS_REVIEW_DONE`` alone, and this
    read starts later than that status -- by hand today, and after the
    second review once #336 lands -- so an arm there would miss the
    normal case. The work is seconds of JSON over stored objects, which
    is what the collect tick is for.

    Unlike :func:`finish_ready_runs` this pass does ask
    :func:`enabled`, because it may create a row, and a row is
    spending. So an environment with no key writes no corrected
    volume's document either, even for the identity run that would
    need no row; the tick writes it as soon as the key returns.

    :returns: How many corrected volumes were glued.
    :rtype: int
    """
    from scanning.models import Scan

    if not s3_sync.s3_active() or not enabled():
        return 0

    candidates = _apply_candidates()
    if not candidates:
        return 0
    glued = 0
    for scan in Scan.objects.filter(pk__in=list(candidates)).select_related(
        "reporter"
    ):
        volume_rows = live_extract_jobs(scan)
        if not volume_rows or any(
            row.status != JobStatus.CONSUMED for row in volume_rows
        ):
            continue
        run, _volume_run = candidates[scan.pk]
        name = run.label
        if int(_glue_state(volume_rows, name).get("attempts") or 0) >= (
            GLUE_MAX_ATTEMPTS
        ):
            continue
        try:
            if _glue_one_apply(scan, run, volume_rows):
                glued += 1
        except Exception as exc:
            _record_glue_failure(scan, volume_rows, name, exc)

    return glued
