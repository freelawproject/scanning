"""Transfer/validation code shared by the RunPod GPU-worker images.

Each worker under ``scanning/runpod*/`` is a separate deploy artifact:
its Dockerfile copies ``handler.py`` plus this module into the image,
and the handler imports it as a top-level module (``import
runpod_common``). Nothing here may depend on Django, the scanning app,
or anything not installed in every worker venv — only ``requests``,
PyMuPDF (imported lazily), and the stdlib.

Extracted from the previously byte-for-byte duplicated copies in the
worker handlers so a fix to the shared logic lands in every worker at
once. Two workers build on it: ``scanning/runpod-dotsmocr/`` (#190) and
the bl_warm detection worker under ``scanning/runpod/`` (#194). The
next engine image (#147) starts from this module too. It carries the
transfer and validation code, and also the worker scaffold: the Sentry
setup, the boot clock, the worker-meta fields, the result envelope, and
the action runner whose error codes the daemon (``runpod_client.py``)
classifies. One daemon reads the output of every worker, so these
blocks are one contract and must not fork per image. Tested in
``scanning/tests/test_runpod_common.py`` and through both handler test
modules.
"""

from __future__ import annotations

import json
import logging
import os
import random
import shutil
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import requests
from urllib3.exceptions import IncompleteRead, ProtocolError

logger = logging.getLogger("runpod_common")


class BadInputError(ValueError):
    """A job input the caller got wrong. Terminal.

    The action runner answers this as ``error_code=BAD_INPUT``, which
    the daemon treats as terminal. A distinct type, and not a bare
    ``ValueError``, because the worker stack raises ``ValueError`` at
    run time too (a degenerate page render, a shape check inside the
    model libraries). An ``except ValueError`` arm around the whole
    action would answer those as BAD_INPUT: the daemon would write the
    shard off as a caller error, and no Sentry event would say why.
    Subclasses ``ValueError`` so an old caller of the input checks
    keeps working.
    """


def coerce_input(name: str, value: Any, cast: Callable) -> Any:
    """Cast one job input, and refuse a bad value as a caller error.

    ``float(None)`` and ``float([])`` raise ``TypeError``, not
    ``ValueError``, and JSON ``null`` is a natural way for a caller to
    say "use the default". Both must come back as ``BAD_INPUT``: an
    uncaught ``TypeError`` reaches the caller as a raw traceback with
    no ``error_code``, so the daemon retries a terminally-bad input at
    full GPU price.

    :param name: The input's name, for the error message.
    :param value: The raw value from the job input.
    :param cast: ``int`` or ``float``.
    :returns: The cast value.
    :raises BadInputError: If the value does not cast.
    """
    try:
        return cast(value)
    except (TypeError, ValueError) as exc:
        raise BadInputError(
            f"{name!r} must be a number, got {value!r}"
        ) from exc


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
# reconnect (resuming from the last byte written on a download, or
# re-PUTting the whole object on an upload). Anything else (a 4xx other
# than the handled 403/416, a 5xx, a malformed URL) is not in here and
# propagates so the daemon can re-queue or fail the scan.
TRANSIENT_TRANSFER_ERRORS = (
    requests.exceptions.ChunkedEncodingError,
    requests.exceptions.ConnectionError,
    requests.exceptions.Timeout,
    ProtocolError,
    IncompleteRead,
)


def expected_total(resp: requests.Response) -> int | None:
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


