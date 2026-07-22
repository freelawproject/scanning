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


# How long after submit a persisted job id is still worth reattaching to.
# RunPod deletes async jobs 24h after submission (queue time included), so
# past this window the id is guaranteed gone and we submit a fresh job
# instead of probing a job that no longer exists (issue #127).
_REATTACH_WINDOW_S = 24 * 60 * 60


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


# ── Remote: invocation + polling ────────────────────────────────────
def _request_timeout_for(scan: Scan) -> int:
    """Wall-clock ceiling (seconds) for this scan's RunPod job.

    Scales the flat base by page count so a large volume (e.g. a
    1300-page scan run through 3 detect models) is not cut off early
    against a ceiling tuned for small scans. A genuine timeout is still
    terminal - this only widens the window for legitimately large work.

    :param scan: The scan whose job is being run.
    :returns: ``RUNPOD_REQUEST_TIMEOUT + RUNPOD_REQUEST_TIMEOUT_PER_PAGE
        * page_count``.
    :rtype: int
    """
    base = int(settings.RUNPOD_REQUEST_TIMEOUT)
    per_page = int(settings.RUNPOD_REQUEST_TIMEOUT_PER_PAGE)
    pages = scan.page_count or 0
    return base + per_page * pages


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

    deadline = time.monotonic() + _request_timeout_for(scan)
    max_retries = int(settings.RUNPOD_MAX_RETRIES)

    job_id = _resume_or_submit(
        action, scan, base_url, headers, body, max_retries, progress_callback
    )
    try:
        return _poll(
            base_url, headers, job_id, action, deadline, progress_callback
        )
    finally:
        # Clear the persisted handle on any terminal outcome (COMPLETED, or a
        # raised RunpodError / RunpodTransientError). If the process is killed
        # mid-poll this never runs, so the id stays on the row and the next
        # daemon tick reattaches instead of submitting a duplicate (issue
        # #127). Guarded on the matching id so a slow clear can't wipe a
        # newer job's handle.
        _clear_job_id(scan.pk, job_id)


def _resume_or_submit(
    action: str,
    scan: Scan,
    base_url: str,
    headers: dict[str, str],
    body: dict[str, Any],
    max_retries: int,
    progress_callback: ProgressCallback | None,
) -> str:
    """Reattach to an in-flight RunPod job if one is persisted, else submit.

    If the scan carries a persisted job id for this ``action`` that is still
    within RunPod's retention window, return it so :func:`_poll` reattaches to
    the running job: it picks up an ``IN_QUEUE`` / ``IN_PROGRESS`` job without
    submitting a duplicate, or skips the GPU work entirely if the job already
    ``COMPLETED``. Otherwise submit a fresh job and persist its id.

    A persisted job that has aged out of RunPod surfaces as an HTTP 404 in
    :func:`_poll`, which re-queues the scan so the next tick submits fresh, so
    we don't need to probe ``/status`` here.

    :returns: The RunPod job id to poll.
    :rtype: str
    :raises RunpodError: If a fresh submit fails after retries.
    """
    resumable = _resumable_job_id(scan.pk, action)
    if resumable:
        logger.info(
            "reattaching to in-flight runpod %s job %s for scan %s",
            action,
            resumable,
            scan.pk,
        )
        if progress_callback:
            label = _ACTION_LABELS.get(action, action)
            progress_callback(
                None,
                None,
                f"{label}: reattaching to RunPod job {resumable}",
            )
        return resumable

    job_id = _submit(
        base_url, headers, body, action, max_retries, progress_callback
    )
    _persist_job_id(scan.pk, job_id, action)
    return job_id


