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
import time
from pathlib import Path
from typing import Any

import runpod
import runpod_common
from runpod_common import (
    RESULT_SCHEMA_VERSION,  # noqa: F401  (part of this module's contract)
    BadInputError,
    WorkerClock,
    coerce_input,
    download_pdf,
    upload_result,
    validate_pdf,
)

# Construct the clock as early as possible so ``boot_ms`` covers the
# full cold-start cost (module imports + model preload).
_CLOCK = WorkerClock()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("blackletter.runpod")

# Populated in ``_preload``. Surfaced in every handler response so the
# caller can tell whether inference actually hit a GPU.
_CUDA_AVAILABLE = False

sentry_sdk = runpod_common.init_sentry(logger)


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

_CLOCK.mark_ready()
logger.info(
    "worker ready: boot_ms=%d cuda=%s", _CLOCK.boot_ms, _CUDA_AVAILABLE
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
# Thin wrappers over the runpod_common scaffold. They bind this
# module's globals at call time, so a test that patches
# ``_CUDA_AVAILABLE`` or ``upload_result`` on this module still steers
# the shared code.
def _with_worker_meta(payload: dict) -> dict:
    """See :func:`runpod_common.with_worker_meta`."""
    return runpod_common.with_worker_meta(
        payload, clock=_CLOCK, gpu_available=_CUDA_AVAILABLE
    )


def _tag_sentry(job: dict, action: str, scan_pk: Any) -> None:
    """See :func:`runpod_common.tag_sentry`."""
    runpod_common.tag_sentry(
        sentry_sdk,
        job,
        action,
        scan_pk,
        clock=_CLOCK,
        gpu_available=_CUDA_AVAILABLE,
        handler_logger=logger,
    )


def _read_models(inputs: dict) -> list[str]:
    """Return the weight names this job runs, validated.

    :param inputs: Handler input payload.
    :returns: Model names, every one of them baked into the image.
    :rtype: list[str]
    :raises BadInputError: If the value is not a non-empty list of
        strings, or names a weight the image does not carry.
    """
    # ``None`` means "use the default"; an empty list does not. A
    # caller bug that filtered a model list down to [] must be refused
    # like every other malformed value, not silently run the default.
    models = inputs.get("models")
    if models is None:
        models = list(DEFAULT_MODELS)
    if isinstance(models, str):
        models = [models]
    if (
        not isinstance(models, list)
        or not models
        or not all(isinstance(name, str) and name for name in models)
    ):
        raise BadInputError(
            f"'models' must be a non-empty list of weight names, "
            f"got {models!r}"
        )
    missing = _missing_weights(models)
    if missing:
        available = sorted(p.stem for p in _weights_dir().glob("*.pt"))
        raise BadInputError(
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
    :raises BadInputError: On any bad input.
    """
    from blackletter.api import detect

    # ``.get`` + explicit raise, not ``inputs["pdf_url"]``: a KeyError
    # would surface as a raw traceback with no error_code, which the
    # caller can't classify. The runner turns BadInputError into
    # BAD_INPUT.
    pdf_url = inputs.get("pdf_url")
    if not pdf_url:
        raise BadInputError("missing required input: pdf_url")
    models = _read_models(inputs)
    confidence = coerce_input(
        "confidence", inputs.get("confidence", DEFAULT_CONFIDENCE), float
    )
    if not 0 < confidence <= 1:
        raise BadInputError(
            f"'confidence' must be in (0, 1], got {confidence!r}"
        )

    pdf_path = tmp_dir / "input.pdf"
    download_pdf(pdf_url, pdf_path)

    pages = validate_pdf(pdf_path)
    if pages > MAX_PAGES:
        raise BadInputError(
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
# Summary fields kept in the job response when the payload goes to S3.
# Everything else -- above all ``detections`` -- is deliberately
# dropped: the response is capped at about 20 MB and discarded with the
# job record, which is the whole reason the payload travels through S3.
_SUMMARY_FIELDS = ("page_count", "models", "duration_ms")


def _detection_count(result: dict) -> dict:
    """Return the extra summary field this worker keeps.

    Kept because it is small and it is what a caller checks first.

    :param result: The action's own return value.
    :returns: ``{"detection_count": int}``.
    :rtype: dict
    """
    return {"detection_count": len(result.get("detections") or [])}


def _deliver(result: dict, inputs: dict, scan_pk: Any) -> dict:
    """See :func:`runpod_common.deliver_result`."""
    return runpod_common.deliver_result(
        result,
        inputs,
        scan_pk,
        summary_fields=_SUMMARY_FIELDS,
        upload=upload_result,
        extra_summary=_detection_count,
    )


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

    # The except arms that map exceptions to error codes live in the
    # runner: they are the daemon's contract, shared by every worker.
    return runpod_common.execute_action(
        fn,
        job,
        inputs,
        action=action,
        scan_pk=scan_pk,
        deliver=_deliver,
        meta=_with_worker_meta,
        sentry=sentry_sdk,
        handler_logger=logger,
    )


if __name__ == "__main__":
    runpod.serverless.start({"handler": handler})
