"""RunPod Serverless handler for dots.mocr document parsing.

Runs the rednote-hilab/dots.mocr vision-language model behind a local
vLLM server (spawned as a subprocess at worker boot) and dispatches on
``job["input"]["action"]``:

- ``parse``: fetch a PDF via presigned GET URL, render each page,
  run dots.mocr on it, and return per-page layout JSON + markdown.

Returns JSON inline in the RunPod HTTP response. Does not write to
S3, does not need AWS credentials.

The vLLM server is started at module import time so cold start pays
the model-load cost once; subsequent warm invocations reuse the
running engine. Page inference goes through the OpenAI-compatible
API on localhost, exactly like upstream's ``dots_mocr/parser.py``
(including the ``<|img|><|imgpad|><|endofimg|>`` prompt prefix that
``inference_with_vllm`` injects), so vLLM's continuous batching
handles the per-page fan-out.
"""

from __future__ import annotations

import logging
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import requests
import runpod
from runpod_common import (
    CorruptDownloadError,
    ResultUploadError,
    download_pdf,
    upload_result,
    validate_pdf,
)

# Capture worker boot start as early as possible so ``_WORKER_BOOT_MS``
# covers the full cold-start cost (module imports + vLLM startup).
# Uses monotonic() so the number isn't affected by wall-clock jumps.
_WORKER_BOOT_START = time.monotonic()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("dotsmocr.runpod")

# Populated in ``_preload``. Surfaced in every handler response so the
# daemon can tell whether inference actually hit a GPU.
_GPU_AVAILABLE = False
_VLLM_READY = False
_VLLM_PROC: subprocess.Popen | None = None


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
# ``max_pages`` contract (``runpod_client.py``, the blackletter worker)
# means *truncate*, and a rejection threshold under the same name would
# invite silent contract confusion. Volumes arrive sharded (#164), so
# a shard exceeding this is a pipeline bug, not a big scan.
MAX_PAGES = int(os.environ.get("HANDLER_MAX_PAGES", "5000"))
# Download tunables (HANDLER_DOWNLOAD_*) live in runpod_common.

# ── vLLM server tunables ────────────────────────────────────────────
# The model lives in the baked HF cache (HF_HOME=/opt/hf, offline).
DOTSMOCR_MODEL = os.environ.get("DOTSMOCR_MODEL", "rednote-hilab/dots.mocr")
# Upstream's parser defaults to model_name="model"; we serve under the
# same alias so their client code works unmodified.
SERVED_MODEL_NAME = "model"
VLLM_HOST = "127.0.0.1"
# HANDLER_-prefixed on purpose: ``VLLM_PORT`` itself is a *reserved*
# vLLM env var (v0.17's envs.py hands it out as the first port for
# internal distributed services), and the spawned ``vllm serve``
# inherits our environment. Exposing the API port under that name
# would make the server bind internal sockets on the same port it
# serves the API on — bind conflict, worker never becomes healthy.
VLLM_PORT = int(os.environ.get("HANDLER_VLLM_PORT", "8000"))
VLLM_GPU_MEMORY_UTILIZATION = os.environ.get(
    "VLLM_GPU_MEMORY_UTILIZATION", "0.9"
)
# Model load from the baked cache is typically 1-3 min; the budget is
# generous because the first boot on a node also compiles CUDA graphs.
VLLM_STARTUP_TIMEOUT = int(os.environ.get("VLLM_STARTUP_TIMEOUT", "900"))
# Extra ``vllm serve`` flags (e.g. "--max-model-len 16384"), split
# shell-style. Escape hatch for endpoint-level tuning without a
# rebuild.
VLLM_EXTRA_ARGS = os.environ.get("VLLM_EXTRA_ARGS", "")

