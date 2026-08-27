"""RunPod Serverless handler for the blackletter detection step.

Dispatches on ``job["input"]["action"]``. There is one action:

- ``detect``: fetch a PDF through a presigned GET URL, run YOLO over
  every page, and return the merged detection list.

The model is ``bl_warm``, one 18-class checkpoint that replaced the
small/medium/large trio (blackletter #73). Its weight file is the only
one baked into the image, so a run of the legacy trio needs a rebuild,
not an input change. Input reads the **original** shard, never the
bitonal copy: the large region classes of bl-warm collapse on 1-bit
pages.

The ``analyze`` action of the previous image is gone with the legacy
pipeline (#173). Page numbers now come from dots.mocr (#149), so this
image carries no PaddleOCR and no tesseract.

Result delivery has two shapes, chosen by the caller:

- **S3** (``result_url`` present): the payload goes to a presigned PUT
  and the response carries only the key, the size and the summary. A
  volume of detections can approach RunPod's ~20 MB response cap, and
  RunPod discards a job record about 30 minutes after the job ends.
- **Inline** (``result_url`` absent): the payload comes back in the
  response. This is what a local test and continuous integration use,
  and what an older caller gets, so a rollback needs no daemon change.

Either way the worker needs no AWS credentials: a presigned PUT is a
capability handed in with the job, scoped to one key and one method.
"""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any

