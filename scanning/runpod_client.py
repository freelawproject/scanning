"""RunPod Serverless client for offloading GPU steps.

Two public entry points: :func:`detect` and :func:`analyze`. Both take
a local ``pdf_path`` and, when ``settings.RUNPOD_ENABLED`` is true,
ship the work out to a RunPod Serverless endpoint; otherwise they
fall back to calling blackletter in-process so dev / tests / staging
keep working without RunPod credentials.

Remote mode flow:

1. Upload the PDF to S3 under the scan's processing prefix if it's
   not already there (idempotent via ``head_object``).
2. Generate a presigned GET URL.
3. ``POST /run`` to the RunPod endpoint with
   ``{"input": {"action": ..., "pdf_url": ..., ...}}``.
4. Poll ``GET /status/{id}`` with exponential backoff until a
   terminal state (``COMPLETED`` / ``FAILED`` / ``TIMED_OUT`` /
   ``CANCELLED``). On timeout, ``POST /cancel/{id}`` so we stop
   paying.
5. Return the handler's ``output`` dict.

Payload size budget (see RUNPOD_MIGRATION_PLAN.md §5): ~2 MB for
detect on a 500-page volume, ~0.5 MB for analyze. Well under
RunPod's ~20 MB response cap.
"""

from __future__ import annotations

import logging
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

import boto3
import requests
from botocore.exceptions import ClientError
from django.conf import settings

if TYPE_CHECKING:
    from scanning.models import Scan

logger = logging.getLogger(__name__)


class RunpodError(RuntimeError):
    """Raised on terminal RunPod failure or exhausted retries."""


class RunpodTransientError(RunpodError):
    """Retriable RunPod failure.

    Callers in ``scanning/services.py`` catch this specifically and
    re-queue the scan to ``Status.QUEUED`` so the next daemon tick
    retries on a (hopefully) different worker. Contrast with the base
    ``RunpodError`` which signals a terminal failure worth marking
    the scan as ``ERROR``.
    """


# Error codes the handler emits that the client translates into a
# ``RunpodTransientError`` (retry) rather than ``RunpodError`` (fail).
_TRANSIENT_ERROR_CODES = {"NO_GPU"}


# Friendly labels for progress-callback strings shown in the UI. The
# raw action names (``detect`` / ``analyze``) are what the client
# sends on the wire; the labels here are what scanners see.
_ACTION_LABELS = {
    "detect": "YOLO (detect)",
    "analyze": "OCR (analyze)",
}


def _redact_pdf_url(body: dict[str, Any]) -> dict[str, Any]:
    """Return a shallow copy of the RunPod submit body with any
    ``pdf_url`` under ``input`` replaced by ``***``.

    Presigned GET URLs grant time-limited read access to a single S3
    object. They should never land in persistent logs (even at DEBUG),
    so we mask them before passing the body to ``logger``.

    :param body: The ``{"input": {...}}`` dict about to be POSTed.
    :returns: A masked shallow copy safe to log.
    :rtype: dict
    """
    redacted = {**body}
    inp = redacted.get("input")
    if isinstance(inp, dict) and "pdf_url" in inp:
        redacted["input"] = {**inp, "pdf_url": "***"}
    return redacted


# ── Public API ──────────────────────────────────────────────────────
ProgressCallback = Callable[[int | None, int | None, str], None]


