"""RunPod Serverless handler for the blackletter GPU steps.

Dispatches on ``job["input"]["action"]`` to run one of:

- ``detect``: YOLO detection (small + medium + large) on a PDF
  fetched via presigned GET URL. Returns the merged detection list.
- ``analyze``: PaddleOCR + YOLO page-number analysis. Returns the
  per-page results list.

Returns JSON inline in the RunPod HTTP response. Does not write to
S3, does not need AWS credentials.

Models are preloaded at module import time so cold start pays the
load cost once; subsequent warm invocations reuse in-process caches.
"""

from __future__ import annotations

import io
import logging
import os
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import requests
import runpod

# Capture worker boot start as early as possible so ``_WORKER_BOOT_MS``
# covers the full cold-start cost (module imports + model preload).
# Uses monotonic() so the number isn't affected by wall-clock jumps.
_WORKER_BOOT_START = time.monotonic()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("blackletter.runpod")

# Populated in ``_preload``. Surfaced in every handler response so the
# daemon can tell whether inference actually hit a GPU.
_CUDA_AVAILABLE = False


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
    logger.info("Sentry initialised")


# ── Tunables ────────────────────────────────────────────────────────
MAX_PAGES = int(os.environ.get("HANDLER_MAX_PAGES", "5000"))
DOWNLOAD_TIMEOUT = int(os.environ.get("HANDLER_DOWNLOAD_TIMEOUT", "300"))


# ── Cold-start preload ──────────────────────────────────────────────
def _preload() -> None:
    """Pay model-load cost at module import so the first job is fast.

    Logs CUDA availability, ensures all weights are on disk, then
    touches the YOLO and PaddleOCR entry points to warm them. Each
    phase is wrapped in a try/except so preload failures don't
    prevent the worker from starting; the first real job will retry
    the load and surface any real error.
    """
    global _CUDA_AVAILABLE
    from blackletter.api import ensure_weights

    # GPU check first. This worker is GPU-only: if CUDA isn't available
    # we skip the warmup entirely (saves ~30-60s of useless CPU loads)
    # and let handler() fail fast with NO_GPU so the daemon can retry
    # on a different worker.
    try:
        import torch

        _CUDA_AVAILABLE = bool(torch.cuda.is_available())
        logger.info(
            "torch %s cuda=%s devices=%s",
            torch.__version__,
            _CUDA_AVAILABLE,
            torch.cuda.device_count() if _CUDA_AVAILABLE else 0,
        )
    except Exception:
        logger.warning("torch diagnostic failed")
        _CUDA_AVAILABLE = False

    if not _CUDA_AVAILABLE:
        logger.error(
            "GPU not available; skipping weight/model preload. Jobs on "
            "this worker will fail fast with error_code=NO_GPU."
        )
        return

    t0 = time.time()
    paths = ensure_weights()
    logger.info(
        "ensure_weights OK in %.1fs (%s)",
        time.time() - t0,
        ", ".join(
            f"{k}={v.stat().st_size // (1024 * 1024)}MB"
            for k, v in paths.items()
        ),
    )

    try:
        import blackletter
        from ultralytics import YOLO

        weights_dir = Path(blackletter.__file__).resolve().parent / "weights"
        for name in ("small", "medium", "large"):
            p = weights_dir / f"{name}.pt"
            if not p.is_file():
                continue
            t0 = time.time()
            YOLO(str(p))
            logger.info("warmed YOLO %s in %.1fs", name, time.time() - t0)
    except Exception:
        logger.exception("YOLO preload failed (first job will be slower)")

    # PaddleOCR is NOT preloaded here.
    #
    # Preloading it initialises Paddle's CUDA context, which then
    # coexists badly with torch's CUDA context during long YOLO runs
    # (detect on 1000+ pages). Paddle's idle allocator fires a
    # cleanup pass while torch is churning GPU memory, the free call
    # hits cudaErrorInitializationError, and the whole worker SIGABRTs.
    # Detect-only workloads should never load paddle.
    #
    # Analyze jobs still work: blackletter.analyze._process_page
    # lazy-loads PaddleOCR on first call. The weights are baked into
    # the image (/opt/paddlex), so that lazy load is ~2 s, not a
    # network download. Tradeoff: analyze's first job on a new worker
    # pays ~2 s extra vs. ~0 s; detect's reliability on large PDFs
    # goes from "crashes" to "works." Clear win.


