"""RunPod Serverless handler for LightOnOCR tiebreak transcription.

Runs ``lightonai/LightOnOCR-2-1B`` behind a local vLLM server (spawned
as a subprocess at worker boot) and dispatches on
``job["input"]["action"]``:

- ``read_crops``: fetch a PDF via presigned GET URL, render only the
  pages that carry disputed regions, cut each region's crop, and
  transcribe it.

Unlike a whole-page OCR worker this one re-reads the small regions the
extraction pipeline could not agree on. Enumeration (which regions are
disputed) and arbitration (whose reading wins) stay with the caller;
this worker does inference and nothing else.

**The job carries the PDF plus page/bbox coordinates, not crop
images.** One job therefore covers every disputed region in a volume:
the worker downloads the PDF once, renders each referenced page once,
and cuts every crop that page owns. Shipping N crop PNGs into the job
input instead would mean N times the payload for the same work.

Coordinates are interpreted in the pipeline's canonical render space
(1700x2200 by default), because that is the space the disputed bboxes
were computed in. The renderer here reproduces that space exactly --
same fit-to-width zoom, same forced resize, same black redaction
rectangles -- so a bbox means the same pixels here as it did upstream.

**One attempt per crop.** Whether a read is good enough is decided by
the caller's guards, not here; a rejected read comes back as another
entry carrying its own tighter ``decode`` settings, which this worker
applies verbatim. A budget-doubling retry loop makes a rambling read
longer, not better, so there isn't one.

Returns JSON inline in the RunPod HTTP response. Reads are short (a
region, not a page), so even a volume's worth stays far under RunPod's
response cap. Does not write to S3, does not need AWS credentials.
"""

from __future__ import annotations

import base64
import io
import logging
import os
import random
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
logger = logging.getLogger("lighton.runpod")

# Populated in ``_preload``. Surfaced in every handler response so the
# caller can tell whether inference actually hit a GPU.
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
MAX_CROPS = int(os.environ.get("HANDLER_MAX_CROPS", "20000"))
DOWNLOAD_TIMEOUT = int(os.environ.get("HANDLER_DOWNLOAD_TIMEOUT", "300"))
DOWNLOAD_CONNECT_TIMEOUT = int(
    os.environ.get("HANDLER_DOWNLOAD_CONNECT_TIMEOUT", "10")
)
DOWNLOAD_MAX_ATTEMPTS = int(
    os.environ.get("HANDLER_DOWNLOAD_MAX_ATTEMPTS", "5")
)
DOWNLOAD_BACKOFF_CAP = int(
    os.environ.get("HANDLER_DOWNLOAD_BACKOFF_CAP", "30")
)

# ── Canonical render space ──────────────────────────────────────────
# The extraction pipeline renders every page to these dimensions and
# every bbox in the system lives in that space. A caller working in a
# different space must say so per job; the defaults must stay in step
# with the pipeline's own render stage.
DEFAULT_RENDER_W = int(os.environ.get("HANDLER_RENDER_W", "1700"))
DEFAULT_RENDER_H = int(os.environ.get("HANDLER_RENDER_H", "2200"))

# A disputed region can be a single glyph -- a bullet, a bracket. At
# native size that crop is too small for the vision tower to accept:
# LightOn merges patches 2x2, so a crop rendering to fewer than two
# patches on a side crashes the image tower rather than returning a bad
# read. Anything smaller is scaled up, aspect preserved.
MIN_CROP_SIDE = int(os.environ.get("HANDLER_MIN_CROP_SIDE", "64"))

# ── Decode policy ───────────────────────────────────────────────────
# Small crops make this decoder repeat and degenerate, so every request
# goes out greedy with repetition brakes and a token ceiling scaled to
# the region's area. An entry's own ``decode`` object overrides both.
REPETITION_PENALTY = float(os.environ.get("HANDLER_REP_PENALTY", "1.15"))
NO_REPEAT_NGRAM = int(os.environ.get("HANDLER_NO_REPEAT_NGRAM", "12"))
BUDGET_MIN_TOKENS = 128
BUDGET_MAX_TOKENS = 1024
BUDGET_AREA_PER_TOKEN = 500