def detect(
    scan: Scan,
    pdf_path: str | Path,
    models: list[str] | None = None,
    confidence: float = 0.20,
    progress_callback: ProgressCallback | None = None,
) -> list[dict]:
    """Run YOLO detection on a PDF, remotely or in-process.

    :param scan: The Scan owning this job (used for S3 pathing and
        Sentry tagging on the worker side).
    :param pdf_path: Local filesystem path to the input PDF. In
        remote mode the file is uploaded to S3 under the scan's
        processing prefix if it's not already there.
    :param models: YOLO model sizes to run. Defaults to all three.
    :param confidence: Minimum confidence threshold.
    :param progress_callback: Optional
        ``callable(current, total, message)`` for progress updates.
        Remote mode emits ``(None, None, status)`` for a handful of
        coarse events (submit / queued / state). Local mode doesn't
        fire the callback (``blackletter.api.detect`` has no callback
        parameter; its progress is printed to stdout, where the
        caller can capture it if needed).
    :returns: Merged detection list (same shape as
        ``blackletter.api.detect``'s return value).
    :rtype: list[dict]
    :raises RunpodError: On terminal remote failure.
    """
    models = models or ["small", "medium", "large"]

    if not _remote_enabled():
        return _detect_local(pdf_path, models, confidence)

    pdf_url = _ensure_presigned_url(scan, pdf_path)
    result = _invoke(
        action="detect",
        scan=scan,
        payload={
            "pdf_url": pdf_url,
            "models": models,
            "confidence": confidence,
        },
        progress_callback=progress_callback,
    )
    return result["detections"]


def analyze(
    scan: Scan,
    pdf_path: str | Path,
    exp_start: int | None,
    exp_end: int | None,
    max_pages: int = 9999,
    num_workers: int | None = None,
    progress_callback: ProgressCallback | None = None,
) -> dict:
    """Run PaddleOCR + YOLO page-number analysis on a PDF.

    Always returns ``{"results": [...]}`` regardless of mode; the
    daemon recomputes ``seq_issues`` / ``duplicates`` /
    ``missing_pages`` from ``results`` via
    ``_rebuild_issues_from_results``, so the extra fields aren't
    needed over the wire.

    :param scan: The Scan owning this job.
    :param pdf_path: Local filesystem path to the input PDF.
    :param exp_start: Expected first page number (or ``None``).
    :param exp_end: Expected last page number (or ``None``).
    :param max_pages: Upper bound on pages to process.
    :param num_workers: ``multiprocessing.Pool`` size for local mode.
        Ignored in remote mode (the worker always runs with
        ``num_workers=1`` to avoid the paddle+fork segfault).
    :param progress_callback: Optional
        ``callable(current, total, message)``. In local mode the
        callback is passed through to blackletter and fires once per
        page; in remote mode only coarse ``(None, None, status)``
        events fire (submit / queued / state) because there's no
        per-page stream across the HTTP boundary.
    :returns: ``{"results": list[dict]}``.
    :rtype: dict
    :raises RunpodError: On terminal remote failure.
    """
    if not _remote_enabled():
        return _analyze_local(
            pdf_path,
            exp_start,
            exp_end,
            max_pages,
            num_workers=num_workers,
            progress_callback=progress_callback,
        )

    pdf_url = _ensure_presigned_url(scan, pdf_path)
    result = _invoke(
        action="analyze",
        scan=scan,
        payload={
            "pdf_url": pdf_url,
            "exp_start": exp_start,
            "exp_end": exp_end,
            "max_pages": max_pages,
        },
        progress_callback=progress_callback,
    )
    return {"results": result["results"]}


