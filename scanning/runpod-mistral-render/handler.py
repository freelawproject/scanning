"""RunPod Serverless handler for the Mistral page render (CPU).

One action, ``render``: fetch one original shard through a presigned
GET URL, render every page as the ensemble's canonical image
(1700x2200 RGB, ``scanning/mistral_ocr.py`` ``_page_image`` line for
line), paint the perpendiculars out when asked, encode it as a PNG,
upload it to Mistral's files API under the name the daemon gave the
page, and answer the file ids in page order. The daemon builds the
batch manifest from those ids and creates the batch itself; this
worker never sees a batch.

Why a worker at all: the render is CPU work, a page at a time, and on
the heavy pod the shards of a volume go through one after another.
Here every shard of a wave is its own job, so a volume renders in the
time of one shard, on as many workers as the endpoint allows
(freelawproject/scanning#562 measured the pod; this is the step past
it). A CPU endpoint, not a GPU one: nothing here needs a GPU, and
``gpu_available`` is reported false on every answer.

Inside one job the pages are rendered by a pool of processes
(PyMuPDF holds the GIL, so threads are one core), each of which
uploads the page it rendered: the PNG never crosses a process
boundary, which was the knee of the in-pod pool. The pool is
oversubscribed over the vCPUs (``HANDLER_PROCESSES_PER_CPU``) so a
process waiting on an upload leaves its core to another.

The Mistral key is read from the environment (``MISTRAL_API_KEY``),
set on the endpoint template, never in the image and never in the job
input, which RunPod logs. No AWS credentials: the shard comes in
through a presigned URL, and nothing is written to S3.

A failure after some pages went up deletes every page this job
uploaded, best effort, before it answers, because the pages are
unredacted and nothing will name them: the daemon lists what is left
by the name prefix and deletes that too.

The answer is small (one id per page), so it is returned inline and
``result_url`` is not needed; the result envelope of ``runpod_common``
is kept for parity with the other workers.
"""

from __future__ import annotations

import io
import logging
import multiprocessing
import os
import random
import time
from concurrent.futures import (
    FIRST_EXCEPTION,
    ProcessPoolExecutor,
    ThreadPoolExecutor,
    wait,
)
from pathlib import Path
from typing import Any

import cv2
import perpendiculars
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
# full cold-start cost (module imports).
_CLOCK = WorkerClock()

# One render process is one core's worth of work: the perpendiculars
# whiteout runs OpenCV, whose default thread pool is one thread per
# host core in every process of the pool, and the pool itself is
# already sized to the cores. The BLAS pools are capped the same way
# through the environment in the Dockerfile. Set before the fork, so
# every child inherits it.
cv2.setNumThreads(1)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("mistral_render.runpod")

sentry_sdk = runpod_common.init_sentry(logger)

# ── The render ──────────────────────────────────────────────────────
#: The canonical page size of the ensemble: US Letter at 200 dpi, the
#: pixel space of every bbox the glue reads. ``scanning/mistral_ocr.py``
#: holds the same two numbers; a change there is a change here.
RENDER_W = 1700
RENDER_H = 2200

#: PNG compression level of the page. Lossless at every level; 1 is
#: about three percent bigger than Pillow's default 6 and a third of
#: its encode time (#562).
DEFAULT_PNG_LEVEL = int(os.environ.get("HANDLER_PNG_LEVEL", "1"))

#: Render processes per vCPU. A process waiting on its upload leaves
#: its core to another, so two per core keeps the cores busy; the
#: pages in memory are bounded by the process count.
PROCESSES_PER_CPU = float(os.environ.get("HANDLER_PROCESSES_PER_CPU", "2"))
#: The pool size when set, else :data:`PROCESSES_PER_CPU` times the
#: vCPUs. One means in process, no pool.
RENDER_PROCESSES = int(os.environ.get("HANDLER_RENDER_PROCESSES", "0"))
MAX_PROCESSES = int(os.environ.get("HANDLER_MAX_PROCESSES", "32"))
#: Pages one process task renders and uploads before it answers, so
#: the tail of a shard spreads over the pool.
CHUNK_PAGES = int(os.environ.get("HANDLER_CHUNK_PAGES", "4"))

