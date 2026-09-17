"""RunPod Serverless handler for Surya OCR 2 full-page reads.

Runs ``datalab-to/surya-ocr-2`` behind a local vLLM server (spawned as
a subprocess at worker boot) and dispatches on ``job["input"]["action"]``:

- ``ocr``: fetch a PDF via presigned GET URL, render each page, read
  it whole with surya's ``RecognitionPredictor``, and return one block
  list per page: label, bbox, HTML and text, in reading order.

The reading itself is the ``surya-ocr`` package's, called the way the
ai-research ``runpod/kits/surya`` runner calls it (``full_page=True``,
one page per call): its prompt, its image fit, its HTML parse, its
loop detection and its block-mode fallback. This worker sets no decode
parameter and accepts none from the job input. What it adds is what a
pod runner does not need: the page fan-out, the retry of an empty
read, the abort when the server has died under the job, the raw answer
kept beside the parsed blocks, and the result envelope the daemon
reads (``runpod_common``).

The vLLM server is started at module import time so cold start pays
the model-load cost once; subsequent warm invocations reuse the
running engine. The ``surya`` client is pointed at it through the
``SURYA_INFERENCE_*`` environment, set here before the package is
imported, because its settings are read once at import.
"""

from __future__ import annotations

import html as _html
import json
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
logger = logging.getLogger("surya.runpod")
# surya's OpenAI client logs one INFO line per request through httpx;
# on a 100-page shard that is the whole log. vLLM's own access log
# already records every request.
logging.getLogger("httpx").setLevel(logging.WARNING)

# Populated in ``_preload``. Surfaced in every handler response so the
# daemon can tell whether inference actually hit a GPU.
_GPU_AVAILABLE = False
_VLLM_READY = False
_VLLM_PROC: subprocess.Popen | None = None

sentry_sdk = runpod_common.init_sentry(logger)


# ── Tunables ────────────────────────────────────────────────────────
# Hard sanity guard on input size, worker-side. Not a caller-tunable
# "process at most N pages" knob: that name means *truncate* elsewhere
# in the repo (``runpod_client.py``), and a partial read merged as a
# whole volume is worse than a failure. Volumes arrive sharded (#164),
# so a shard over this is a pipeline bug, not a big scan.
MAX_PAGES = int(os.environ.get("HANDLER_MAX_PAGES", "5000"))
# Download tunables (HANDLER_DOWNLOAD_*) live in runpod_common.

# ── vLLM server tunables ────────────────────────────────────────────
# The model lives in the baked HF cache (HF_HOME=/opt/hf, offline). The
# served name must equal this id: surya's client asks the server for
# its model list and refuses a name other than its own checkpoint
# setting (``surya.inference.backends.spawn.attach_or_spawn``).
SURYA_MODEL = os.environ.get("SURYA_MODEL", "datalab-to/surya-ocr-2")
VLLM_HOST = "127.0.0.1"
# HANDLER_-prefixed on purpose: ``VLLM_PORT`` itself is a *reserved*
# vLLM env var (the base port for internal distributed services), and
# the spawned ``vllm serve`` inherits our environment. Exposing the API
# port under that name would make the server bind internal sockets on
# the port it serves the API on.
VLLM_PORT = int(os.environ.get("HANDLER_VLLM_PORT", "8000"))
# The serve flags of the ai-research kit (``runpod/kits/surya/run.sh``),
# which mirror Datalab's own launcher: bfloat16, an 18000-token context
# (the full-page budget is 12288 output tokens plus the image), the
# processor pixel bounds the model was served with, prefix caching on.
# bfloat16 needs an Ampere or newer card; a T4 wants float16.
VLLM_DTYPE = os.environ.get("VLLM_DTYPE", "bfloat16")
VLLM_MAX_MODEL_LEN = os.environ.get("VLLM_MAX_MODEL_LEN", "18000")
VLLM_GPU_MEMORY_UTILIZATION = os.environ.get(
    "VLLM_GPU_MEMORY_UTILIZATION", "0.85"
)
VLLM_MM_PROCESSOR_KWARGS = json.dumps(
    {"min_pixels": 3136, "max_pixels": 6291456}
)
# MTP speculative decoding is a decode speedup Datalab enables by
# default and the kit left off; it changes no answer under greedy
# decoding, so it is a throughput knob and nothing more.
VLLM_ENABLE_MTP = os.environ.get("VLLM_ENABLE_MTP", "0") == "1"
VLLM_MTP_TOKENS = os.environ.get("VLLM_MTP_TOKENS", "2")
# Model load from the baked cache is typically 1-3 min; the budget is
# generous because the first boot on a node also compiles CUDA graphs.
VLLM_STARTUP_TIMEOUT = int(os.environ.get("VLLM_STARTUP_TIMEOUT", "900"))
# Extra ``vllm serve`` flags, split shell-style. Escape hatch for
# endpoint-level tuning without a rebuild.
VLLM_EXTRA_ARGS = os.environ.get("VLLM_EXTRA_ARGS", "")

