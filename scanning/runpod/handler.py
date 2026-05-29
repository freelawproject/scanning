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
import random
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import requests
import runpod
from urllib3.exceptions import IncompleteRead, ProtocolError

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
# Read timeout: max seconds to wait for the next chunk of body. A single
# value would also bound connect; we split connect/read so a stalled
# socket mid-download raises a ReadTimeout promptly and triggers a
# resume instead of hanging for the whole budget.
DOWNLOAD_TIMEOUT = int(os.environ.get("HANDLER_DOWNLOAD_TIMEOUT", "300"))
DOWNLOAD_CONNECT_TIMEOUT = int(
    os.environ.get("HANDLER_DOWNLOAD_CONNECT_TIMEOUT", "10")
)
# How many times to (re)issue the GET before giving up. Each retry
# resumes from the last byte written via a Range request.
DOWNLOAD_MAX_ATTEMPTS = int(
    os.environ.get("HANDLER_DOWNLOAD_MAX_ATTEMPTS", "5")
)
# Upper bound (seconds) on the exponential backoff between retries.
DOWNLOAD_BACKOFF_CAP = int(
    os.environ.get("HANDLER_DOWNLOAD_BACKOFF_CAP", "30")
)

# Stream errors that mean "the connection died mid-transfer" — safe to
# reconnect and resume from the last byte written. Anything else (a 4xx
# other than the handled 403/416, a 5xx, a malformed URL) is not in here
# and propagates so the daemon can re-queue or fail the scan.
_TRANSIENT_DOWNLOAD_ERRORS = (
    requests.exceptions.ChunkedEncodingError,
    requests.exceptions.ConnectionError,
    requests.exceptions.Timeout,
    ProtocolError,
    IncompleteRead,
)


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
        # The fitness check (_require_gpu) blocks this worker from the
        # available pool, so queued jobs route to healthy GPU workers.
        # If a job does leak through it returns error_code=NO_GPU, which
        # the scanning daemon classifies as transient and re-queues the
        # scan (up to RUNPOD_MAX_TRANSIENT_RETRIES attempts) for the next
        # tick to resubmit to a different worker.
        logger.error(
            "GPU not available; skipping weight/model preload. Jobs "
            "will return error_code=NO_GPU; scans are re-queued "
            "automatically by the daemon."
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


# ── Fitness check ────────────────────────────────────────────────────
@runpod.serverless.register_fitness_check
def _require_gpu() -> None:
    """Exit before accepting jobs if no GPU is available.

    Runs at startup before RunPod's heartbeat, so the worker is never
    added to the available pool and no jobs are ever assigned to it.
    Queued jobs are picked up automatically by healthy GPU workers.
    """
    if not _CUDA_AVAILABLE:
        raise RuntimeError(
            "GPU not available on this worker. Exiting to avoid CPU-only billing."
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
    """Tag the current Sentry scope with job-identifying context.

    Every exception captured during a handler invocation will carry
    these tags, making it easy to correlate a Sentry event with the
    specific scan and RunPod job that triggered it.

    :param job: RunPod job dict (expects an ``id`` field).
    :param action: The handler action being executed.
    :param scan_pk: Primary key of the scan this job belongs to.
    """
    if sentry_sdk is None:
        return
    sentry_sdk.set_tag("action", action)
    sentry_sdk.set_tag("gpu_available", str(_CUDA_AVAILABLE))
    sentry_sdk.set_tag("worker_boot_ms", str(_WORKER_BOOT_MS))
    sentry_sdk.set_tag("worker_uptime_ms", str(_worker_uptime_ms()))
    if scan_pk is not None:
        try:
            sentry_sdk.set_tag("scan_pk", str(int(scan_pk)))
        except (TypeError, ValueError):
            logger.warning("ignoring non-integer scan_pk: %r", scan_pk)
    job_id = job.get("id")
    if job_id:
        sentry_sdk.set_tag("runpod_job_id", str(job_id))


def _expected_total(resp: requests.Response) -> int | None:
    """Best-effort total file size, in bytes, from a download response.

    On a ``206 Partial Content`` the ``Content-Length`` header is only
    the size of *this* chunk, so the full size has to come from the
    ``Content-Range`` total (the value after the slash in
    ``bytes 0-1023/2048`` -> ``2048``). On a plain ``200 OK`` the
    ``Content-Length`` is the full size.

    :param resp: A streamed download response.
    :returns: Total size in bytes, or ``None`` if no header is present
        or parseable (in which case the caller skips the completeness
        check rather than failing a download that may be fine).
    :rtype: int | None
    """
    content_range = resp.headers.get("Content-Range")
    if content_range and "/" in content_range:
        total = content_range.rsplit("/", 1)[1].strip()
        if total.isdigit():
            return int(total)
    content_length = resp.headers.get("Content-Length")
    if content_length and content_length.strip().isdigit():
        return int(content_length)
    return None


def _download_pdf(url: str, dest: Path) -> None:
    """Download a PDF from a presigned GET URL to ``dest``, resuming
    across dropped connections.

    Large scans (multi-GB, 1000+ pages) take long enough that a single
    streamed GET routinely dies mid-transfer: the socket stalls, S3
    resets the connection, or urllib3 raises ``IncompleteRead``. Rather
    than restart the whole transfer (or fail the job) on every blip,
    this retries with an HTTP ``Range`` request and appends to the
    partial file from the last byte written, with exponential backoff
    between attempts.

    Range-response handling:

    - **206 Partial Content** — server honoured ``Range``; append.
    - **200 OK** (on a resume) — server ignored ``Range`` and is
      resending the whole body; truncate and restart from byte 0.
      Appending here would write a second copy of the head and corrupt
      the PDF, so this guard is load-bearing.
    - **416 Range Not Satisfiable** — our offset is at/past EOF, i.e.
      we already hold the whole file; treat as complete.
    - **403 Forbidden** — the presigned URL has expired or is invalid.
      Not retryable here (the URL is dead): raise so RunPod marks the
      job FAILED and the daemon re-queues the scan with a fresh URL.

    Only accepts ``http://`` or ``https://`` URLs. Rejects everything
    else (``file://``, ``gopher://``, bare paths, etc.) defensively:
    today the trust boundary is "only the daemon holds the RunPod API
    key, so only the daemon can submit jobs," but a scheme check
    costs nothing and eliminates the obvious SSRF / local-file-read
    class of bugs if that boundary ever weakens.

    :param url: PDF URL (typically a presigned S3 GET URL).
    :param dest: Local filesystem target.
    :raises ValueError: If ``url`` is not http(s).
    :raises requests.HTTPError: On a non-2xx response other than the
        handled 403/416 cases.
    :raises RuntimeError: If the presigned URL is expired (403), or if
        the file on disk is still short of the expected size after
        ``DOWNLOAD_MAX_ATTEMPTS`` attempts.
    """
    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
        raise ValueError(
            f"refusing to download PDF from non-http(s) URL: {url!r}"
        )

    timeout = (DOWNLOAD_CONNECT_TIMEOUT, DOWNLOAD_TIMEOUT)
    offset = 0
    total: int | None = None

    for attempt in range(DOWNLOAD_MAX_ATTEMPTS):
        headers = {"Range": f"bytes={offset}-"} if offset else {}
        try:
            with requests.get(
                url, headers=headers, stream=True, timeout=timeout
            ) as r:
                # Expired/invalid presigned URL. Retrying the same URL is
                # pointless; raise so the daemon re-signs and re-queues.
                if r.status_code == 403:
                    raise RuntimeError(
                        "presigned URL expired or invalid (HTTP 403); "
                        "daemon will re-queue with a fresh URL"
                    )
                # We asked to resume past EOF -> already have it all.
                if offset and r.status_code == 416:
                    logger.info(
                        "resume at byte %d returned 416; file already "
                        "complete",
                        offset,
                    )
                    break
                # Server ignored Range and is resending from the start.
                # Discard the partial file and restart, or we'd append a
                # second copy of the head and corrupt the PDF.
                if offset and r.status_code == 200:
                    logger.warning(
                        "server ignored Range (HTTP 200); restarting "
                        "download from byte 0"
                    )
                    offset = 0
                    total = None
                r.raise_for_status()

                if total is None:
                    total = _expected_total(r)

                mode = "ab" if offset else "wb"
                with dest.open(mode) as f:
                    for chunk in r.iter_content(chunk_size=1024 * 1024):
                        if chunk:
                            f.write(chunk)
                            offset += len(chunk)
            # Stream drained without raising: this attempt finished.
            break
        except _TRANSIENT_DOWNLOAD_ERRORS as exc:
            # Re-derive the offset from disk: a partial write may have
            # landed bytes the in-memory counter didn't account for.
            offset = dest.stat().st_size if dest.exists() else 0
            if attempt == DOWNLOAD_MAX_ATTEMPTS - 1:
                logger.error(
                    "download failed after %d attempts at byte %d: %s",
                    DOWNLOAD_MAX_ATTEMPTS,
                    offset,
                    exc,
                )
                raise
            backoff = min(2**attempt, DOWNLOAD_BACKOFF_CAP)
            backoff += random.uniform(0, backoff / 2)  # jitter
            logger.warning(
                "download interrupted at byte %d (attempt %d/%d): %s; "
                "resuming in %.1fs",
                offset,
                attempt + 1,
                DOWNLOAD_MAX_ATTEMPTS,
                exc,
                backoff,
            )
            time.sleep(backoff)

    # Hand analyze_pdf/detect a complete file or nothing: a truncated PDF
    # silently produces wrong page counts and detections. Raising re-queues.
    if total is not None:
        actual = dest.stat().st_size if dest.exists() else 0
        if actual != total:
            raise RuntimeError(
                f"incomplete download: {actual}/{total} bytes after "
                f"{DOWNLOAD_MAX_ATTEMPTS} attempts"
            )


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

    def fileno(self) -> int:
        return self._real.fileno()

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
        {model_name: int, ...}}``. Per-model durations are populated
        only for models that actually ran, as parsed from stdout.
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
        device). On bad/unknown input an ``{"error": ..., "error_code":
        ...}`` dict is returned; the SDK moves ``error`` to the top
        level and RunPod marks the job ``FAILED``. The daemon reads
        ``error_code`` from ``output`` to distinguish transient errors
        (re-queue) from terminal ones (mark scan ERROR).
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

    # Belt-and-suspenders: fitness check should prevent CPU-only workers
    # from ever reaching this point, but handle it defensively in case
    # CUDA becomes unavailable after startup.
    if not _CUDA_AVAILABLE:
        # error_code=NO_GPU is in scanning's _TRANSIENT_ERROR_CODES, so
        # the daemon re-queues the scan (up to RUNPOD_MAX_TRANSIENT_RETRIES
        # attempts) and the next tick lands on a different worker.
        logger.error(
            "rejecting %s job for scan %s: no GPU on this worker; "
            "daemon will re-queue.",
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
