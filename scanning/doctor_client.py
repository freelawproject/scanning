"""HTTP client for doctor's bitonal conversion endpoint (issue #176).

One request converts one shard: doctor fetches it from a presigned GET,
rasterizes at ``DOCTOR_BITONAL_DPI``, re-encodes as CCITT G4, PUTs the
result to a presigned PUT, and answers with a JSON summary. Takes two
URLs and knows nothing about job rows, S3 keys or scans.

Three properties shape everything here and everything in
:mod:`scanning.jobs`:

- **No job id, no status endpoint.** The response *is* the completion
  signal; nothing to poll, nothing to cancel.
- **Losing the response does not lose the work.** Doctor's view is
  sync: on disconnect Django cancels the coroutine, but the thread
  running the conversion cannot be killed, so it finishes and the PUT
  lands. A read timeout costs the answer, not the artifact -- hence the
  caller's S3 HEAD instead of a resubmit.
- **Absence of a result cannot be diagnosed.** "Not yet" and "failed
  silently" look identical, so every lost shard costs its full
  deadline. Tolerable only because a wasted bitonal rerun costs CPU
  seconds; do not generalize this to the paid providers.
"""

from __future__ import annotations

import logging
from typing import Any

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

#: Path of the conversion endpoint on the doctor service.
BITONAL_PATH = "/convert/pdf/bitonal/"

#: Content type doctor sends on the result PUT. The presigned PUT must
#: be signed with exactly this, or S3 answers 403 and doctor reports
#: ``RESULT_URL_EXPIRED``.
RESULT_CONTENT_TYPE = "application/pdf"

#: Error codes worth another attempt: blips on doctor's own egress,
#: plus its "unexpected, retryable" label. The ``*_EXPIRED`` pair is
#: retryable only because a new attempt mints fresh URLs -- never by
#: replaying the same request. ``CONVERSION_TIMEOUT`` is doctor's split
#: of a time budget out of ``CONVERSION_FAILED`` (doctor #245): the page
#: may convert on a less loaded node, or under a larger ``page_timeout``,
#: so it retries where a defective PDF does not.
TRANSIENT_ERROR_CODES = frozenset(
    {
        "INTERNAL_ERROR",
        "INPUT_DOWNLOAD_FAILED",
        "RESULT_UPLOAD_FAILED",
        "INPUT_URL_EXPIRED",
        "RESULT_URL_EXPIRED",
        "CONVERSION_TIMEOUT",
    }
)

#: Fields doctor reports beside a failure on a particular page, so a
#: caller never parses the message text for them. Integers, hence safe
#: to format into a log line: the page within the shard (1-indexed), how
#: many pages converted before it, the elapsed time, and the page's
#: raster size at the requested dpi -- which is what separates "one
#: enormous page" from "poppler wedged".
FAILURE_DETAIL_KEYS = (
    "page_number",
    "pages_completed",
    "elapsed_ms",
    "pixels",
)

#: "We never got an answer, so the work may still be running." Every
#: other failure arrived in a response, meaning doctor is done with that
#: request and will never upload for it -- which is what lets a caller
#: resubmit at once in one case and wait on an S3 HEAD in the other.
UNANSWERED_ERROR_CODE = "TRANSPORT_ERROR"


class DoctorError(RuntimeError):
    """A conversion failed for a reason retrying will not fix.

    :ivar error_code: Doctor's error code, or ``""`` when it never
        answered in its documented shape.
    :ivar details: The :data:`FAILURE_DETAIL_KEYS` doctor reported, when
        it failed on a particular page. Empty for every other failure,
        and for a doctor too old to send them.
    """

    def __init__(
        self,
        message: str,
        error_code: str = "",
        details: dict | None = None,
    ):
        super().__init__(message)
        self.error_code = error_code
        self.details = details or {}


class DoctorTransientError(DoctorError):
    """A conversion failed in a way another attempt may survive."""


def enabled() -> bool:
    """Return whether shards may be dispatched to doctor.

    ``DOCTOR_ENABLED`` is the switch; the host is checked too so that
    blanking it turns the stage off rather than posting to a bare path.
    There is no in-process fallback (issue #173), so with this false an
    environment uploads and browses, and the viewer serves originals.

    :returns: Whether conversion requests should be made.
    :rtype: bool
    """
    return bool(settings.DOCTOR_ENABLED and settings.DOCTOR_HOST)


def _redact(url: str) -> str:
    """Return a presigned URL with its query string masked.

    The path is the half worth logging; the signature is a bearer
    capability that must not reach a log aggregator.

    :param url: The URL to mask.
    :returns: The URL up to the ``?``, with ``?***`` appended when a
        query string was dropped.
    :rtype: str
    """
    base, _, query = url.partition("?")
    return f"{base}?***" if query else base