# ── Inference tunables (job input can override the per-job ones) ────
# 200 dpi renders US Letter to 1700x2200: the canonical page size the
# ai-research pipeline staged for this model, and the pixel space of
# the dots.mocr cells and the detection rows (``DPI = 200`` there too),
# so a box from this worker lands on the same pixels as theirs.
DEFAULT_DPI = int(os.environ.get("HANDLER_DPI", "200"))
# Pages in flight against the local vLLM server. The kit's default was
# 8 and its README measured 16 as the good value on an A40, with 32
# slower than 16 for a chunk-synchronous client; this client is not
# chunked, and the first smoke run on a 24 GB card showed vLLM at
# ``Running: 8, Waiting: 0`` with KV cache for 57 requests, so the
# client threads were the ceiling. Each page is one request while the
# full-page answer holds; a page that falls back to block mode fans out
# into one request per block, bounded by SURYA_PARALLEL below. The
# server batches continuously, so this bounds client-side memory
# (rendered pages, about 11 MB each at 200 dpi), not GPU batch size.
DEFAULT_NUM_THREADS = int(os.environ.get("HANDLER_NUM_THREADS", "16"))
# The most a job may ask for. surya's own client stops scaling at 96
# (``VllmBackend.MAX_AUTO_PARALLEL``), and every thread holds a render.
MAX_NUM_THREADS = 64
# Concurrent requests surya's own client may open inside one page read
# (the block-mode fallback). The kit's PARALLEL default; left to surya
# it would size this from a 4090's capacity, and eight pages each
# fanning out that wide floods the server with 500s.
SURYA_PARALLEL = os.environ.get("HANDLER_SURYA_PARALLEL", "8")
# A page whose read comes back with no block is read once more before
# it is written as empty. Observed in the kit: a server can answer a
# whole chunk with no blocks and no exception, and a run that wrote
# that as success lost real pages. A truly blank page is rare, and even
# a near-blank one normally yields its page number.
EMPTY_READS = 2
# When this many pages in a row come back failed or empty and the
# server no longer answers its health check, the job stops: every
# remaining page would cost surya's retry ladder (about twenty seconds
# against a dead socket) and come back empty too. The daemon re-queues
# a FAILED job onto a fresh worker.
ABORT_STREAK = int(os.environ.get("HANDLER_ABORT_STREAK", "16"))

# Decode parameters this worker refuses to carry. surya's predictor
# owns the decode (greedy, with its own loop retry), and a knob here
# would be a second copy that drifts from what the ai-research runs
# measured. Refused loudly rather than ignored: a caller who sends one
# expects it to do something.
REFUSED_INPUTS = (
    "temperature",
    "top_p",
    "max_tokens",
    "max_completion_tokens",
)

#: The prompt type surya uses for a whole-page read
#: (``surya.inference.schema.PROMPT_TYPE_HIGH_ACCURACY_BBOX``). Named
#: here so the recorder can pick the page's own answer out of the
#: requests a read made without importing surya at module scope.
FULL_PAGE_PROMPT = "high_accuracy_bbox"
#: The prompt type of the layout pass surya runs when the full-page
#: answer failed (``PROMPT_TYPE_LAYOUT``): its presence in a page's
#: request log is what says the page was read in block mode.
LAYOUT_PROMPT = "layout"


def _configure_surya() -> None:
    """Point the ``surya`` client at the local server, before its import.

    surya reads its settings once, at import, from the environment
    (``surya.settings.Settings`` is a pydantic settings object built at
    module load), so this must run before anything imports the package.
    ``setdefault``: an endpoint env var wins over these, and the URL
    follows the handler's own port, so the two cannot disagree.
    """
    os.environ.setdefault("SURYA_INFERENCE_BACKEND", "vllm")
    os.environ.setdefault(
        "SURYA_INFERENCE_URL", f"http://{VLLM_HOST}:{VLLM_PORT}/v1"
    )
    os.environ.setdefault("SURYA_MODEL_CHECKPOINT", SURYA_MODEL)
    os.environ.setdefault("SURYA_INFERENCE_PARALLEL", SURYA_PARALLEL)
    # Progress bars in a worker log are noise.
    os.environ.setdefault("DISABLE_TQDM", "1")