def download_pdf(url: str, dest: Path) -> None:
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
    :raises BadInputError: If ``url`` is not http(s). A caller error,
        so the handlers answer it as a terminal ``BAD_INPUT``.
    :raises requests.HTTPError: On a non-2xx response other than the
        handled 403/416 cases.
    :raises RuntimeError: If the presigned URL is expired (403), or if
        the file on disk is still short of the expected size after
        ``DOWNLOAD_MAX_ATTEMPTS`` attempts.
    """
    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
        raise BadInputError(
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
                    total = expected_total(r)

                mode = "ab" if offset else "wb"
                with dest.open(mode) as f:
                    for chunk in r.iter_content(chunk_size=1024 * 1024):
                        if chunk:
                            f.write(chunk)
                            offset += len(chunk)
            # Stream drained without raising: this attempt finished.
            break
        except TRANSIENT_TRANSFER_ERRORS as exc:
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
PDF_EOF_WINDOW = 2048


class CorruptDownloadError(RuntimeError):
    """The downloaded PDF is not a sound, openable document.

    Distinct from ``ValueError`` so a caller can tell it from a bad
    *input* (a missing url, an out-of-range option). The bytes at the
    source are sound -- scanning cuts each shard itself and verifies it
    against the original -- so a copy that will not open is a transfer
    fault, and the next attempt is quite likely to get it right. A
    handler that reported this as a terminal input error would write a
    volume off for a dropped connection.
    """


def validate_pdf(pdf_path: Path) -> int:
    """Validate that ``pdf_path`` is a complete, openable PDF and return
    its page count.

    Defense-in-depth against a truncated download slipping through: the
    resumable :func:`download_pdf` already checks the byte count against
    the server's advertised size, but that only fires when the server
    sends a size. The real hazard is a partial PDF that PyMuPDF can
    still *open* by rebuilding a broken xref, reporting far fewer pages
    than the real document — which would then silently process a
    fraction of the volume. Checking the header and the ``%%EOF``
    trailer turns that silent corruption into a hard failure.

    Checks run cheapest-first so a bad file fails before the fitz open.

    :param pdf_path: Path to the downloaded PDF on disk.
    :returns: Page count.
    :rtype: int
    :raises CorruptDownloadError: If the file is empty, lacks a
        ``%PDF-`` header or an ``%%EOF`` trailer, or cannot be opened as
        a PDF with at least one page. Every one of those describes the
        copy we received rather than the object we asked for, so all of
        them are worth another attempt.
    """
    import fitz

    size = pdf_path.stat().st_size if pdf_path.exists() else 0
    if size == 0:
        raise CorruptDownloadError(f"downloaded PDF is empty: {pdf_path}")

    with pdf_path.open("rb") as f:
        header = f.read(1024)
        f.seek(max(0, size - PDF_EOF_WINDOW))
        trailer = f.read()

    if b"%PDF-" not in header:
        raise CorruptDownloadError(
            f"downloaded file is not a PDF (no %PDF- header): {pdf_path}"
        )
    if b"%%EOF" not in trailer:
        raise CorruptDownloadError(
            "downloaded PDF is truncated (no %%EOF trailer in last "
            f"{PDF_EOF_WINDOW} bytes): {pdf_path}"
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
    except CorruptDownloadError:
        raise
    except Exception as exc:
        raise CorruptDownloadError(
            f"downloaded file could not be opened as a PDF: {exc}"
        ) from exc

    if page_count < 1:
        raise CorruptDownloadError(f"downloaded PDF has no pages: {pdf_path}")
    return page_count


# ── Result delivery ─────────────────────────────────────────────────
# How many times to (re)issue the PUT before giving up. Unlike a
# download there is nothing to resume: S3 makes a single PUT atomic, so
# a dropped connection means the object was never created and the whole
# body goes again.
UPLOAD_MAX_ATTEMPTS = int(os.environ.get("HANDLER_UPLOAD_MAX_ATTEMPTS", "3"))
UPLOAD_TIMEOUT = int(os.environ.get("HANDLER_UPLOAD_TIMEOUT", "300"))
UPLOAD_CONNECT_TIMEOUT = int(
    os.environ.get("HANDLER_UPLOAD_CONNECT_TIMEOUT", "10")
)


class ResultUploadError(RuntimeError):
    """A result could not be delivered to its presigned PUT.

    Carries the ``error_code`` the caller reports, because the three
    failure causes need different handling and only the worker can tell
    them apart.

    :ivar error_code: ``RESULT_UPLOAD_FAILED`` (transport, retry),
        ``RESULT_URL_EXPIRED`` (the signature died, so a fresh job must
        mint a new one), or ``RESULT_UPLOAD_REJECTED`` (S3 refused the
        request itself -- terminal).
    """

    def __init__(self, message: str, error_code: str):
        super().__init__(message)
        self.error_code = error_code


def upload_result(url: str, payload: dict, content_type: str) -> int:
    """PUT a JSON result to a presigned URL and return its size.

    Why the result travels this way rather than inline in the job
    response: RunPod caps a response at about 20 MB and discards it with
    the job record roughly 30 minutes after it finishes. An S3 object has
    neither limit, so a caller whose daemon was down for an hour can
    still recover work it already paid for.

    ``Content-Type`` is sent because it was **signed** into the URL.
    Whether S3 folds the header into the signature depends on the
    signature version, so signing it makes both sides agree either way
    -- and it means this header and the caller's signing parameter must
    match exactly. A mismatch is a 403 that reads like an expired
    signature.

    :param url: Presigned PUT URL for exactly one object.
    :param payload: The envelope to serialize and send.
    :param content_type: Must equal what the URL was signed with.
    :returns: Bytes uploaded.
    :rtype: int
    :raises ResultUploadError: With an ``error_code`` the caller
        classifies. See :class:`ResultUploadError`.
    """
    body = json.dumps(payload).encode("utf-8")
    last_exc: Exception | None = None

    for attempt in range(1, UPLOAD_MAX_ATTEMPTS + 1):
        try:
            resp = requests.put(
                url,
                data=body,
                headers={"Content-Type": content_type},
                timeout=(UPLOAD_CONNECT_TIMEOUT, UPLOAD_TIMEOUT),
            )
        except TRANSIENT_TRANSFER_ERRORS as exc:
            last_exc = exc
            logger.warning(
                "result upload attempt %d/%d failed: %s",
                attempt,
                UPLOAD_MAX_ATTEMPTS,
                exc,
            )
            if attempt < UPLOAD_MAX_ATTEMPTS:
                time.sleep(
                    min(
                        2 ** (attempt - 1) + random.random(),
                        DOWNLOAD_BACKOFF_CAP,
                    )
                )
            continue

        if resp.status_code in (200, 201, 204):
            logger.info(
                "uploaded %d byte result on attempt %d", len(body), attempt
            )
            return len(body)

        detail = (resp.text or "")[:300]
        # 403 is the one S3 status worth its own code. It is what an
        # expired signature answers, and a fresh job mints a fresh URL,
        # so the caller retries. It is *also* what a signing mismatch
        # answers, which no retry fixes -- but the two are
        # indistinguishable from here, and treating both as retryable
        # costs a bounded number of attempts where treating both as
        # terminal would lose a volume to a slow queue.
        if resp.status_code == 403:
            raise ResultUploadError(
                f"presigned PUT rejected with 403 (expired signature, or a "
                f"Content-Type mismatch): {detail}",
                error_code="RESULT_URL_EXPIRED",
            )
        # Anything else 4xx is S3 refusing the request as formed: a
        # bucket policy demanding headers we do not send, a wrong
        # region, a malformed key. Every retry re-uploads and fails
        # identically, so this is terminal.
        if 400 <= resp.status_code < 500:
            raise ResultUploadError(
                f"presigned PUT rejected with {resp.status_code}: {detail}",
                error_code="RESULT_UPLOAD_REJECTED",
            )

        last_exc = RuntimeError(f"HTTP {resp.status_code}: {detail}")
        logger.warning(
            "result upload attempt %d/%d got HTTP %d",
            attempt,
            UPLOAD_MAX_ATTEMPTS,
            resp.status_code,
        )
        if attempt < UPLOAD_MAX_ATTEMPTS:
            time.sleep(
                min(2 ** (attempt - 1) + random.random(), DOWNLOAD_BACKOFF_CAP)
            )

    raise ResultUploadError(
        f"could not upload the result after {UPLOAD_MAX_ATTEMPTS} "
        f"attempt(s): {last_exc}",
        error_code="RESULT_UPLOAD_FAILED",
    )


# ── Worker scaffold ─────────────────────────────────────────────────
# The blocks below existed once per handler. They are one contract:
# one daemon reads every worker's envelope, error codes and meta
# fields, so a change that lands in one image and not the other makes
# the daemon misread paid work. Each handler keeps thin wrappers that
# bind its own module globals (its GPU flag, its patched transfer
# functions), so the handler tests keep their patch points.

#: Version of the result envelope. A caller reading an object written
#: here checks this before it trusts the payload, so bump it only when
#: the payload's shape changes -- and expect the caller to treat an
#: unknown version as "worker deployed ahead of the daemon" rather
#: than as a bad result. ``runpod_client.py`` imports this same
#: constant, so the writer and the reader cannot drift in source; a
#: deploy skew between them is what the unknown-version rule is for.
RESULT_SCHEMA_VERSION = 1

#: Content type sent on the result PUT. Signed into the presigned URL
#: by the caller, so this constant and its signing parameter must stay
#: in lockstep: a mismatch is a 403 that reads like an expired
#: signature.
RESULT_CONTENT_TYPE = "application/json"


def init_sentry(handler_logger: logging.Logger) -> Any:
    """Initialise Sentry for a worker process, if it is configured.

    :param handler_logger: The handler's logger, for the init line.
    :returns: The ``sentry_sdk`` module, or ``None`` when the image
        does not carry it. The caller guards every capture on that.
    :rtype: module | None
    """
    try:
        import sentry_sdk
    except ImportError:
        return None

    dsn = os.environ.get("SENTRY_DSN_GPU", "").strip()
    if dsn:
        sentry_sdk.init(
            dsn=dsn,
            environment=os.environ.get("SENTRY_ENV", "prod"),
            release=os.environ.get("GIT_SHA", "unknown"),
            traces_sample_rate=0.0,
        )
        handler_logger.info("Sentry initialised")
    return sentry_sdk


class WorkerClock:
    """Boot and uptime accounting for one worker process.

    Construct it as early as possible in the handler module, so
    ``boot_ms`` covers the cold-start cost (module imports plus the
    model preload). Call :meth:`mark_ready` once the preload is done.
    ``boot_ms`` is then constant per worker, and ``uptime_ms()``
    anchors per-job uptime: the same ``boot_ms`` plus an increasing
    uptime across jobs identifies one warm worker. Uses
    ``time.monotonic()`` so wall-clock jumps do not move the numbers.
    """

    def __init__(self) -> None:
        self._start = time.monotonic()
        self._ready_at = self._start
        self.boot_ms = 0

    def mark_ready(self) -> None:
        """Freeze the cold-start cost. Call once, after the preload."""
        self._ready_at = time.monotonic()
        self.boot_ms = int((self._ready_at - self._start) * 1000)

    def uptime_ms(self) -> int:
        """Return milliseconds since :meth:`mark_ready`.

        :returns: Uptime in ms. A small value means a cold start; a
            large value means a reused warm worker.
        :rtype: int
        """
        return int((time.monotonic() - self._ready_at) * 1000)


def with_worker_meta(
    payload: dict, *, clock: WorkerClock, gpu_available: bool
) -> dict:
    """Attach worker boot / uptime / GPU status to a response dict.

    Used for both successful and structured-error returns so the
    caller can always see whether this job hit a warm worker and
    whether inference ran on GPU.

    :param payload: The response dict to augment in place.
    :param clock: The worker's boot clock.
    :param gpu_available: The handler's own GPU flag.
    :returns: The same dict with meta fields added.
    :rtype: dict
    """
    payload["worker_boot_ms"] = clock.boot_ms
    payload["worker_uptime_ms"] = clock.uptime_ms()
    payload["gpu_available"] = gpu_available
    return payload


def tag_sentry(
    sentry: Any,
    job: dict,
    action: str,
    scan_pk: Any,
    *,
    clock: WorkerClock,
    gpu_available: bool,
    handler_logger: logging.Logger,
) -> None:
    """Tag the current Sentry scope with job-identifying context.

    Every exception captured during a handler invocation carries these
    tags, so an event points at the scan and the RunPod job that
    triggered it.

    :param sentry: The ``sentry_sdk`` module, or ``None`` (no-op).
    :param job: RunPod job dict (expects an ``id`` field).
    :param action: The handler action being executed.
    :param scan_pk: Primary key of the scan this job belongs to.
    :param clock: The worker's boot clock.
    :param gpu_available: The handler's own GPU flag.
    :param handler_logger: The handler's logger, for the warning line.
    """
    if sentry is None:
        return
    sentry.set_tag("action", action)
    sentry.set_tag("gpu_available", str(gpu_available))
    sentry.set_tag("worker_boot_ms", str(clock.boot_ms))
    sentry.set_tag("worker_uptime_ms", str(clock.uptime_ms()))
    # Set unconditionally: tags live on the global scope, which a warm
    # worker reuses across jobs. Skipping the set when the value is
    # absent would leave the *previous* job's scan_pk/job id on every
    # event this job captures, misattributing its failures.
    scan_tag = "unknown"
    if scan_pk is not None:
        try:
            scan_tag = str(int(scan_pk))
        except (TypeError, ValueError):
            handler_logger.warning("ignoring non-integer scan_pk: %r", scan_pk)
    sentry.set_tag("scan_pk", scan_tag)
    sentry.set_tag("runpod_job_id", str(job.get("id") or "unknown"))


def deliver_result(
    result: dict,
    inputs: dict,
    scan_pk: Any,
    *,
    summary_fields: tuple,
    upload: Callable,
    extra_summary: Callable[[dict], dict] | None = None,
) -> dict:
    """Send a result to S3 when asked, and answer with a summary.

    Two shapes, chosen by the caller and not by us:

    - ``result_url`` present -> wrap the payload in a self-describing
      envelope, PUT it, and return only the summary plus the key. This
      is what a volume-sized payload needs: RunPod caps a response at
      about 20 MB and discards it with the job record.
    - absent -> return the payload inline. That path is what dev and
      continuous integration use without credentials, and what a
      caller running an older contract gets, so rolling an image back
      needs no daemon change.

    :param result: The action's own return value.
    :param inputs: The handler input payload.
    :param scan_pk: The scan the job belongs to, for the envelope.
    :param summary_fields: The small ``result`` fields the response
        keeps on the S3 path. The volume-sized field must not be in
        here -- echoing it back reintroduces the response cap.
    :param upload: The handler's own ``upload_result`` reference, so a
        test that patches the handler module still intercepts the PUT.
    :param extra_summary: Optional; computes handler-specific summary
        fields from ``result`` (the detect worker's
        ``detection_count``).
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
    size = upload(result_url, envelope, RESULT_CONTENT_TYPE)

    summary = {
        field: result[field] for field in summary_fields if field in result
    }
    if extra_summary is not None:
        summary.update(extra_summary(result))
    summary["result_key"] = inputs.get("result_key")
    summary["bytes"] = size
    return summary