#: Hard sanity guard on input size. Volumes arrive sharded (#164), so a
#: shard over this is a pipeline bug, not a big scan.
MAX_PAGES = int(os.environ.get("HANDLER_MAX_PAGES", "5000"))

# ── Mistral ─────────────────────────────────────────────────────────
MISTRAL_API_URL = os.environ.get(
    "MISTRAL_API_URL", "https://api.mistral.ai"
).rstrip("/")
#: The purpose Mistral's files API files a page image under
#: (``mistral_ocr.PAGE_FILE_PURPOSE``).
PAGE_FILE_PURPOSE = "ocr"
UPLOAD_TIMEOUT = int(os.environ.get("HANDLER_UPLOAD_TIMEOUT", "120"))
#: Tries of a throttled or transiently failed page upload, after the
#: first, with a doubling backoff from :data:`UPLOAD_BACKOFF_SECONDS`
#: plus jitter (``mistral_ocr.UPLOAD_RETRIES``).
UPLOAD_RETRIES = int(os.environ.get("HANDLER_UPLOAD_RETRIES", "5"))
UPLOAD_BACKOFF_SECONDS = float(
    os.environ.get("HANDLER_UPLOAD_BACKOFF_SECONDS", "2")
)
DELETE_THREADS = 8

#: Error codes this worker answers, read by ``mistral_ocr`` on the
#: daemon: ``RATE_LIMITED`` costs the row no attempt, the transient
#: ones spend one, the rest fail it.
RATE_LIMITED = "RATE_LIMITED"
NOT_CONFIGURED = "NOT_CONFIGURED"
MISTRAL_UNREACHABLE = "MISTRAL_UNREACHABLE"
RENDER_FAILED = "RENDER_FAILED"


class MistralUploadError(RuntimeError):
    """One page could not be uploaded, after its tries.

    :ivar error_code: The code the daemon classifies.
    """

    def __init__(self, message: str, error_code: str):
        super().__init__(message)
        self.error_code = error_code


def _api_key() -> str:
    """Return the Mistral key of this endpoint, or raise.

    :returns: The key.
    :rtype: str
    :raises MistralUploadError: ``NOT_CONFIGURED`` when the template
        carries no key. A transient code on the daemon, because the
        shard is fine and the endpoint is what wants fixing.
    """
    key = os.environ.get("MISTRAL_API_KEY", "").strip()
    if not key:
        raise MistralUploadError(
            "MISTRAL_API_KEY is not set on this endpoint", NOT_CONFIGURED
        )
    return key


def _page_image(page):
    """Render one page as the ensemble's canonical image.

    ``scanning/mistral_ocr.py`` ``_page_image`` line for line: zoom the
    page to ``RENDER_W`` wide, render RGB with no alpha, resize to
    exactly ``RENDER_W`` x ``RENDER_H`` when the page is another size.

    :param page: A ``fitz.Page``.
    :returns: The page, as a PIL image.
    """
    import fitz
    from PIL import Image

    zoom = RENDER_W / page.rect.width
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
    img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    if img.size != (RENDER_W, RENDER_H):
        img = img.resize((RENDER_W, RENDER_H))
    return img


def _render_dpi(page) -> int:
    """Return the resolution the canonical render has: ``RENDER_W``
    over the page's width in inches, 200 for US Letter."""
    width_in = page.rect.width / 72 if page.rect.width else 8.5
    return int(round(RENDER_W / width_in))


def _png(img, level: int) -> bytes:
    """Encode one image as a PNG at ``level``."""
    buffer = io.BytesIO()
    img.save(buffer, format="PNG", compress_level=level)
    return buffer.getvalue()


def render_page(page, *, whiteout: bool, level: int) -> bytes:
    """Render one page as the canonical PNG, the perpendiculars painted
    out when asked.

    :param page: A ``fitz.Page``.
    :param whiteout: Whether to paint the perpendiculars out.
    :param level: The PNG compression level.
    :returns: PNG bytes.
    :rtype: bytes
    """
    image = _page_image(page)
    if whiteout:
        image = perpendiculars.paint(image, _render_dpi(page))
    return _png(image, level)