_configure_surya()


# ── GPU / vLLM lifecycle ────────────────────────────────────────────
def _gpu_available() -> bool:
    """Return True if the NVIDIA driver exposes at least one GPU.

    The handler venv carries a CPU torch (surya imports it), so the
    check shells out to ``nvidia-smi``, which the container toolkit
    bind-mounts on GPU hosts and which is absent on CPU-only ones.

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


def _vllm_command() -> list[str]:
    """Build the ``vllm serve`` command line.

    The flags are the ai-research kit's, which mirror Datalab's own
    launcher (``surya/inference/backends/vllm.py``). ``--served-model-name``
    repeats the model id because surya's client refuses any other name.
    Bound to localhost: the only client is this handler.

    :returns: The argv.
    :rtype: list[str]
    """
    cmd = [
        "vllm",
        "serve",
        SURYA_MODEL,
        "--host",
        VLLM_HOST,
        "--port",
        str(VLLM_PORT),
        "--served-model-name",
        SURYA_MODEL,
        "--dtype",
        VLLM_DTYPE,
        "--max-model-len",
        VLLM_MAX_MODEL_LEN,
        "--gpu-memory-utilization",
        VLLM_GPU_MEMORY_UTILIZATION,
        "--mm-processor-kwargs",
        VLLM_MM_PROCESSOR_KWARGS,
        "--enable-prefix-caching",
    ]
    if VLLM_ENABLE_MTP:
        cmd += [
            "--speculative-config",
            json.dumps(
                {
                    "method": "mtp",
                    "num_speculative_tokens": int(VLLM_MTP_TOKENS),
                }
            ),
        ]
    return cmd + shlex.split(VLLM_EXTRA_ARGS)


def _start_vllm() -> subprocess.Popen:
    """Spawn ``vllm serve`` for surya-ocr-2, inheriting our stdout/stderr.

    :returns: The server process handle.
    :rtype: subprocess.Popen
    """
    cmd = _vllm_command()
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
        # Logged at warning, not error: expected and self-healing (the
        # fitness check keeps this worker out of the pool), so it must
        # not raise a Sentry event.
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
            SURYA_MODEL,
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


# ── The surya client ────────────────────────────────────────────────
class RequestLog:
    """A thread-local record of the model answers one page read made.

    surya's ``RecognitionPredictor`` consumes the model's answer: it
    parses the HTML into blocks and keeps no copy, and when the answer
    fails it asks again (a layout pass, then one request per block)
    without saying so. The issue asked for the raw output beside the
    parsed one, so a page can be re-read later when a parse turns out
    wrong. The recorder sits on the inference manager, whose
    ``generate`` every request goes through, and notes each answer in
    a thread-local list: a page is read on one worker thread from
    :meth:`open` to :meth:`close`, so the list holds that page's
    requests and nobody else's.
    """

    def __init__(self) -> None:
        self._local = threading.local()

    def open(self) -> None:
        """Start recording on the calling thread."""
        self._local.records = []

    def close(self) -> list[dict]:
        """Stop recording on the calling thread and return the notes.

        :returns: One dict per request, in request order:
            ``prompt_type``, ``raw``, ``completion_tokens``, ``error``
            and ``confidence`` (surya's mean token probability, when
            logprobs were returned).
        :rtype: list[dict]
        """
        records = getattr(self._local, "records", None) or []
        self._local.records = None
        return records

    def note(self, batch: list, outputs: list) -> None:
        """Record one ``generate`` call, if the thread is recording.

        :param batch: surya's ``BatchInputItem`` list, as given.
        :param outputs: The matching ``BatchOutputItem`` list.
        """
        records = getattr(self._local, "records", None)
        if records is None:
            return
        for item, out in zip(batch, outputs):
            records.append(
                {
                    "prompt_type": getattr(item, "prompt_type", None),
                    "raw": getattr(out, "raw", None),
                    "completion_tokens": getattr(out, "token_count", None),
                    "error": bool(getattr(out, "error", False)),
                    "confidence": getattr(out, "mean_token_prob", None),
                }
            )


_LOG = RequestLog()
_RECOGNIZER = None
_RECOGNIZER_LOCK = threading.Lock()


def _build_recognizer(log: RequestLog):
    """Build surya's predictor over a recording inference manager.

    Imported here and not at module scope: the package imports torch on
    load, which takes seconds a CPU-only worker (which refuses every
    job anyway) should not pay, and the tests stub the package.
    ``manager.start()`` attaches to the local server: it probes
    ``/health`` and ``/v1/models`` and refuses a served name other than
    the checkpoint, so a wrong ``--served-model-name`` fails here, at
    the first job, with its own message.

    :param log: The recorder every request is noted on.
    :returns: A ``RecognitionPredictor``.
    """
    from surya.inference import SuryaInferenceManager
    from surya.recognition import RecognitionPredictor

    class RecordingManager(SuryaInferenceManager):
        """surya's manager, with every answer noted on the log."""

        def generate(self, batch):
            outputs = super().generate(batch)
            log.note(batch, outputs)
            return outputs

    manager = RecordingManager(method="vllm")
    manager.start()
    return RecognitionPredictor(manager)


