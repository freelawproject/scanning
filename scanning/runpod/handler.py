"""RunPod Serverless handler for the blackletter GPU steps.

Dispatches on ``job["input"]["action"]`` to run one of:

- ``detect``: YOLO detection (small + medium + large) on a PDF
  fetched via presigned GET URL. Returns the merged detection list.
- ``analyze``: PaddleOCR + YOLO page-number analysis. Returns the
  per-page results list.

Result delivery has two modes, picked per job by the daemon:

- **S3** (``result_url`` present in the input): the payload is written
  to S3 with a single PUT against a presigned URL, and the response
  carries only the key, size, digest and timings. This is the path
  that lifts the ~20 MB inline response cap and outlives RunPod's
  result-retention window.
- **Inline** (``result_url`` absent): the payload comes back in the
  job response exactly as it always has. Keeps dev / CI and older
  daemons working.

Either way the worker needs no AWS credentials: a presigned PUT is a
capability handed in with the job, scoped to one key and one method.

Models are preloaded at module import time so cold start pays the
load cost once; subsequent warm invocations reuse in-process caches.
"""

from __future__ import annotations

import hashlib
import io
import json
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
from runpod_common import (
    TRANSIENT_TRANSFER_ERRORS,
    download_pdf,
    validate_pdf,
)

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
# Download tunables (HANDLER_DOWNLOAD_*) live in runpod_common.

# Result-upload budget. The GPU work is already paid for by the time we
# PUT, so a network blip must not cost a re-run: retry generously.
RESULT_UPLOAD_MAX_ATTEMPTS = int(
    os.environ.get("HANDLER_RESULT_UPLOAD_MAX_ATTEMPTS", "5")
)
RESULT_UPLOAD_TIMEOUT = int(
    os.environ.get("HANDLER_RESULT_UPLOAD_TIMEOUT", "300")
)
RESULT_UPLOAD_CONNECT_TIMEOUT = int(
    os.environ.get("HANDLER_RESULT_UPLOAD_CONNECT_TIMEOUT", "10")
)
RESULT_UPLOAD_BACKOFF_CAP = int(
    os.environ.get("HANDLER_RESULT_UPLOAD_BACKOFF_CAP", "30")
)

# Version of the JSON envelope written to S3. Bump when the envelope's
# shape changes incompatibly; the daemon refuses versions it doesn't
# know rather than consuming a payload it may misread.
RESULT_SCHEMA_VERSION = 1

