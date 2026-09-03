"""Transport to Mistral's files, OCR and batch APIs (issue #191).

Moves bytes and classifies failures, and knows nothing about job rows,
S3 keys or scans -- the shape of :mod:`scanning.runpod_client`. The
call shapes are the ones the ai-research runner used in production
(``runpod/mistral/mistral_ocr.py`` on its ``extraction_align`` branch):
one ``ocr`` file per page, one ``batch`` manifest, one batch job over
``/v1/ocr``, and the output file read line by line.

Three properties shape everything here and in :mod:`scanning.mistral_ocr`:

- **A batch has a job id and a status.** ``client.batch.jobs.get``
  reports ``QUEUED``, ``RUNNING`` and the terminal states, so a lost
  answer is free to recover: poll the id again.
- **Nothing is written to our bucket by the provider.** The output is a
  file at Mistral, downloaded by us. So the harvest, not a presigned
  PUT, is what writes ``result_key``.
- **Every call may be rate limited.** A 429 on an upload is not a fault
  of the job, so it is its own error class and the row is deferred with
  its attempt intact.

Nothing here reads a response beyond what routing needs: the output
lines are handed back as Mistral wrote them, and every transform waits
for the glue. The SDK is imported lazily, so an environment with no key
never loads it.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from django.conf import settings

from scanning.models import JobStatus

logger = logging.getLogger(__name__)

#: The batch endpoint every line of a manifest is run against.
BATCH_ENDPOINT = "/v1/ocr"

#: Seconds one HTTP call to Mistral may take: an upload of one page
#: image, a batch create, a poll, or a download of one output file.
REQUEST_TIMEOUT = 120

#: "We never got an answer." For a batch create this is the one case a
#: caller cannot recover: a job may or may not exist, and nothing we
#: hold names it. For a poll it means "ask again".
UNANSWERED_ERROR_CODE = "TRANSPORT_ERROR"

#: Mistral's batch statuses, normalized onto :class:`JobStatus`. A
#: status not listed reads as "still at work", so Mistral adding a
#: state cannot fail jobs (the same rule ``runpod_client`` follows).
BATCH_STATUS_MAP = {
    "QUEUED": JobStatus.IN_QUEUE,
    "RUNNING": JobStatus.IN_PROGRESS,
    "SUCCESS": JobStatus.COMPLETED,
    "FAILED": JobStatus.FAILED,
    "TIMEOUT_EXCEEDED": JobStatus.FAILED,
    "CANCELLATION_REQUESTED": JobStatus.CANCELLED,
    "CANCELLED": JobStatus.CANCELLED,
}

#: Terminal batch statuses worth another attempt. A timeout is Mistral's
#: own queue running long; a failure of the whole job (not of a line)
#: is most often the service, not the pages.
RETRIABLE_BATCH_STATUSES = frozenset({"FAILED", "TIMEOUT_EXCEEDED"})


class MistralError(RuntimeError):
    """A Mistral call failed for good.

    :ivar error_code: A short code for the row's ``error_code``.
    :ivar status_code: The HTTP status, when there was one.
    """

    def __init__(
        self,
        message: str,
        error_code: str = "MISTRAL_ERROR",
        status_code: int | None = None,
    ):
        super().__init__(message)
        self.error_code = error_code
        self.status_code = status_code


class MistralTransientError(MistralError):
    """A failure another attempt may not repeat: a 5xx, a lost answer."""


class MistralBusy(MistralTransientError):
    """Mistral declined the work for now (HTTP 429).

    Nothing is wrong with the job, so the caller defers the row and
    spends no attempt.
    """


class MistralMissing(MistralError):
    """The job or file asked for is gone (HTTP 404)."""


@dataclass
class BatchOutcome:
    """One batch status answer, normalized onto :class:`JobStatus`.

    A value rather than an exception, for the same reason
    ``runpod_client.PollOutcome`` is: the confirm pass sweeps every
    in-flight row on one tick, and one row's failure must not abort
    the sweep for the rest.

    :ivar status: A ``JobStatus`` value, or ``None`` when the poll
        itself failed and we learned nothing.
    :ivar provider_status: Mistral's own status string, for logs.
    :ivar total: ``total_requests`` -- the lines of the manifest.
    :ivar succeeded: ``succeeded_requests`` so far.
    :ivar failed: ``failed_requests`` so far.
    :ivar output_file: Id of the output file, once there is one.
    :ivar error_file: Id of the error file, once there is one.
    :ivar error_code: Why the job is written off, when it is.
    :ivar error_message: Human-readable failure detail.
    :ivar retriable: Whether resubmitting could plausibly succeed.
    :ivar job: The batch job object as Mistral returned it, whole, so
        the harvest can store it beside the output.
    """

    status: str | None
    provider_status: str = ""
    total: int = 0
    succeeded: int = 0
    failed: int = 0
    output_file: str | None = None
    error_file: str | None = None
    error_code: str = ""
    error_message: str = ""
    retriable: bool = False
    job: dict | None = None


def enabled() -> bool:
    """Return whether Mistral jobs may be dispatched.

    A set key is the switch, as it is for the ai-research runner: a
    wasted run costs money, so an environment that must not spend
    leaves the key unset.

    :returns: Whether requests should be made.
    :rtype: bool
    """
    return bool(settings.MISTRAL_API_KEY)


def model() -> str:
    """Return the model name every request names.

    :returns: ``settings.MISTRAL_MODEL``.
    :rtype: str
    """
    return settings.MISTRAL_MODEL


def client():
    """Return an SDK client bound to the configured key.

    Imported here, not at module level: the daemon and the web pod load
    this module through the provider table whether or not the stage is
    on, and the SDK is a dependency an environment with no key never
    needs to exercise.

    :returns: A ``mistralai`` client.
    :raises MistralError: If the key is unset.
    """
    if not settings.MISTRAL_API_KEY:
        raise MistralError(
            "MISTRAL_API_KEY is not set", error_code="NOT_CONFIGURED"
        )
    try:
        from mistralai import Mistral
    except ImportError:  # 2.x namespace layout
        from mistralai.client import Mistral
    return Mistral(
        api_key=settings.MISTRAL_API_KEY, timeout_ms=REQUEST_TIMEOUT * 1000
    )


def classify(exc: Exception) -> MistralError:
    """Return the :class:`MistralError` one SDK failure amounts to.

    One function, so the split between "declined for now", "try again",
    "gone" and "done for good" is decided in one place. The SDK's own
    errors carry ``status_code``; anything without one -- an ``httpx``
    transport error, the SDK's ``NoResponseError``, a socket error --
    is a lost answer.

    :param exc: What the SDK raised.
    :returns: The classified error, with the original as its cause.
    :rtype: MistralError
    """
    if isinstance(exc, MistralError):
        return exc
    status = getattr(exc, "status_code", None)
    message = str(exc)[:500]
    if isinstance(status, int):
        if status == 429:
            error = MistralBusy(message, "RATE_LIMITED", status)
        elif status == 404:
            error = MistralMissing(message, "NOT_FOUND", status)
        elif status >= 500:
            error = MistralTransientError(message, f"HTTP_{status}", status)
        else:
            error = MistralError(message, f"HTTP_{status}", status)
    else:
        error = MistralTransientError(message, UNANSWERED_ERROR_CODE)
    error.__cause__ = exc
    return error


def _call(fn, *args, **kwargs):
    """Call one SDK method and classify whatever it raises.

    :param fn: The bound SDK method.
    :returns: Whatever the method returns.
    :raises MistralError: The classified failure.
    """
    try:
        return fn(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001 - classified into our types
        raise classify(exc) from exc


def _dump(obj: Any) -> dict:
    """Return an SDK object as a plain dict, whole.

    :param obj: A pydantic model, a dict, or something with ``__dict__``.
    :returns: Its fields.
    :rtype: dict
    """
    if isinstance(obj, dict):
        return obj
    if hasattr(obj, "model_dump"):
        return obj.model_dump(mode="json")
    return dict(vars(obj))


def upload_file(name: str, content: bytes, purpose: str) -> str:
    """Upload one file and return its id.

    :param name: The file name Mistral shows.
    :param content: The bytes.
    :param purpose: ``"ocr"`` for a page image, ``"batch"`` for a
        manifest.
    :returns: The file id.
    :rtype: str
    :raises MistralError: The classified failure; a 429 is
        :class:`MistralBusy`.
    """
    uploaded = _call(
        client().files.upload,
        file={"file_name": name, "content": content},
        purpose=purpose,
    )
    return str(uploaded.id)


def create_batch(
    input_file_id: str, *, metadata: dict[str, str], timeout_hours: int
) -> str:
    """Create one batch job over ``/v1/ocr`` and return its id.

    :param input_file_id: The uploaded JSONL manifest.
    :param metadata: Short strings Mistral shows beside the job.
    :param timeout_hours: Mistral's own budget for the whole job; past
        it the job ends ``TIMEOUT_EXCEEDED``.
    :returns: The batch job id.
    :rtype: str
    :raises MistralError: The classified failure. A transient one here
        means a job may exist that nothing we hold names.
    """
    job = _call(
        client().batch.jobs.create,
        endpoint=BATCH_ENDPOINT,
        input_files=[input_file_id],
        model=model(),
        metadata=metadata,
        timeout_hours=timeout_hours,
    )
    return str(job.id)


def poll_batch(job_id: str, label: str = "") -> BatchOutcome:
    """Ask after one batch job. Never raises.

    :param job_id: The batch job id.
    :param label: What to call the job in a log line.
    :returns: The normalized outcome; ``status=None`` when the poll
        itself failed.
    :rtype: BatchOutcome
    """
    try:
        job = _call(client().batch.jobs.get, job_id=job_id)
    except MistralMissing as exc:
        # The job record is gone. The inputs may be gone with it, so
        # this is a loss, and a retriable one: a new attempt uploads
        # them again.
        return BatchOutcome(
            status=JobStatus.EXPIRED,
            provider_status="MISSING",
            error_code="BATCH_MISSING",
            error_message=str(exc),
            retriable=True,
        )
    except MistralError as exc:
        logger.warning(
            "poll of Mistral batch %s (%s) failed: %s", job_id, label, exc
        )
        return BatchOutcome(status=None, error_message=str(exc))

    data = _dump(job)
    provider_status = str(data.get("status") or "")
    status = BATCH_STATUS_MAP.get(provider_status, JobStatus.IN_PROGRESS)
    outcome = BatchOutcome(
        status=status,
        provider_status=provider_status,
        total=int(data.get("total_requests") or 0),
        succeeded=int(data.get("succeeded_requests") or 0),
        failed=int(data.get("failed_requests") or 0),
        output_file=data.get("output_file") or None,
        error_file=data.get("error_file") or None,
        job=data,
    )
    if status == JobStatus.FAILED:
        outcome.error_code = f"BATCH_{provider_status}"
        outcome.error_message = json.dumps(data.get("errors") or [])[:2000]
        outcome.retriable = provider_status in RETRIABLE_BATCH_STATUSES
    elif status == JobStatus.CANCELLED:
        outcome.error_code = "CANCELLED_UPSTREAM"
        outcome.error_message = f"Mistral reports {provider_status}"
    return outcome


def download_lines(file_id: str) -> list[dict]:
    """Download one JSONL file and return its lines, parsed and whole.

    Each line is ``json.loads``-ed and nothing else: no key is read,
    renamed or dropped. A line that is not JSON is kept as
    ``{"raw": <text>}`` rather than lost.

    :param file_id: The output or error file of a batch.
    :returns: One dict per non-empty line.
    :rtype: list[dict]
    :raises MistralError: The classified failure.
    """
    response = _call(client().files.download, file_id=file_id)
    if hasattr(response, "read"):
        raw = response.read()
    elif isinstance(response, (bytes, bytearray)):
        raw = bytes(response)
    else:
        raw = b"".join(response)
    lines = []
    for line in raw.decode("utf-8", "replace").splitlines():
        if not line.strip():
            continue
        try:
            lines.append(json.loads(line))
        except ValueError:
            lines.append({"raw": line})
    return lines


def cancel_batch(job_id: str) -> None:
    """Cancel one batch job. Best effort, never raises.

    :param job_id: The batch job id.
    :return: None.
    """
    try:
        _call(client().batch.jobs.cancel, job_id=job_id)
    except MistralError as exc:
        logger.warning("could not cancel Mistral batch %s: %s", job_id, exc)


def delete_file(file_id: str) -> None:
    """Delete one file at Mistral. Best effort, never raises.

    :param file_id: The file id.
    :return: None.
    """
    try:
        _call(client().files.delete, file_id=file_id)
    except MistralError as exc:
        logger.warning("could not delete Mistral file %s: %s", file_id, exc)


def ocr_page_realtime(
    png: bytes, *, include_blocks: bool = True, table_format: str = "html"
) -> dict:
    """Read one page image on the synchronous ``/v1/ocr`` endpoint.

    For the smoke test the ai-research runner prescribes before a
    batch ("run realtime first, then batch"), never for the stage: the
    realtime price is twice the batch price. The response is returned
    whole.

    :param png: The rendered page.
    :param include_blocks: Ask for the block bboxes.
    :param table_format: How tables come back.
    :returns: The OCR response as a dict.
    :rtype: dict
    :raises MistralError: The classified failure.
    """
    import base64

    encoded = base64.b64encode(png).decode()
    response = _call(
        client().ocr.process,
        model=model(),
        document={
            "type": "image_url",
            "image_url": f"data:image/png;base64,{encoded}",
        },
        include_blocks=include_blocks,
        table_format=table_format,
    )
    return _dump(response)