# ── Inference tunables (job input can override the per-job ones) ────
DEFAULT_PROMPT_MODE = "prompt_layout_all_en"
DEFAULT_DPI = int(os.environ.get("HANDLER_DPI", "200"))
# Concurrent in-flight requests against the local vLLM server. The
# server continuously batches, so this bounds client-side memory
# (rendered pages), not GPU batch size.
DEFAULT_NUM_THREADS = int(os.environ.get("HANDLER_NUM_THREADS", "16"))
# 0.0 (greedy), matching the ai-research extraction_align runners the
# experiments were validated with — NOT upstream DotsOCRParser's 0.1.
# Greedy decoding keeps output deterministic per page (modulo vLLM
# batch-composition numerics), which the alignment pipeline relies on.
DEFAULT_TEMPERATURE = 0.0
DEFAULT_TOP_P = 1.0
DEFAULT_MAX_COMPLETION_TOKENS = 16384
# Per-page attempts when the vLLM call returns nothing (upstream's
# ``inference_with_vllm`` swallows transport errors and returns None).
INFERENCE_ATTEMPTS = int(os.environ.get("HANDLER_INFERENCE_ATTEMPTS", "2"))

# Prompt modes this worker accepts. The remaining upstream modes
# (grounding OCR, web parsing, scene spotting, SVG) target single
# images with extra inputs and are out of scope for PDF parsing.
_ALLOWED_PROMPT_MODES = (
    "prompt_layout_all_en",
    "prompt_layout_only_en",
    "prompt_ocr",
)


