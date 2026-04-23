"""Pure action bodies for the GPU pod worker.

This module has no RunPod / FastAPI dependency. The FastAPI server in
``server.py`` imports from here, and so could a CLI or a direct
in-process caller.

Two actions, both invoked by ``server.py`` route handlers:

- :func:`action_detect` -- YOLO detection (small + medium + large) on
  a PDF fetched from a presigned GET URL. Returns the merged detection
  list plus a ``metrics`` block.
- :func:`action_analyze` -- PaddleOCR + YOLO page-number analysis.
  Returns the per-page results list plus a ``metrics`` block.

The module also exposes preload helpers so ``server.py`` can pay the
model-load cost on ``POST /warmup`` instead of inside the first real
scan: :func:`preload_yolo` and :func:`preload_paddle`.
"""

from __future__ import annotations

import io
import logging
import os
import re
import sys
import time
from pathlib import Path

import requests

logger = logging.getLogger("blackletter.pod")


# Capture process boot start as early as possible so ``boot_ms``
# covers the full cold-start cost (module imports + model preload).
# Uses monotonic() so the number isn't affected by wall-clock jumps.
_BOOT_START = time.monotonic()
_READY_AT: float | None = None

# Populated the first time :func:`detect_cuda` runs. Surfaced in every
# action response so the daemon can tell whether inference hit a GPU.
_CUDA_AVAILABLE: bool | None = None

# Per-model warmup durations, populated by :func:`preload_yolo`. Empty
# dict until warmup runs; on subsequent ``/warmup`` calls the numbers
# are overwritten with zero to communicate "already warm."
_YOLO_WARMUP_MS: dict[str, int] = {}
_YOLO_WARMED = False

# Paddle warmup duration, populated by :func:`preload_paddle` on first
# call. None until run; a subsequent call returns 0.
_PADDLE_WARMUP_MS: int | None = None
_PADDLE_WARMED = False


# ── Tunables ────────────────────────────────────────────────────────
MAX_PAGES = int(os.environ.get("HANDLER_MAX_PAGES", "5000"))
DOWNLOAD_TIMEOUT = int(os.environ.get("HANDLER_DOWNLOAD_TIMEOUT", "300"))


# ── GPU / boot state ────────────────────────────────────────────────
def detect_cuda() -> bool:
    """Return True if torch sees a CUDA device.

    Cached after first call so repeat checks are free.

    :returns: Whether inference can run on GPU.
    :rtype: bool
    """
    global _CUDA_AVAILABLE
    if _CUDA_AVAILABLE is not None:
        return _CUDA_AVAILABLE
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
    return _CUDA_AVAILABLE


def mark_ready() -> None:
    """Record the "process is ready to serve" timestamp.

    Called from ``server.py`` after FastAPI has wired up its routes
    but before the first request arrives. ``boot_ms`` is the delta
    between this and module import.

    :returns: None.
    """
    global _READY_AT
    _READY_AT = time.monotonic()
    logger.info("pod ready: boot_ms=%d", boot_ms())


def boot_ms() -> int:
    """Return milliseconds between module import and :func:`mark_ready`.

    :returns: Boot duration in ms; ``0`` if :func:`mark_ready` hasn't
        been called yet.
    :rtype: int
    """
    if _READY_AT is None:
        return 0
    return int((_READY_AT - _BOOT_START) * 1000)


def uptime_ms() -> int:
    """Return milliseconds since :func:`mark_ready` fired.

    :returns: Uptime in ms; ``0`` if the process isn't ready yet.
    :rtype: int
    """
    if _READY_AT is None:
        return 0
    return int((time.monotonic() - _READY_AT) * 1000)