# Concurrent in-flight requests against the local vLLM server. The
# server continuously batches, so this bounds client-side memory
# (encoded crops held in flight), not GPU batch size.
DEFAULT_CONCURRENCY = int(os.environ.get("HANDLER_CONCURRENCY", "16"))
# How many encoded crops may be queued ahead of the workers. Bounds
# peak memory on a volume with thousands of disputed regions.
QUEUE_DEPTH_FACTOR = 4
# Attempts per crop when the call fails at the transport layer. This is
# NOT a quality retry -- see the module docstring.
INFERENCE_ATTEMPTS = int(os.environ.get("HANDLER_INFERENCE_ATTEMPTS", "2"))

# ── vLLM server tunables ────────────────────────────────────────────
# The model lives in the baked HF cache (HF_HOME=/opt/hf, offline).
LIGHTON_MODEL = os.environ.get("LIGHTON_MODEL", "lightonai/LightOnOCR-2-1B")
SERVED_MODEL_NAME = "model"
VLLM_HOST = "127.0.0.1"
VLLM_PORT = int(os.environ.get("VLLM_PORT", "8000"))
VLLM_GPU_MEMORY_UTILIZATION = os.environ.get(
    "VLLM_GPU_MEMORY_UTILIZATION", "0.9"
)
VLLM_STARTUP_TIMEOUT = int(os.environ.get("VLLM_STARTUP_TIMEOUT", "900"))
VLLM_EXTRA_ARGS = os.environ.get("VLLM_EXTRA_ARGS", "")