# ── GPU / vLLM lifecycle ────────────────────────────────────────────
def _gpu_available() -> bool:
    """Return True if the NVIDIA driver exposes at least one GPU.

    The handler venv deliberately has no torch (the GPU stack lives in
    the vLLM server's environment), so the check shells out to
    ``nvidia-smi``, which the container toolkit bind-mounts on GPU
    hosts and which is absent on CPU-only ones.

    :returns: Whether a GPU is visible.
    :rtype: bool
    """
    exe = shutil.which("nvidia-smi")
    if exe is None:
        return False
    try:
        proc = subprocess.run(
            [exe, "-L"], capture_output=True, text=True, timeout=30
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0 and "GPU" in proc.stdout


def _start_vllm() -> subprocess.Popen:
    """Spawn ``vllm serve`` for dots.mocr, inheriting our stdout/stderr.

    Flags follow the upstream README's recommended serve command
    verbatim (``--chat-template-content-format string`` and
    ``--trust-remote-code`` are load-bearing: without them the chat
    template mangles the image tokens). Bound to localhost — the only
    client is this handler.

    :returns: The server process handle.
    :rtype: subprocess.Popen
    """
    cmd = [
        "vllm",
        "serve",
        DOTSMOCR_MODEL,
        "--host",
        VLLM_HOST,
        "--port",
        str(VLLM_PORT),
        "--served-model-name",
        SERVED_MODEL_NAME,
        "--tensor-parallel-size",
        "1",
        "--gpu-memory-utilization",
        VLLM_GPU_MEMORY_UTILIZATION,
        "--chat-template-content-format",
        "string",
        "--trust-remote-code",
    ] + shlex.split(VLLM_EXTRA_ARGS)
    logger.info("starting vLLM: %s", " ".join(cmd))
    # Belt-and-suspenders next to the HANDLER_VLLM_PORT rename above:
    # never let a stray VLLM_PORT reach the server, where vLLM treats
    # it as the base port for internal service sockets and collides
    # with the --port the API listens on.
    env = {k: v for k, v in os.environ.items() if k != "VLLM_PORT"}
    return subprocess.Popen(cmd, env=env)


def _vllm_healthy(timeout: float = 5.0) -> bool:
    """Return True if the local vLLM server answers its health check.

    :param timeout: Per-request timeout in seconds.
    :returns: Whether ``GET /health`` returned 200.
    :rtype: bool
    """
    try:
        r = requests.get(
            f"http://{VLLM_HOST}:{VLLM_PORT}/health", timeout=timeout
        )
        return r.status_code == 200
    except requests.RequestException:
        return False


def _wait_for_vllm(proc: subprocess.Popen, timeout: int) -> bool:
    """Poll the vLLM health endpoint until ready, dead, or timed out.

    :param proc: The server process (checked for early exit so a crash
        fails fast instead of burning the whole timeout).
    :param timeout: Max seconds to wait.
    :returns: True once healthy; False if the process died or the
        budget ran out.
    :rtype: bool
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            logger.error(
                "vLLM exited with code %s during startup", proc.returncode
            )
            return False
        if _vllm_healthy():
            return True
        time.sleep(2)
    logger.error("vLLM not healthy after %ds", timeout)
    return False


# ── Cold-start preload ──────────────────────────────────────────────
def _preload() -> None:
    """Start the vLLM server at module import so the first job is fast.

    GPU check first: this worker is GPU-only, so on a CPU-only host we
    skip the server start entirely (vLLM would just crash trying to
    initialise CUDA) and let the fitness check block the worker /
    ``handler()`` fail fast with NO_GPU so the daemon can retry on a
    different worker.
    """
    global _GPU_AVAILABLE, _VLLM_READY, _VLLM_PROC

    _GPU_AVAILABLE = _gpu_available()
    if not _GPU_AVAILABLE:
        # The fitness check (_require_vllm) blocks this worker from the
        # available pool, so queued jobs route to healthy GPU workers.
        # If a job does leak through it returns error_code=NO_GPU, which
        # a client should classify as transient and re-queue.
        #
        # Logged at warning, not error: this is expected and fully
        # self-healing, so it must not raise a Sentry event.
        logger.warning(
            "GPU not available; skipping vLLM startup. Jobs will "
            "return error_code=NO_GPU; scans should be re-queued "
            "automatically by the caller."
        )
        return

    t0 = time.monotonic()
    try:
        _VLLM_PROC = _start_vllm()
        _VLLM_READY = _wait_for_vllm(_VLLM_PROC, VLLM_STARTUP_TIMEOUT)
    except Exception:
        # Never let a spawn failure kill the module import: the
        # fitness check reports the worker unfit instead, which is
        # visible in the endpoint's Workers tab.
        logger.exception("vLLM startup failed")
        _VLLM_READY = False
    if _VLLM_READY:
        logger.info(
            "vLLM ready in %.1fs (model=%s)",
            time.monotonic() - t0,
            DOTSMOCR_MODEL,
        )
    else:
        logger.error("vLLM failed to become ready; worker is unfit")


_preload()

# Freeze the cold-start cost once the process is ready. ``_WORKER_BOOT_MS``
# is constant per worker; ``_WORKER_READY_AT`` anchors per-job uptime so
# the caller can tell cold from warm calls (same boot_ms + increasing
# uptime_ms across jobs = same warm worker).
_WORKER_READY_AT = time.monotonic()
_WORKER_BOOT_MS = int((_WORKER_READY_AT - _WORKER_BOOT_START) * 1000)
logger.info(
    "worker ready: boot_ms=%d gpu=%s vllm=%s",
    _WORKER_BOOT_MS,
    _GPU_AVAILABLE,
    _VLLM_READY,
)


# ── Fitness check ────────────────────────────────────────────────────
@runpod.serverless.register_fitness_check
def _require_vllm() -> None:
    """Exit before accepting jobs if the GPU or vLLM server is missing.

    Runs at startup before RunPod's heartbeat, so the worker is never
    added to the available pool and no jobs are ever assigned to it.
    Queued jobs are picked up automatically by healthy GPU workers.
    """
    if not _GPU_AVAILABLE:
        raise RuntimeError(
            "GPU not available on this worker. Exiting to avoid CPU-only billing."
        )
    if not _VLLM_READY:
        raise RuntimeError(
            "vLLM server failed to start on this worker. Exiting."
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
    caller can always see whether this job hit a warm worker and
    whether inference ran on GPU.

    :param payload: The response dict to augment in place.
    :returns: The same dict with meta fields added.
    :rtype: dict
    """
    payload["worker_boot_ms"] = _WORKER_BOOT_MS
    payload["worker_uptime_ms"] = _worker_uptime_ms()
    payload["gpu_available"] = _GPU_AVAILABLE
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
    sentry_sdk.set_tag("gpu_available", str(_GPU_AVAILABLE))
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


# ── dots_mocr import shim + vLLM client ─────────────────────────────
def _ensure_dots_mocr_importable() -> None:
    """Make the installed ``dots_mocr`` package importable.

    At the pinned commit, ``dots_mocr/model/`` ships without an
    ``__init__.py``, so ``find_packages()`` in upstream's setup.py
    skips it and the installed wheel has no ``dots_mocr.model``. The
    package ``__init__`` unconditionally does ``from .parser import
    DotsMOCRParser``, and parser.py imports ``dots_mocr.model
    .inference`` — so importing *anything* from the package explodes.

    This registers a stub ``dots_mocr.model.inference`` module (whose
    ``inference_with_vllm`` is never called — this handler uses its
    own ``_vllm_inference``) before retrying the import. Conditional
    on the ImportError so it becomes a no-op if a future pin fixes
    upstream packaging.
    """
    try:
        import dots_mocr  # noqa: F401

        return
    except ModuleNotFoundError as exc:
        if exc.name != "dots_mocr.model":
            raise

    import sys
    import types

    def _not_installed(*args, **kwargs):
        raise NotImplementedError(
            "dots_mocr.model.inference is not shipped in the installed "
            "package; use handler._vllm_inference instead"
        )

    pkg = types.ModuleType("dots_mocr.model")
    mod = types.ModuleType("dots_mocr.model.inference")
    mod.inference_with_vllm = _not_installed
    pkg.inference = mod
    sys.modules["dots_mocr.model"] = pkg
    sys.modules["dots_mocr.model.inference"] = mod

    import dots_mocr  # noqa: F401


def _vllm_inference(
    client,
    image,
    prompt: str,
    temperature: float,
    top_p: float,
    max_completion_tokens: int,
) -> tuple[str | None, str | None, int | None]:
    """Run one chat completion against the local vLLM server.

    Adapted from upstream ``dots_mocr/model/inference.py`` (MIT),
    which the installed package doesn't ship (see
    :func:`_ensure_dots_mocr_importable`). The
    ``<|img|><|imgpad|><|endofimg|>`` prefix on the text part mirrors
    upstream exactly: without it, vLLM v1's chat template inserts a
    stray newline between the image tokens and the prompt.

    :param client: A shared ``openai.OpenAI`` client pointed at the
        local server (shared so the 16-thread fan-out reuses one
        connection pool).
    :param image: PIL image of the page, already sized as the model
        should see it.
    :param prompt: The prompt-mode text.
    :returns: ``(content, finish_reason, completion_tokens)``.
        ``content`` may be ``None`` if the server returns an empty
        choice. ``finish_reason == "length"`` means the output was cut
        at ``max_completion_tokens`` — the caller must not treat that
        text as a complete page. ``completion_tokens`` is the generated
        token count from the response's usage block (``None`` if the
        server omitted it), recorded per page for observability.
    :rtype: tuple[str | None, str | None, int | None]
    """
    from dots_mocr.utils.image_utils import PILimage_to_base64

    response = client.chat.completions.create(
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": PILimage_to_base64(image)},
                    },
                    {
                        "type": "text",
                        "text": f"<|img|><|imgpad|><|endofimg|>{prompt}",
                    },
                ],
            }
        ],
        model=SERVED_MODEL_NAME,
        max_completion_tokens=max_completion_tokens,
        temperature=temperature,
        top_p=top_p,
    )
    choice = response.choices[0]
    usage = getattr(response, "usage", None)
    completion_tokens = getattr(usage, "completion_tokens", None)
    return choice.message.content, choice.finish_reason, completion_tokens