_preload()

# Freeze the cold-start cost once the process is ready. ``_WORKER_BOOT_MS``
# is constant per worker; ``_WORKER_READY_AT`` anchors per-job uptime so
# the daemon can tell cold from warm calls (same boot_ms + increasing
# uptime_ms across jobs = same warm worker).
_WORKER_READY_AT = time.monotonic()
_WORKER_BOOT_MS = int((_WORKER_READY_AT - _WORKER_BOOT_START) * 1000)
logger.info(
    "worker ready: boot_ms=%d cuda=%s", _WORKER_BOOT_MS, _CUDA_AVAILABLE
)


# ── Helpers ─────────────────────────────────────────────────────────
def _worker_uptime_ms() -> int:
    """Return milliseconds since ``_preload`` finished.

    :returns: Uptime in ms. Small values indicate a cold start; large
        values indicate a reused warm worker.
    :rtype: int
    """
    return int((time.monotonic() - _WORKER_READY_AT) * 1000)


def _with_worker_meta(payload: dict) -> dict:
    """Attach worker boot / uptime / GPU status to a response dict.

    Used for both successful and structured-error returns so the
    daemon can always see whether this job hit a warm worker and
    whether inference ran on GPU.

    :param payload: The response dict to augment in place.
    :returns: The same dict with meta fields added.
    :rtype: dict
    """
    payload["worker_boot_ms"] = _WORKER_BOOT_MS
    payload["worker_uptime_ms"] = _worker_uptime_ms()
    payload["gpu_available"] = _CUDA_AVAILABLE
    return payload


def _tag_sentry(job: dict, action: str, scan_pk: Any) -> None:
    """Attach job-identifying tags to the current Sentry scope.

    :param job: RunPod job dict (expects an ``id`` field).
    :param action: The handler action being executed.
    :param scan_pk: Primary key of the scan this job belongs to.
    """
    if sentry_sdk is None:
        return
    with sentry_sdk.configure_scope() as scope:
        scope.set_tag("action", action)
        scope.set_tag("gpu_available", str(_CUDA_AVAILABLE))
        scope.set_tag("worker_boot_ms", str(_WORKER_BOOT_MS))
        scope.set_tag("worker_uptime_ms", str(_worker_uptime_ms()))
        if scan_pk is not None:
            scope.set_tag("scan_pk", str(scan_pk))
        job_id = job.get("id")
        if job_id:
            scope.set_tag("runpod_job_id", str(job_id))


