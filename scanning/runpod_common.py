"""Transfer/validation code shared by the RunPod GPU-worker images.

Each worker under ``scanning/runpod*/`` is a separate deploy artifact:
its Dockerfile copies ``handler.py`` plus this module into the image,
and the handler imports it as a top-level module (``import
runpod_common``). Nothing here may depend on Django, the scanning app,
or anything not installed in every worker venv — only ``requests``,
PyMuPDF (imported lazily), and the stdlib.

Extracted from the previously byte-for-byte duplicated copies in the
worker handlers so a fix to the resume/validation logic lands in every
worker at once. The only worker left is ``scanning/runpod-dotsmocr/``
(the legacy blackletter-gpu-worker went with the legacy pipeline,
issue #173), but the next engine image (#147) starts from this module
too. Tested in ``scanning/tests/test_runpod_common.py``.
"""

from __future__ import annotations

import json
import logging
import os
import random
import time
from pathlib import Path

import requests
from urllib3.exceptions import IncompleteRead, ProtocolError

logger = logging.getLogger("runpod_common")

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