# ── The upload ──────────────────────────────────────────────────────
def _classify(response: requests.Response | None, exc: Exception | None):
    """Return ``(error_code, retry)`` for one failed upload try.

    :param response: The answer, when there was one.
    :param exc: The transport failure, when there was no answer.
    :returns: The code to answer if the tries run out, and whether
        another try is worth it.
    :rtype: tuple[str, bool]
    """
    if response is None:
        return MISTRAL_UNREACHABLE, True
    status = response.status_code
    if status == 429:
        return RATE_LIMITED, True
    if status >= 500:
        return f"MISTRAL_HTTP_{status}", True
    return f"MISTRAL_HTTP_{status}", False


def upload_png(api_key: str, name: str, png: bytes) -> str:
    """Upload one page to Mistral's files API and return its id.

    A throttled try (429), a 5xx and a transport failure are tried
    again up to :data:`UPLOAD_RETRIES` times, after
    :data:`UPLOAD_BACKOFF_SECONDS` doubled at each try plus a little
    jitter, so the processes of a pool throttled at once do not knock
    again as one. Any other 4xx is refused at once.

    :param api_key: The Mistral key.
    :param name: The file name Mistral shows, the daemon's prefix plus
        the page.
    :param png: The bytes.
    :returns: The file id.
    :rtype: str
    :raises MistralUploadError: With the code of the last try.
    """
    tried = 0
    while True:
        response = None
        exc: Exception | None = None
        try:
            response = requests.post(
                f"{MISTRAL_API_URL}/v1/files",
                headers={"Authorization": f"Bearer {api_key}"},
                data={"purpose": PAGE_FILE_PURPOSE},
                files={"file": (name, png, "image/png")},
                timeout=UPLOAD_TIMEOUT,
            )
        except requests.RequestException as caught:
            exc = caught
        if response is not None and 200 <= response.status_code < 300:
            try:
                file_id = str((response.json() or {}).get("id") or "")
            except ValueError:
                file_id = ""
            if file_id:
                return file_id
            raise MistralUploadError(
                f"{name}: Mistral answered no file id: "
                f"{response.text[:200]!r}",
                "MISTRAL_BAD_RESPONSE",
            )
        code, retry = _classify(response, exc)
        detail = (
            f"HTTP {response.status_code} {response.text[:200]!r}"
            if response is not None
            else str(exc)[:200]
        )
        if not retry or tried >= UPLOAD_RETRIES:
            raise MistralUploadError(f"{name}: {detail}", code)
        delay = UPLOAD_BACKOFF_SECONDS * 2**tried + random.uniform(
            0, UPLOAD_BACKOFF_SECONDS
        )
        logger.info("%s: %s; trying again in %.1f s", name, detail, delay)
        time.sleep(delay)
        tried += 1


def delete_file(api_key: str, file_id: str) -> bool:
    """Delete one file at Mistral, once, best effort.

    :param api_key: The Mistral key.
    :param file_id: The file.
    :returns: Whether the delete landed (a file already gone counts).
    :rtype: bool
    """
    try:
        response = requests.delete(
            f"{MISTRAL_API_URL}/v1/files/{file_id}",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=UPLOAD_TIMEOUT,
        )
    except requests.RequestException as exc:
        logger.warning("delete of %s failed: %s", file_id, exc)
        return False
    if 200 <= response.status_code < 300 or response.status_code == 404:
        return True
    logger.warning(
        "delete of %s answered HTTP %s", file_id, response.status_code
    )
    return False


def _delete_files(api_key: str, file_ids: list[str]) -> list[str]:
    """Delete some files side by side and return the ids left.

    :param api_key: The Mistral key.
    :param file_ids: The files.
    :returns: The ids no delete reached.
    :rtype: list[str]
    """
    if not file_ids:
        return []
    with ThreadPoolExecutor(
        max_workers=min(DELETE_THREADS, len(file_ids))
    ) as pool:
        landed = list(pool.map(lambda f: delete_file(api_key, f), file_ids))
    return [f for f, ok in zip(file_ids, landed, strict=True) if not ok]


# ── The pool ────────────────────────────────────────────────────────
def page_name(prefix: str, page_no: int) -> str:
    """Return the file name of one page, as ``mistral_ocr`` names it:
    ``{prefix}p{page_no}.png``."""
    return f"{prefix}p{page_no}.png"