# ── Preload helpers (called by /warmup) ─────────────────────────────
def preload_yolo() -> dict[str, int]:
    """Load each YOLO weight once to warm CUDA kernels and caches.

    Idempotent: subsequent calls return ``{name: 0}`` for every model
    already warmed. The first call populates ``_YOLO_WARMUP_MS`` with
    real per-model durations.

    :returns: ``{"small": ms, "medium": ms, "large": ms}`` on first
        call; zeros on subsequent calls.
    :rtype: dict[str, int]
    """
    global _YOLO_WARMED
    if _YOLO_WARMED:
        return {name: 0 for name in _YOLO_WARMUP_MS}

    from blackletter.api import ensure_weights

    if not detect_cuda():
        logger.error(
            "GPU not available; skipping YOLO preload. Jobs on this pod "
            "will fail fast with error_code=NO_GPU."
        )
        return {}

    t0 = time.monotonic()
    paths = ensure_weights()
    logger.info(
        "ensure_weights OK in %.1fs (%s)",
        time.monotonic() - t0,
        ", ".join(
            f"{k}={v.stat().st_size // (1024 * 1024)}MB"
            for k, v in paths.items()
        ),
    )

    import blackletter
    from ultralytics import YOLO

    weights_dir = Path(blackletter.__file__).resolve().parent / "weights"
    for name in ("small", "medium", "large"):
        p = weights_dir / f"{name}.pt"
        if not p.is_file():
            continue
        t0 = time.monotonic()
        YOLO(str(p))
        dur_ms = int((time.monotonic() - t0) * 1000)
        _YOLO_WARMUP_MS[name] = dur_ms
        logger.info("warmed YOLO %s in %d ms", name, dur_ms)

    _YOLO_WARMED = True
    return dict(_YOLO_WARMUP_MS)


def preload_paddle() -> int:
    """Load PaddleOCR once to warm CUDA kernels and weight caches.

    Idempotent: subsequent calls return ``0``.

    Preloading Paddle before any YOLO work is risky on long detect
    runs (issue #42): Paddle's idle CUDA allocator can fire a cleanup
    pass while torch is driving the GPU, producing
    ``cudaErrorInitializationError`` and SIGABRT.
    :func:`preload_paddle` should only be called when no concurrent
    detect is in flight (e.g. during ``POST /warmup``), or left to
    :func:`action_analyze`'s own lazy load on the first analyze call.

    :returns: Load duration in ms on first call; ``0`` on subsequent
        calls.
    :rtype: int
    """
    global _PADDLE_WARMED, _PADDLE_WARMUP_MS
    if _PADDLE_WARMED:
        return 0
    if not detect_cuda():
        logger.error("GPU not available; skipping Paddle preload.")
        return 0

    t0 = time.monotonic()
    try:
        from paddleocr import PaddleOCR

        PaddleOCR(
            text_detection_model_name="PP-OCRv5_server_det",
            text_recognition_model_name="PP-OCRv5_server_rec",
            use_textline_orientation=False,
            use_doc_orientation_classify=False,
            use_doc_unwarping=False,
            enable_mkldnn=False,
        )
    except Exception:
        logger.exception("Paddle preload failed (first analyze will pay it)")
        return 0
    dur_ms = int((time.monotonic() - t0) * 1000)
    _PADDLE_WARMUP_MS = dur_ms
    _PADDLE_WARMED = True
    logger.info("warmed PaddleOCR in %d ms", dur_ms)
    return dur_ms


def yolo_warmup_ms() -> dict[str, int]:
    """Return the per-model warmup durations captured by the last
    successful :func:`preload_yolo`.

    :returns: ``{model_name: ms}``; empty dict if never warmed.
    :rtype: dict[str, int]
    """
    return dict(_YOLO_WARMUP_MS)


def paddle_warmup_ms() -> int | None:
    """Return the Paddle warmup duration captured by :func:`preload_paddle`.

    :returns: Milliseconds, or ``None`` if Paddle has never been warmed.
    :rtype: int | None
    """
    return _PADDLE_WARMUP_MS


