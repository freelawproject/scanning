"""The Mistral OCR stage (issue #191): rows, render, submit, poll, harvest.

One :class:`~scanning.models.ExternalJob` row per **original** shard at
``EXTRACT``/``MISTRAL_OCR``/``MISTRAL``, and one Mistral batch job per
row. The daemon does the intermediate step Mistral needs: it renders
every page of the shard the way every engine of the ai-research
ensemble saw it (``pipeline/core/render.py`` on the ``extraction_align``
branch -- 1700x2200 RGB, the redaction rects painted black), uploads
each page as an ``ocr`` file, uploads a JSONL manifest naming them, and
creates the batch. The confirm tick polls the batch, and on ``SUCCESS``
downloads the output and stores it, **whole**, at the row's
``result_key``.

This module is the Mistral entry of :func:`jobs._providers`. It uses
the lifecycle primitives of :mod:`scanning.jobs` -- the claim, the
compare-and-swap writes, the retry ledger -- rather than copying them,
which is what the provider table exists for. What is specific to
Mistral, and must not be broken:

- **The harvest stores every byte Mistral returned, and nothing else
  transforms it.** The result document holds the output lines and the
  error lines as they came, plus the batch job object. ``markdown``,
  ``blocks``, ``images``, ``tables``, ``usage_info`` and whatever a
  later model adds all land in S3. The glue (a follow-up) is where
  ``parse()`` and every other transform run, so a better transform is a
  re-glue at no API cost, never a re-paid read.
- **The render is the branch's, line for line.** RGB, ``zoom = 1700 /
  page width``, a resize to exactly 1700x2200, every rect black
  whatever its ``fill`` says: the downstream reads detect a redaction
  by its black placeholder. Our rects are 200 dpi pixels and the
  branch's space is 1700x2200; those are one space on a US letter page
  and the rect is scaled on any other.
- **The rects are part of the shard identity** (``rects_digest`` in
  ``input_manifest``): a curator who moves a box changes the identity,
  so the next run re-reads that shard alone and the carry keeps the
  rest.
- **The deadline is Mistral's own timeout**, stamped once at the first
  claim and never restamped on ``RUNNING``: a batch is queued and run
  inside one budget Mistral enforces.
- **A job nothing will read is cancelled, and its files deleted.**
  Every page image, the manifest and the two output files live at
  Mistral until we delete them, so every path that writes a row off
  deletes what it uploaded.
- **The source is the original scan, never the bitonal copy.** Rachel's
  tests all ran on non-bitonal images (#case-law-extraction,
  2026-08-04), and the shards are cut from the original. ``SOURCE`` is
  a constant with no per-row override yet, because the bitonal copy is
  one merged file with no shard set to read.
"""

from __future__ import annotations

import hashlib
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
from PIL import Image, ImageDraw