def render_chunk(
    pdf_path: str,
    page_nos: list[int],
    prefix: str,
    whiteout: bool,
    level: int,
) -> dict:
    """Render and upload some pages of a shard. Runs in a pool process.

    Opens its own document: a PyMuPDF document is not shared across
    processes. Never raises: the pages that went up are answered
    beside the failure, so the parent can delete them.

    :param pdf_path: The downloaded shard.
    :param page_nos: The pages, counted from zero inside the shard.
    :param prefix: The daemon's file prefix.
    :param whiteout: Whether to paint the perpendiculars out.
    :param level: The PNG compression level.
    :returns: ``{"pages": [(page_no, file_id, bytes), ...], "error":
        None | {"error_code", "message", "page_no"}, "render_ms",
        "upload_ms"}``.
    :rtype: dict
    """
    import fitz

    done: list[tuple[int, str, int]] = []
    render_ms = 0
    upload_ms = 0
    error = None
    try:
        api_key = _api_key()
        with fitz.open(pdf_path) as doc:
            for page_no in page_nos:
                started = time.monotonic()
                try:
                    png = render_page(
                        doc[page_no], whiteout=whiteout, level=level
                    )
                except Exception as exc:  # noqa: BLE001 - answered as a code
                    raise MistralUploadError(
                        f"page {page_no} would not render: {exc}",
                        RENDER_FAILED,
                    ) from exc
                rendered = time.monotonic()
                file_id = upload_png(api_key, page_name(prefix, page_no), png)
                uploaded = time.monotonic()
                render_ms += int((rendered - started) * 1000)
                upload_ms += int((uploaded - rendered) * 1000)
                done.append((page_no, file_id, len(png)))
    except MistralUploadError as exc:
        error = {
            "error_code": exc.error_code,
            "message": str(exc)[:500],
            "page_no": page_nos[len(done)]
            if len(done) < len(page_nos)
            else None,
        }
    except Exception as exc:  # noqa: BLE001 - answered, never raised
        logger.exception("render chunk failed")
        error = {
            "error_code": RENDER_FAILED,
            "message": f"{type(exc).__name__}: {str(exc)[:400]}",
            "page_no": page_nos[len(done)]
            if len(done) < len(page_nos)
            else None,
        }
    return {
        "pages": done,
        "error": error,
        "render_ms": render_ms,
        "upload_ms": upload_ms,
    }


def _cpus() -> int:
    """Return the CPUs this process may run on: the affinity mask where
    the platform has one (a container pinned to a cpuset), else the
    machine's count. ``os.cpu_count`` alone reads the host's cores
    from inside a container, and a pool sized by it on a four-vCPU
    worker ran twenty-five processes on four cores."""
    getter = getattr(os, "sched_getaffinity", None)
    if getter is not None:
        try:
            return len(getter(0)) or 1
        except OSError:
            pass
    return os.cpu_count() or 1