def yolo_warmed() -> bool:
    """Whether :func:`preload_yolo` has ever completed successfully.

    :returns: ``True`` if YOLO models are in memory / CUDA is warm.
    :rtype: bool
    """
    return _YOLO_WARMED


def paddle_warmed() -> bool:
    """Whether :func:`preload_paddle` has ever completed successfully.

    :returns: ``True`` if Paddle is in memory.
    :rtype: bool
    """
    return _PADDLE_WARMED


# ── PDF helpers ─────────────────────────────────────────────────────
def _download_pdf(url: str, dest: Path) -> int:
    """Download a PDF from a presigned GET URL to ``dest``.

    :param url: Presigned GET URL.
    :param dest: Local filesystem target.
    :returns: Download duration in ms.
    :rtype: int
    :raises requests.HTTPError: On non-2xx response.
    """
    t0 = time.monotonic()
    with requests.get(url, stream=True, timeout=DOWNLOAD_TIMEOUT) as r:
        r.raise_for_status()
        with dest.open("wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    f.write(chunk)
    return int((time.monotonic() - t0) * 1000)


def _page_count(pdf_path: Path) -> int:
    """Return the number of pages in a PDF.

    :param pdf_path: Path to the PDF on disk.
    :returns: Page count.
    :rtype: int
    """
    import fitz

    with fitz.open(str(pdf_path)) as doc:
        return doc.page_count


# ── Per-model timing tap for detect() ───────────────────────────────
#
# blackletter's ``detect()`` prints lines of the form:
#     "  Detecting with {model}..."        (start)
#     "    {model} done ({N}s)"            (end)
# We wrap stdout with :class:`_DetectStdoutTap` for the duration of
# the call so we can measure per-model wall clock with millisecond
# resolution. Best-effort: if blackletter ever changes its log format
# these regexes stop matching and per-model durations come back empty;
# the total ``detect_ms`` stays accurate regardless.
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


# ── Actions ─────────────────────────────────────────────────────────
def action_detect(inputs: dict, tmp_dir: Path) -> dict:
    """Run YOLO detection and return the merged detection list.

    :param inputs: Handler input payload. Required: ``pdf_url``.
        Optional: ``models`` (list, default all three),
        ``confidence`` (float, default 0.20).
    :param tmp_dir: Per-job scratch directory.
    :returns: ``{"detections": list[dict], "page_count": int,
        "metrics": {...}}``. The ``metrics`` block contains
        ``download_ms``, ``yolo_inference_ms`` (per model),
        ``yolo_warmup_ms`` (per model, zeroed after first warmup),
        ``postprocess_ms``, ``detect_ms`` (total spent in
        ``blackletter.detect``), and ``total_ms`` (full call).
    :rtype: dict
    """
    from blackletter.api import detect, ensure_weights

    t_start = time.monotonic()
    pdf_url = inputs["pdf_url"]
    models = inputs.get("models") or ["small", "medium", "large"]
    confidence = float(inputs.get("confidence", 0.20))

    # Defensive: if image was built without large.pt, fetch it now.
    ensure_weights([m for m in models if m == "large"])

    pdf_path = tmp_dir / "input.pdf"
    download_ms = _download_pdf(pdf_url, pdf_path)

    pages = _page_count(pdf_path)
    if pages > MAX_PAGES:
        raise ValueError(
            f"PDF has {pages} pages, exceeds MAX_PAGES={MAX_PAGES}"
        )

    real_stdout = sys.stdout
    tap = _DetectStdoutTap(real_stdout)
    sys.stdout = tap
    t_detect = time.monotonic()
    try:
        detections = detect(
            pdf_path, tmp_dir, models=models, confidence=confidence
        )
    finally:
        sys.stdout = real_stdout
    detect_ms = int((time.monotonic() - t_detect) * 1000)

    total_ms = int((time.monotonic() - t_start) * 1000)
    # postprocess_ms approximates time spent after inference but
    # before return (e.g. merging model outputs). We don't have a
    # clean hook inside blackletter, so compute it as the residual
    # of detect_ms minus the sum of per-model inference windows.
    per_model_total = sum(tap.durations_ms.values())
    postprocess_ms = max(detect_ms - per_model_total, 0)

    logger.info(
        "detect OK: %d detections in %d ms (%d pages, models=%s, per-model=%s)",
        len(detections),
        total_ms,
        pages,
        models,
        tap.durations_ms,
    )
    return {
        "detections": detections,
        "page_count": pages,
        "metrics": {
            "download_ms": download_ms,
            "yolo_inference_ms": tap.durations_ms,
            "yolo_warmup_ms": yolo_warmup_ms(),
            "postprocess_ms": postprocess_ms,
            "detect_ms": detect_ms,
            "total_ms": total_ms,
        },
    }


def action_analyze(inputs: dict, tmp_dir: Path) -> dict:
    """Run PaddleOCR + YOLO page-number analysis.

    :param inputs: Handler input payload. Required: ``pdf_url``.
        Optional: ``exp_start`` / ``exp_end`` (ints), ``max_pages``.
    :param tmp_dir: Per-job scratch directory.
    :returns: ``{"results": list[dict], "page_count": int,
        "metrics": {...}}``. The ``metrics`` block contains
        ``download_ms``, ``paddle_warmup_ms`` (lazy-warmed on first
        analyze if /warmup didn't warm it; subsequent calls see 0),
        ``analyze_ms`` (total spent in ``blackletter.analyze_pdf``),
        and ``total_ms`` (full call).
    :rtype: dict
    """
    from blackletter.analyze import analyze_pdf
    from blackletter.api import ensure_weights

    t_start = time.monotonic()
    pdf_url = inputs["pdf_url"]
    exp_start = inputs.get("exp_start")
    exp_end = inputs.get("exp_end")
    max_pages = int(inputs.get("max_pages", MAX_PAGES))

    ensure_weights(["large"])

    pdf_path = tmp_dir / "input.pdf"
    download_ms = _download_pdf(pdf_url, pdf_path)

    pages = _page_count(pdf_path)
    if pages > MAX_PAGES:
        raise ValueError(
            f"PDF has {pages} pages, exceeds MAX_PAGES={MAX_PAGES}"
        )

    # Snapshot whether Paddle was warm before this call so we can
    # report the first-call load cost as ``paddle_warmup_ms``. If a
    # prior /warmup already loaded Paddle, this reads 0.
    paddle_was_warm = paddle_warmed()

    t_analyze = time.monotonic()
    # num_workers=1 is required: multiprocessing.Pool + Paddle + torch
    # fork-segfaults. We only run on a single-GPU pod anyway.
    result = analyze_pdf(
        pdf_path,
        exp_start=exp_start,
        exp_end=exp_end,
        max_pages=max_pages,
        num_workers=1,
    )
    analyze_ms = int((time.monotonic() - t_analyze) * 1000)
    total_ms = int((time.monotonic() - t_start) * 1000)

    logger.info(
        "analyze OK: %d results in %d ms (%d pages)",
        len(result["results"]),
        total_ms,
        pages,
    )
    # blackletter.analyze lazy-loads PaddleOCR inside analyze_pdf. If
    # this was the first analyze call in the process, mark paddle
    # warm so subsequent calls report a 0 warmup cost. We don't know
    # the exact lazy-load duration (no hook), so attribute it to the
    # overall analyze_ms.
    if not paddle_was_warm:
        global _PADDLE_WARMED
        _PADDLE_WARMED = True

    return {
        "results": result["results"],
        "page_count": pages,
        "metrics": {
            "download_ms": download_ms,
            "paddle_warmup_ms": paddle_warmup_ms() or 0,
            "paddle_lazy_loaded_this_call": not paddle_was_warm,
            "analyze_ms": analyze_ms,
            "total_ms": total_ms,
        },
    }