# ── Markdown post-processing ────────────────────────────────────────
# Upstream's ``layoutjson2md`` inlines every 'Picture' cell as a base64
# data URI (a crop of the page image). On image-heavy volumes that can
# blow past RunPod's ~20 MB response cap, so pictures are stripped from
# the markdown by default; the cell (bbox + category) survives in the
# layout JSON regardless.
_DATA_URI_MD_IMG_RE = re.compile(r"!\[[^\]]*\]\(data:image/[^)]*\)")


def _strip_data_uris(md: str) -> str:
    """Replace inline base64 markdown images with an empty placeholder.

    :param md: Markdown produced by ``layoutjson2md``.
    :returns: The same markdown with data-URI images replaced by
        ``![]()``.
    :rtype: str
    """
    return _DATA_URI_MD_IMG_RE.sub("![]()", md)


# ── Actions ─────────────────────────────────────────────────────────
def _action_parse(job: dict, inputs: dict, tmp_dir: Path) -> dict:
    """Parse every page of a PDF with dots.mocr.

    Pages are rendered lazily (one fitz document per worker thread —
    PyMuPDF documents are not safe to render from concurrently) and
    fanned out to the local vLLM server, mirroring upstream's
    ``DotsMOCRParser.parse_pdf`` flow: render at ``dpi``, no resize
    unless ``min_pixels``/``max_pixels`` is given (the server-side
    processor handles its own smart resize), post-process the raw
    response into rescaled layout cells, then convert to markdown.

    :param job: RunPod job dict (used for progress updates).
    :param inputs: Handler input payload. Required: ``pdf_url``.
        Result delivery (handled by :func:`_deliver`, not here):
        ``result_url`` and ``result_key``. Optional:
        ``prompt_mode`` (default ``prompt_layout_all_en``),
        ``dpi`` (default 200), ``num_threads``, ``temperature``,
        ``top_p``, ``max_completion_tokens``, ``min_pixels``,
        ``max_pixels``, ``include_pictures`` (default False: strip
        base64 picture crops from the markdown). There is deliberately
        no ``max_pages`` input: that name means "truncate to the first
        N pages" elsewhere in the repo, and a partial parse silently
        merged as a full volume is worse than a failure. Inputs over
        the env-level ``MAX_PAGES`` are rejected.
    :param tmp_dir: Per-job scratch directory.
    :returns: ``{"pages": list[dict], "page_count": int,
        "failed_pages": list[int], "duration_ms": int}``. Each page
        dict carries ``page_no``, ``input_width``/``input_height``
        (model-input dims for interpreting bboxes),
        ``origin_width``/``origin_height`` (actual render dims — the
        pixel space ``cells`` bboxes are rescaled into), a
        ``completion_tokens`` count, ``duration_ms``, and either
        ``cells`` (layout JSON) + ``md`` (markdown), or ``error``.
        ``filtered: true`` marks pages where the model output wasn't
        valid JSON and ``md`` holds the cleaned text fallback.
        ``render_fallback: true`` flags pages the pinned upstream
        silently re-rendered at 72 dpi (any dimension over 4500 px at
        the requested dpi): bboxes are still consistent with
        ``origin_width``/``origin_height``, just not with ``dpi``.
    :rtype: dict
    """
    import fitz
    import openai

    _ensure_dots_mocr_importable()
    from dots_mocr.utils.consts import MAX_PIXELS, MIN_PIXELS
    from dots_mocr.utils.doc_utils import fitz_doc_to_image
    from dots_mocr.utils.format_transformer import layoutjson2md
    from dots_mocr.utils.image_utils import fetch_image, smart_resize
    from dots_mocr.utils.layout_utils import post_process_output
    from dots_mocr.utils.prompts import dict_promptmode_to_prompt

    # ``.get`` + explicit raise, not ``inputs["pdf_url"]``: a KeyError
    # would surface as a raw traceback with no error_code, which the
    # daemon can't classify. handler() turns ValueError into BAD_INPUT.
    pdf_url = inputs.get("pdf_url")
    if not pdf_url:
        raise ValueError("missing required input: pdf_url")
    prompt_mode = inputs.get("prompt_mode", DEFAULT_PROMPT_MODE)
    if prompt_mode not in _ALLOWED_PROMPT_MODES:
        raise ValueError(
            f"unsupported prompt_mode: {prompt_mode!r}. "
            f"Expected one of {sorted(_ALLOWED_PROMPT_MODES)}"
        )
    dpi = int(inputs.get("dpi", DEFAULT_DPI))
    num_threads = int(inputs.get("num_threads", DEFAULT_NUM_THREADS))
    temperature = float(inputs.get("temperature", DEFAULT_TEMPERATURE))
    top_p = float(inputs.get("top_p", DEFAULT_TOP_P))
    max_completion_tokens = int(
        inputs.get("max_completion_tokens", DEFAULT_MAX_COMPLETION_TOKENS)
    )
    # Coerced, not just range-checked. These two are handed straight to
    # ``fetch_image`` / ``post_process_output``, which do arithmetic on
    # them; a JSON-encoded string would pass the check below and then
    # fail inside every page, surfacing as "all N pages failed" rather
    # than as the input error it is.
    min_pixels = inputs.get("min_pixels")
    max_pixels = inputs.get("max_pixels")
    if min_pixels is not None:
        min_pixels = int(min_pixels)
        if min_pixels < MIN_PIXELS:
            raise ValueError(f"min_pixels must be >= {MIN_PIXELS}")
    if max_pixels is not None:
        max_pixels = int(max_pixels)
        if max_pixels > MAX_PIXELS:
            raise ValueError(f"max_pixels must be <= {MAX_PIXELS}")
    include_pictures = bool(inputs.get("include_pictures", False))
    prompt = dict_promptmode_to_prompt[prompt_mode]

    pdf_path = tmp_dir / "input.pdf"
    download_pdf(pdf_url, pdf_path)

    pages = validate_pdf(pdf_path)
    if pages > MAX_PAGES:
        raise ValueError(
            f"PDF has {pages} pages, exceeds MAX_PAGES={MAX_PAGES}"
        )

    # One client for all threads: the OpenAI SDK is thread-safe and a
    # shared httpx pool beats a fresh TCP handshake per page.
    client = openai.OpenAI(
        api_key="0", base_url=f"http://{VLLM_HOST}:{VLLM_PORT}/v1"
    )
    # Server hiccups worth one more try per page; 4xx (bad request,
    # context overflow) are not in here and fail the page immediately.
    transient_vllm_errors = (
        openai.APIConnectionError,
        openai.APITimeoutError,
        openai.InternalServerError,
    )

    # One fitz document per thread: rendering from a shared Document
    # across threads segfaults. Docs are tracked so they can be closed
    # deterministically instead of waiting on thread-local GC.
    local = threading.local()
    open_docs: list = []
    docs_lock = threading.Lock()

    def _doc():
        doc = getattr(local, "doc", None)
        if doc is None:
            doc = fitz.open(str(pdf_path))
            local.doc = doc
            with docs_lock:
                open_docs.append(doc)
        return doc

    def _parse_page(page_idx: int) -> dict:
        t0 = time.monotonic()
        page = _doc()[page_idx]
        origin_image = fitz_doc_to_image(page, target_dpi=dpi)
        if origin_image is None:
            return {"page_no": page_idx, "error": "page rendered empty"}
        # The pinned upstream silently re-renders any page over 4500 px
        # at 72 dpi with no signal, which puts that page's bboxes in a
        # different pixel space than every other page's. Detect it by
        # comparing against the size the requested dpi should produce,
        # and flag the page so downstream can rescale instead of
        # misplacing every box.
        expected_w = page.rect.width * dpi / 72
        render_fallback = bool(
            expected_w
            and abs(origin_image.width - expected_w)
            > max(2, 0.01 * expected_w)
        )
        if render_fallback:
            logger.warning(
                "page %d: rendered %dx%d, expected ~%dx%d at dpi=%d "
                "(upstream 4500px fallback); bboxes are in the rendered "
                "space",
                page_idx,
                origin_image.width,
                origin_image.height,
                round(expected_w),
                round(page.rect.height * dpi / 72),
                dpi,
            )
        image = fetch_image(
            origin_image, min_pixels=min_pixels, max_pixels=max_pixels
        )
        input_height, input_width = smart_resize(image.height, image.width)

        response = None
        finish_reason = None
        completion_tokens = None
        for attempt in range(INFERENCE_ATTEMPTS):
            try:
                response, finish_reason, completion_tokens = _vllm_inference(
                    client,
                    image,
                    prompt,
                    temperature=temperature,
                    top_p=top_p,
                    max_completion_tokens=max_completion_tokens,
                )
            except transient_vllm_errors as exc:
                if attempt == INFERENCE_ATTEMPTS - 1:
                    raise
                logger.warning(
                    "page %d: vLLM error (attempt %d/%d): %s",
                    page_idx,
                    attempt + 1,
                    INFERENCE_ATTEMPTS,
                    exc,
                )
                continue
            # Truthiness, not ``is not None``: an empty-string response
            # (immediate EOS) is exactly the case this retry exists for.
            if response:
                break
            logger.warning(
                "page %d: empty vLLM response (attempt %d/%d)",
                page_idx,
                attempt + 1,
                INFERENCE_ATTEMPTS,
            )
        if not response:
            raise RuntimeError("empty response from vLLM")
        # A generation cut at max_completion_tokens is almost never
        # dense content (the cap is ~2x the worst realistic page) — it
        # is a repetition loop, and its output was garbage before the
        # cut. Fail the page loudly instead of letting the truncated
        # JSON degrade into filtered=true, indistinguishable from
        # genuine model garbage.
        if finish_reason == "length":
            raise RuntimeError(
                f"output truncated at {completion_tokens} tokens "
                "(finish_reason='length'); likely a repetition loop"
            )

        result = {
            "page_no": page_idx,
            "input_width": input_width,
            "input_height": input_height,
            "origin_width": origin_image.width,
            "origin_height": origin_image.height,
            "completion_tokens": completion_tokens,
        }
        if render_fallback:
            result["render_fallback"] = True
        if prompt_mode in ("prompt_layout_all_en", "prompt_layout_only_en"):
            cells, filtered = post_process_output(
                response,
                prompt_mode,
                origin_image,
                image,
                min_pixels=min_pixels,
                max_pixels=max_pixels,
            )
            result["filtered"] = bool(filtered)
            if filtered:
                # Model output wasn't valid JSON; ``cells`` is upstream's
                # cleaned-text fallback, usable only as markdown.
                result["cells"] = None
                result["md"] = cells if isinstance(cells, str) else None
            else:
                result["cells"] = cells
                if prompt_mode == "prompt_layout_all_en":
                    md = layoutjson2md(origin_image, cells, text_key="text")
                    if not include_pictures:
                        md = _strip_data_uris(md)
                    result["md"] = md
        else:  # prompt_ocr: plain text extraction, no layout JSON
            result["md"] = response
        result["duration_ms"] = int((time.monotonic() - t0) * 1000)
        return result

    def _parse_page_safe(page_idx: int) -> dict:
        try:
            return _parse_page(page_idx)
        except Exception as exc:
            # One bad page must not sink a 1000-page job: record the
            # error per page and keep going. If *every* page fails the
            # whole job raises below.
            logger.exception("page %d failed", page_idx)
            return {"page_no": page_idx, "error": str(exc)}

    t0 = time.monotonic()
    results: list[dict] = []
    workers = max(1, min(pages, num_threads))
    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(_parse_page_safe, i) for i in range(pages)]
            for done, fut in enumerate(as_completed(futures), start=1):
                results.append(fut.result())
                if done % 25 == 0 or done == pages:
                    logger.info("parsed %d/%d pages", done, pages)
                    try:
                        runpod.serverless.progress_update(
                            job, f"{done}/{pages} pages"
                        )
                    except Exception:
                        # Progress is best-effort; never fail a job
                        # over it.
                        pass
    finally:
        for doc in open_docs:
            doc.close()

    results.sort(key=lambda r: r["page_no"])
    failed = [r["page_no"] for r in results if "error" in r]
    if len(failed) == pages:
        raise RuntimeError(
            f"all {pages} pages failed; first error: {results[0].get('error')}"
        )

    duration_ms = int((time.monotonic() - t0) * 1000)
    logger.info(
        "parse OK: %d pages (%d failed) in %d ms (mode=%s, dpi=%d)",
        pages,
        len(failed),
        duration_ms,
        prompt_mode,
        dpi,
    )
    return {
        "pages": results,
        "page_count": pages,
        "failed_pages": failed,
        "duration_ms": duration_ms,
    }


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
# Everything else -- above all ``pages`` -- is deliberately dropped: the
# response is capped at about 20 MB and discarded with the job record,
# which is the whole reason the payload travels through S3.
_SUMMARY_FIELDS = ("page_count", "failed_pages", "duration_ms")


