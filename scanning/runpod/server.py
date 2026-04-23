"""FastAPI server for the GPU pod worker.

Four routes, all requiring ``Authorization: Bearer $POD_API_KEY``:

- ``GET  /health``  -- liveness probe; returns boot/uptime and what's loaded.
- ``POST /warmup``  -- eagerly preload YOLO + Paddle; returns load times.
- ``POST /detect``  -- YOLO detection on a PDF fetched from a presigned URL.
- ``POST /analyze`` -- PaddleOCR + YOLO page-number analysis.

Body format is the same ``input`` dict the serverless handler used to
accept, so callers sending ``{"action": "detect", ...}`` just strip
``action`` and POST the rest to ``/detect``. Every response carries a
``worker`` block with ``boot_ms``, ``uptime_ms``, and ``gpu_available``
so the daemon can tell cold from warm.
"""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

import actions
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

# ── Sentry ──────────────────────────────────────────────────────────
try:
    import sentry_sdk
except ImportError:
    sentry_sdk = None

_SENTRY_DSN = os.environ.get("SENTRY_DSN_GPU", "").strip()
if sentry_sdk is not None and _SENTRY_DSN:
    sentry_sdk.init(
        dsn=_SENTRY_DSN,
        environment=os.environ.get("SENTRY_ENV", "prod"),
        release=os.environ.get("GIT_SHA", "unknown"),
        traces_sample_rate=0.0,
    )

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("blackletter.pod.server")

# ── Auth ────────────────────────────────────────────────────────────
_POD_API_KEY = os.environ.get("POD_API_KEY", "").strip()


def _require_bearer(request: Request) -> None:
    """Validate the Authorization header against ``POD_API_KEY``.

    :param request: Incoming FastAPI request.
    :raises HTTPException: 401 if the header is missing or wrong.
    """
    if not _POD_API_KEY:
        # Fail closed: refusing requests is safer than leaving the
        # pod unauthenticated if the env var was forgotten.
        raise HTTPException(
            status_code=503, detail="pod POD_API_KEY not configured"
        )
    header = request.headers.get("authorization", "")
    if not header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    if header[len("Bearer ") :] != _POD_API_KEY:
        raise HTTPException(status_code=401, detail="invalid bearer token")


# ── App ─────────────────────────────────────────────────────────────
app = FastAPI(title="blackletter-gpu-pod", version="0.1.0")


@app.on_event("startup")
def _on_startup() -> None:
    """Record the "ready to serve" time after FastAPI wires routes."""
    actions.detect_cuda()
    actions.mark_ready()


def _worker_meta() -> dict[str, Any]:
    """Return the standard worker-meta block attached to every response.

    :returns: ``{boot_ms, uptime_ms, gpu_available}``.
    :rtype: dict
    """
    return {
        "boot_ms": actions.boot_ms(),
        "uptime_ms": actions.uptime_ms(),
        "gpu_available": actions.detect_cuda(),
    }


def _tag_sentry(action: str, scan_pk: Any) -> None:
    """Attach job-identifying tags to the current Sentry scope.

    :param action: The handler action being executed.
    :param scan_pk: Primary key of the scan this request belongs to.
    """
    if sentry_sdk is None:
        return
    with sentry_sdk.configure_scope() as scope:
        scope.set_tag("action", action)
        scope.set_tag("gpu_available", str(actions.detect_cuda()))
        scope.set_tag("boot_ms", str(actions.boot_ms()))
        scope.set_tag("uptime_ms", str(actions.uptime_ms()))
        if scan_pk is not None:
            scope.set_tag("scan_pk", str(scan_pk))


# ── Routes ──────────────────────────────────────────────────────────
@app.get("/health", dependencies=[Depends(_require_bearer)])
def health() -> dict[str, Any]:
    """Liveness + state probe.

    :returns: ``{status, worker, yolo_loaded, paddle_loaded}``.
    :rtype: dict
    """
    return {
        "status": "ok",
        "worker": _worker_meta(),
        "yolo_loaded": actions.yolo_warmed(),
        "paddle_loaded": actions.paddle_warmed(),
    }


