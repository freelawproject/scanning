"""RunPod pod client for offloading GPU steps.

Two public entry points: :func:`detect` and :func:`analyze`. Both take
a local ``pdf_path`` and, when ``settings.RUNPOD_ENABLED`` is true,
ship the work out to a dedicated RunPod GPU pod running the FastAPI
server in ``scanning/runpod/server.py``; otherwise they fall back to
calling blackletter in-process so dev / tests / staging keep working
without RunPod credentials.

Remote mode flow:

1. Upload the PDF to S3 under the scan's processing prefix if it's
   not already there (idempotent via ``head_object``).
2. Generate a presigned GET URL.
3. :func:`scanning.pod_manager.ensure_pod_ready` starts the pod if
   it's stopped and waits for its FastAPI server to answer
   ``GET /health``.
4. ``POST {base_url}/{action}`` with the same JSON body shape the
   old serverless handler accepted (sans ``action``, which is in
   the path), using a bearer token for auth.
5. Return the handler's response dict.

On transient failures (``error_code="NO_GPU"``, pod boot timeout,
connection refused during pod startup) :class:`RunpodTransientError`
is raised; ``scanning/services.py`` re-queues the scan. Terminal
failures raise :class:`RunpodError` and mark the scan as ERROR.
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

from scanning import pod_manager

if TYPE_CHECKING:
    from scanning.models import Scan

logger = logging.getLogger(__name__)


class RunpodError(RuntimeError):
    """Raised on terminal RunPod failure or exhausted retries."""


class RunpodTransientError(RunpodError):
    """Retriable RunPod failure.

    Raised on ``error_code="NO_GPU"`` from the pod handler, on
    :class:`~scanning.pod_manager.PodBootTimeout`, and on repeated
    connection-refused errors during pod startup. Callers in
    ``scanning/services.py`` catch this specifically and re-queue the
    scan to ``Status.QUEUED`` so the next daemon tick retries.
    Contrast with the base :class:`RunpodError` which signals a
    terminal failure worth marking the scan as ``ERROR``.
    """


# Error codes the pod emits that the client translates into a
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
        Sentry tagging on the pod side).
    :param pdf_path: Local filesystem path to the input PDF. In
        remote mode the file is uploaded to S3 under the scan's
        processing prefix if it's not already there.
    :param models: YOLO model sizes to run. Defaults to all three.
    :param confidence: Minimum confidence threshold.
    :param progress_callback: Optional
        ``callable(current, total, message)`` for coarse progress
        updates (pod boot, dispatch). Local mode doesn't fire it.
    :returns: Merged detection list (same shape as
        ``blackletter.api.detect``'s return value).
    :rtype: list[dict]
    :raises RunpodError: On terminal remote failure.
    :raises RunpodTransientError: On retriable remote failure.
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
        Ignored in remote mode (the pod always runs with
        ``num_workers=1`` to avoid the paddle+fork segfault).
    :param progress_callback: Optional
        ``callable(current, total, message)``. In local mode the
        callback is passed through to blackletter and fires once per
        page; in remote mode only coarse events fire.
    :returns: ``{"results": list[dict]}``.
    :rtype: dict
    :raises RunpodError: On terminal remote failure.
    :raises RunpodTransientError: On retriable remote failure.
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
            "cannot generate a presigned URL for the pod."
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

    ttl = int(getattr(settings, "RUNPOD_PRESIGNED_TTL", 3600))
    return s3.generate_presigned_url(
        "get_object",
        Params={"Bucket": bucket, "Key": key},
        ExpiresIn=ttl,
    )


# ── Remote: invocation ──────────────────────────────────────────────
def _invoke(
    action: str,
    scan: Scan,
    payload: dict[str, Any],
    progress_callback: ProgressCallback | None,
) -> dict:
    """Dispatch an action to the GPU pod over HTTP.

    Ensures the pod is running and its FastAPI server answers
    ``/health`` before POSTing. Records activity so the idle-stopper
    waits the configured grace window before shutting the pod down.

    :param action: ``"detect"`` or ``"analyze"``.
    :param scan: Scan owning the job.
    :param payload: Action input fields (``pdf_url`` plus action args).
        Merged with ``scan_pk`` for Sentry tagging on the pod.
    :param progress_callback: Optional coarse progress emitter.
    :returns: The pod's JSON response dict.
    :rtype: dict
    :raises RunpodError: On terminal failure.
    :raises RunpodTransientError: On retriable failure (NO_GPU, pod
        boot timeout, connection refused).
    """
    pod_api_key = getattr(settings, "RUNPOD_POD_API_KEY", "")
    if not pod_api_key:
        raise RunpodError("RUNPOD_POD_API_KEY is not configured")

    if progress_callback:
        progress_callback(
            None, None, f"Ensuring GPU pod is ready for {action}..."
        )

    try:
        base_url, boot_ms = pod_manager.ensure_pod_ready()
    except pod_manager.PodBootTimeout as exc:
        raise RunpodTransientError(str(exc)) from exc
    except pod_manager.PodError as exc:
        raise RunpodError(str(exc)) from exc

    pod_manager.record_activity()

    if progress_callback:
        msg = (
            f"Dispatching {action} to pod (ready after {boot_ms} ms)..."
            if boot_ms
            else f"Dispatching {action} to pod..."
        )
        progress_callback(None, None, msg)

    body = {"scan_pk": scan.pk, **payload}
    headers = {"Authorization": f"Bearer {pod_api_key}"}
    timeout = int(getattr(settings, "RUNPOD_REQUEST_TIMEOUT", 1800))
    max_retries = int(getattr(settings, "RUNPOD_MAX_RETRIES", 2))

    response = _post_with_retry(
        url=f"{base_url}/{action}",
        headers=headers,
        body=body,
        timeout=timeout,
        max_retries=max_retries,
        action=action,
    )

    data = _parse_json(response, action)

    # 2xx: success. Return the pod's JSON body verbatim.
    if 200 <= response.status_code < 300:
        logger.info(
            "pod %s OK in %s ms (boot %s ms)",
            action,
            data.get("metrics", {}).get("total_ms", "?"),
            boot_ms,
        )
        pod_manager.record_activity()
        return data

    # Structured error path: non-2xx with ``error_code`` in the body.
    err_code = data.get("error_code") or ""
    err_msg = data.get("error") or err_code or "unknown"
    exc_cls = (
        RunpodTransientError
        if err_code in _TRANSIENT_ERROR_CODES
        else RunpodError
    )
    raise exc_cls(
        f"pod {action} returned HTTP {response.status_code} "
        f"error_code={err_code!r}: {err_msg}"
    )


def _post_with_retry(
    url: str,
    headers: dict[str, str],
    body: dict[str, Any],
    timeout: int,
    max_retries: int,
    action: str,
) -> requests.Response:
    """POST with exponential backoff on ConnectionError / ReadTimeout.

    5xx responses are NOT retried here -- the pod already did its
    work and returned a structured error; ``_invoke`` handles the
    classification. We retry only network-level failures (pod still
    booting, connection reset).

    :param url: Target URL (``{base_url}/{action}``).
    :param headers: Bearer auth header.
    :param body: JSON body to POST.
    :param timeout: Per-request timeout in seconds.
    :param max_retries: Extra attempts after the first.
    :param action: For logging only.
    :returns: The final ``requests.Response`` (status may be any).
    :rtype: requests.Response
    :raises RunpodTransientError: If all attempts hit a network error.
    """
    last_exc: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            return requests.post(
                url, headers=headers, json=body, timeout=timeout
            )
        except (requests.ConnectionError, requests.ReadTimeout) as exc:
            last_exc = exc
            sleep_for = 2**attempt
            logger.warning(
                "pod %s post attempt %d/%d failed: %s; sleeping %ds",
                action,
                attempt + 1,
                max_retries + 1,
                exc,
                sleep_for,
            )
            time.sleep(sleep_for)
        except requests.RequestException as exc:
            # Terminal transport failure (malformed URL, bad cert,
            # etc.) -- no point retrying.
            raise RunpodError(f"pod {action} transport error: {exc}") from exc

    raise RunpodTransientError(
        f"pod {action} unreachable after {max_retries + 1} attempts: {last_exc}"
    )


def _parse_json(response: requests.Response, action: str) -> dict:
    """Parse a JSON response and require a dict body.

    :param response: The HTTP response to parse.
    :param action: For error messages.
    :returns: The parsed dict.
    :rtype: dict
    :raises RunpodError: On malformed or non-dict JSON.
    """
    try:
        data = response.json()
    except ValueError as exc:
        raise RunpodError(
            f"pod {action} returned non-JSON body "
            f"(HTTP {response.status_code}): {response.text[:200]!r}"
        ) from exc
    if not isinstance(data, dict):
        raise RunpodError(
            f"pod {action} returned non-dict JSON "
            f"(HTTP {response.status_code}): {data!r}"
        )
    return data