def _download_pdf(url: str, dest: Path) -> None:
    """Download a PDF from a presigned GET URL to ``dest``.

    :param url: Presigned GET URL.
    :param dest: Local filesystem target.
    :raises requests.HTTPError: On non-2xx response.
    """
    with requests.get(url, stream=True, timeout=DOWNLOAD_TIMEOUT) as r:
        r.raise_for_status()
        with dest.open("wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    f.write(chunk)


def _page_count(pdf_path: Path) -> int:
    """Return the number of pages in a PDF.

    :param pdf_path: Path to the PDF on disk.
    :returns: Page count.
    :rtype: int
    """
    import fitz

    with fitz.open(str(pdf_path)) as doc:
        return doc.page_count


# ── Actions ─────────────────────────────────────────────────────────
#
# Per-model timing for detect() is derived by tapping stdout and
# matching the log lines blackletter already prints:
#     "  Detecting with {model}..."        (start of a model's pass)
#     "    {model} done ({N}s)"            (end of a model's pass)
# blackletter's own printed duration is integer-seconds; we measure
# with monotonic() for millisecond resolution. Best-effort: if
# blackletter ever changes its log format these regexes stop matching
# and ``model_durations_ms`` returns empty, but ``duration_ms`` (the
# overall detect() wall clock) stays correct.
_DETECT_START_RE = re.compile(r"Detecting with (\w+)\.\.\.")
_DETECT_DONE_RE = re.compile(r"\b(\w+)\s+done\s+\(")


class _DetectStdoutTap(io.TextIOBase):
    """Stdout proxy that records per-model start/stop times."""

    def __init__(self, real_stdout) -> None:
        self._real = real_stdout
        self._buf = ""
        self._starts: dict[str, float] = {}
        self.durations_ms: dict[str, int] = {}

    def write(self, s: str) -> int:
        self._real.write(s)
        self._real.flush()
        self._buf += s
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            self._handle(line.strip())
        return len(s)

    def flush(self) -> None:
        self._real.flush()

    def _handle(self, line: str) -> None:
        m = _DETECT_START_RE.search(line)
        if m:
            self._starts[m.group(1)] = time.monotonic()
            return
        m = _DETECT_DONE_RE.search(line)
        if m:
            name = m.group(1)
            start = self._starts.pop(name, None)
            if start is not None:
                self.durations_ms[name] = int(
                    (time.monotonic() - start) * 1000
                )


def _action_detect(inputs: dict, tmp_dir: Path) -> dict:
    """Run YOLO detection and return the merged detection list.

    :param inputs: Handler input payload. Required: ``pdf_url``.
        Optional: ``models`` (list, default all three),
        ``confidence`` (float, default 0.20).
    :param tmp_dir: Per-job scratch directory.
    :returns: ``{"detections": list[dict], "page_count": int,
        "duration_ms": int, "model_durations_ms":
        {model_name: int, ...}}``. Missing models (skipped because
        weight absent) are simply absent from ``model_durations_ms``.
    :rtype: dict
    """
    from blackletter.api import detect, ensure_weights

    pdf_url = inputs["pdf_url"]
    models = inputs.get("models") or ["small", "medium", "large"]
    confidence = float(inputs.get("confidence", 0.20))

    # Defensive: if image was built without large.pt, fetch it now.
    ensure_weights([m for m in models if m == "large"])

    pdf_path = tmp_dir / "input.pdf"
    _download_pdf(pdf_url, pdf_path)

    pages = _page_count(pdf_path)
    if pages > MAX_PAGES:
        raise ValueError(
            f"PDF has {pages} pages, exceeds MAX_PAGES={MAX_PAGES}"
        )

    real_stdout = sys.stdout
    tap = _DetectStdoutTap(real_stdout)
    sys.stdout = tap
    t0 = time.monotonic()
    try:
        detections = detect(
            pdf_path, tmp_dir, models=models, confidence=confidence
        )
    finally:
        sys.stdout = real_stdout
    duration_ms = int((time.monotonic() - t0) * 1000)

    logger.info(
        "detect OK: %d detections in %d ms (%d pages, models=%s, per-model=%s)",
        len(detections),
        duration_ms,
        pages,
        models,
        tap.durations_ms,
    )
    return {
        "detections": detections,
        "page_count": pages,
        "duration_ms": duration_ms,
        "model_durations_ms": tap.durations_ms,
    }


def _action_analyze(inputs: dict, tmp_dir: Path) -> dict:
    """Run PaddleOCR + YOLO page-number analysis.

    :param inputs: Handler input payload. Required: ``pdf_url``.
        Optional: ``exp_start`` / ``exp_end`` (ints), ``max_pages``.
    :param tmp_dir: Per-job scratch directory.
    :returns: ``{"results": list[dict], "page_count": int,
        "duration_ms": int}``.
    :rtype: dict
    """
    from blackletter.analyze import analyze_pdf
    from blackletter.api import ensure_weights

    pdf_url = inputs["pdf_url"]
    exp_start = inputs.get("exp_start")
    exp_end = inputs.get("exp_end")
    max_pages = int(inputs.get("max_pages", MAX_PAGES))

    ensure_weights(["large"])

    pdf_path = tmp_dir / "input.pdf"
    _download_pdf(pdf_url, pdf_path)

    pages = _page_count(pdf_path)
    if pages > MAX_PAGES:
        raise ValueError(
            f"PDF has {pages} pages, exceeds MAX_PAGES={MAX_PAGES}"
        )

    t0 = time.monotonic()
    # num_workers=1 is required: multiprocessing.Pool + Paddle + torch
    # fork-segfaults (see blackletter/analyze.py's comment near the
    # Pool() call). We only run on a single-GPU worker anyway.
    result = analyze_pdf(
        pdf_path,
        exp_start=exp_start,
        exp_end=exp_end,
        max_pages=max_pages,
        num_workers=1,
    )
    duration_ms = int((time.monotonic() - t0) * 1000)
    logger.info(
        "analyze OK: %d results in %d ms (%d pages)",
        len(result["results"]),
        duration_ms,
        pages,
    )
    # Return only `results`; seq_issues / duplicates / seen_nums etc.
    # are recomputed locally by the daemon (and have non-JSON-safe int
    # keys in the dict entries).
    return {
        "results": result["results"],
        "page_count": pages,
        "duration_ms": duration_ms,
    }


_ACTIONS = {
    "detect": _action_detect,
    "analyze": _action_analyze,
}


# ── Entry point ─────────────────────────────────────────────────────
def handler(job: dict) -> dict:
    """RunPod Serverless entry point.

    :param job: RunPod job dict. ``job["input"]`` must carry an
        ``action`` ("detect" | "analyze") and action-specific args.
        An optional ``scan_pk`` is used to tag Sentry events.
    :returns: Action-specific result dict. Every successful return
        (and every structured error) also carries ``worker_boot_ms``
        (cold-start cost of this worker process, constant per
        worker), ``worker_uptime_ms`` (ms since preload finished, at
        job start), and ``gpu_available`` (whether torch.cuda saw a
        device). On unknown action an ``{"error": ...}`` dict is
        returned (status COMPLETED with an error payload, so the
        client can distinguish "handler rejected input" from
        "handler crashed").
    :rtype: dict
    """
    inputs = job.get("input") or {}
    action = inputs.get("action")
    scan_pk = inputs.get("scan_pk")

    if not isinstance(action, str):
        return _with_worker_meta(
            {
                "error": f"missing or invalid 'action' in input: {action!r}",
                "error_code": "BAD_INPUT",
            }
        )

    _tag_sentry(job, action, scan_pk)

    # Fail fast if this worker got scheduled onto a CPU-only host.
    # Daemon-side maps error_code=NO_GPU to a transient error and
    # re-queues the scan so the next daemon tick gets a fresh worker.
    if not _CUDA_AVAILABLE:
        logger.error(
            "rejecting %s job for scan %s: no GPU on this worker",
            action,
            scan_pk,
        )
        return _with_worker_meta(
            {
                "error": "GPU unavailable on this worker",
                "error_code": "NO_GPU",
            }
        )

    fn = _ACTIONS.get(action)
    if fn is None:
        return _with_worker_meta(
            {
                "error": f"unknown action: {action!r}. Expected one of {sorted(_ACTIONS)}",
                "error_code": "UNKNOWN_ACTION",
            }
        )

    tmp_dir = Path(tempfile.mkdtemp(prefix="runpod-"))
    try:
        result = fn(inputs, tmp_dir)
        return _with_worker_meta(result)
    except Exception as exc:
        if sentry_sdk is not None:
            sentry_sdk.capture_exception(exc)
        logger.exception(
            "handler failed: action=%s scan_pk=%s", action, scan_pk
        )
        # Re-raise so RunPod marks the job FAILED with the traceback.
        # Worker meta is on the Sentry event via _tag_sentry; no
        # output dict to attach it to here.
        raise
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


if __name__ == "__main__":
    runpod.serverless.start({"handler": handler})
