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
import random
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
from urllib3.exceptions import IncompleteRead, ProtocolError

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

# ── vLLM server tunables ────────────────────────────────────────────
# The model lives in the baked HF cache (HF_HOME=/opt/hf, offline).
DOTSMOCR_MODEL = os.environ.get("DOTSMOCR_MODEL", "rednote-hilab/dots.mocr")
# Upstream's parser defaults to model_name="model"; we serve under the
# same alias so their client code works unmodified.
SERVED_MODEL_NAME = "model"
VLLM_HOST = "127.0.0.1"
VLLM_PORT = int(os.environ.get("VLLM_PORT", "8000"))
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
DEFAULT_TEMPERATURE = 0.1
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
    return subprocess.Popen(cmd)


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

    # Hand the parser a complete file or nothing: a truncated PDF
    # silently produces wrong page counts and text. Raising re-queues.
    if total is not None:
        actual = dest.stat().st_size if dest.exists() else 0
        if actual != total:
            raise RuntimeError(
                f"incomplete download: {actual}/{total} bytes after "
                f"{DOWNLOAD_MAX_ATTEMPTS} attempts"
            )


# Trailer window scanned for the %%EOF marker. A conforming PDF ends with
# %%EOF (optionally followed by whitespace); incrementally-updated files
# carry several, and only the last one matters. 2 KB comfortably covers
# trailing newlines and a final cross-reference stream.
_PDF_EOF_WINDOW = 2048


def _validate_pdf(pdf_path: Path) -> int:
    """Validate that ``pdf_path`` is a complete, openable PDF and return
    its page count.

    Defense-in-depth against a truncated download slipping through: the
    resumable ``_download_pdf`` already checks the byte count against the
    server's advertised size, but that only fires when the server sends a
    size. The real hazard is a partial PDF that PyMuPDF can still *open*
    by rebuilding a broken xref, reporting far fewer pages than the real
    document — which would then silently OCR a fraction of the volume.
    Checking the header and the ``%%EOF`` trailer turns that silent
    corruption into a hard failure.

    Checks run cheapest-first so a bad file fails before the fitz open.

    :param pdf_path: Path to the downloaded PDF on disk.
    :returns: Page count.
    :rtype: int
    :raises ValueError: If the file is empty, lacks a ``%PDF-`` header or
        an ``%%EOF`` trailer, or cannot be opened as a PDF with at least
        one page.
    """
    import fitz

    size = pdf_path.stat().st_size if pdf_path.exists() else 0
    if size == 0:
        raise ValueError(f"downloaded PDF is empty: {pdf_path}")

    with pdf_path.open("rb") as f:
        header = f.read(1024)
        f.seek(max(0, size - _PDF_EOF_WINDOW))
        trailer = f.read()

    if b"%PDF-" not in header:
        raise ValueError(
            f"downloaded file is not a PDF (no %PDF- header): {pdf_path}"
        )
    if b"%%EOF" not in trailer:
        raise ValueError(
            "downloaded PDF is truncated (no %%EOF trailer in last "
            f"{_PDF_EOF_WINDOW} bytes): {pdf_path}"
        )

    try:
        with fitz.open(str(pdf_path)) as doc:
            page_count = doc.page_count
            # A rebuilt xref usually means damage; the %%EOF check above
            # catches the common (truncation) cause, so this is just a
            # breadcrumb rather than a hard failure.
            if getattr(doc, "is_repaired", False):
                logger.warning(
                    "PyMuPDF repaired %s on open; proceeding", pdf_path
                )
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError(
            f"downloaded file could not be opened as a PDF: {exc}"
        ) from exc

    if page_count < 1:
        raise ValueError(f"downloaded PDF has no pages: {pdf_path}")
    return page_count


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
) -> str | None:
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
    :returns: The model's text response (may be ``None`` if the server
        returns an empty choice).
    :rtype: str | None
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
    return response.choices[0].message.content


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
        Optional: ``prompt_mode`` (default ``prompt_layout_all_en``),
        ``dpi`` (default 200), ``num_threads``, ``temperature``,
        ``top_p``, ``max_completion_tokens``, ``min_pixels``,
        ``max_pixels``, ``max_pages``, ``include_pictures`` (default
        False: strip base64 picture crops from the markdown).
    :param tmp_dir: Per-job scratch directory.
    :returns: ``{"pages": list[dict], "page_count": int,
        "failed_pages": list[int], "duration_ms": int}``. Each page
        dict carries ``page_no``, ``input_width``/``input_height``
        (model-input dims for interpreting bboxes), ``duration_ms``,
        and either ``cells`` (layout JSON) + ``md`` (markdown), or
        ``error``. ``filtered: true`` marks pages where the model
        output wasn't valid JSON and ``md`` holds the cleaned text
        fallback.
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

    pdf_url = inputs["pdf_url"]
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
    min_pixels = inputs.get("min_pixels")
    max_pixels = inputs.get("max_pixels")
    if min_pixels is not None and int(min_pixels) < MIN_PIXELS:
        raise ValueError(f"min_pixels must be >= {MIN_PIXELS}")
    if max_pixels is not None and int(max_pixels) > MAX_PIXELS:
        raise ValueError(f"max_pixels must be <= {MAX_PIXELS}")
    max_pages = int(inputs.get("max_pages", MAX_PAGES))
    include_pictures = bool(inputs.get("include_pictures", False))
    prompt = dict_promptmode_to_prompt[prompt_mode]

    pdf_path = tmp_dir / "input.pdf"
    _download_pdf(pdf_url, pdf_path)

    pages = _validate_pdf(pdf_path)
    if pages > min(max_pages, MAX_PAGES):
        raise ValueError(
            f"PDF has {pages} pages, exceeds max_pages="
            f"{min(max_pages, MAX_PAGES)}"
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
        origin_image = fitz_doc_to_image(_doc()[page_idx], target_dpi=dpi)
        if origin_image is None:
            return {"page_no": page_idx, "error": "page rendered empty"}
        image = fetch_image(
            origin_image, min_pixels=min_pixels, max_pixels=max_pixels
        )
        input_height, input_width = smart_resize(image.height, image.width)

        response = None
        for attempt in range(INFERENCE_ATTEMPTS):
            try:
                response = _vllm_inference(
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
            if response is not None:
                break
            logger.warning(
                "page %d: empty vLLM response (attempt %d/%d)",
                page_idx,
                attempt + 1,
                INFERENCE_ATTEMPTS,
            )
        if response is None:
            raise RuntimeError("no response from vLLM")

        result = {
            "page_no": page_idx,
            "input_width": input_width,
            "input_height": input_height,
        }
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
        return _with_worker_meta(
            {
                "error": "GPU unavailable on this worker",
                "error_code": "NO_GPU",
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
        return _with_worker_meta(
            {
                "error": "vLLM server not healthy on this worker",
                "error_code": "VLLM_UNHEALTHY",
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