def _deliver(result: dict, inputs: dict, scan_pk: Any) -> dict:
    """Send a result to S3 when asked, and answer with a summary.

    Two shapes, chosen by the caller and not by us:

    - ``result_url`` present -> wrap the payload in a self-describing
      envelope, PUT it, and return only the summary plus the key. This
      is what a volume-sized parse needs.
    - absent -> return the payload inline, as this worker always did.
      That path is what dev and continuous integration use without
      credentials, and what a caller running an older contract gets, so
      rolling the image back needs no daemon change.

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
    summary["result_key"] = inputs.get("result_key")
    summary["bytes"] = size
    return summary


_ACTIONS = {
    "parse": _action_parse,
}


# ── Entry point ─────────────────────────────────────────────────────
def handler(job: dict) -> dict:
    """RunPod Serverless entry point.

    :param job: RunPod job dict. ``job["input"]`` must carry an
        ``action`` ("parse") and action-specific args. An optional
        ``scan_pk`` is used to tag Sentry events.
    :returns: Action-specific result dict. Every successful return
        (and every structured error) also carries ``worker_boot_ms``
        (cold-start cost of this worker process, constant per
        worker), ``worker_uptime_ms`` (ms since preload finished, at
        job start), and ``gpu_available`` (whether nvidia-smi saw a
        device). On bad/unknown input an ``{"error": ..., "error_code":
        ...}`` dict is returned; the SDK moves ``error`` to the top
        level and RunPod marks the job ``FAILED``. The caller reads
        ``error_code`` from ``output`` to distinguish transient errors
        (re-queue) from terminal ones.

        When the input carries ``result_url``, the payload is PUT to S3
        and the response holds only a summary (``result_key``, ``bytes``,
        ``page_count``, ``failed_pages``, ``duration_ms``). Without it
        the payload comes back inline, as it always did.
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
    # a job leaks through anyway.
    if not _GPU_AVAILABLE:
        # NO_GPU is transient from the caller's perspective: re-queue
        # and the next attempt lands on a different worker. Logged at
        # warning, not error: expected and self-healing, so no Sentry
        # event.
        logger.warning(
            "rejecting %s job for scan %s: no GPU on this worker; "
            "caller should re-queue.",
            action,
            scan_pk,
        )
        # refresh_worker: the pinned runpod SDK pops this from the
        # return dict, delivers the result, then terminates the worker
        # (stopPod). A CPU-only worker never grows a GPU, so keeping it
        # warm would let it keep swallowing re-queued jobs.
        return _with_worker_meta(
            {
                "error": "GPU unavailable on this worker",
                "error_code": "NO_GPU",
                "refresh_worker": True,
            }
        )

    # The engine can die after a healthy boot (CUDA OOM, driver hiccup).
    # A dead engine on this worker says nothing about the job itself,
    # so surface it as its own transient code rather than failing pages
    # one by one.
    if not _VLLM_READY or not _vllm_healthy():
        logger.warning(
            "rejecting %s job for scan %s: vLLM not healthy on this "
            "worker; caller should re-queue.",
            action,
            scan_pk,
        )
        # refresh_worker is load-bearing here: a crashed vLLM engine
        # never restarts, but the worker stays warm and "completes"
        # every job in milliseconds, so the scheduler keeps routing
        # re-queued jobs straight back to it — and the retry traffic
        # itself resets the idle timeout that would otherwise reap it.
        # Flagging the worker for termination breaks that livelock at
        # the cost of one cold boot.
        return _with_worker_meta(
            {
                "error": "vLLM server not healthy on this worker",
                "error_code": "VLLM_UNHEALTHY",
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
        # Input validation (missing/invalid pdf_url, bad prompt_mode,
        # out-of-range pixel bounds, over-MAX_PAGES input). Returned as
        # a structured error, not raised: a raw traceback carries no
        # error_code, so the daemon couldn't tell this terminal input
        # error from a transient failure and would re-queue it to fail
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
        # Worker meta is on the Sentry event via _tag_sentry; no
        # output dict to attach it to here.
        raise
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


if __name__ == "__main__":
    runpod.serverless.start({"handler": handler})