def execute_action(
    fn: Callable,
    job: dict,
    inputs: dict,
    *,
    action: str,
    scan_pk: Any,
    deliver: Callable,
    meta: Callable[[dict], dict],
    sentry: Any,
    handler_logger: logging.Logger,
) -> dict:
    """Run one action inside the error-code contract the daemon reads.

    The ``except`` arms below *are* that contract, which is why they
    live here and not once per handler: the daemon classifies each
    ``error_code`` as a retry or a terminal failure, and an arm that
    drifted in one image would misroute paid work.

    :param fn: The action function: ``fn(job, inputs, tmp_dir)``.
    :param job: The RunPod job dict.
    :param inputs: The handler input payload.
    :param action: The action name, for the log lines.
    :param scan_pk: The scan the job belongs to, for the log lines.
    :param deliver: The handler's ``_deliver`` wrapper.
    :param meta: The handler's ``_with_worker_meta`` wrapper.
    :param sentry: The ``sentry_sdk`` module, or ``None``.
    :param handler_logger: The handler's logger.
    :returns: What to answer RunPod with.
    :rtype: dict
    """
    tmp_dir = Path(tempfile.mkdtemp(prefix="runpod-"))
    try:
        result = fn(job, inputs, tmp_dir)
        return meta(deliver(result, inputs, scan_pk))
    except ResultUploadError as exc:
        # The compute succeeded but could not be delivered, so nothing
        # was written and there is nothing for the caller to harvest.
        # Returned as a structured error rather than raised, because
        # the code is the whole point: RESULT_UPLOAD_FAILED and
        # RESULT_URL_EXPIRED are worth a fresh job (which mints a fresh
        # URL), and RESULT_UPLOAD_REJECTED never is.
        handler_logger.error(
            "result delivery failed: action=%s scan_pk=%s code=%s: %s",
            action,
            scan_pk,
            exc.error_code,
            exc,
        )
        if sentry is not None:
            sentry.capture_exception(exc)
        return meta({"error": str(exc), "error_code": exc.error_code})
    except CorruptDownloadError as exc:
        # Our copy of the PDF would not open, or arrived truncated. The
        # object in the bucket is sound -- the caller cut each shard
        # and verified it against the original -- so this describes the
        # transfer, not the input, and the next attempt may well get it
        # right. Its own code, because BAD_INPUT is terminal and would
        # write a volume off for a dropped connection.
        handler_logger.warning(
            "corrupt download: action=%s scan_pk=%s: %s",
            action,
            scan_pk,
            exc,
        )
        return meta(
            {"error": str(exc), "error_code": "INPUT_DOWNLOAD_CORRUPT"}
        )
    except BadInputError as exc:
        # Input validation (a missing pdf_url, an out-of-range option,
        # an over-MAX_PAGES input). Returned as a structured error, not
        # raised: a raw traceback carries no error_code, so the caller
        # couldn't tell this terminal input error from a transient
        # failure and would retry it to fail identically forever.
        # Deliberately NOT ``except ValueError``: the worker stack
        # raises ValueError at run time too (a degenerate page render,
        # a model shape check), and answering one of those as BAD_INPUT
        # would write the shard off as a caller error with no Sentry
        # event. Those fall through to the arm below instead.
        handler_logger.warning(
            "bad input: action=%s scan_pk=%s: %s", action, scan_pk, exc
        )
        return meta({"error": str(exc), "error_code": "BAD_INPUT"})
    except Exception as exc:
        if sentry is not None:
            sentry.capture_exception(exc)
        handler_logger.exception(
            "handler failed: action=%s scan_pk=%s", action, scan_pk
        )
        # Re-raise so RunPod marks the job FAILED with the traceback.
        # Worker meta is on the Sentry event through tag_sentry; there
        # is no output dict to attach it to here.
        raise
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