def _recognizer():
    """Return the one predictor of this process, building it on first use.

    :returns: The ``RecognitionPredictor`` over the local server.
    """
    global _RECOGNIZER
    with _RECOGNIZER_LOCK:
        if _RECOGNIZER is None:
            _RECOGNIZER = _build_recognizer(_LOG)
        return _RECOGNIZER


# ── Page rendering and serialization ────────────────────────────────
def _render_page(page, dpi: int):
    """Render one PyMuPDF page to an RGB PIL image at ``dpi``.

    No resize: the model's own image fit (``surya.inference.util
    .scale_to_fit``) runs inside the predictor, and the bboxes come back
    in the size of the image handed in, so a box is in this render's
    pixel space. US Letter at 200 dpi is 1700x2200.

    :param page: A ``fitz.Page``.
    :param dpi: The render resolution.
    :returns: The page, as a PIL image.
    """
    import fitz
    from PIL import Image

    zoom = dpi / 72
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
    return Image.frombytes("RGB", (pix.width, pix.height), pix.samples)


_TAG_RE = re.compile(r"<[^>]+>")
# A line break, a rule, or the close of a paragraph, heading, list item,
# table cell or row: the boundaries a reader would not want glued. The
# model writes an author block as a table, one cell per author, and a
# flattening that dropped ``</td>`` read them as one word.
_BREAK_RE = re.compile(
    r"<(?:br|hr)\s*/?>|</(?:p|div|li|td|th|tr|h[1-6])>", re.IGNORECASE
)


def _html_to_text(html: str) -> str:
    """Flatten a block's HTML to plain text.

    The kit's reading, with the boundaries kept: a ``<br>``, a rule or a
    closed paragraph, heading, list item, table cell or row becomes a
    newline, every other tag is dropped, entities are unescaped and
    runs of blanks squeezed. A convenience beside
    ``html``, which is the model's verbatim answer.

    :param html: The block's HTML.
    :returns: The text.
    :rtype: str
    """
    if not html:
        return ""
    text = _TAG_RE.sub("", _BREAK_RE.sub("\n", html))
    text = _html.unescape(text)
    # ``[^\S\n]``: every blank but the newline, the no-break space of
    # an ``&nbsp;`` included, so a reader comparing text never trips on
    # the entity the model chose.
    lines = [
        re.sub(r"[^\S\n]+", " ", line).strip() for line in text.split("\n")
    ]
    return "\n".join(line for line in lines if line)


def _field(obj, name: str, default=None):
    """Read ``name`` off a pydantic model or a plain dict.

    :param obj: A surya result object, or its dumped dict.
    :param name: The field.
    :param default: The answer when the field is absent.
    """
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _bbox(block) -> list[float] | None:
    """The block's ``[x0, y0, x1, y1]`` in the render's pixel space.

    surya's ``PolygonBox`` derives ``bbox`` from ``polygon``; the
    fallback here computes it the same way for a dumped dict that
    carries only the polygon.

    :param block: A ``BlockOCRResult`` or its dict.
    :returns: The box, or None when the block has no geometry.
    :rtype: list[float] | None
    """
    bbox = _field(block, "bbox")
    if bbox is not None:
        return [float(v) for v in bbox]
    polygon = _field(block, "polygon")
    if not polygon:
        return None
    xs = [float(p[0]) for p in polygon]
    ys = [float(p[1]) for p in polygon]
    return [min(xs), min(ys), max(xs), max(ys)]