def ocr(
    scan: Scan,
    pdf_path: str | Path,
    output_dir: str | Path,
    reporter: str = "",
    volume: str = "",
    first_page: int = 1,
    language: str = "eng",
    progress_callback: ProgressCallback | None = None,
) -> str:
    """Add a text layer to a PDF, remotely (PaddleOCR/GPU) or locally.

    In remote mode the input PDF is uploaded to S3 (if needed), OCR'd on
    the GPU worker with PaddleOCR, and the worker uploads the result back
    to the scan's processing prefix via a presigned PUT; this function
    then downloads it into *output_dir*. In local mode it falls back to
    in-process Tesseract OCR (PaddleOCR on CPU is unreliable; the GPU
    worker is the supported PaddleOCR path).

    :param scan: The Scan owning this job.
    :param pdf_path: Local path to the input (bitonal) PDF.
    :param output_dir: Directory the OCR'd PDF is written into.
    :param reporter: Reporter short name (for the output filename).
    :param volume: Volume number (for the output filename).
    :param first_page: First logical page number (for the filename).
    :param language: OCR language code.
    :param progress_callback: Optional ``callable(current, total, msg)``.
        Remote mode emits only coarse ``(None, None, status)`` events.
    :returns: Local filesystem path to the OCR'd PDF.
    :rtype: str
    :raises RunpodError: On terminal remote failure.
    """
    import fitz

    output_dir = Path(output_dir)
    with fitz.open(str(pdf_path)) as doc:
        pages = doc.page_count
    last_page = first_page + pages - 1
    parts = [
        p
        for p in [reporter, str(volume), str(first_page), str(last_page)]
        if p
    ]
    stem = ".".join(parts) if parts else Path(pdf_path).stem
    output_name = f"{stem}.pdf"

    if not _remote_enabled():
        return _ocr_local(
            pdf_path,
            output_dir,
            reporter,
            volume,
            first_page,
            language,
            progress_callback,
        )

    pdf_url = _ensure_presigned_url(scan, pdf_path)
    output_url, output_key = _presigned_put_url(scan, output_name)
    _invoke(
        action="ocr",
        scan=scan,
        payload={
            "pdf_url": pdf_url,
            "output_url": output_url,
            "language": language,
        },
        progress_callback=progress_callback,
    )
    # The worker uploaded the OCR'd PDF to output_key; pull it down so
    # downstream pipeline steps (pairing, redaction) can open it locally.
    bucket = settings.AWS_PRIVATE_STORAGE_BUCKET_NAME
    dest = output_dir / output_name
    boto3.client("s3").download_file(bucket, output_key, str(dest))
    return str(dest)


# ── Local fallback ──────────────────────────────────────────────────
def _remote_enabled() -> bool:
    """Return True if remote RunPod dispatch should be used.

    :returns: Whether the remote path is active.
    :rtype: bool
    """
    return bool(settings.RUNPOD_ENABLED)


def _detect_local(
    pdf_path: str | Path, models: list[str], confidence: float
) -> list[dict]:
    """In-process fallback for :func:`detect`.

    Uses a scratch ``TemporaryDirectory`` for blackletter's
    ``detections.json`` side-effect; the caller gets the return value
    only, so the tmp directory is disposed after.

    :param pdf_path: Local PDF path.
    :param models: YOLO model sizes.
    :param confidence: Confidence threshold.
    :returns: Detection list.
    :rtype: list[dict]
    """
    from blackletter.api import detect as bl_detect

    with tempfile.TemporaryDirectory(prefix="detect-local-") as tmp:
        return bl_detect(
            str(pdf_path), tmp, models=models, confidence=confidence
        )


def _analyze_local(
    pdf_path: str | Path,
    exp_start: int | None,
    exp_end: int | None,
    max_pages: int,
    num_workers: int | None,
    progress_callback: ProgressCallback | None,
) -> dict:
    """In-process fallback for :func:`analyze`.

    :param pdf_path: Local PDF path.
    :param exp_start: Expected first page number.
    :param exp_end: Expected last page number.
    :param max_pages: Upper bound on pages to process.
    :param num_workers: Pool size (passed through to blackletter).
    :param progress_callback: Per-page callback (passed through).
    :returns: ``{"results": list[dict]}``.
    :rtype: dict
    """
    from blackletter.analyze import analyze_pdf as bl_analyze_pdf

    kwargs: dict[str, Any] = {
        "exp_start": exp_start,
        "exp_end": exp_end,
        "max_pages": max_pages,
    }
    if num_workers is not None:
        kwargs["num_workers"] = num_workers
    if progress_callback is not None:
        kwargs["progress_callback"] = progress_callback

    result = bl_analyze_pdf(str(pdf_path), **kwargs)
    return {"results": result["results"]}