from scanning import jobs, mistral_client, runpod_client, s3_sync
from scanning.models import (
    DEAD_JOB_STATUSES,
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
#: bbox of every engine lives in this pixel space.
RENDER_W = 1700
RENDER_H = 2200

#: The resolution ``Scan.redaction_rects`` are measured at
#: (``blackletter.scanner.DPI``). US letter at this dpi is exactly
#: ``RENDER_W`` x ``RENDER_H``, which is why the two spaces are one on
#: the pages the corpus is made of.
DPI = 200

#: What the pages are rendered from. See the module docstring.
SOURCE = "original"

#: What every manifest line asks for, as the branch did.
INCLUDE_BLOCKS = True
TABLE_FORMAT = "html"

#: Batch jobs in flight at once, not pages. A debug guard on blast
#: radius, not a cost control: Mistral bills per page whatever the
#: parallelism.
MAX_CONCURRENCY = 4

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

#: Prefix of the scratch directory one submission renders in, so the
#: cleanup sweep can reclaim one a SIGKILL orphans.
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


def page_rects(scan) -> dict[int, list[dict]]:
    """Return the redaction rects of a scan, by physical page index.

    The one reader of where the rects live, so #241 (rects as rows)
    changes one function. Pages with no rect are absent.

    :param scan: The scan.
    :returns: ``{page_index: [{x0, y0, x1, y1, ...}, ...]}`` in 200 dpi
        pixels.
    :rtype: dict[int, list[dict]]
    """
    by_page: dict[int, list[dict]] = {}
    for entry in scan.redaction_rects or []:
        if not isinstance(entry, dict):
            continue
        index = entry.get("page_index")
        rects = entry.get("rects") or []
        if isinstance(index, int) and rects:
            by_page[index] = list(rects)
    return by_page


def rects_digest(
    rects_by_page: dict[int, list[dict]], from_page: int, to_page: int
) -> str:
    """Return a digest of the rects painted onto one shard's pages.

    Part of the shard identity: the input Mistral reads is the shard
    plus these rects, so a moved box is a changed shard. Stable across
    key order, and it names the page of each rect, so a rect that
    moves to another page changes it too.

    :param rects_by_page: From :func:`page_rects`.
    :param from_page: First page of the shard (fitz index).
    :param to_page: Last page of the shard (fitz index).
    :returns: A hex sha256.
    :rtype: str
    """
    payload = {
        str(index): rects_by_page[index]
        for index in range(from_page, to_page + 1)
        if index in rects_by_page
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _rects_identity(scan, identity: dict) -> dict:
    """Return the identity keys this engine adds to one shard.

    :param scan: The scan the shard belongs to.
    :param identity: The shard's base identity (pages, size).
    :returns: ``{"rects_digest": ...}``.
    :rtype: dict
    """
    return {
        "rects_digest": rects_digest(
            page_rects(scan), identity["from_page"], identity["to_page"]
        )
    }


def ensure_extract_jobs(
    scan, manifest: dict, *, force_new_run: bool = False
) -> list[ExternalJob]:
    """Return the live Mistral jobs for ``scan``, creating them if the
    current run does not describe today's shard set and rects.

    Idempotent, so a second press of the button is a no-op rather than
    a second run over pages already read. A run holding a dead row is
    replaced. A replacement run carries every shard whose identity --
    bytes *and* rects -- is unchanged and whose result object is still
    on S3, so a curator who moves one box re-pays one shard.

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
        extend_identity=_rects_identity,
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
def render_page(page: fitz.Page, rects: list[dict]) -> bytes:
    """Render one page as the ensemble's canonical PNG.

    ``pipeline/core/render.py`` line for line: zoom the page to
    ``RENDER_W`` wide, render RGB with no alpha, resize to exactly
    ``RENDER_W`` x ``RENDER_H`` when the page is another size, then
    paint every rect black. One addition: the rects arrive in 200 dpi
    pixels of the page's own size, so they are scaled into the render's
    space first -- a no-op on a letter page, and the difference between
    a redaction and a black box beside it on any other.

    :param page: The fitz page.
    :param rects: ``[{x0, y0, x1, y1, ...}, ...]`` in 200 dpi pixels.
    :returns: PNG bytes.
    :rtype: bytes
    """
    zoom = RENDER_W / page.rect.width
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
    img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    if img.size != (RENDER_W, RENDER_H):
        img = img.resize((RENDER_W, RENDER_H))
    if rects:
        width_px = page.rect.width * DPI / 72
        height_px = page.rect.height * DPI / 72
        scale_x = RENDER_W / width_px
        scale_y = RENDER_H / height_px
        draw = ImageDraw.Draw(img)
        for rect in rects:
            draw.rectangle(
                [
                    float(rect["x0"]) * scale_x,
                    float(rect["y0"]) * scale_y,
                    float(rect["x1"]) * scale_x,
                    float(rect["y1"]) * scale_y,
                ],
                fill=(0, 0, 0),
            )
    buffer = io.BytesIO()
    img.save(buffer, format="PNG")
    return buffer.getvalue()


def render_shard_pages(
    pdf_path: Path, rects_by_page: dict[int, list[dict]], from_page: int
) -> Iterator[tuple[int, bytes]]:
    """Render every page of one shard, in order.

    :param pdf_path: The downloaded shard.
    :param rects_by_page: From :func:`page_rects`, keyed by *volume*
        page index.
    :param from_page: The volume page the shard starts at.
    :returns: ``(page_no, png)`` per page, ``page_no`` counted from
        zero inside the shard.
    :rtype: Iterator[tuple[int, bytes]]
    """
    with fitz.open(str(pdf_path)) as doc:
        for page_no, page in enumerate(doc):
            rects = rects_by_page.get(from_page + page_no, [])
            yield page_no, render_page(page, rects)


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


def _prepare_and_submit(
    job: ExternalJob, rects_by_page: dict[int, list[dict]]
) -> _Submission:
    """Render, upload and submit one shard. Runs on a worker thread.

    No database access here: the row's fields were read on the main
    thread, and every write happens there afterwards. On any failure
    the files uploaded so far are deleted, because nothing will name
    them: a failed create left no job, and a lost create left a job
    nothing we hold names.

    :param job: The claimed row.
    :param rects_by_page: The scan's rects, from :func:`page_rects`.
    :returns: The submission.
    :raises Exception: Whatever the download, the render or the API
        raised; classified by :func:`_apply_outcome`.
    """
    started = time.monotonic()
    manifest = job.input_manifest or {}
    from_page = int(manifest.get("from_page") or 0)
    model = model_for(job)
    files: list[str] = []
    try:
        with tempfile.TemporaryDirectory(prefix=RENDER_TMP_PREFIX) as tmp:
            pdf_path = Path(tmp) / "shard.pdf"
            s3_sync.download_object(job.input_key, pdf_path)
            lines = []
            for page_no, png in render_shard_pages(
                pdf_path, rects_by_page, from_page
            ):
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

    :param summary: Counts to update.
    :param limit: Concurrency override; defaults to
        :data:`MAX_CONCURRENCY`.
    :return: None.
    """
    ours = ExternalJob.objects.filter(
        provider=JobProvider.MISTRAL,
        stage=JobStage.EXTRACT,
        engine=JobEngine.MISTRAL_OCR,
    )
    room = jobs._room_for(ours, int(limit or MAX_CONCURRENCY), "Mistral OCR")
    if not room:
        return
    pending = jobs._pending_slice(ours, room)
    if not pending:
        return

    now = timezone.now()
    claimed = jobs._claim_wave(pending, now, summary)
    if not claimed:
        return

    # Read on this thread: the rects come off the scan row.
    rects = {job.pk: page_rects(job.scan) for job, _ in claimed}
    logger.info(
        "rendering and submitting %d shard(s) to Mistral", len(claimed)
    )
    with ThreadPoolExecutor(max_workers=len(claimed)) as pool:
        futures = [
            (job, pool.submit(_prepare_and_submit, job, rects[job.pk]))
            for job, _ in claimed
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
def _count(summary: jobs.SweepSummary, result: str) -> None:
    """Add one retry or failure outcome to a sweep summary.

    :param summary: Counts to update.
    :param result: What ``_retry_or_fail`` or ``_fail`` answered.
    :return: None.
    """
    if result in ("retried", "failed"):
        setattr(summary, result, getattr(summary, result) + 1)


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
        _count(
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

    if outcome.status is None:
        # We learned nothing, which is not the same as learning it
        # failed. The deadline check below still ends a row whose
        # status call is permanently unhappy.
        jobs._write(job, last_polled_at=now)
        summary.pending += 1
    elif outcome.status == JobStatus.COMPLETED:
        if harvest(job, outcome, now):
            summary.completed += 1
        else:
            # The output is still at Mistral; the next tick downloads
            # it again. Counted as a check error, like an S3 blip.
            summary.errors += 1
        return
    elif outcome.status in DEAD_JOB_STATUSES:
        if outcome.retriable:
            result = jobs._retry_or_fail(
                job, outcome.error_code, outcome.error_message, now
            )
        else:
            result = (
                "failed"
                if jobs._fail(job, outcome.error_code, outcome.error_message)
                else "skipped"
            )
        _count(summary, result)
        return
    else:
        meta = dict(job.provider_meta or {})
        meta["progress"] = {
            "status": outcome.provider_status,
            "total": outcome.total,
            "succeeded": outcome.succeeded,
            "failed": outcome.failed,
        }
        jobs._write(
            job,
            status=outcome.status,
            last_polled_at=now,
            provider_meta=meta,
        )
        summary.pending += 1

    if job.is_overdue(now):
        summary.pending -= 1
        _count(
            summary,
            jobs._retry_or_fail(
                job,
                "DEADLINE_EXCEEDED",
                f"still {outcome.provider_status or job.status} at "
                f"{job.deadline}",
                now,
            ),
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
        "schema_version": runpod_client.RESULT_SCHEMA_VERSION,
        "action": ACTION,
        "scan_pk": job.scan_id,
        "result_key": job.result_key,
        "payload": {
            # Verbatim: what Mistral wrote, line for line.
            "output": output,
            "errors": errors,
            "batch": outcome.job,
            # What was sent, so a reader knows the space the bboxes
            # are in and what was painted onto the pages.
            "model": model_for(job),
            "render": {
                "width": RENDER_W,
                "height": RENDER_H,
                "dpi": DPI,
                "source": SOURCE,
                "redactions": "black",
                "rects_digest": (job.input_manifest or {}).get("rects_digest"),
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
    # The files are about to be deleted, so the row stops naming them.
    job.provider_meta = {
        key: value
        for key, value in (job.provider_meta or {}).items()
        if key != "files"
    }
    if not jobs._complete(job, summary, now):
        return False
    _delete_files(
        files + [f for f in (outcome.output_file, outcome.error_file) if f]
    )
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
