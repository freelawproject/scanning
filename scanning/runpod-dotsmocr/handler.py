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
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import requests
import runpod
import runpod_common
from runpod_common import (
    BadInputError,
    WorkerClock,
    coerce_input,
    download_pdf,
    upload_result,
    validate_pdf,
)

# Construct the clock as early as possible so ``boot_ms`` covers the
# full cold-start cost (module imports + vLLM startup).
_CLOCK = WorkerClock()

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

sentry_sdk = runpod_common.init_sentry(logger)


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
# Generation cap per page. Measured over 13159 pages of nine reporters
# (scanning #238): a real page takes 1498 tokens at the median, 1950
# at p95 and 3114 at most, so 6144 is about twice the longest page.
# The cap is what ends a repetition loop, and a loop runs alone for
# minutes after the rest of the shard is done -- at the old 16384 a
# shard with one looping page took about four times as long as a clean
# one. Every rung of the retry ladder below pays the cap once, so it
# has to be tight.
DEFAULT_MAX_COMPLETION_TOKENS = int(
    os.environ.get("HANDLER_MAX_COMPLETION_TOKENS", "6144")
)
# Per-page attempts when the vLLM call returns nothing (upstream's
# ``inference_with_vllm`` swallows transport errors and returns None).
INFERENCE_ATTEMPTS = int(os.environ.get("HANDLER_INFERENCE_ATTEMPTS", "2"))

# ── Retry ladder (scanning #238) ────────────────────────────────────
# Every page failure seen in 30 days of production was one thing: a
# repetition loop on the last, mostly blank page of an opinion whose
# verso showed through as faint mirrored text. Greedy decoding is
# deterministic per page, so the same render loops again; a retry has
# to change the input. Rung 2 re-runs the page on the same render with
# a grey threshold that removes the show-through and keeps the real
# text (100 does on the sampled pages; doctor's bitonal 160 does not).
# The render is the only thing that changes: both rungs decode greedily
# with the same parameters, so the stage gives the same output for the
# same page on every run. No sampling and no repetition penalty, on
# purpose -- a penalty cannot tell a loop from the keys and brackets
# layout JSON repeats by design.
RETRY_THRESHOLD = int(os.environ.get("HANDLER_RETRY_THRESHOLD", "100"))

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

