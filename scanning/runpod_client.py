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
from django.conf import settings

if TYPE_CHECKING:
    from scanning.models import Scan

logger = logging.getLogger(__name__)


class RunpodError(RuntimeError):
    """Raised on terminal RunPod failure or exhausted retries."""


class RunpodTransientError(RunpodError):
    """Retriable RunPod failure.

    Today this is raised when the handler reports ``error_code="NO_GPU"``
    (worker scheduled onto a CPU-only host). Callers in
    ``scanning/services.py`` catch this specifically and re-queue the
    scan to ``Status.QUEUED`` so the next daemon tick retries on a
    (hopefully) different worker. Contrast with the base
    ``RunpodError`` which signals a terminal failure worth marking
    the scan as ``ERROR``.
    """


# Error codes the handler emits that the client translates into a
# ``RunpodTransientError`` (retry) rather than ``RunpodError`` (fail).
_TRANSIENT_ERROR_CODES = {"NO_GPU"}


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
    return bool(getattr(settings, "RUNPOD_ENABLED", False))


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

    try:
        s3.head_object(Bucket=bucket, Key=key)
    except Exception:
        local = Path(pdf_path)
        if not local.is_file():
            raise FileNotFoundError(
                f"{pdf_path} not present locally and missing from "
                f"s3://{bucket}/{key}; cannot run GPU step."
            )
        logger.info(
            "uploading %s to s3://%s/%s before presign",
            local.name,
            bucket,
            key,
        )
        s3.upload_file(str(local), bucket, key)

    ttl = int(getattr(settings, "RUNPOD_PRESIGNED_TTL", 1800))
    return s3.generate_presigned_url(
        "get_object",
        Params={"Bucket": bucket, "Key": key},
        ExpiresIn=ttl,
    )


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
    endpoint = getattr(settings, "RUNPOD_ENDPOINT_ID", "")
    api_key = getattr(settings, "RUNPOD_API_KEY", "")
    if not endpoint or not api_key:
        raise RunpodError("RUNPOD_ENDPOINT_ID / RUNPOD_API_KEY not configured")

    base_url = f"https://api.runpod.ai/v2/{endpoint}"
    headers = {"Authorization": f"Bearer {api_key}"}
    body = {
        "input": {"action": action, "scan_pk": scan.pk, **payload},
    }

    deadline = time.monotonic() + int(
        getattr(settings, "RUNPOD_REQUEST_TIMEOUT", 900)
    )
    max_retries = int(getattr(settings, "RUNPOD_MAX_RETRIES", 2))

    job_id = _submit(
        base_url, headers, body, action, max_retries, progress_callback
    )
    return _poll(
        base_url, headers, job_id, action, deadline, progress_callback
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
            logger.info("runpod %s job %s submitted", action, job_id)
            if progress_callback:
                progress_callback(None, None, f"RunPod job {job_id} queued")
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

    :returns: The handler's ``output`` dict on COMPLETED.
    :rtype: dict
    :raises RunpodError: On FAILED / TIMED_OUT / CANCELLED / deadline
        exceeded / malformed output.
    """
    sleep_s = 1.0
    last_status: str | None = None

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
            # 404 from /status is terminal, not transient: either the
            # job was discarded by RunPod after exhausting its internal
            # retries, or it aged past the result-retention window.
            # Either way, polling further will never succeed.
            if r.status_code == 404:
                raise RunpodError(
                    f"RunPod job {job_id} not found (HTTP 404 from "
                    "/status). The job was either discarded after "
                    "RunPod-internal retries or aged out of the "
                    "result-retention window."
                )
            r.raise_for_status()
            body = r.json()
        except RunpodError:
            # Don't swallow the 404-derived RunpodError above.
            raise
        except Exception as exc:
            # Transient status-poll error (5xx, network blip, timeout):
            # keep trying until deadline.
            logger.warning(
                "runpod status poll for %s failed: %s; retrying", job_id, exc
            )
            time.sleep(min(sleep_s, 10))
            sleep_s = min(sleep_s * 2, 15)
            continue

        status = body.get("status")
        if status == "COMPLETED":
            output = body.get("output")
            if not isinstance(output, dict):
                raise RunpodError(
                    f"RunPod job {job_id} returned non-dict output: {output!r}"
                )
            if "error" in output:
                # Handler returned a structured error. ``error_code``
                # disambiguates the retriable ones (NO_GPU →
                # RunpodTransientError) from terminal ones (bad input,
                # unknown action → RunpodError).
                err_code = output.get("error_code") or ""
                err_msg = output.get("error") or err_code or "unknown"
                exc_cls = (
                    RunpodTransientError
                    if err_code in _TRANSIENT_ERROR_CODES
                    else RunpodError
                )
                raise exc_cls(f"handler error_code={err_code!r}: {err_msg}")
            logger.info(
                "runpod %s job %s COMPLETED in %s ms",
                action,
                job_id,
                output.get("duration_ms", "?"),
            )
            return output

        if status in ("FAILED", "TIMED_OUT", "CANCELLED"):
            err = body.get("error") or status
            raise RunpodError(f"RunPod job {job_id} {status}: {err}")

        # IN_QUEUE / IN_PROGRESS / unknown — keep polling.
        if status and status != last_status:
            logger.info("runpod job %s -> %s", job_id, status)
            if progress_callback:
                progress_callback(None, None, f"RunPod job {job_id}: {status}")
            last_status = status

        time.sleep(min(sleep_s, 15))
        sleep_s = min(sleep_s * 2, 15)


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