def convert_bitonal(
    input_url: str,
    output_url: str,
    *,
    dpi: int | None = None,
    threshold: int | None = None,
) -> dict[str, Any]:
    """Convert one shard to bitonal, returning doctor's JSON summary.

    Blocks for the whole conversion (~25-45s per 100-page shard), so
    callers run it from a bounded pool -- and must already have recorded
    that the request went out, since a lost response is recoverable only
    if something names the key to look at.

    The parameters travel in the request rather than doctor's settings,
    so retuning them is a scanning deploy, not a doctor release.

    :param input_url: Presigned GET of the shard PDF.
    :param output_url: Presigned PUT for the converted PDF, signed with
        :data:`RESULT_CONTENT_TYPE`.
    :param dpi: Rasterization resolution; defaults to
        ``settings.DOCTOR_BITONAL_DPI``.
    :param threshold: Grayscale cutoff; defaults to
        ``settings.DOCTOR_BITONAL_THRESHOLD``.
    :returns: Doctor's summary: ``pages``, ``page_count``, ``dpi``,
        ``threshold``, ``first_page``, ``last_page``, ``bytes``,
        ``sha256``, ``source_sha256``, ``duration_ms``.
    :rtype: dict
    :raises DoctorTransientError: On a transport failure, a 5xx, or a
        retryable ``error_code``.
    :raises DoctorError: On a terminal ``error_code`` (corrupt PDF,
        blocked URL, failed conversion) or an unreadable response.
    """
    if not enabled():
        raise DoctorError(
            "DOCTOR_ENABLED is false or DOCTOR_HOST is unset, and there "
            "is no in-process bitonal fallback."
        )

    url = f"{settings.DOCTOR_HOST.rstrip('/')}{BITONAL_PATH}"
    payload = {
        "input_url": input_url,
        "output_url": output_url,
        "dpi": settings.DOCTOR_BITONAL_DPI if dpi is None else dpi,
        "threshold": (
            settings.DOCTOR_BITONAL_THRESHOLD
            if threshold is None
            else threshold
        ),
    }
    logger.info(
        "doctor bitonal: POST %s dpi=%s threshold=%s in=%s out=%s",
        url,
        payload["dpi"],
        payload["threshold"],
        _redact(input_url),
        _redact(output_url),
    )

    try:
        # Form-encoded: doctor reads request.POST, parses no JSON body
        # and ignores query parameters.
        response = requests.post(
            url,
            data=payload,
            timeout=(
                int(settings.DOCTOR_CONNECT_TIMEOUT),
                int(settings.DOCTOR_READ_TIMEOUT),
            ),
        )
    except requests.ConnectTimeout as exc:
        # Provably never reached the application, so nothing is running
        # and nothing will be uploaded: safe to resubmit at once.
        raise DoctorTransientError(
            f"doctor connect timeout: {exc}", error_code="CONNECT_TIMEOUT"
        ) from exc
    except requests.RequestException as exc:
        # Everything else, the read timeout above all: doctor may still
        # be converting and its PUT will still land, so the caller must
        # check the result key before resubmitting.
        raise DoctorTransientError(
            f"doctor request failed: {exc}", error_code="TRANSPORT_ERROR"
        ) from exc

    try:
        body = response.json()
    except ValueError:
        # Doctor answers its own errors as JSON, so a non-JSON body is
        # something in front of it (an ingress 502/504) or a crash
        # outside its error handling. The shape says nothing, so fall
        # back to the status: 5xx retryable, anything else terminal.
        detail = response.text[:200]
        message = (
            f"doctor returned a non-JSON {response.status_code} "
            f"response: {detail!r}"
        )
        if response.status_code >= 500:
            raise DoctorTransientError(
                message, error_code="BAD_GATEWAY"
            ) from None
        raise DoctorError(message, error_code="BAD_RESPONSE") from None

    if not isinstance(body, dict):
        raise DoctorError(
            f"doctor returned a {type(body).__name__}, expected an object",
            error_code="BAD_RESPONSE",
        )

    if response.ok and body.get("success"):
        return body

    error_code = str(body.get("error_code") or "")
    message = (
        f"doctor bitonal failed ({response.status_code} {error_code or '?'}): "
        f"{str(body.get('msg'))[:300]}"
    )
    details = _failure_details(body)
    if error_code in TRANSIENT_ERROR_CODES:
        raise DoctorTransientError(
            message, error_code=error_code, details=details
        )
    if not error_code and response.status_code >= 500:
        # A 5xx with no code to classify: assume infrastructure.
        raise DoctorTransientError(message, error_code="BAD_GATEWAY")
    raise DoctorError(
        message, error_code=error_code or "UNKNOWN", details=details
    )


def _failure_details(body: dict) -> dict:
    """Pick doctor's per-page failure fields out of an error body.

    Filtered to :data:`FAILURE_DETAIL_KEYS`, and to integers rather than
    passed through, so a caller can format them without re-checking each
    one. A doctor too old to send them, or a failure that names no page,
    yields ``{}`` rather than a partly-typed dict.

    :param body: Doctor's decoded JSON error body.
    :returns: The reported detail fields, integers only.
    :rtype: dict
    """
    details = {}
    for key in FAILURE_DETAIL_KEYS:
        value = body.get(key)
        # bool is an int subclass, and "page_number": true must not
        # reach a log line as a page number.
        if isinstance(value, int) and not isinstance(value, bool):
            details[key] = value
    return details