_CLOCK.mark_ready()
logger.info(
    "worker ready: boot_ms=%d gpu=%s vllm=%s",
    _CLOCK.boot_ms,
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
# Thin wrappers over the runpod_common scaffold. They bind this
# module's globals at call time, so a test that patches
# ``_GPU_AVAILABLE`` or ``upload_result`` on this module still steers
# the shared code.
def _with_worker_meta(payload: dict) -> dict:
    """See :func:`runpod_common.with_worker_meta`."""
    return runpod_common.with_worker_meta(
        payload, clock=_CLOCK, gpu_available=_GPU_AVAILABLE
    )


def _tag_sentry(job: dict, action: str, scan_pk: Any) -> None:
    """See :func:`runpod_common.tag_sentry`."""
    runpod_common.tag_sentry(
        sentry_sdk,
        job,
        action,
        scan_pk,
        clock=_CLOCK,
        gpu_available=_GPU_AVAILABLE,
        handler_logger=logger,
    )


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


class TruncatedOutput(RuntimeError):
    """A rung's generation was cut at the cap: a repetition loop.

    :ivar raw: The truncated answer, kept so a failed page can still
        show what the loop repeated.
    """

    def __init__(self, message: str, raw: str | None = None):
        self.raw = raw
        super().__init__(message)


class PageFailed(RuntimeError):
    """Every rung of the retry ladder failed on one page (#238).

    :ivar errors: One error text per rung, in rung order.
    :ivar last: The last rung's text, which is what the page's
        ``error`` field carries -- the shape every reader knows.
    :ivar raw: The last truncated answer any rung produced, or None
        when no rung produced text at all.
    """

    def __init__(self, errors: list[str], raw: str | None = None):
        self.errors = list(errors)
        self.last = self.errors[-1] if self.errors else "no output"
        self.raw = raw
        super().__init__(
            "; ".join(
                f"rung {rung}: {text}"
                for rung, text in enumerate(self.errors, start=1)
            )
        )


def _threshold_render(image):
    """Return ``image`` with every pixel lighter than the threshold white.

    The retry render of scanning #238. The verso of a thin page shows
    through as mid-grey mirrored text; the real ink is near black. A
    cut at ``RETRY_THRESHOLD`` removes the one and keeps the other,
    and the size does not change, so a cell's bbox stays in the same
    pixel space as every other page of the corpus. Back to RGB because
    that is what the model's image pipeline expects.

    :param image: The page render, a PIL image.
    :returns: The thresholded render, RGB, same size.
    """
    grey = image.convert("L")
    return grey.point(lambda v: 255 if v > RETRY_THRESHOLD else 0).convert(
        "RGB"
    )


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
        "failed_pages": list[int], "filtered_pages": list[int],
        "recovered_pages": list[int], "duration_ms": int}``. Each page
        dict carries ``page_no``, ``input_width``/``input_height``
        (model-input dims for interpreting bboxes),
        ``origin_width``/``origin_height`` (actual render dims — the
        pixel space ``cells`` bboxes are rescaled into), a
        ``completion_tokens`` count, ``duration_ms``, and either
        ``cells`` (layout JSON) + ``md`` (markdown), or ``error``.
        Every page that got an answer also carries ``raw``, the answer
        as the model wrote it: ``cells`` is a parsed and rescaled copy,
        so ``raw`` is what a later post-processor starts from. On a
        failed page it is the last truncated answer, when there was one.
        ``filtered: true`` marks pages where the model output wasn't
        valid JSON and ``md`` holds the cleaned text fallback.
        The retry ladder (#238) adds ``attempts`` (rungs spent),
        ``recovered_by`` (the rung that gave usable output, absent on
        a first-try success and on a filtered page),
        ``fallback_from_rung`` (on a page filtered on every rung, the
        rung whose text was kept, when not the first),
        ``render: "threshold"`` for the rung that changed
        the render, and ``errors``
        (one text per failed rung) whenever a rung failed.
        ``recovered_pages`` lists the pages a retry rung saved.
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
    # daemon can't classify. The runner turns BadInputError into
    # BAD_INPUT.
    pdf_url = inputs.get("pdf_url")
    if not pdf_url:
        raise BadInputError("missing required input: pdf_url")
    prompt_mode = inputs.get("prompt_mode", DEFAULT_PROMPT_MODE)
    if prompt_mode not in _ALLOWED_PROMPT_MODES:
        raise BadInputError(
            f"unsupported prompt_mode: {prompt_mode!r}. "
            f"Expected one of {sorted(_ALLOWED_PROMPT_MODES)}"
        )
    dpi = coerce_input("dpi", inputs.get("dpi", DEFAULT_DPI), int)
    num_threads = coerce_input(
        "num_threads", inputs.get("num_threads", DEFAULT_NUM_THREADS), int
    )
    temperature = coerce_input(
        "temperature", inputs.get("temperature", DEFAULT_TEMPERATURE), float
    )
    top_p = coerce_input("top_p", inputs.get("top_p", DEFAULT_TOP_P), float)
    max_completion_tokens = coerce_input(
        "max_completion_tokens",
        inputs.get("max_completion_tokens", DEFAULT_MAX_COMPLETION_TOKENS),
        int,
    )
    # Coerced, not just range-checked. These two are handed straight to
    # ``fetch_image`` / ``post_process_output``, which do arithmetic on
    # them; a JSON-encoded string would pass the check below and then
    # fail inside every page, surfacing as "all N pages failed" rather
    # than as the input error it is.
    min_pixels = inputs.get("min_pixels")
    max_pixels = inputs.get("max_pixels")
    if min_pixels is not None:
        min_pixels = coerce_input("min_pixels", min_pixels, int)
        if min_pixels < MIN_PIXELS:
            raise BadInputError(f"min_pixels must be >= {MIN_PIXELS}")
    if max_pixels is not None:
        max_pixels = coerce_input("max_pixels", max_pixels, int)
        if max_pixels > MAX_PIXELS:
            raise BadInputError(f"max_pixels must be <= {MAX_PIXELS}")
    include_pictures = bool(inputs.get("include_pictures", False))
    prompt = dict_promptmode_to_prompt[prompt_mode]

    pdf_path = tmp_dir / "input.pdf"
    download_pdf(pdf_url, pdf_path)

    pages = validate_pdf(pdf_path)
    if pages > MAX_PAGES:
        raise BadInputError(
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
            # No ladder: the render is the input to every rung, and a
            # re-render of the same page gives the same nothing.
            return {
                "page_no": page_idx,
                "error": "page rendered empty",
                "attempts": 1,
            }
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

        def _infer(page_image) -> dict:
            """Run one rung: inference plus post-process on one render.

            :param page_image: The render this rung reads.
            :returns: The page dict, without the ladder bookkeeping.
            :raises RuntimeError: When the rung produced no usable
                output; the message is the rung's error text.
            """
            image = fetch_image(
                page_image, min_pixels=min_pixels, max_pixels=max_pixels
            )
            input_height, input_width = smart_resize(image.height, image.width)

            response = None
            finish_reason = None
            completion_tokens = None
            for attempt in range(INFERENCE_ATTEMPTS):
                try:
                    response, finish_reason, completion_tokens = (
                        _vllm_inference(
                            client,
                            image,
                            prompt,
                            temperature=temperature,
                            top_p=top_p,
                            max_completion_tokens=max_completion_tokens,
                        )
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
                # Truthiness, not ``is not None``: an empty-string
                # response (immediate EOS) is exactly the case this
                # retry exists for.
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
            # dense content (the cap is ~2x the longest measured page)
            # -- it is a repetition loop, and its output was garbage
            # before the cut. Fail the rung loudly instead of letting
            # the truncated JSON degrade into filtered=true,
            # indistinguishable from genuine model garbage.
            if finish_reason == "length":
                raise TruncatedOutput(
                    f"output truncated at {completion_tokens} tokens "
                    "(finish_reason='length'); likely a repetition loop",
                    raw=response,
                )

            result = {
                "page_no": page_idx,
                "input_width": input_width,
                "input_height": input_height,
                "origin_width": origin_image.width,
                "origin_height": origin_image.height,
                "completion_tokens": completion_tokens,
                # The answer as the model wrote it, on every page (#238).
                # ``cells`` below is upstream's parsed and rescaled copy
                # (``int()`` on every coordinate, pictures stripped from
                # the markdown), so a later post-processor would have
                # nothing else to start from. About 6 KB a page, and the
                # payload goes to S3, so the response cap is no concern.
                # The glue leaves it out of the volume document.
                "raw": response,
            }
            if render_fallback:
                result["render_fallback"] = True
            if prompt_mode in (
                "prompt_layout_all_en",
                "prompt_layout_only_en",
            ):
                cells, filtered = post_process_output(
                    response,
                    prompt_mode,
                    page_image,
                    image,
                    min_pixels=min_pixels,
                    max_pixels=max_pixels,
                )
                result["filtered"] = bool(filtered)
                if filtered:
                    # Model output wasn't valid JSON; ``cells`` is
                    # upstream's cleaned-text fallback, usable only as
                    # markdown. ``raw`` above is what says which
                    # character failed the parse: upstream's cleaner
                    # consumes the broken JSON and returns the words.
                    result["cells"] = None
                    result["md"] = cells if isinstance(cells, str) else None
                else:
                    result["cells"] = cells
                    if prompt_mode == "prompt_layout_all_en":
                        md = layoutjson2md(page_image, cells, text_key="text")
                        if not include_pictures:
                            md = _strip_data_uris(md)
                        result["md"] = md
            else:  # prompt_ocr: plain text extraction, no layout JSON
                result["md"] = response
            return result

        # The ladder (scanning #238). Rung 1 is the baseline; rung 2
        # reads the same page with the show-through thresholded away.
        # The render is the only difference, so the stage stays
        # deterministic. A rung fails on a loop, an exhausted transient
        # error, an exhausted empty answer, a post-process exception,
        # or a filtered answer. The filtered answer is kept as the
        # fallback result in case the later rung does no better: it is
        # not an error, only an answer the layout reader cannot use.
        # The threshold render is built lazily: most pages never need it.
        rungs = (
            (lambda: origin_image, {}),
            (lambda: _threshold_render(origin_image), {"render": "threshold"}),
        )
        errors: list[str] = []
        fallback: dict | None = None
        last_raw: str | None = None
        for rung, (render, marks) in enumerate(rungs, start=1):
            try:
                result = _infer(render())
            except TruncatedOutput as exc:
                errors.append(str(exc))
                last_raw = exc.raw
                continue
            except Exception as exc:
                errors.append(str(exc))
                continue
            result.update(marks)
            result["attempts"] = rung
            if result.get("filtered"):
                errors.append("model output was not layout JSON")
                if fallback is None:
                    fallback = result
                continue
            if rung > 1:
                result["recovered_by"] = rung
                result["errors"] = errors
                logger.warning(
                    "page %d recovered on rung %d after: %s",
                    page_idx,
                    rung,
                    "; ".join(
                        f"rung {n}: {text}"
                        for n, text in enumerate(errors, start=1)
                    ),
                )
            result["duration_ms"] = int((time.monotonic() - t0) * 1000)
            return result
        if fallback is not None:
            # Every rung answered garbage or nothing; the first
            # filtered answer is still text a reader can search. It is
            # not a recovery: ``recovered_by`` means a rung gave usable
            # output, and ``recovered_pages`` is the number the deploy
            # reads to judge the threshold. The rung that produced the
            # text is recorded under its own name.
            if fallback["attempts"] > 1:
                fallback["fallback_from_rung"] = fallback["attempts"]
            fallback["attempts"] = len(rungs)
            fallback["errors"] = errors
            fallback["duration_ms"] = int((time.monotonic() - t0) * 1000)
            return fallback
        raise PageFailed(errors, raw=last_raw)

    def _parse_page_safe(page_idx: int) -> dict:
        try:
            return _parse_page(page_idx)
        except PageFailed as exc:
            # Out of rungs. Keep the last rung's text as ``error`` (the
            # shape every reader knows) and the whole history beside it,
            # plus the last truncated answer when a rung produced one:
            # it is the only evidence of what the loop repeated.
            logger.error("page %d failed: %s", page_idx, exc)
            page = {
                "page_no": page_idx,
                "error": exc.last,
                "attempts": len(exc.errors),
                "errors": exc.errors,
            }
            if exc.raw is not None:
                page["raw"] = exc.raw
            return page
        except Exception as exc:
            # One bad page must not sink a 1000-page job: record the
            # error per page and keep going. If *every* page fails the
            # whole job raises below.
            logger.exception("page %d failed", page_idx)
            return {"page_no": page_idx, "error": str(exc), "attempts": 1}

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
    filtered = [r["page_no"] for r in results if r.get("filtered")]
    recovered = [r["page_no"] for r in results if "recovered_by" in r]
    if len(failed) == pages:
        raise RuntimeError(
            f"all {pages} pages failed; first error: {results[0].get('error')}"
        )

    duration_ms = int((time.monotonic() - t0) * 1000)
    logger.info(
        "parse OK: %d pages (%d failed, %d filtered, %d recovered on a "
        "retry) in %d ms (mode=%s, dpi=%d)",
        pages,
        len(failed),
        len(filtered),
        len(recovered),
        duration_ms,
        prompt_mode,
        dpi,
    )
    return {
        "pages": results,
        "page_count": pages,
        "failed_pages": failed,
        "filtered_pages": filtered,
        "recovered_pages": recovered,
        "duration_ms": duration_ms,
    }


# Summary fields kept in the job response when the payload goes to S3.
# Everything else -- above all ``pages`` -- is deliberately dropped: the
# response is capped at about 20 MB and discarded with the job record,
# which is the whole reason the payload travels through S3. The three
# page lists are small and are what the daemon acts on: a filtered page
# is as much a hole to the page-number reader as a failed one (#238),
# and a list that only the S3 object carried was invisible to it.
_SUMMARY_FIELDS = (
    "page_count",
    "failed_pages",
    "filtered_pages",
    "recovered_pages",
    "duration_ms",
)


def _deliver(result: dict, inputs: dict, scan_pk: Any) -> dict:
    """See :func:`runpod_common.deliver_result`."""
    return runpod_common.deliver_result(
        result,
        inputs,
        scan_pk,
        summary_fields=_SUMMARY_FIELDS,
        upload=upload_result,
    )


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