def _ocr_local(
    pdf_path: str | Path,
    output_dir: Path,
    reporter: str,
    volume: str,
    first_page: int,
    language: str,
    progress_callback: ProgressCallback | None,
) -> str:
    """In-process fallback for :func:`ocr` (Tesseract; no GPU needed).

    Uses Tesseract rather than PaddleOCR: PaddleOCR on CPU is unreliable
    (oneDNN crash), and the GPU worker is the supported Paddle path.

    :param pdf_path: Local input PDF path.
    :param output_dir: Directory the OCR'd PDF is written into.
    :param reporter: Reporter short name (for the output filename).
    :param volume: Volume number (for the output filename).
    :param first_page: First logical page number (for the filename).
    :param language: OCR language code.
    :param progress_callback: Optional per-page callback.
    :returns: Local filesystem path to the OCR'd PDF.
    :rtype: str
    """
    from blackletter.api import ocr as bl_ocr

    out = bl_ocr(
        str(pdf_path),
        str(output_dir),
        reporter=reporter,
        volume=volume,
        first_page=first_page,
        language=language,
        engine="tesseract",
        progress_callback=progress_callback,
    )
    return str(out)


# ── Remote: S3 presigning ───────────────────────────────────────────
def _ensure_presigned_url(scan: Scan, pdf_path: str | Path) -> str:
    """Upload ``pdf_path`` to the scan's S3 processing prefix if
    missing, then return a presigned GET URL.

    Uses the PDF's basename as the S3 key suffix, so if the file is
    already under the scan's processing prefix (the common case for
    bitonal.pdf / *.original.pdf), the ``head_object`` check succeeds
    and no upload happens.

    :param scan: The Scan owning the file.
    :param pdf_path: Local filesystem path.
    :returns: Presigned GET URL.
    :rtype: str
    :raises FileNotFoundError: If the file isn't in S3 and isn't
        present locally.
    :raises RunpodError: If S3 credentials are missing.
    """
    from scanning import s3_sync
    from scanning.utils import has_s3_credentials

    if not has_s3_credentials():
        raise RunpodError(
            "RUNPOD_ENABLED is true but no AWS credentials are configured; "
            "cannot generate a presigned URL for the worker."
        )

    bucket = settings.AWS_PRIVATE_STORAGE_BUCKET_NAME
    key = f"{s3_sync.s3_processing_prefix(scan)}{Path(pdf_path).name}"
    s3 = boto3.client("s3")

    # Only "the object isn't there yet" should trigger an upload. Any
    # other ClientError (bad credentials, wrong region, access denied,
    # bucket missing) means S3 is misconfigured and must surface, not
    # get swallowed by a fall-through to upload_file().
    try:
        s3.head_object(Bucket=bucket, Key=key)
    except ClientError as exc:
        err_code = exc.response.get("Error", {}).get("Code", "")
        if err_code not in ("404", "NoSuchKey"):
            raise
        local = Path(pdf_path)
        if not local.is_file():
            raise FileNotFoundError(
                f"{pdf_path} not present locally and missing from "
                f"s3://{bucket}/{key}; cannot run GPU step."
            ) from exc
        logger.info(
            "uploading %s to s3://%s/%s before presign",
            local.name,
            bucket,
            key,
        )
        s3.upload_file(str(local), bucket, key)

    ttl = int(settings.RUNPOD_PRESIGNED_TTL)
    return s3.generate_presigned_url(
        "get_object",
        Params={"Bucket": bucket, "Key": key},
        ExpiresIn=ttl,
    )


def _presigned_put_url(scan: Scan, output_name: str) -> tuple[str, str]:
    """Return a presigned PUT URL + key for an output under the scan's
    S3 processing prefix.

    The worker uploads the OCR'd PDF to this URL (it has no AWS
    credentials of its own). The Content-Type is pinned to
    ``application/pdf`` and must match the worker's PUT header.

    :param scan: The Scan owning the file.
    :param output_name: Basename for the output object.
    :returns: ``(presigned_put_url, s3_key)``.
    :rtype: tuple[str, str]
    :raises RunpodError: If S3 credentials are missing.
    """
    from scanning import s3_sync
    from scanning.utils import has_s3_credentials

    if not has_s3_credentials():
        raise RunpodError(
            "RUNPOD_ENABLED is true but no AWS credentials are configured; "
            "cannot presign an upload URL for the worker."
        )

    bucket = settings.AWS_PRIVATE_STORAGE_BUCKET_NAME
    key = f"{s3_sync.s3_processing_prefix(scan)}{output_name}"
    ttl = int(settings.RUNPOD_PRESIGNED_TTL)
    url = boto3.client("s3").generate_presigned_url(
        "put_object",
        Params={
            "Bucket": bucket,
            "Key": key,
            "ContentType": "application/pdf",
        },
        ExpiresIn=ttl,
    )
    return url, key