def _resumable_job_id(scan_pk: int, action: str) -> str | None:
    """Return a persisted, still-fresh RunPod job id to reattach to, or None.

    Reattach only when the scan has a persisted id for this same ``action``
    submitted within :data:`_REATTACH_WINDOW_S`. A job past that window is
    guaranteed gone from RunPod, so skip it and let the caller submit fresh.

    :param scan_pk: Primary key of the scan.
    :param action: The action being run (``"detect"`` / ``"analyze"``); the
        persisted job must match, since a scan runs both in one pipeline.
    :returns: The job id to reattach to, or ``None`` to submit fresh.
    :rtype: str | None
    """
    from django.utils import timezone

    from scanning.models import Scan

    row = (
        Scan.objects.filter(pk=scan_pk)
        .values(
            "runpod_job_id", "runpod_job_action", "runpod_job_submitted_at"
        )
        .first()
    )
    if not row or not row["runpod_job_id"]:
        return None
    if row["runpod_job_action"] != action:
        return None
    submitted_at = row["runpod_job_submitted_at"]
    if submitted_at is None:
        return None
    if (timezone.now() - submitted_at).total_seconds() > _REATTACH_WINDOW_S:
        return None
    return row["runpod_job_id"]


def _persist_job_id(scan_pk: int, job_id: str, action: str) -> None:
    """Persist the in-flight RunPod job handle on the scan row.

    Filtered by pk only (not guarded by ``status``): a concurrent shutdown may
    have already re-queued the scan to ``QUEUED``, and we specifically want the
    id recorded on that row so the re-queued scan can reattach (issue #127).

    :param scan_pk: Primary key of the scan.
    :param job_id: The RunPod job id to persist.
    :param action: The action the job is running.
    """
    from django.utils import timezone

    from scanning.models import Scan

    Scan.objects.filter(pk=scan_pk).update(
        runpod_job_id=job_id,
        runpod_job_action=action,
        runpod_job_submitted_at=timezone.now(),
    )


def _clear_job_id(scan_pk: int, job_id: str) -> None:
    """Clear the persisted RunPod job handle once the job is terminal.

    Guarded on the matching ``job_id`` so a slow clear can never wipe a newer
    job's handle written by a subsequent submit.

    :param scan_pk: Primary key of the scan.
    :param job_id: The job id that just reached a terminal state.
    """
    from scanning.models import Scan

    Scan.objects.filter(pk=scan_pk, runpod_job_id=job_id).update(
        runpod_job_id="",
        runpod_job_action="",
        runpod_job_submitted_at=None,
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
    deadline: float,
    progress_callback: ProgressCallback | None,
) -> dict:
    """Poll ``/status/{job_id}`` until terminal, with backoff.

    Poll cadence starts at 1 s, doubles each idle tick, capped at 15 s.
    Exceeding ``deadline`` triggers ``/cancel/{job_id}`` and raises.

    Every poll logs at DEBUG (enable with ``SCANNING_LOG_LEVEL=DEBUG``);
    status transitions log at INFO.

    :returns: The handler's ``output`` dict on COMPLETED.
    :rtype: dict
    :raises RunpodError: On FAILED / TIMED_OUT / CANCELLED / deadline
        exceeded / malformed output.
    :raises RunpodTransientError: On HTTP 404 from ``/status`` (the
        job was either aged out of RunPod's retention window or
        discarded after internal retries; the inputs are still on S3
        so a fresh submit will re-run the work).
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

    while True:
        if time.monotonic() > deadline:
            _cancel(base_url, headers, job_id)
            raise RunpodError(
                f"RunPod job {job_id} exceeded RUNPOD_REQUEST_TIMEOUT"
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


def cancel_job(job_id: str) -> None:
    """Best-effort cancel of a RunPod job by id, wiring the endpoint from
    settings.

    For callers that hold only a job id (e.g. the daemon shutdown handler
    cancelling jobs on scans it is flagging terminal, which will never be
    reattached). No-op when RunPod is not configured or ``job_id`` is empty.

    :param job_id: RunPod job id to cancel.
    """
    endpoint = settings.RUNPOD_ENDPOINT_ID
    api_key = settings.RUNPOD_API_KEY
    if not endpoint or not api_key or not job_id:
        return
    base_url = f"https://api.runpod.ai/v2/{endpoint}"
    headers = {"Authorization": f"Bearer {api_key}"}
    _cancel(base_url, headers, job_id)