import runpod
from runpod_common import (
    CorruptDownloadError,
    ResultUploadError,
    download_pdf,
    upload_result,
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
# caller can tell whether inference actually hit a GPU.
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
# Hard sanity guard on input size, worker-side. Deliberately NOT a
# caller-tunable "process at most N pages" knob: the established
# ``max_pages`` contract means *truncate*, and a rejection threshold
# under the same name would invite silent contract confusion. Volumes
# arrive sharded (#164), so a shard over this is a pipeline bug.
MAX_PAGES = int(os.environ.get("HANDLER_MAX_PAGES", "5000"))
# Download tunables (HANDLER_DOWNLOAD_*) live in runpod_common.

# Weights this worker runs when the input names none. A tuple, and the
# only name baked into the image; see ``_missing_weights``.
DEFAULT_MODELS = ("bl_warm",)

# Detection floor, matching ``blackletter.scanner.CONFIDENCE_THRESHOLD``.
# The per-label gates that actually shape the redactions live in
# blackletter and are applied downstream, so this only has to be low
# enough not to hide a row those gates would keep.
DEFAULT_CONFIDENCE = 0.20

# Render resolution and batch size are deliberately absent. blackletter
# fixes them (``scanner.DPI = 200``, ``scanner.YOLO_BATCH = 4``), and
# the dpi matches DOCTOR_BITONAL_DPI and the dots.mocr constant so every
# bounding box in the corpus describes the same pixel space.
#
# ``imgsz`` is absent for a different reason: the checkpoint carries it.
# Ultralytics keeps the training value when it loads a ``.pt``
# (``Model._reset_ckpt_args``) and ``predict`` merges those overrides
# ahead of its own defaults, which name no ``imgsz``. bl_warm was
# trained at 1024, so that is what it predicts at. Passing 640 -- the
# library default -- would quietly cost accuracy on the small classes.


# ── Weights ─────────────────────────────────────────────────────────
def _weights_dir() -> Path:
    """Return the directory blackletter resolves its weights from.

    :returns: ``<blackletter package>/weights``.
    :rtype: Path
    """
    import blackletter

    return Path(blackletter.__file__).resolve().parent / "weights"


def _missing_weights(models: list[str]) -> list[str]:
    """Return the requested model names that have no weight file.

    The check exists because ``api.detect`` calls ``ensure_weights``
    itself, which would reach Hugging Face at run time for a name it
    cannot resolve locally. On a serverless worker that is a slow job
    or a failed one, far from its cause, so an unbaked name is refused
    up front instead.

    :param models: Requested blackletter weight names.
    :returns: The subset with no ``.pt`` file in the image.
    :rtype: list[str]
    """
    weights_dir = _weights_dir()
    return [n for n in models if not (weights_dir / f"{n}.pt").is_file()]


# ── Cold-start preload ──────────────────────────────────────────────
def _preload() -> None:
    """Pay the import and weight-read cost at module import.

    Logs CUDA availability, then opens each default weight once so the
    first job pays neither the ultralytics import nor the file read.
    blackletter builds its own model instance per call, so this warms
    the process rather than handing anything over.

    Every phase is wrapped, so a preload failure cannot stop the worker
    from starting; the first real job retries the load and surfaces the
    real error.
    """
    global _CUDA_AVAILABLE

    # GPU check first. This worker is GPU-only: with no CUDA we skip the
    # warmup entirely and let handler() answer NO_GPU, so the caller can
    # retry on a different worker.
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
        # the caller classifies as transient and retries.
        #
        # Logged at warning, not error: this is expected and fully
        # self-healing, so it must not raise a Sentry event.
        logger.warning(
            "GPU not available; skipping the weight preload. Jobs will "
            "return error_code=NO_GPU; the caller retries them on "
            "another worker."
        )
        return

    try:
        from ultralytics import YOLO

        weights_dir = _weights_dir()
        for name in DEFAULT_MODELS:
            path = weights_dir / f"{name}.pt"
            if not path.is_file():
                # The build bakes this file and verifies it, so a miss
                # here means the image is wrong. Loud, but not fatal:
                # the job's own refusal names it too.
                logger.error("weight file is missing from the image: %s", path)
                continue
            t0 = time.monotonic()
            model = YOLO(str(path))
            # imgsz comes from the checkpoint, not from our code, so log
            # it. A file that lost its training arguments falls back to
            # the library default of 640 with no error and a lower
            # score, and this line is what makes that visible.
            logger.info(
                "warmed YOLO %s in %.1fs (imgsz=%s, classes=%d)",
                name,
                time.monotonic() - t0,
                model.overrides.get("imgsz"),
                len(model.names or {}),
            )
    except Exception:
        logger.exception("YOLO preload failed (the first job will be slower)")


_preload()

# Freeze the cold-start cost once the process is ready. ``_WORKER_BOOT_MS``
# is constant per worker; ``_WORKER_READY_AT`` anchors per-job uptime so
# the caller can tell cold from warm calls (same boot_ms + increasing
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

    Used for both successful and structured-error returns so the caller
    can always see whether this job hit a warm worker and whether
    inference ran on GPU.

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

    Every exception captured during a handler invocation carries these
    tags, so an event points at the scan and the RunPod job that
    triggered it.

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


def _read_models(inputs: dict) -> list[str]:
    """Return the weight names this job runs, validated.

    :param inputs: Handler input payload.
    :returns: Model names, every one of them baked into the image.
    :rtype: list[str]
    :raises ValueError: If the value is not a list of strings, or names
        a weight the image does not carry.
    """
    models = inputs.get("models") or list(DEFAULT_MODELS)
    if isinstance(models, str):
        models = [models]
    if not isinstance(models, list) or not all(
        isinstance(name, str) and name for name in models
    ):
        raise ValueError(
            f"'models' must be a list of weight names, got {models!r}"
        )
    missing = _missing_weights(models)
    if missing:
        available = sorted(p.stem for p in _weights_dir().glob("*.pt"))
        raise ValueError(
            f"weights not in this image: {missing}. Available: {available}. "
            "Bake the weight into the image rather than downloading it at "
            "run time."
        )
    return models


def _action_detect(job: dict, inputs: dict, tmp_dir: Path) -> dict:
    """Run YOLO detection over a PDF and return the merged rows.

    :param job: RunPod job dict (used for the progress update).
    :param inputs: Handler input payload. Required: ``pdf_url``.
        Result delivery (handled by :func:`_deliver`, not here):
        ``result_url`` and ``result_key``. Optional: ``models``
        (default ``["bl_warm"]``) and ``confidence`` (default 0.20).
        There is deliberately no ``max_pages``: a partial detection
        merged as a whole volume is worse than a failure. An input over
        the env-level ``MAX_PAGES`` is rejected.
    :param tmp_dir: Per-job scratch directory.
    :returns: ``{"detections": list[dict], "page_count": int,
        "models": list[str], "duration_ms": int}``. Each detection
        carries ``page_index`` (counted from zero **inside this PDF**,
        so a caller working on shards offsets it by the shard's own
        first page), ``label``, ``label_id``, ``confidence``, ``bbox``,
        ``img_width``/``img_height`` and the ``found_by`` provenance
        blackletter's merge produces.
    :rtype: dict
    :raises ValueError: On any bad input.
    """
    from blackletter.api import detect

    # ``.get`` + explicit raise, not ``inputs["pdf_url"]``: a KeyError
    # would surface as a raw traceback with no error_code, which the
    # caller can't classify. handler() turns ValueError into BAD_INPUT.
    pdf_url = inputs.get("pdf_url")
    if not pdf_url:
        raise ValueError("missing required input: pdf_url")
    models = _read_models(inputs)
    confidence = float(inputs.get("confidence", DEFAULT_CONFIDENCE))
    if not 0 < confidence <= 1:
        raise ValueError(f"'confidence' must be in (0, 1], got {confidence!r}")

    pdf_path = tmp_dir / "input.pdf"
    download_pdf(pdf_url, pdf_path)

    pages = validate_pdf(pdf_path)
    if pages > MAX_PAGES:
        raise ValueError(
            f"PDF has {pages} pages, exceeds MAX_PAGES={MAX_PAGES}"
        )

    # One update, not per page: ``api.detect`` is a single blocking call
    # with no page callback, and the only earlier reading of its
    # progress parsed blackletter's printed prose, which broke whenever
    # that prose changed.
    try:
        runpod.serverless.progress_update(
            job, f"detecting {pages} pages with {','.join(models)}"
        )
    except Exception:
        # Progress is best-effort; never fail a job over it.
        pass

    t0 = time.monotonic()
    detections = detect(
        pdf_path, tmp_dir, models=models, confidence=confidence
    )
    duration_ms = int((time.monotonic() - t0) * 1000)

    logger.info(
        "detect OK: %d detections in %d ms (%d pages, models=%s, conf=%.2f)",
        len(detections),
        duration_ms,
        pages,
        models,
        confidence,
    )
    return {
        "detections": detections,
        "page_count": pages,
        "models": models,
        "duration_ms": duration_ms,
    }


# ── Result delivery ─────────────────────────────────────────────────
# Version of the result envelope. A caller reading an object written
# here checks this before it trusts the payload, so bump it only when
# the payload's shape changes -- and expect the caller to treat an
# unknown version as "worker deployed ahead of the daemon" rather than
# as a bad result.
RESULT_SCHEMA_VERSION = 1

# Content type sent on the result PUT. Signed into the presigned URL by
# the caller, so this constant and its signing parameter must stay in
# lockstep: a mismatch is a 403 that reads like an expired signature.
RESULT_CONTENT_TYPE = "application/json"

# Summary fields kept in the job response when the payload goes to S3.
# Everything else -- above all ``detections`` -- is deliberately
# dropped: the response is capped at about 20 MB and discarded with the
# job record, which is the whole reason the payload travels through S3.
_SUMMARY_FIELDS = ("page_count", "models", "duration_ms")


def _deliver(result: dict, inputs: dict, scan_pk: Any) -> dict:
    """Send a result to S3 when asked, and answer with a summary.

    Two shapes, chosen by the caller and not by us:

    - ``result_url`` present -> wrap the payload in a self-describing
      envelope, PUT it, and return only the summary plus the key. This
      is what a volume-sized detection needs.
    - absent -> return the payload inline. That path is what dev and
      continuous integration use without credentials, and what a caller
      running an older contract gets, so rolling the image back needs no
      daemon change.

    :param result: The action's own return value.
    :param inputs: The handler input payload.
    :param scan_pk: The scan the job belongs to, for the envelope.
    :returns: What to answer RunPod with.
    :rtype: dict
    :raises ResultUploadError: If the upload failed. The caller turns
        its ``error_code`` into a retry or a terminal failure.
    """
    result_url = inputs.get("result_url")
    if not result_url:
        return result

    envelope = {
        "schema_version": RESULT_SCHEMA_VERSION,
        "action": inputs.get("action"),
        "scan_pk": scan_pk,
        # Echoed so a reader can confirm the object is the one it asked
        # for, without trusting the key it happens to be stored under.
        "result_key": inputs.get("result_key"),
        "payload": result,
    }
    size = upload_result(result_url, envelope, RESULT_CONTENT_TYPE)

    summary = {
        field: result[field] for field in _SUMMARY_FIELDS if field in result
    }
    # Kept because it is small and it is what a caller checks first.
    summary["detection_count"] = len(result.get("detections") or [])
    summary["result_key"] = inputs.get("result_key")
    summary["bytes"] = size
    return summary


_ACTIONS = {
    "detect": _action_detect,
}


# ── Entry point ─────────────────────────────────────────────────────
def handler(job: dict) -> dict:
    """RunPod Serverless entry point.

    :param job: RunPod job dict. ``job["input"]`` must carry an
        ``action`` ("detect") and action-specific args. An optional
        ``scan_pk`` is used to tag Sentry events.
    :returns: Action-specific result dict. Every successful return
        (and every structured error) also carries ``worker_boot_ms``
        (cold-start cost of this worker process, constant per worker),
        ``worker_uptime_ms`` (ms since preload finished, at job start),
        and ``gpu_available`` (whether torch.cuda saw a device). On bad
        or unknown input an ``{"error": ..., "error_code": ...}`` dict
        is returned; the SDK moves ``error`` to the top level and
        RunPod marks the job ``FAILED``. The caller reads
        ``error_code`` from ``output`` to tell a transient failure
        (retry) from a terminal one.

        When the input carries ``result_url``, the payload is PUT to S3
        and the response holds only a summary (``result_key``,
        ``bytes``, ``detection_count``, ``page_count``, ``models``,
        ``duration_ms``). Without it the payload comes back inline.
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

    # Belt-and-suspenders: the fitness check should keep CPU-only
    # workers away from jobs, but handle it defensively in case CUDA
    # becomes unavailable after startup.
    if not _CUDA_AVAILABLE:
        # NO_GPU is transient from the caller's perspective: retry and
        # the next attempt lands on a different worker. Logged at
        # warning, not error: expected and self-healing, so no Sentry
        # event.
        logger.warning(
            "rejecting %s job for scan %s: no GPU on this worker; "
            "the caller should retry.",
            action,
            scan_pk,
        )
        # refresh_worker: the pinned runpod SDK pops this from the
        # return dict, delivers the result, then terminates the worker
        # (stopPod). A CPU-only worker never grows a GPU, so keeping it
        # warm would let it keep swallowing retried jobs.
        return _with_worker_meta(
            {
                "error": "GPU unavailable on this worker",
                "error_code": "NO_GPU",
                "refresh_worker": True,
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
        result = fn(job, inputs, tmp_dir)
        return _with_worker_meta(_deliver(result, inputs, scan_pk))
    except ResultUploadError as exc:
        # The compute succeeded but could not be delivered, so nothing
        # was written and there is nothing for the caller to harvest.
        # Returned as a structured error rather than raised, because the
        # code is the whole point: RESULT_UPLOAD_FAILED and
        # RESULT_URL_EXPIRED are worth a fresh job (which mints a fresh
        # URL), and RESULT_UPLOAD_REJECTED never is.
        logger.error(
            "result delivery failed: action=%s scan_pk=%s code=%s: %s",
            action,
            scan_pk,
            exc.error_code,
            exc,
        )
        if sentry_sdk is not None:
            sentry_sdk.capture_exception(exc)
        return _with_worker_meta(
            {"error": str(exc), "error_code": exc.error_code}
        )
    except CorruptDownloadError as exc:
        # Our copy of the PDF would not open, or arrived truncated. The
        # object in the bucket is sound -- the caller cut each shard and
        # verified it against the original -- so this describes the
        # transfer, not the input, and the next attempt may well get it
        # right. Its own code, because BAD_INPUT is terminal and would
        # write a volume off for a dropped connection.
        logger.warning(
            "corrupt download: action=%s scan_pk=%s: %s",
            action,
            scan_pk,
            exc,
        )
        return _with_worker_meta(
            {"error": str(exc), "error_code": "INPUT_DOWNLOAD_CORRUPT"}
        )
    except ValueError as exc:
        # Input validation (missing pdf_url, an unbaked weight name, a
        # confidence out of range, an over-MAX_PAGES input). Returned as
        # a structured error, not raised: a raw traceback carries no
        # error_code, so the caller couldn't tell this terminal input
        # error from a transient failure and would retry it to fail
        # identically forever.
        logger.warning(
            "bad input: action=%s scan_pk=%s: %s", action, scan_pk, exc
        )
        return _with_worker_meta(
            {"error": str(exc), "error_code": "BAD_INPUT"}
        )
    except Exception as exc:
        if sentry_sdk is not None:
            sentry_sdk.capture_exception(exc)
        logger.exception(
            "handler failed: action=%s scan_pk=%s", action, scan_pk
        )
        # Re-raise so RunPod marks the job FAILED with the traceback.
        # Worker meta is on the Sentry event through _tag_sentry; there
        # is no output dict to attach it to here.
        raise
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


if __name__ == "__main__":
    runpod.serverless.start({"handler": handler})