# ── Remote: invocation + polling ────────────────────────────────────
def _invoke(
    action: str,
    scan: Scan,
    payload: dict[str, Any],
    progress_callback: ProgressCallback | None,
) -> dict:
    """Submit a RunPod job and poll until terminal state.

    :param action: ``"detect"`` or ``"analyze"``.
    :param scan: Scan owning the job (``scan.pk`` is passed through
        so the handler can tag Sentry events).
    :param payload: Remaining ``input`` fields (e.g. ``pdf_url``,
        action args). Merged with ``action`` and ``scan_pk``.
    :param progress_callback: Optional coarse progress emitter.
    :returns: The handler's ``output`` dict.
    :rtype: dict
    :raises RunpodError: On terminal failure, handler error, or
        exhausted retries.
    """
    endpoint = settings.RUNPOD_ENDPOINT_ID
    api_key = settings.RUNPOD_API_KEY
    if not endpoint or not api_key:
        raise RunpodError("RUNPOD_ENDPOINT_ID / RUNPOD_API_KEY not configured")

    base_url = f"https://api.runpod.ai/v2/{endpoint}"
    headers = {"Authorization": f"Bearer {api_key}"}
    body = {
        "input": {"action": action, "scan_pk": scan.pk, **payload},
    }

    # RUNPOD_REQUEST_TIMEOUT bounds how long the handler may *run*. Waiting
    # for a serverless worker to spin up (a cold start with no warm workers)
    # can take several minutes and must not eat into that execution budget,
    # so the queue wait is bounded separately by RUNPOD_QUEUE_TIMEOUT.
    exec_timeout = int(settings.RUNPOD_REQUEST_TIMEOUT)
    queue_timeout = int(settings.RUNPOD_QUEUE_TIMEOUT)
    max_retries = int(settings.RUNPOD_MAX_RETRIES)

    job_id = _submit(
        base_url, headers, body, action, max_retries, progress_callback
    )
    return _poll(
        base_url,
        headers,
        job_id,
        action,
        exec_timeout,
        queue_timeout,
        progress_callback,
    )


def _submit(
    base_url: str,
    headers: dict[str, str],
    body: dict[str, Any],
    action: str,
    max_retries: int,
    progress_callback: ProgressCallback | None,
) -> str:
    """POST to ``/run`` with retry on transport errors.

    :returns: The RunPod job id.
    :rtype: str
    :raises RunpodError: If all retries fail or the response is malformed.
    """
    last_exc: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            if progress_callback:
                progress_callback(
                    None, None, f"Submitting {action} to RunPod..."
                )
            r = requests.post(
                f"{base_url}/run", headers=headers, json=body, timeout=30
            )
            r.raise_for_status()
            submit = r.json()
            job_id = submit.get("id")
            if not job_id:
                raise RunpodError(
                    f"RunPod /run returned no job id: {submit!r}"
                )
            logger.info(
                "runpod %s job %s submitted (input=%s)",
                action,
                job_id,
                _redact_pdf_url(body).get("input"),
            )
            if progress_callback:
                label = _ACTION_LABELS.get(action, action)
                progress_callback(
                    None, None, f"{label}: queued — RunPod job {job_id}"
                )
            return job_id
        except Exception as exc:
            last_exc = exc
            sleep_for = 2**attempt
            logger.warning(
                "runpod submit attempt %d/%d failed: %s; sleeping %ds",
                attempt + 1,
                max_retries + 1,
                exc,
                sleep_for,
            )
            if attempt < max_retries:
                time.sleep(sleep_for)

    raise RunpodError(
        f"failed to submit RunPod job after {max_retries + 1} attempts: {last_exc}"
    )