def _serialize_blocks(result) -> list[dict]:
    """Turn surya's ``PageOCRResult`` into the block list of the answer.

    One dict per block, in surya's order: ``order`` (reading order),
    ``label`` (surya's canonical layout label), ``raw_label`` (the
    model's), ``bbox``, ``confidence``, ``html`` (the model's verbatim
    block HTML), ``text`` (flattened), ``skipped`` (a label surya does
    not OCR: figures and images) and ``error`` (a block whose read
    failed in block mode; its ``html`` is empty).

    :param result: A ``PageOCRResult``.
    :returns: The blocks.
    :rtype: list[dict]
    """
    blocks = []
    for index, block in enumerate(_field(result, "blocks") or []):
        html = _field(block, "html") or ""
        order = _field(block, "reading_order")
        blocks.append(
            {
                "order": index if order is None else order,
                "label": _field(block, "label"),
                "raw_label": _field(block, "raw_label"),
                "bbox": _bbox(block),
                "confidence": _field(block, "confidence"),
                "html": html,
                "text": _html_to_text(html),
                "skipped": bool(_field(block, "skipped", False)),
                "error": bool(_field(block, "error", False)),
            }
        )
    return blocks


def _is_empty(blocks: list[dict]) -> bool:
    """Whether a read gave the page nothing: no block that was read.

    A page of nothing but failed blocks is as empty as one of no blocks:
    both are what a dead server answers, and both are worth the second
    read. A page whose only block is a skipped figure is not empty: the
    model saw the page and said what was on it.

    :param blocks: The serialized blocks.
    :returns: True when nothing was read.
    :rtype: bool
    """
    return all(block["error"] for block in blocks)


def _raw_answer(records: list[dict]) -> str | None:
    """The full-page answer among a read's requests, as the model wrote it.

    :param records: The recorder's notes for one read.
    :returns: The answer, or None when there was none: no full-page
        request made, or one that errored (surya hands those back as
        ``""``).
    :rtype: str | None
    """
    for record in records:
        if record["prompt_type"] == FULL_PAGE_PROMPT:
            return record["raw"] or None
    return None


def _account_for_raw(page: dict, parse_raw) -> None:
    """Say what surya dropped between the answer and the blocks (#344).

    surya's parse of the full-page answer loses things on the way to
    ``blocks``, each of them silently or on one INFO line of its own
    logger: a top-level div with a missing or malformed ``data-bbox``
    is skipped; a text block over a crop that is 99 percent near-white
    is dropped as a hallucination (``_drop_blank_text_blocks``); a
    label surya does not OCR (``Figure``, ``Picture``, ``Diagram``,
    ``BlankPage``, and ``Complex-Block``, which canonicalizes to
    ``Figure``) keeps its geometry but has its HTML replaced by ``""``.
    The dots.mocr and YOLO post-processing incidents were exactly a
    count nobody had, so the answer is parsed once more here, with
    surya's own parser, and compared.

    Only on a page read whole: surya numbers the blocks of that path by
    their index in the parsed list (``reading_order=idx``) and a later
    drop keeps the survivors' numbers, so the two lists align by
    ``order``. A page read in block mode numbers its blocks by the
    layout pass instead, and its answer is the one that failed, so the
    count is recorded and nothing is restored.

    :param page: The page dict, updated in place: ``parsed_blocks``
        (top-level divs surya's parser finds in ``raw``, None when it
        raises), ``dropped_blocks`` (the parsed entries with no block,
        as ``{"order", "raw_label"}``), and on a skipped block ``html``
        and ``text`` restored from the parsed entry.
    :param parse_raw: surya's ``parse_full_page_html``.
    """
    raw = page.get("raw")
    if not raw:
        return
    try:
        parsed = parse_raw(raw)
    except Exception as exc:
        logger.warning(
            "page %d: surya's parser refused raw: %s", page["page_no"], exc
        )
        page["parsed_blocks"] = None
        return
    page["parsed_blocks"] = len(parsed)
    if "fallback" in page:
        return
    by_order = {block["order"]: block for block in page["blocks"]}
    dropped = []
    restored = False
    for order, item in enumerate(parsed):
        block = by_order.get(order)
        if block is None:
            dropped.append(
                {"order": order, "raw_label": _field(item, "label")}
            )
            continue
        if block["skipped"] and not block["html"]:
            html = _field(item, "html") or ""
            block["html"] = html
            block["text"] = _html_to_text(html)
            restored = restored or bool(html)
    if restored:
        # The page text is the blocks' text in order, and a block just
        # got some back.
        page["text"] = "\n".join(
            block["text"] for block in page["blocks"] if block["text"]
        )
    if dropped:
        page["dropped_blocks"] = dropped
        logger.warning(
            "page %d: surya dropped %d of %d parsed blocks: %s",
            page["page_no"],
            len(dropped),
            len(parsed),
            ", ".join(f"{d['order']} {d['raw_label']}" for d in dropped),
        )