def pool_size(page_count: int) -> int:
    """Return how many processes to render ``page_count`` pages with.

    :param page_count: The shard's pages.
    :returns: :data:`RENDER_PROCESSES` when set, else
        :data:`PROCESSES_PER_CPU` times the vCPUs, capped by
        :data:`MAX_PROCESSES` and by the chunks there are to run.
    :rtype: int
    """
    if RENDER_PROCESSES > 0:
        size = RENDER_PROCESSES
    else:
        size = max(1, int(round(_cpus() * PROCESSES_PER_CPU)))
    chunks = max(1, -(-page_count // max(1, CHUNK_PAGES)))
    return max(1, min(size, MAX_PROCESSES, chunks))


def _chunks(page_count: int) -> list[list[int]]:
    """Cut the pages of a shard into runs of :data:`CHUNK_PAGES`."""
    size = max(1, CHUNK_PAGES)
    return [
        list(range(start, min(start + size, page_count)))
        for start in range(0, page_count, size)
    ]


def _run_chunks(
    pdf_path: Path,
    page_count: int,
    prefix: str,
    whiteout: bool,
    level: int,
    processes: int,
) -> list[dict]:
    """Run every chunk and return their answers.

    In process when the pool is one wide; else over a pool of forked
    processes. A chunk that answers a failure cancels the chunks not
    started; the ones running finish, so every id that will exist is
    known when the files go.

    :returns: One answer per chunk run (see :func:`render_chunk`).
    :rtype: list[dict]
    """
    chunks = _chunks(page_count)
    args = (str(pdf_path), prefix, whiteout, level)
    if processes <= 1:
        answers = []
        for pages in chunks:
            answer = render_chunk(args[0], pages, *args[1:])
            answers.append(answer)
            if answer["error"]:
                break
        return answers
    context = multiprocessing.get_context("fork")
    with ProcessPoolExecutor(
        max_workers=processes, mp_context=context
    ) as pool:
        futures = [
            pool.submit(render_chunk, args[0], pages, *args[1:])
            for pages in chunks
        ]
        pending = set(futures)
        answers = []
        while pending:
            finished, pending = wait(pending, return_when=FIRST_EXCEPTION)
            for future in finished:
                answer = future.result()
                answers.append(answer)
                if answer["error"]:
                    for other in list(pending):
                        if other.cancel():
                            pending.discard(other)
        return answers


# ── The action ──────────────────────────────────────────────────────
def _action_render(job: dict, inputs: dict, tmp_dir: Path) -> dict:
    """Render every page of a shard and upload it to Mistral.

    :param job: RunPod job dict.
    :param inputs: Handler input payload. Required: ``pdf_url`` and
        ``file_prefix``. Optional: ``page_count`` (refused when the
        PDF has another count, because the daemon's manifest would
        name pages that do not exist), ``whiteout_perpendiculars``
        (default false), ``png_level`` (0 to 9, default
        :data:`DEFAULT_PNG_LEVEL`).
    :param tmp_dir: Per-job scratch directory.
    :returns: ``{"file_ids": [...], "page_count", "bytes",
        "render_ms", "upload_ms", "duration_ms", "processes",
        "png_level", "whiteout_perpendiculars"}`` with the ids in page
        order; or, when a page failed, ``{"error", "error_code",
        "page_count", "uploaded", "deleted", "files_left"}``, every
        page this job uploaded deleted first, best effort.
    :rtype: dict
    """
    started = time.monotonic()
    pdf_url = inputs.get("pdf_url")
    if not pdf_url:
        raise BadInputError("missing required input: pdf_url")
    prefix = inputs.get("file_prefix")
    if not isinstance(prefix, str) or not prefix:
        raise BadInputError("missing required input: file_prefix")
    level = coerce_input(
        "png_level", inputs.get("png_level", DEFAULT_PNG_LEVEL), int
    )
    if level < 0 or level > 9:
        raise BadInputError(f"png_level must be between 0 and 9, got {level}")
    whiteout = bool(inputs.get("whiteout_perpendiculars", False))
    expected = inputs.get("page_count")
    if expected is not None:
        expected = coerce_input("page_count", expected, int)

    pdf_path = tmp_dir / "input.pdf"
    download_started = time.monotonic()
    download_pdf(pdf_url, pdf_path)
    download_ms = int((time.monotonic() - download_started) * 1000)
    shard_bytes = pdf_path.stat().st_size
    page_count = validate_pdf(pdf_path)
    logger.info(
        "downloaded %d byte(s) of %s in %d ms",
        shard_bytes,
        prefix,
        download_ms,
    )
    if page_count > MAX_PAGES:
        raise BadInputError(
            f"PDF has {page_count} pages, exceeds MAX_PAGES={MAX_PAGES}"
        )
    if expected is not None and expected != page_count:
        raise BadInputError(
            f"PDF has {page_count} pages, the caller expected {expected}"
        )

    processes = pool_size(page_count)
    logger.info(
        "rendering %d page(s) of %s with %d process(es), png level %d, "
        "whiteout=%s",
        page_count,
        prefix,
        processes,
        level,
        whiteout,
    )
    answers = _run_chunks(
        pdf_path, page_count, prefix, whiteout, level, processes
    )
    uploaded = sorted(
        (page for answer in answers for page in answer["pages"]),
        key=lambda page: page[0],
    )
    failures = [answer["error"] for answer in answers if answer["error"]]
    if failures:
        # The first by page, so the log names the page that broke the
        # shard and not the one that failed because of it.
        failure = min(
            failures,
            key=lambda f: (f["page_no"] is None, f["page_no"] or 0),
        )
        ids = [file_id for _, file_id, _ in uploaded]
        left = _delete_files(_api_key_quietly(), ids) if ids else []
        logger.warning(
            "%s: page %s failed (%s): %s; deleted %d of %d uploaded page(s)",
            prefix,
            failure["page_no"],
            failure["error_code"],
            failure["message"],
            len(ids) - len(left),
            len(ids),
        )
        return {
            "error": failure["message"],
            "error_code": failure["error_code"],
            "page_count": page_count,
            "uploaded": len(ids),
            "deleted": len(ids) - len(left),
            "files_left": left,
        }
    if [page_no for page_no, _, _ in uploaded] != list(range(page_count)):
        # A pool that answered no failure and not every page is a bug
        # here, never the shard's. Delete and say so.
        ids = [file_id for _, file_id, _ in uploaded]
        left = _delete_files(_api_key_quietly(), ids) if ids else []
        return {
            "error": (
                f"{len(uploaded)} of {page_count} page(s) answered with "
                "no failure reported"
            ),
            "error_code": RENDER_FAILED,
            "page_count": page_count,
            "uploaded": len(ids),
            "deleted": len(ids) - len(left),
            "files_left": left,
        }
    return {
        "file_ids": [file_id for _, file_id, _ in uploaded],
        "page_count": page_count,
        "bytes": sum(size for _, _, size in uploaded),
        "shard_bytes": shard_bytes,
        "download_ms": download_ms,
        # Summed over the processes: process time, not wall.
        "render_ms": sum(answer["render_ms"] for answer in answers),
        "upload_ms": sum(answer["upload_ms"] for answer in answers),
        "duration_ms": int((time.monotonic() - started) * 1000),
        "processes": processes,
        "png_level": level,
        "whiteout_perpendiculars": whiteout,
    }


def _api_key_quietly() -> str:
    """Return the key for a delete, or an empty string: a delete after
    a ``NOT_CONFIGURED`` failure has nothing to delete with."""
    try:
        return _api_key()
    except MistralUploadError:
        return ""


# ── Scaffold ────────────────────────────────────────────────────────
_SUMMARY_FIELDS = (
    "file_ids",
    "page_count",
    "bytes",
    "shard_bytes",
    "download_ms",
    "render_ms",
    "upload_ms",
    "duration_ms",
    "processes",
    "png_level",
    "whiteout_perpendiculars",
)


def _with_worker_meta(payload: dict) -> dict:
    """See :func:`runpod_common.with_worker_meta`."""
    return runpod_common.with_worker_meta(
        payload, clock=_CLOCK, gpu_available=False
    )


def _tag_sentry(job: dict, action: str, scan_pk: Any) -> None:
    """See :func:`runpod_common.tag_sentry`."""
    runpod_common.tag_sentry(
        sentry_sdk,
        job,
        action,
        scan_pk,
        clock=_CLOCK,
        gpu_available=False,
        handler_logger=logger,
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
    "render": _action_render,
}


def handler(job: dict) -> dict:
    """RunPod Serverless entry point.

    :param job: RunPod job dict. ``job["input"]`` must carry an
        ``action`` ("render") and action-specific args. An optional
        ``scan_pk`` is used to tag Sentry events.
    :returns: The action's result dict, with the worker meta of
        ``runpod_common.with_worker_meta``. On bad or unknown input an
        ``{"error": ..., "error_code": ...}`` dict is returned; the SDK
        moves ``error`` to the top level and RunPod marks the job
        ``FAILED``. The daemon reads ``error_code`` from ``output``.
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

    fn = _ACTIONS.get(action)
    if fn is None:
        return _with_worker_meta(
            {
                "error": f"unknown action: {action!r}. Expected one of {sorted(_ACTIONS)}",
                "error_code": "UNKNOWN_ACTION",
            }
        )

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


_CLOCK.mark_ready()

if __name__ == "__main__":
    runpod.serverless.start({"handler": handler})