# Stream errors that mean "the connection died mid-transfer" -- safe to
# reconnect and resume from the last byte written.
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
    """Spawn ``vllm serve`` for LightOnOCR, inheriting our stdout/stderr.

    Flags follow the model card's serve command. All three are
    load-bearing for this workload: one image per prompt is all we ever
    send, the multimodal processor cache is dead weight because no two
    crops are alike, and prefix caching cannot hit either -- every
    request is a different image with no shared text prefix.

    :returns: The server process handle.
    :rtype: subprocess.Popen
    """
    cmd = [
        "vllm",
        "serve",
        LIGHTON_MODEL,
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
        "--limit-mm-per-prompt",
        '{"image": 1}',
        "--mm-processor-cache-gb",
        "0",
        "--no-enable-prefix-caching",
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
    ``handler()`` fail fast with NO_GPU so the caller can retry on a
    different worker.
    """
    global _GPU_AVAILABLE, _VLLM_READY, _VLLM_PROC

    _GPU_AVAILABLE = _gpu_available()
    if not _GPU_AVAILABLE:
        # Logged at warning, not error: this is expected and fully
        # self-healing, so it must not raise a Sentry event.
        logger.warning(
            "GPU not available; skipping vLLM startup. Jobs will "
            "return error_code=NO_GPU; work should be re-queued "
            "automatically by the caller."
        )
        return

    t0 = time.monotonic()
    try:
        _VLLM_PROC = _start_vllm()
        _VLLM_READY = _wait_for_vllm(_VLLM_PROC, VLLM_STARTUP_TIMEOUT)
    except Exception:
        # Never let a spawn failure kill the module import: the fitness
        # check reports the worker unfit instead, which is visible in
        # the endpoint's Workers tab.
        logger.exception("vLLM startup failed")
        _VLLM_READY = False
    if _VLLM_READY:
        logger.info(
            "vLLM ready in %.1fs (model=%s)",
            time.monotonic() - t0,
            LIGHTON_MODEL,
        )
    else:
        logger.error("vLLM failed to become ready; worker is unfit")


_preload()

# Freeze the cold-start cost once the process is ready.
# ``_WORKER_BOOT_MS`` is constant per worker; ``_WORKER_READY_AT``
# anchors per-job uptime so the caller can tell cold from warm calls.
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
            "GPU not available on this worker. Exiting to avoid "
            "CPU-only billing."
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


def _redact_urls(body: dict) -> dict:
    """Return a copy of a job input with presigned URLs masked.

    Presigned URLs grant time-limited access to an S3 object and must
    never land in persistent logs.

    :param body: The ``input`` dict about to be logged.
    :returns: A shallow copy safe to log.
    :rtype: dict
    """
    out = {k: v for k, v in body.items() if k != "crops"}
    if "pdf_url" in out:
        out["pdf_url"] = "***"
    out["crops"] = f"<{len(body.get('crops') or [])} entries>"
    return out


def _expected_total(resp: requests.Response) -> int | None:
    """Best-effort total file size, in bytes, from a download response.

    Reads ``Content-Range`` on a 206 and falls back to
    ``Content-Length`` on a 200.

    :param resp: The response to inspect.
    :returns: Total size, or None if the server didn't say.
    :rtype: int | None
    """
    if resp.status_code == 206:
        rng = resp.headers.get("Content-Range", "")
        if "/" in rng:
            tail = rng.rsplit("/", 1)[-1].strip()
            if tail.isdigit():
                return int(tail)
        return None
    length = resp.headers.get("Content-Length")
    if length and length.isdigit():
        return int(length)
    return None


def _download_pdf(url: str, dest: Path) -> None:
    """Download a PDF from a presigned GET URL, resuming across drops.

    Volume-sized scans take long enough that a single streamed GET
    routinely dies mid-transfer. Rather than restart the whole
    transfer on every blip, this retries with an HTTP ``Range``
    request and appends from the last byte written.

    Range-response handling:

    - **206 Partial Content** -- server honoured ``Range``; append.
    - **200 OK** (on a resume) -- server ignored ``Range`` and is
      resending the whole body; truncate and restart from byte 0.
      Appending here would write a second copy of the head and
      corrupt the PDF, so this guard is load-bearing.
    - **416 Range Not Satisfiable** -- our offset is at/past EOF, so
      we already hold the whole file; treat as complete.
    - **403 Forbidden** -- the presigned URL expired. Not retryable
      here: raise so RunPod marks the job FAILED and the caller
      re-submits with a fresh URL.

    Only accepts ``http://`` or ``https://``. Rejects everything else
    defensively: today only the daemon holds the RunPod API key, but a
    scheme check costs nothing and eliminates the obvious SSRF /
    local-file-read class of bugs if that boundary ever weakens.

    :param url: PDF URL (typically a presigned S3 GET URL).
    :param dest: Local filesystem target.
    :raises ValueError: If ``url`` is not http(s).
    :raises RuntimeError: If the URL expired, or the file is still
        short after ``DOWNLOAD_MAX_ATTEMPTS`` attempts.
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
                if r.status_code == 403:
                    raise RuntimeError(
                        "presigned URL rejected (403) -- expired or "
                        "invalid; re-submit with a fresh URL"
                    )
                # Only meaningful as an answer to a Range we actually
                # sent. A 416 to the rangeless first GET is the server
                # refusing the request, not telling us we already have
                # the file, and returning here would leave no file for
                # the caller to open.
                if offset and r.status_code == 416:
                    logger.info("range past EOF; download already complete")
                    return
                r.raise_for_status()
                if offset and r.status_code == 200:
                    # Server ignored Range and is resending everything.
                    # Resetting the offset is enough to start over: the
                    # open below is then in "wb", which truncates.
                    logger.warning(
                        "server ignored Range; restarting from byte 0"
                    )
                    offset = 0
                total = _expected_total(r) or total
                mode = "ab" if offset else "wb"
                with open(dest, mode) as fh:
                    for chunk in r.iter_content(chunk_size=1 << 20):
                        if chunk:
                            fh.write(chunk)
                            offset += len(chunk)
            if total is None or offset >= total:
                logger.info("downloaded %d bytes to %s", offset, dest.name)
                return
            logger.warning(
                "short download (%d/%d bytes); resuming", offset, total
            )
        except _TRANSIENT_DOWNLOAD_ERRORS as exc:
            offset = dest.stat().st_size if dest.exists() else 0
            if attempt == DOWNLOAD_MAX_ATTEMPTS - 1:
                raise RuntimeError(
                    f"download failed after {DOWNLOAD_MAX_ATTEMPTS} "
                    f"attempts at byte {offset}: {exc!r}"
                ) from exc
            backoff = min(2**attempt, DOWNLOAD_BACKOFF_CAP)
            backoff += random.uniform(0, backoff / 2)
            logger.warning(
                "download error at byte %d (attempt %d/%d): %r; "
                "retrying in %.1fs",
                offset,
                attempt + 1,
                DOWNLOAD_MAX_ATTEMPTS,
                exc,
                backoff,
            )
            time.sleep(backoff)

    raise RuntimeError(
        f"download incomplete after {DOWNLOAD_MAX_ATTEMPTS} attempts "
        f"({offset}/{total} bytes)"
    )


# ── Rendering and cropping ──────────────────────────────────────────
def _render_page(doc: Any, page_index: int, width: int, height: int) -> Any:
    """Render one PDF page into the canonical pixel space.

    Reproduces the extraction pipeline's render stage exactly: zoom to
    fit the target width, then force the result to the target size if
    the page's aspect ratio doesn't match. The force-resize is what
    makes a bbox mean the same pixels for every engine regardless of
    the source page's true dimensions -- so it must not be "improved"
    into an aspect-preserving fit.

    :param doc: An open ``fitz.Document``.
    :param page_index: Zero-based page index.
    :param width: Target width in pixels.
    :param height: Target height in pixels.
    :returns: An RGB ``PIL.Image``.
    :rtype: PIL.Image.Image
    """
    import fitz
    from PIL import Image

    page = doc[page_index]
    zoom = width / page.rect.width
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
    img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    if img.size != (width, height):
        img = img.resize((width, height))
    return img


def _apply_redactions(img: Any, rects: list) -> None:
    """Paint redaction rectangles black, in place.

    The canonical render is black-redacted before any engine sees it,
    and downstream stages rely on that: a region that is mostly black
    is treated as redaction territory rather than as text. Skipping
    this would let the model read text that is supposed to be gone.

    :param img: The page image to modify.
    :param rects: Bboxes as ``[x0, y0, x1, y1]`` in render space.
    """
    from PIL import ImageDraw

    if not rects:
        return
    draw = ImageDraw.Draw(img)
    for r in rects:
        x0, y0, x1, y1 = (int(v) for v in r)
        draw.rectangle([x0, y0, x1, y1], fill=(0, 0, 0))


def _crop_png(img: Any, bbox: list) -> bytes:
    """Cut one region out of a rendered page and encode it as PNG.

    The bbox is clamped to the image bounds, and a crop smaller than
    ``MIN_CROP_SIDE`` on either side is scaled up with the aspect ratio
    preserved -- see that constant for why.

    :param img: The rendered page image.
    :param bbox: ``[x0, y0, x1, y1]`` in render space.
    :returns: PNG bytes.
    :rtype: bytes
    """
    from PIL import Image

    x0, y0, x1, y1 = (int(v) for v in bbox)
    left = max(0, min(x0, img.width - 1))
    top = max(0, min(y0, img.height - 1))
    right = min(img.width, max(x1, left + 1))
    bottom = min(img.height, max(y1, top + 1))
    crop = img.crop((left, top, right, bottom))
    if min(crop.size) < MIN_CROP_SIDE:
        scale = MIN_CROP_SIDE / min(crop.size)
        crop = crop.resize(
            (round(crop.width * scale), round(crop.height * scale)),
            Image.Resampling.LANCZOS,
        )
    buf = io.BytesIO()
    crop.save(buf, format="PNG")
    return buf.getvalue()


def _token_budget(area: float) -> int:
    """Token ceiling for a crop, scaled to its area.

    :param area: Region area in square pixels, from the ORIGINAL bbox
        (not the min-side upscale, which adds no content).
    :returns: Max new tokens.
    :rtype: int
    """
    return max(
        BUDGET_MIN_TOKENS,
        min(BUDGET_MAX_TOKENS, int(area / BUDGET_AREA_PER_TOKEN)),
    )


# ── Inference ───────────────────────────────────────────────────────
def _transcribe(client: Any, png: bytes, entry: dict) -> str:
    """Transcribe one crop through the local vLLM server.

    The request carries the image and no text prompt -- this model
    reads what it is shown. A retry entry names its own ceiling and
    brakes; everything else gets the area-scaled default.

    :param client: A shared ``openai.OpenAI`` pointed at the local
        server (shared so the fan-out reuses one connection pool).
    :param png: The crop, PNG-encoded.
    :param entry: The crop entry (``area``, optional ``decode``).
    :returns: The transcript, stripped.
    :rtype: str
    """
    decode = dict(entry.get("decode") or {})
    max_tokens = int(decode.pop("max_new_tokens", 0)) or _token_budget(
        entry["area"]
    )
    extra = {
        "repetition_penalty": REPETITION_PENALTY,
        "no_repeat_ngram_size": NO_REPEAT_NGRAM,
        **decode,
    }
    uri = "data:image/png;base64," + base64.b64encode(png).decode()
    resp = client.chat.completions.create(
        model=SERVED_MODEL_NAME,
        max_tokens=max_tokens,
        temperature=0.0,
        extra_body=extra,
        messages=[
            {
                "role": "user",
                "content": [{"type": "image_url", "image_url": {"url": uri}}],
            }
        ],
    )
    return (resp.choices[0].message.content or "").strip()


def _validate_crops(raw: Any) -> list[dict]:
    """Normalise and validate the job's crop list.

    Fills in ``area`` from the bbox when the caller didn't supply it,
    so the token budget always has a basis.

    :param raw: The ``crops`` value from the job input.
    :returns: Validated entries, each with key/page_index/bbox/area.
    :rtype: list[dict]
    :raises ValueError: On a malformed or oversized crop list.
    """
    if not isinstance(raw, list) or not raw:
        raise ValueError("'crops' must be a non-empty list")
    if len(raw) > MAX_CROPS:
        raise ValueError(
            f"{len(raw)} crops exceeds HANDLER_MAX_CROPS ({MAX_CROPS})"
        )
    out: list[dict] = []
    seen: set[str] = set()
    for i, c in enumerate(raw):
        if not isinstance(c, dict):
            raise ValueError(f"crops[{i}] is not an object")
        key = c.get("key")
        if not isinstance(key, str) or not key:
            raise ValueError(f"crops[{i}] has no 'key'")
        if key in seen:
            raise ValueError(f"duplicate crop key: {key!r}")
        seen.add(key)
        try:
            page_index = int(c["page_index"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(
                f"crops[{i}] ({key}) has no valid 'page_index'"
            ) from exc
        if page_index < 0:
            raise ValueError(f"crops[{i}] ({key}) has negative page_index")
        bbox = c.get("bbox")
        if not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
            raise ValueError(
                f"crops[{i}] ({key}) 'bbox' must be [x0, y0, x1, y1]"
            )
        try:
            x0, y0, x1, y1 = (int(v) for v in bbox)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"crops[{i}] ({key}) has a non-numeric bbox"
            ) from exc
        entry = dict(c)
        entry["key"] = key
        entry["page_index"] = page_index
        entry["bbox"] = [x0, y0, x1, y1]
        # Area comes from the caller when it has it (it knows the
        # original bbox); otherwise derive it. Either way it is the
        # region's own area, never the upscaled crop's.
        if not entry.get("area"):
            entry["area"] = max(0, x1 - x0) * max(0, y1 - y0)
        out.append(entry)
    return out


def _validate_redactions(raw: Any) -> dict[int, list]:
    """Normalise the per-page redaction map.

    :param raw: ``{page_index: [[x0, y0, x1, y1], ...]}``. JSON object
        keys arrive as strings, so they are coerced to int here.
    :returns: The map keyed by int page index.
    :rtype: dict[int, list]
    :raises ValueError: If the shape is wrong.
    """
    if raw in (None, {}):
        return {}
    if not isinstance(raw, dict):
        raise ValueError("'redactions' must be an object keyed by page index")
    out: dict[int, list] = {}
    for k, rects in raw.items():
        try:
            page_index = int(k)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"redactions key {k!r} is not a page index"
            ) from exc
        if not isinstance(rects, list):
            raise ValueError(f"redactions[{k}] must be a list of bboxes")
        coerced = []
        for r in rects:
            if not isinstance(r, (list, tuple)) or len(r) != 4:
                raise ValueError(
                    f"redactions[{k}] holds a malformed bbox: {r!r}"
                )
            # Coerced here rather than in _apply_redactions, for the
            # same reason _validate_crops coerces its own bboxes: a
            # non-numeric value found mid-render has already cost a
            # download, and int(None) raises TypeError, which the
            # handler's BAD_INPUT branch does not catch. Both failures
            # go away if the value is rejected before either happens.
            try:
                coerced.append([int(v) for v in r])
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"redactions[{k}] holds a non-numeric bbox: {r!r}"
                ) from exc
        out[page_index] = coerced
    return out