def _page_from_read(page_idx: int, image, result, records: list[dict]) -> dict:
    """Assemble one page dict from a read and the requests it made.

    :param page_idx: The 0-based page index in the shard.
    :param image: The render the model saw.
    :param result: surya's ``PageOCRResult``.
    :param records: The recorder's notes for this read.
    :returns: The page dict, without the ladder bookkeeping
        (``attempts``, ``duration_ms``) the caller adds.
    :rtype: dict
    """
    blocks = _serialize_blocks(result)
    texts = [block["text"] for block in blocks if block["text"]]
    full_page = [r for r in records if r["prompt_type"] == FULL_PAGE_PROMPT]
    tokens = [
        r["completion_tokens"]
        for r in records
        if isinstance(r["completion_tokens"], int)
    ]
    page = {
        "page_no": page_idx,
        "origin_width": image.width,
        "origin_height": image.height,
        "blocks": blocks,
        "text": "\n".join(texts),
        # The answer as the model wrote it, on every page: what a later
        # post-processor starts from, and on a page surya could not
        # parse, the only evidence of what it wrote. surya keeps no
        # copy. The payload goes to S3, so its size is no concern.
        "raw": _raw_answer(records),
        "requests": len(records),
        "completion_tokens": sum(tokens) if tokens else None,
    }
    confidences = [
        r["confidence"] for r in full_page if r["confidence"] is not None
    ]
    if confidences:
        page["confidence"] = confidences[0]
    if any(r["prompt_type"] == LAYOUT_PROMPT for r in records):
        # surya ran its layout pass: the full-page answer looped or
        # would not parse, and the blocks were read one by one. This
        # is the last rung, not the first: surya's client retries a
        # looping or errored answer up to three times inside one
        # request (``chat_completions_batch``, at a temperature it
        # raises itself), and only the final answer reaches the log.
        # A page saved by such a retry looks like a clean page here.
        page["fallback"] = "block"
    error_blocks = sum(1 for block in blocks if block["error"])
    if error_blocks:
        page["error_blocks"] = error_blocks
    return page