# Content type sent on the result PUT. The daemon signs this exact
# value into the presigned URL (``_presign_result_put``), so the two
# must stay in lockstep: S3 answers SignatureDoesNotMatch if the header
# it verifies differs from the one that was signed.
RESULT_CONTENT_TYPE = "application/json"


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
        #
        # Logged at warning, not error: this is expected and fully
        # self-healing, so it must not raise a Sentry event. The daemon
        # escalates to error only if a scan actually exhausts its
        # transient retries.
        logger.warning(
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
    # Set unconditionally: tags live on the global scope, which a warm
    # worker reuses across jobs. Skipping the set when the value is
    # absent would leave the *previous* job's scan_pk/job id on every
    # event this job captures, misattributing its failures.
    scan_tag = "unknown"
    if scan_pk is not None:
        try:
            scan_tag = str(int(scan_pk))
        except (TypeError, ValueError):
            logger.warning("ignoring non-integer scan_pk: %r", scan_pk)
    sentry_sdk.set_tag("scan_pk", scan_tag)
    sentry_sdk.set_tag("runpod_job_id", str(job.get("id") or "unknown"))


# ── Result delivery ─────────────────────────────────────────────────
class ResultUploadError(RuntimeError):
    """The result object could not be written to S3.

    Surfaces to the daemon as ``error_code=RESULT_UPLOAD_FAILED``,
    which it classifies as transient: the job is resubmitted with a
    fresh presigned URL. The GPU run is lost, which is why
    :func:`_put_result` retries hard before raising this.
    """

    error_code = "RESULT_UPLOAD_FAILED"


class ResultUploadRejectedError(ResultUploadError):
    """S3 refused the write for a reason a re-run can't fix.

    Anything that isn't a 403 or a retryable 5xx/429: a wrong region in
    the signature, a bucket policy that demands a header we don't send,
    a malformed request. Surfaces as ``RESULT_UPLOAD_REJECTED``, which
    the daemon treats as **terminal**. That distinction is worth its
    keep -- these are configuration errors, and classifying them
    transient means the scan re-runs the GPU work (and pays for it)
    once per retry before failing with the same message anyway.
    """

    error_code = "RESULT_UPLOAD_REJECTED"


class ResultUrlExpiredError(ResultUploadError):
    """The presigned PUT URL is no longer valid (HTTP 403).

    Distinct from a generic upload failure because retrying the *same*
    URL can never succeed: the signature is dead. Mirrors how
    ``download_pdf`` treats a 403 on the input URL. Still
    transient daemon-side, since resubmitting mints a new signature.
    """

    error_code = "RESULT_URL_EXPIRED"


def _put_result(url: str, body: bytes) -> None:
    """Upload ``body`` to a presigned PUT URL, retrying transient errors.

    One PUT, one object: no multipart. A single PUT is atomic in S3, so
    the object only becomes gettable once every byte has landed. That is
    what lets the daemon treat "the object exists" as "the result is
    complete" when it recovers a job whose ``/status`` record is gone.

    Only accepts ``http(s)`` URLs, for the same defensive reason
    ``download_pdf`` does.

    :param url: Presigned S3 PUT URL covering exactly one key.
    :param body: Encoded JSON envelope.
    :raises ResultUrlExpiredError: On HTTP 403 (dead signature).
    :raises ResultUploadError: On any other non-2xx, or once the
        retries are exhausted.
    """
    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
        raise ResultUploadError(
            f"refusing to upload result to non-http(s) URL: {url!r}"
        )

    timeout = (RESULT_UPLOAD_CONNECT_TIMEOUT, RESULT_UPLOAD_TIMEOUT)
    last_error = ""

    for attempt in range(RESULT_UPLOAD_MAX_ATTEMPTS):
        try:
            r = requests.put(
                url,
                data=body,
                headers={"Content-Type": RESULT_CONTENT_TYPE},
                timeout=timeout,
            )
            # Expired or otherwise invalid signature. Retrying the same
            # URL is pointless; fail fast so the daemon re-signs.
            #
            # A bucket policy that rejects the PUT (a required
            # encryption header, say) also answers 403, and no amount
            # of re-signing fixes that -- it just burns the daemon's
            # transient retries, one paid GPU run each. S3's reason is
            # in the body, so log it rather than assuming expiry.
            if r.status_code == 403:
                raise ResultUrlExpiredError(
                    "presigned PUT rejected with HTTP 403 (expired "
                    "signature, or the bucket refused the write): "
                    f"{r.text[:300]}"
                )
            if r.status_code < 300:
                return
            # 5xx / 429 are S3 hiccups worth another attempt; anything
            # else is a configuration error that fails identically on
            # every retry, so stop and say so terminally.
            if r.status_code != 429 and r.status_code < 500:
                raise ResultUploadRejectedError(
                    f"result upload rejected with HTTP {r.status_code}: "
                    f"{r.text[:300]}"
                )
            last_error = f"HTTP {r.status_code}"
        except TRANSIENT_TRANSFER_ERRORS as exc:
            last_error = str(exc)

        if attempt == RESULT_UPLOAD_MAX_ATTEMPTS - 1:
            break
        backoff = min(2**attempt, RESULT_UPLOAD_BACKOFF_CAP)
        backoff += random.uniform(0, backoff / 2)  # jitter
        logger.warning(
            "result upload attempt %d/%d failed (%s); retrying in %.1fs",
            attempt + 1,
            RESULT_UPLOAD_MAX_ATTEMPTS,
            last_error,
            backoff,
        )
        time.sleep(backoff)

    raise ResultUploadError(
        f"result upload failed after {RESULT_UPLOAD_MAX_ATTEMPTS} "
        f"attempts: {last_error}"
    )


def _result_envelope(
    result: dict, action: str, scan_pk: Any, job_id: Any
) -> dict:
    """Wrap an action's result in the self-describing S3 envelope.

    The envelope exists so a consumer can tell *what* it just read
    without trusting the key it read it from: schema version, which
    action produced it, and which scan and job it belongs to. The
    daemon checks all of that before using the payload.

    :param result: The action's return dict.
    :param action: Handler action that produced it.
    :param scan_pk: Scan the job belongs to.
    :param job_id: RunPod job id, i.e. which run generated this object.
    :returns: The envelope to serialise.
    :rtype: dict
    """
    return {
        "schema_version": RESULT_SCHEMA_VERSION,
        "action": action,
        "scan_pk": scan_pk,
        "job_id": job_id,
        "payload": result,
    }


def _deliver_result(
    result: dict, inputs: dict, action: str, job: dict
) -> dict:
    """PUT the result to S3 and return the slim job response.

    Nothing is written to the worker's disk: the envelope is
    serialised in memory and streamed straight to S3.

    The response deliberately carries no copy of the payload. An
    inline fallback would reinstate the size cap this exists to escape
    and leave two sources of truth for one result. What's left is what
    the daemon needs to find the object and log how the run went.

    :param result: The action's return dict.
    :param inputs: Handler input payload (``result_url`` /
        ``result_key``).
    :param action: Handler action that produced ``result``.
    :param job: RunPod job dict, for the job id.
    :returns: ``{"status": "succeed", "action", "result_key", "bytes",
        "sha256", "upload_ms", ...timings}``.
    :rtype: dict
    :raises ResultUploadError: If the upload fails (see
        :func:`_put_result`).
    """
    envelope = _result_envelope(
        result, action, inputs.get("scan_pk"), job.get("id")
    )
    body = json.dumps(envelope, separators=(",", ":")).encode("utf-8")

    t0 = time.monotonic()
    _put_result(inputs["result_url"], body)
    upload_ms = int((time.monotonic() - t0) * 1000)

    result_key = inputs.get("result_key")
    logger.info(
        "result uploaded: %d bytes in %d ms (action=%s key=%s)",
        len(body),
        upload_ms,
        action,
        result_key,
    )
    return {
        "status": "succeed",
        "action": action,
        "result_key": result_key,
        "bytes": len(body),
        "sha256": hashlib.sha256(body).hexdigest(),
        "upload_ms": upload_ms,
        "duration_ms": result.get("duration_ms"),
        "model_durations_ms": result.get("model_durations_ms"),
        "page_count": result.get("page_count"),
    }


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
    from blackletter.api import detect

    pdf_url = inputs["pdf_url"]
    models = inputs.get("models") or ["small", "medium", "large"]
    confidence = float(inputs.get("confidence", 0.20))

    # No ensure_weights() call needed here: as of blackletter 0.1.1,
    # detect() ensures every requested weight itself (downloading from
    # HF if the baked ones are somehow absent) instead of silently
    # skipping missing models.

    pdf_path = tmp_dir / "input.pdf"
    download_pdf(pdf_url, pdf_path)

    pages = validate_pdf(pdf_path)
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
    download_pdf(pdf_url, pdf_path)

    pages = validate_pdf(pdf_path)
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
        An optional ``scan_pk`` is used to tag Sentry events. When
        ``result_url`` (presigned PUT) and ``result_key`` are present
        the payload goes to S3 and the response carries only the key,
        size, digest and timings; otherwise it comes back inline.
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
        # attempts) and the next tick lands on a different worker. Logged
        # at warning, not error: expected and self-healing, so no Sentry
        # event (the daemon escalates only on exhausted retries).
        logger.warning(
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
        if not inputs.get("result_url"):
            # No presigned PUT in the input: answer inline, as always.
            return _with_worker_meta(result)
        return _with_worker_meta(_deliver_result(result, inputs, action, job))
    except ResultUploadError as exc:
        # The compute succeeded and the delivery didn't, so this is
        # worth a Sentry event: the job will be re-run and re-billed.
        # Returned as a structured error rather than raised so the
        # daemon can read ``error_code`` and classify it as transient.
        if sentry_sdk is not None:
            sentry_sdk.capture_exception(exc)
        logger.error(
            "result delivery failed: action=%s scan_pk=%s key=%s: %s",
            action,
            scan_pk,
            inputs.get("result_key"),
            exc,
        )
        return _with_worker_meta(
            {"error": str(exc), "error_code": exc.error_code}
        )
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