# ── Actions ─────────────────────────────────────────────────────────
def _action_read_crops(job: dict, inputs: dict, tmp_dir: Path) -> dict:
    """Transcribe every disputed region named in the job.

    Downloads the PDF once, then walks the referenced pages in order.
    Each page is rendered once, all of its crops are cut and dispatched
    to the vLLM server, and the page image is released before the next
    one is rendered -- so peak memory is one page plus the crops in
    flight, not the whole volume.

    :param job: The RunPod job dict (unused beyond logging).
    :param inputs: ``job["input"]``. Requires ``pdf_url`` and
        ``crops``; optional ``redactions``, ``render_width``,
        ``render_height``, ``concurrency``.
    :param tmp_dir: Scratch directory, removed by the caller.
    :returns: ``reads`` (one entry per successful crop), ``failed``
        (one per crop that errored), and counters.
    :rtype: dict
    :raises ValueError: On malformed input.
    """
    t0 = time.monotonic()
    # Validate BEFORE importing or downloading anything: bad input is
    # terminal, and the caller should learn that in milliseconds rather
    # than after a multi-GB download.
    pdf_url = inputs.get("pdf_url")
    if not pdf_url:
        raise ValueError("'pdf_url' is required")
    crops = _validate_crops(inputs.get("crops"))
    redactions = _validate_redactions(inputs.get("redactions"))
    width = int(inputs.get("render_width") or DEFAULT_RENDER_W)
    height = int(inputs.get("render_height") or DEFAULT_RENDER_H)
    concurrency = int(inputs.get("concurrency") or DEFAULT_CONCURRENCY)
    if width <= 0 or height <= 0:
        raise ValueError("render_width and render_height must be positive")
    if concurrency < 1:
        raise ValueError("concurrency must be >= 1")

    import fitz
    from openai import OpenAI

    logger.info("read_crops input: %s", _redact_urls(inputs))

    pdf_path = tmp_dir / "input.pdf"
    _download_pdf(pdf_url, pdf_path)

    by_page: dict[int, list[dict]] = {}
    for c in crops:
        by_page.setdefault(c["page_index"], []).append(c)

    client = OpenAI(
        base_url=f"http://{VLLM_HOST}:{VLLM_PORT}/v1",
        api_key="EMPTY",
        timeout=600.0,
    )
    reads: list[dict] = []
    failed: list[dict] = []
    lock = threading.Lock()
    # Bound how far rendering may run ahead of inference, so a volume
    # with thousands of regions doesn't hold every encoded crop at once.
    slots = threading.Semaphore(concurrency * QUEUE_DEPTH_FACTOR)

    def _one(entry: dict, png: bytes) -> None:
        c0 = time.monotonic()
        try:
            for attempt in range(INFERENCE_ATTEMPTS):
                try:
                    text = _transcribe(client, png, entry)
                    break
                except Exception as exc:
                    if attempt == INFERENCE_ATTEMPTS - 1:
                        raise
                    logger.warning(
                        "crop %s: vLLM error (attempt %d/%d): %s",
                        entry["key"],
                        attempt + 1,
                        INFERENCE_ATTEMPTS,
                        exc,
                    )
            with lock:
                reads.append(
                    {
                        "key": entry["key"],
                        "text": text,
                        "duration_ms": int((time.monotonic() - c0) * 1000),
                    }
                )
        except Exception as exc:
            # One bad crop must not sink the job: the caller treats a
            # missing read as an honest no-vote.
            logger.warning("crop %s failed: %r", entry["key"], exc)
            with lock:
                failed.append({"key": entry["key"], "error": str(exc)})
        finally:
            slots.release()

    doc = fitz.open(str(pdf_path))
    n_pages = doc.page_count
    pages_rendered = 0
    try:
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            futures = []
            for page_index in sorted(by_page):
                entries = by_page[page_index]
                if page_index >= n_pages:
                    logger.warning(
                        "page_index %d beyond the PDF's %d pages; "
                        "failing its %d crop(s)",
                        page_index,
                        n_pages,
                        len(entries),
                    )
                    for e in entries:
                        failed.append(
                            {
                                "key": e["key"],
                                "error": (
                                    f"page_index {page_index} out of range "
                                    f"(pdf has {n_pages} pages)"
                                ),
                            }
                        )
                    continue
                img = _render_page(doc, page_index, width, height)
                _apply_redactions(img, redactions.get(page_index, []))
                pages_rendered += 1
                for entry in entries:
                    png = _crop_png(img, entry["bbox"])
                    slots.acquire()
                    futures.append(pool.submit(_one, entry, png))
                img.close()
            for _ in as_completed(futures):
                pass
    finally:
        doc.close()

    duration_ms = int((time.monotonic() - t0) * 1000)
    logger.info(
        "read_crops OK: %d read, %d failed, %d page(s) rendered in %d ms",
        len(reads),
        len(failed),
        pages_rendered,
        duration_ms,
    )
    if crops and not reads:
        # Every crop failed -- that is a worker or model problem, not a
        # data one. Raise so RunPod marks the job FAILED and the caller
        # re-submits, rather than silently returning nothing usable.
        raise RuntimeError(
            f"all {len(crops)} crops failed; first error: "
            f"{failed[0]['error'] if failed else 'unknown'}"
        )
    return {
        "reads": reads,
        "failed": failed,
        "crop_count": len(crops),
        "pages_rendered": pages_rendered,
        "duration_ms": duration_ms,
    }