# ── Actions ─────────────────────────────────────────────────────────
def _action_ocr(job: dict, inputs: dict, tmp_dir: Path) -> dict:
    """Read every page of a PDF with Surya OCR 2.

    Pages are rendered lazily (one fitz document per worker thread:
    PyMuPDF documents are not safe to render from concurrently) and
    fanned out to the local vLLM server one page per predictor call,
    so a finished page frees its slot at once and a slow page holds
    only itself. Each call is the kit's ``rec(imgs, full_page=True)``
    with a list of one.

    :param job: RunPod job dict (used for progress updates).
    :param inputs: Handler input payload. Required: ``pdf_url``.
        Result delivery (handled by :func:`_deliver`, not here):
        ``result_url`` and ``result_key``. Optional: ``dpi`` (default
        200) and ``num_threads`` (default 16, at most
        ``MAX_NUM_THREADS``). No decode parameter is
        accepted (:data:`REFUSED_INPUTS`), and there is no ``max_pages``:
        that name means "truncate" elsewhere in the repo, and a partial
        read merged as a whole volume is worse than a failure. Inputs
        over the env-level ``MAX_PAGES`` are rejected.
    :param tmp_dir: Per-job scratch directory.
    :returns: ``{"pages": list[dict], "page_count": int,
        "failed_pages": list[int], "empty_pages": list[int],
        "fallback_pages": list[int], "dropped_block_pages": list[int],
        "duration_ms": int}``. Each page
        dict carries ``page_no``, ``origin_width``/``origin_height``
        (the render size, the pixel space of every ``bbox``),
        ``blocks`` (see :func:`_serialize_blocks`), ``text`` (the
        blocks' text in reading order), ``raw`` (the full-page answer
        as the model wrote it, None when there was no answer),
        ``requests`` (model requests the read made), ``completion_tokens``
        (their generated tokens), ``attempts`` (reads spent: 2 when the
        first came back empty), ``duration_ms``, and on some pages
        ``confidence`` (surya's mean token probability of the answer),
        ``fallback: "block"`` (surya re-read the page block by block
        after the full-page answer failed), ``error_blocks`` (blocks
        that failed in block mode) and ``empty: true`` (no block after
        every read; kept, not failed, so the shard converges). On a
        page read whole, ``parsed_blocks`` and ``dropped_blocks`` say
        what surya's parse lost on the way to ``blocks``, and a skipped
        block carries the HTML of its parsed entry
        (:func:`_account_for_raw`); ``dropped_block_pages`` lists the
        pages with a drop. A page whose read raised is ``{"page_no",
        "error", "attempts"}`` plus ``raw`` when the model had answered,
        and is listed in ``failed_pages``.
    :rtype: dict
    :raises RuntimeError: When every page failed or came back empty,
        or when a run of empty pages met a server that no longer
        answers: a whole shard of nothing is a dead server and not a
        blank volume, and RunPod must mark the job FAILED so the daemon
        re-queues it.
    """
    import fitz

    # ``.get`` + explicit raise, not ``inputs["pdf_url"]``: a KeyError
    # would surface as a raw traceback with no error_code, which the
    # daemon can't classify. The runner turns BadInputError into
    # BAD_INPUT.
    pdf_url = inputs.get("pdf_url")
    if not pdf_url:
        raise BadInputError("missing required input: pdf_url")
    refused = [name for name in REFUSED_INPUTS if name in inputs]
    if refused:
        raise BadInputError(
            f"this worker sets no decode parameter; refusing {refused}. "
            "surya's predictor owns the decode."
        )
    dpi = coerce_input("dpi", inputs.get("dpi", DEFAULT_DPI), int)
    if dpi < 36 or dpi > 600:
        raise BadInputError(f"dpi must be between 36 and 600, got {dpi}")
    num_threads = coerce_input(
        "num_threads", inputs.get("num_threads", DEFAULT_NUM_THREADS), int
    )
    if num_threads < 1 or num_threads > MAX_NUM_THREADS:
        raise BadInputError(
            f"num_threads must be between 1 and {MAX_NUM_THREADS}, "
            f"got {num_threads}"
        )

    pdf_path = tmp_dir / "input.pdf"
    download_pdf(pdf_url, pdf_path)

    pages = validate_pdf(pdf_path)
    if pages > MAX_PAGES:
        raise BadInputError(
            f"PDF has {pages} pages, exceeds MAX_PAGES={MAX_PAGES}"
        )

    # Built before the fan-out: the attach probes the server and the
    # served name once, on this thread, so a wrong name fails the job
    # with its own message and not as N identical page errors.
    recognizer = _recognizer()
    from surya.inference.parsers import parse_full_page_html

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

    def _read_page(page_idx: int) -> dict:
        t0 = time.monotonic()
        attempt = 0
        records: list[dict] = []
        try:
            image = _render_page(_doc()[page_idx], dpi)
            page: dict | None = None
            for attempt in range(1, EMPTY_READS + 1):
                _LOG.open()
                try:
                    # The kit's call, with a list of one. ``full_page=True``
                    # is the block mode of the kit: one whole-page request,
                    # layout blocks with a bbox and HTML each. Nothing else
                    # is passed: the decode is surya's.
                    result = recognizer([image], full_page=True)[0]
                finally:
                    records = _LOG.close()
                page = _page_from_read(page_idx, image, result, records)
                _account_for_raw(page, parse_full_page_html)
                page["attempts"] = attempt
                if not _is_empty(page["blocks"]):
                    break
                if attempt < EMPTY_READS:
                    logger.warning(
                        "page %d: no block on read %d/%d; reading again",
                        page_idx,
                        attempt,
                        EMPTY_READS,
                    )
        except Exception as exc:
            # One bad page must not sink a 100-page job: record the
            # error per page, with the read it happened on, and keep
            # going. If *every* page fails the whole job raises below.
            logger.exception("page %d failed", page_idx)
            failed = {
                "page_no": page_idx,
                "error": str(exc),
                "attempts": max(attempt, 1),
            }
            # The answer the model gave before the read raised, when
            # there was one: the only evidence of what went wrong, as
            # on a failed dots.mocr page.
            raw = _raw_answer(records)
            if raw is not None:
                failed["raw"] = raw
            return failed
        if _is_empty(page["blocks"]):
            # Twice empty: written, so the shard converges instead of
            # looping, but marked, because a truly blank page is rare.
            page["empty"] = True
            logger.warning(
                "page %d: no block after %d reads; written as empty",
                page_idx,
                EMPTY_READS,
            )
        elif page["attempts"] > 1:
            logger.info(
                "page %d: empty on the first read, the second gave %d blocks",
                page_idx,
                len(page["blocks"]),
            )
        page["duration_ms"] = int((time.monotonic() - t0) * 1000)
        return page

    t0 = time.monotonic()
    results: list[dict] = []
    workers = max(1, min(pages, num_threads))
    # A page that failed or came back empty, ``streak`` times in a row
    # in completion order, with a server that no longer answers, is a
    # dead server: stop, instead of paying surya's retry ladder on
    # every remaining page for nothing.
    streak = 0
    pool = ThreadPoolExecutor(max_workers=workers)
    try:
        futures = [pool.submit(_read_page, i) for i in range(pages)]
        for done, fut in enumerate(as_completed(futures), start=1):
            page = fut.result()
            results.append(page)
            if "error" in page or page.get("empty"):
                streak += 1
            else:
                streak = 0
            if streak >= ABORT_STREAK and not _vllm_healthy():
                raise RuntimeError(
                    f"vLLM server stopped answering: {streak} pages in a "
                    f"row came back failed or empty after {done}/{pages} "
                    "pages; aborting so the job is re-queued"
                )
            if done % 25 == 0 or done == pages:
                logger.info("read %d/%d pages", done, pages)
                try:
                    runpod.serverless.progress_update(
                        job, f"{done}/{pages} pages"
                    )
                except Exception:
                    # Progress is best-effort; never fail a job over it.
                    pass
    finally:
        # ``cancel_futures``: on the abort above, the queued pages are
        # dropped; the pages already reading finish (or fail) first.
        pool.shutdown(wait=True, cancel_futures=True)
        for doc in open_docs:
            doc.close()

    results.sort(key=lambda r: r["page_no"])
    failed = [r["page_no"] for r in results if "error" in r]
    empty = [r["page_no"] for r in results if r.get("empty")]
    fallback = [r["page_no"] for r in results if "fallback" in r]
    dropped = [r["page_no"] for r in results if r.get("dropped_blocks")]
    if len(failed) + len(empty) == pages:
        raise RuntimeError(
            f"all {pages} pages failed or came back empty; "
            f"first error: {results[0].get('error', 'no block read')}"
        )

    duration_ms = int((time.monotonic() - t0) * 1000)
    logger.info(
        "ocr OK: %d pages (%d failed, %d empty, %d read in block mode, "
        "%d with a dropped block) in %d ms (dpi=%d)",
        pages,
        len(failed),
        len(empty),
        len(fallback),
        len(dropped),
        duration_ms,
        dpi,
    )
    return {
        "pages": results,
        "page_count": pages,
        "failed_pages": failed,
        "empty_pages": empty,
        "fallback_pages": fallback,
        "dropped_block_pages": dropped,
        "duration_ms": duration_ms,
    }


# Summary fields kept in the job response when the payload goes to S3.
# Everything else -- above all ``pages`` -- is deliberately dropped: the
# response is capped at about 20 MB and discarded with the job record,
# which is the whole reason the payload travels through S3. The three
# page lists are small and are what a caller acts on: an empty page is
# as much a hole to a reader as a failed one, and a list that only the
# S3 object carried would be invisible to it.
_SUMMARY_FIELDS = (
    "page_count",
    "failed_pages",
    "empty_pages",
    "fallback_pages",
    "dropped_block_pages",
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
    "ocr": _action_ocr,
}


# ── Entry point ─────────────────────────────────────────────────────
def handler(job: dict) -> dict:
    """RunPod Serverless entry point.

    :param job: RunPod job dict. ``job["input"]`` must carry an
        ``action`` ("ocr") and action-specific args. An optional
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
        ``page_count``, the page lists, ``duration_ms``). Without it
        the payload comes back inline.
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
        # re-queued jobs straight back to it. Flagging the worker for
        # termination breaks that livelock at the cost of one cold boot.
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