@app.post("/warmup", dependencies=[Depends(_require_bearer)])
def warmup() -> dict[str, Any]:
    """Eagerly load YOLO + Paddle so the first real scan is fast.

    Safe to call repeatedly; subsequent calls return zero durations.

    :returns: ``{worker, yolo_warmup_ms, paddle_warmup_ms}``.
    :rtype: dict
    """
    if not actions.detect_cuda():
        return JSONResponse(
            status_code=503,
            content={
                "error": "GPU unavailable on this pod",
                "error_code": "NO_GPU",
                "worker": _worker_meta(),
            },
        )
    yolo_ms = actions.preload_yolo()
    paddle_ms = actions.preload_paddle()
    return {
        "worker": _worker_meta(),
        "yolo_warmup_ms": yolo_ms,
        "paddle_warmup_ms": paddle_ms,
    }


@app.post("/detect", dependencies=[Depends(_require_bearer)])
async def detect_route(request: Request) -> JSONResponse:
    """Run YOLO detection on a PDF fetched via presigned URL.

    Body schema: same ``input`` dict the serverless handler accepted,
    minus the ``action`` key. Required: ``pdf_url``. Optional:
    ``models``, ``confidence``, ``scan_pk``.

    :param request: FastAPI request carrying the JSON body.
    :returns: ``{detections, page_count, metrics, worker}`` on success;
        ``{error, error_code, worker}`` on structured failure.
    :rtype: JSONResponse
    """
    return await _run_action("detect", request)


@app.post("/analyze", dependencies=[Depends(_require_bearer)])
async def analyze_route(request: Request) -> JSONResponse:
    """Run PaddleOCR + YOLO page-number analysis.

    Body schema: same ``input`` dict the serverless handler accepted.
    Required: ``pdf_url``. Optional: ``exp_start``, ``exp_end``,
    ``max_pages``, ``scan_pk``.

    :param request: FastAPI request carrying the JSON body.
    :returns: ``{results, page_count, metrics, worker}`` on success;
        ``{error, error_code, worker}`` on structured failure.
    :rtype: JSONResponse
    """
    return await _run_action("analyze", request)


async def _run_action(action: str, request: Request) -> JSONResponse:
    """Shared body for the detect/analyze routes.

    :param action: ``"detect"`` or ``"analyze"``.
    :param request: FastAPI request carrying the JSON body.
    :returns: Action output plus worker meta; or a structured error
        with the same shape the serverless handler used.
    :rtype: JSONResponse
    """
    try:
        inputs = await request.json()
    except Exception:
        return JSONResponse(
            status_code=400,
            content={
                "error": "request body must be JSON",
                "error_code": "BAD_INPUT",
                "worker": _worker_meta(),
            },
        )

    if not isinstance(inputs, dict):
        return JSONResponse(
            status_code=400,
            content={
                "error": f"request body must be a JSON object, got {type(inputs).__name__}",
                "error_code": "BAD_INPUT",
                "worker": _worker_meta(),
            },
        )

    scan_pk = inputs.get("scan_pk")
    _tag_sentry(action, scan_pk)

    # Fail fast if the pod was scheduled onto a CPU-only host. Daemon
    # maps error_code=NO_GPU to a transient error and re-queues the
    # scan.
    if not actions.detect_cuda():
        logger.error(
            "rejecting %s job for scan %s: no GPU on this pod",
            action,
            scan_pk,
        )
        return JSONResponse(
            status_code=503,
            content={
                "error": "GPU unavailable on this pod",
                "error_code": "NO_GPU",
                "worker": _worker_meta(),
            },
        )

    fn = {"detect": actions.action_detect, "analyze": actions.action_analyze}[
        action
    ]
    tmp_dir = Path(tempfile.mkdtemp(prefix="pod-"))
    try:
        result = fn(inputs, tmp_dir)
        result["worker"] = _worker_meta()
        return JSONResponse(content=result)
    except Exception as exc:
        if sentry_sdk is not None:
            sentry_sdk.capture_exception(exc)
        logger.exception("action %s failed (scan_pk=%s)", action, scan_pk)
        return JSONResponse(
            status_code=500,
            content={
                "error": str(exc),
                "error_code": "ACTION_FAILED",
                "worker": _worker_meta(),
            },
        )
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