_ACTIONS = {
    "read_crops": _action_read_crops,
}


# ── Entry point ─────────────────────────────────────────────────────
def handler(job: dict) -> dict:
    """RunPod Serverless entry point.

    :param job: RunPod job dict. ``job["input"]`` must carry an
        ``action`` ("read_crops") and action-specific args. An optional
        ``scan_pk`` is used to tag Sentry events.
    :returns: Action-specific result dict. Every successful return (and
        every structured error) also carries ``worker_boot_ms``,
        ``worker_uptime_ms`` and ``gpu_available``. On bad/unknown
        input an ``{"error": ..., "error_code": ...}`` dict is
        returned; the SDK moves ``error`` to the top level and RunPod
        marks the job ``FAILED``. The caller reads ``error_code`` from
        ``output`` to tell transient errors (re-queue) from terminal
        ones.
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
    # workers out of the pool, but handle it defensively in case a job
    # leaks through anyway.
    if not _GPU_AVAILABLE:
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

    # The engine can die after a healthy boot (CUDA OOM, driver
    # hiccup). A dead engine says nothing about the job itself, so
    # surface it as its own transient code rather than failing every
    # crop one by one.
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
                "error": (
                    f"unknown action: {action!r}. "
                    f"Expected one of {sorted(_ACTIONS)}"
                ),
                "error_code": "UNKNOWN_ACTION",
            }
        )

    tmp_dir = Path(tempfile.mkdtemp(prefix="runpod-"))
    try:
        result = fn(job, inputs, tmp_dir)
        return _with_worker_meta(result)
    except ValueError as exc:
        # Malformed input is the caller's bug, not a transient fault:
        # return it as terminal so the job is not re-queued forever.
        logger.warning("bad input for %s: %s", action, exc)
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
        raise
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


if __name__ == "__main__":
    runpod.serverless.start({"handler": handler})