def _poll(
    base_url: str,
    headers: dict[str, str],
    job_id: str,
    action: str,
    exec_timeout: int,
    queue_timeout: int,
    progress_callback: ProgressCallback | None,
) -> dict:
    """Poll ``/status/{job_id}`` until terminal, with backoff.

    Poll cadence starts at 1 s, doubles each idle tick, capped at 15 s.

    The deadline is two-phase: while the job is still waiting for a worker
    it is bounded by ``queue_timeout``; the moment it transitions to
    IN_PROGRESS the clock is reset to ``exec_timeout``, so a long serverless
    cold-start queue wait never consumes the handler's execution budget.
    Exceeding either deadline triggers ``/cancel/{job_id}`` and raises.

    Every poll logs at DEBUG (enable with ``SCANNING_LOG_LEVEL=DEBUG``);
    status transitions log at INFO.

    :param exec_timeout: Seconds the handler may run once IN_PROGRESS.
    :param queue_timeout: Seconds to wait for a worker before giving up.
    :returns: The handler's ``output`` dict on COMPLETED.
    :rtype: dict
    :raises RunpodError: On FAILED / TIMED_OUT / CANCELLED / execution
        deadline exceeded / malformed output.
    :raises RunpodTransientError: On HTTP 404 from ``/status`` (the job
        aged out or was discarded), or if the queue-wait deadline is
        exceeded before the job starts (no worker became available); the
        inputs are still on S3 so a fresh submit re-runs the work.
    """
    # Happy-path cadence: 1 s, 2 s, 4 s, 8 s, 15 s, 15 s, ... Advances
    # on every successful poll that returns a non-terminal status.
    poll_sleep_s = 1.0
    # Error-retry cadence: independent of poll_sleep_s so a single
    # 5xx or network blip doesn't permanently slow down happy-path
    # polling after the underlying issue resolves.
    error_sleep_s = 1.0
    last_status: str | None = None
    label = _ACTION_LABELS.get(action, action)

    # Phase 1: waiting for a worker. Reset to the execution budget once the
    # job starts running (see the IN_PROGRESS handling below).
    started = False
    deadline = time.monotonic() + queue_timeout

    while True:
        if time.monotonic() > deadline:
            _cancel(base_url, headers, job_id)
            if started:
                raise RunpodError(
                    f"RunPod job {job_id} exceeded execution budget "
                    f"(RUNPOD_REQUEST_TIMEOUT={exec_timeout}s)"
                )
            raise RunpodTransientError(
                f"RunPod job {job_id} waited longer than "
                f"RUNPOD_QUEUE_TIMEOUT={queue_timeout}s without starting "
                "(no worker available); re-queueing for the next tick."
            )

        try:
            r = requests.get(
                f"{base_url}/status/{job_id}", headers=headers, timeout=30
            )
            # 404 from /status means the job is gone: either RunPod
            # discarded it after exhausting their internal retries, or
            # the result aged past the retention window (30 min async).
            # The inputs are still on S3, so re-queueing the scan lets
            # the next daemon tick resubmit with a fresh presigned URL.
            if r.status_code == 404:
                raise RunpodTransientError(
                    f"RunPod job {job_id} not found (HTTP 404 from "
                    "/status). Re-queueing so the next daemon tick "
                    "submits a fresh job."
                )
            r.raise_for_status()
            body = r.json()
        except RunpodError:
            # Don't swallow the 404-derived RunpodTransientError above.
            raise
        except Exception as exc:
            # Transient status-poll error (5xx, network blip, timeout):
            # keep trying until deadline. Uses its own backoff counter
            # so happy-path poll cadence is unaffected once the blip
            # resolves.
            logger.warning(
                "runpod status poll for %s failed: %s; retrying", job_id, exc
            )
            time.sleep(min(error_sleep_s, 10))
            error_sleep_s = min(error_sleep_s * 2, 15)
            continue

        # Status poll succeeded: reset the error backoff so the next
        # transient blip starts fresh rather than inheriting the
        # previous escalation.
        error_sleep_s = 1.0

        status = body.get("status")
        logger.debug("poll runpod job %s -> %s", job_id, status)

        # First time the worker picks up the job, switch from the queue-wait
        # budget to the execution budget so cold-start time isn't charged
        # against the handler's run time.
        if status == "IN_PROGRESS" and not started:
            started = True
            deadline = time.monotonic() + exec_timeout
            logger.info(
                "runpod job %s running; execution budget %ds",
                job_id,
                exec_timeout,
            )

        if status == "COMPLETED":
            output = body.get("output")
            if not isinstance(output, dict):
                raise RunpodError(
                    f"RunPod job {job_id} returned non-dict output: {output!r}"
                )
            execution_ms = body.get("executionTime", "?")
            action_ms = output.get("duration_ms", "?")
            model_durations = output.get("model_durations_ms")
            detail_parts = [f"action={action_ms}ms"]
            if model_durations:
                detail_parts += [
                    f"{m}={ms}ms" for m, ms in model_durations.items()
                ]
            logger.info(
                "runpod %s job %s COMPLETED in %sms (%s)",
                action,
                job_id,
                execution_ms,
                ", ".join(detail_parts),
            )
            return output

        if status in ("FAILED", "TIMED_OUT", "CANCELLED"):
            err = body.get("error") or ""
            # The RunPod SDK (rp_job.py::run_job) pops ``error`` from
            # the handler's return dict and places it at the top level
            # of the result payload before POSTing to RunPod. For example,
            # a handler returning:
            #
            #   {"error": "bad input", "error_code": "BAD_INPUT", ...}
            #
            # becomes:
            #
            #   {"output": {"error_code": "BAD_INPUT", ...}, "error": "bad input"}
            #
            # RunPod marks this FAILED with ``error`` at the top level.
            # ``error_code`` survives inside ``output``, so we can still
            # distinguish transient errors (re-queue) from terminal ones
            # (mark scan ERROR).
            #
            # When ``output`` is absent the failure is a RunPod platform
            # error (e.g. "job timed out after 1 retries", worker crash).
            # Those are infrastructure problems, not handler logic failures,
            # so re-queue rather than permanently failing the scan.
            err_code = (body.get("output") or {}).get("error_code") or ""
            if err_code:
                exc_cls = (
                    RunpodTransientError
                    if err_code in _TRANSIENT_ERROR_CODES
                    else RunpodError
                )
            elif status == "FAILED":
                exc_cls = RunpodTransientError
            else:
                exc_cls = RunpodError
            raise exc_cls(
                f"RunPod job {job_id} {status}" + (f": {err}" if err else "")
            )

        # IN_QUEUE / IN_PROGRESS / unknown — keep polling.
        if status and status != last_status:
            logger.info("runpod job %s -> %s", job_id, status)
            if progress_callback:
                progress_callback(
                    None,
                    None,
                    f"{label}: {status} — RunPod job {job_id}",
                )
            last_status = status

        time.sleep(min(poll_sleep_s, 15))
        poll_sleep_s = min(poll_sleep_s * 2, 15)


def _cancel(base_url: str, headers: dict[str, str], job_id: str) -> None:
    """Best-effort cancel so we stop paying for a wedged job.

    :param base_url: RunPod endpoint base URL.
    :param headers: Authorization header dict.
    :param job_id: RunPod job id.
    """
    try:
        requests.post(
            f"{base_url}/cancel/{job_id}", headers=headers, timeout=15
        )
        logger.info("runpod job %s cancelled", job_id)
    except Exception:
        logger.warning("runpod job %s cancel failed", job_id, exc_info=True)
