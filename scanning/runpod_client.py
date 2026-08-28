"""RunPod Serverless transport: submit a job, ask after it, cancel it.

Four functions and a value type. Everything about *which* job to submit,
when to retry it and what to do with its output lives on
``ExternalJob`` rows (:mod:`scanning.jobs`), because the process that
polls a job is generally not the one that submitted it.

The blocking submit-and-poll loop this module used to hold went with the
legacy pipeline (issue #173 deleted its last caller, and #190 deleted
the loop). Nothing here sleeps, retries in-call, or waits: pacing is the
daemon's tick interval. That matters because the daemon's scheduler is
serial (issue #156), so a client that slept would stall every other task.

Three properties shape how :mod:`scanning.jobs` uses this:

- **RunPod is asynchronous.** ``POST /run`` answers with a job id, and
  ``GET /status/{id}`` reports progress. Unlike doctor, there is
  something to poll and something to cancel -- and cancelling matters,
  because a graphics processing unit (GPU) job bills while it runs.
- **Queue time is free and unbounded.** An endpoint scaled to a narrow
  worker pool queues the excess by design. So a job's deadline may only
  become an execution budget once ``/status`` first reports
  ``IN_PROGRESS``; charging queue time to a run budget would cancel the
  tail of every batch and resubmit it to the back of the same queue.
  :func:`poll_once` reports the crossing; ``jobs`` stamps the deadline.
- **The job record outlives neither the result nor the work.** RunPod
  discards a job about 30 minutes after it finishes, and answers 404
  from then on. The worker's output does not go with it: it is PUT to an
  attempt-scoped S3 key, so a 404 is answered by probing that key
  rather than by paying for the work again.

Why the payload travels through S3 rather than inline in the job
response: RunPod caps a response at about 20 MB and discards it with the
job record. An S3 object has neither limit. Inline delivery is still
supported, and is what a worker returns when ``result_url`` is absent
from the input -- dev and CI without credentials, and worker images
predating the contract.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import requests
from django.conf import settings

# Re-exported: ``jobs.py`` reads them from this module, and they come
# from ``runpod_common`` -- the module the workers themselves are built
# on -- so the writer and this reader cannot drift in source. The
# presigned PUT must be signed with exactly this content type, or S3
# answers 403 and the worker reports ``RESULT_URL_EXPIRED``. A version
# skew between a deployed image and this daemon is still possible and
# is handled at read time: treat an unknown ``schema_version`` as
# "worker deployed ahead of the daemon".
from scanning.runpod_common import (
    RESULT_CONTENT_TYPE as RESULT_CONTENT_TYPE,
)
from scanning.runpod_common import (
    RESULT_SCHEMA_VERSION as RESULT_SCHEMA_VERSION,
)

logger = logging.getLogger(__name__)

#: How long a status call may take. Generous against a healthy endpoint,
#: short enough that a wedged one does not hold the serial daemon loop.
_HTTP_TIMEOUT = 30

#: Worker error codes worth another attempt.
#:
#: ``NO_GPU`` / ``VLLM_UNHEALTHY``: the worker was misprovisioned or its
#: inference server died. Both also set ``refresh_worker``, so RunPod
#: terminates that worker and the next attempt lands elsewhere.
#:
#: ``RESULT_UPLOAD_FAILED`` / ``RESULT_URL_EXPIRED``: the compute
#: succeeded but the worker could not deliver it, so nothing was written
#: and there is nothing to harvest. Resubmitting is the only recovery,
#: and it mints a fresh presigned PUT -- which is what an expired
#: signature needs, since the dead URL can never be reused.
#:
#: Deliberately absent: ``RESULT_UPLOAD_REJECTED``, which S3 returns for
#: a misconfiguration (wrong signing region, a bucket policy demanding
#: headers we do not send). Every retry re-runs the GPU work, pays for
#: it, and fails identically, so that one stays terminal.
#: ``INPUT_DOWNLOAD_CORRUPT``: the worker's copy of the shard would not
#: open, or was truncated. We cut that shard ourselves and verified it
#: against the original (issue #164), so the bytes in the bucket are
#: sound and the fault is in the transfer -- which the next attempt is
#: quite likely to get right. Terminal here would write a volume off for
#: a dropped connection.
TRANSIENT_ERROR_CODES = frozenset(
    {
        "NO_GPU",
        "VLLM_UNHEALTHY",
        "RESULT_UPLOAD_FAILED",
        "RESULT_URL_EXPIRED",
        "INPUT_DOWNLOAD_CORRUPT",
    }
)

#: Submit-time codes meaning "the endpoint cannot take work right now"
#: rather than "this job is bad" -- an endpoint scaled to zero
#: (``max_workers=0``) reports HTTP 409 ``ENDPOINT_PAUSED``. These raise
#: :class:`RunpodEndpointBusy`, which costs the row no attempt: the job
#: is fine and the endpoint will come back. HTTP 429 (rate limited) is
#: classified the same way by status alone, since it carries no code.
TRANSIENT_SUBMIT_CODES = frozenset({"ENDPOINT_PAUSED"})

#: "We never got an answer, so the job may exist and may still run."
#: Mirrors ``doctor_client.UNANSWERED_ERROR_CODE`` so ``jobs`` handles
#: both providers' lost answers on one path: leave the row in flight and
#: let the confirm pass judge it by the result key. Resubmitting here
#: would pay for the shard twice.
UNANSWERED_ERROR_CODE = "TRANSPORT_ERROR"

#: Input fields holding a presigned URL. Masked before logging: each is
#: a time-limited capability on one object, read for ``pdf_url`` and
#: **write** for ``result_url``. ``result_key`` stays visible -- it is
#: the key alone, with no capability attached, and it is what you need
#: in a log line to find a job's output later.
_SIGNED_URL_FIELDS = ("pdf_url", "result_url")


class RunpodError(RuntimeError):
    """A job failed for a reason retrying will not fix.

    Carries an ``error_code`` like :class:`~scanning.doctor_client
    .DoctorError` does, so ``jobs`` reads one shape for both providers.

    :ivar error_code: The worker's code, or one synthesised for a
        failure RunPod reported no code for.
    """

    def __init__(self, message: str, error_code: str = ""):
        super().__init__(message)
        self.error_code = error_code


class RunpodTransientError(RunpodError):
    """A job failed in a way another attempt may survive."""


class RunpodEndpointBusy(RunpodTransientError):
    """The endpoint is not accepting work, so the job never started.

    Distinct from a plain transient failure because it costs no
    attempt. Nothing is wrong with the job, and burning its retry
    budget on a paused endpoint would fail a volume for being
    submitted at the wrong moment.
    """


@dataclass(frozen=True)
class PollOutcome:
    """One status answer, normalized onto :class:`JobStatus`.

    A value rather than an exception because the confirm pass sweeps
    every in-flight job on one tick, and one job's terminal failure
    must not abort the sweep for the rest.

    :ivar status: A ``JobStatus`` value, or ``None`` when the status
        call itself failed and we learned nothing. ``None`` is not a
        job state: it means ask again, and the row stays as it was.
    :ivar provider_status: RunPod's own status string, for logs. Empty
        when there was no answer.
    :ivar output: The job's ``output`` dict on ``COMPLETED``.
    :ivar error_code: The worker's ``error_code``, or one synthesised
        for a failure RunPod reported no code for.
    :ivar error_message: Human-readable failure detail.
    :ivar retriable: Whether resubmitting could plausibly succeed.
        Orthogonal to whether ``status`` is terminal: ``EXPIRED`` is
        both terminal and worth another submit, since the inputs are
        still on S3 and only the job record is gone.
    :ivar confirmed_by: How a ``COMPLETED`` was learned --
        ``"response"`` when RunPod said so, ``"s3_head"`` when its job
        record was already gone and the output was found at the result
        key instead. Carried rather than inferred from ``output``,
        because the recovery path synthesises a truthy ``output`` and
        would otherwise be recorded as a normal provider answer, hiding
        from anyone triaging the run that the job record had expired.
    """

    status: str | None
    provider_status: str = ""
    output: dict | None = None
    error_code: str = ""
    error_message: str = ""
    retriable: bool = False
    confirmed_by: str = "response"


def enabled(endpoint_id: str) -> bool:
    """Return whether jobs may be dispatched to ``endpoint_id``.

    Three switches, because each fails differently: ``RUNPOD_ENABLED``
    turns dispatch off for a whole environment, the API key is account
    credentials, and the endpoint id is per engine -- dots.mocr and
    YOLO run on separate endpoints, so one being unset must not stop
    the other.

    There is no in-process fallback (issue #173), so with any of these
    missing an environment uploads and browses but runs no GPU stage.

    :param endpoint_id: The engine's RunPod endpoint id.
    :returns: Whether requests should be made.
    :rtype: bool
    """
    return bool(
        settings.RUNPOD_ENABLED and settings.RUNPOD_API_KEY and endpoint_id
    )


def endpoint_config(endpoint_id: str) -> tuple[str, dict[str, str]]:
    """Return the base URL and auth header for one endpoint.

    Takes the id rather than reading a single setting, because a stage
    is dispatched per engine and each engine has its own endpoint on
    the shared account.

    :param endpoint_id: The engine's RunPod endpoint id.
    :returns: ``(base_url, headers)``.
    :rtype: tuple[str, dict[str, str]]
    :raises RunpodError: If the endpoint id or the API key is unset.
    """
    api_key = settings.RUNPOD_API_KEY
    if not endpoint_id or not api_key:
        raise RunpodError(
            "RunPod endpoint id or RUNPOD_API_KEY is not configured, and "
            "there is no in-process fallback.",
            error_code="NOT_CONFIGURED",
        )
    return (
        f"https://api.runpod.ai/v2/{endpoint_id}",
        {"Authorization": f"Bearer {api_key}"},
    )


def _redact_urls(job_input: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of one job input with its presigned URLs masked.

    :param job_input: The ``input`` dict about to be POSTed.
    :returns: A masked shallow copy, safe to log.
    :rtype: dict
    """
    masked = {
        field: "***" for field in _SIGNED_URL_FIELDS if field in job_input
    }
    return {**job_input, **masked} if masked else dict(job_input)


def _submit_error_detail(exc: Exception) -> tuple[int | None, str, str]:
    """Pull the HTTP status and body out of a failed ``/run`` request.

    RunPod error responses carry a JSON body such as
    ``{"status":409,"title":"Conflict","detail":"Endpoint is paused
    (max_workers=0)...","code":"ENDPOINT_PAUSED"}``. ``raise_for_status``
    discards it, so this recovers the parts worth logging and
    classifying.

    :param exc: The exception raised by ``requests``.
    :returns: ``(status_code, runpod_code, body_text)``. A
        transport-level failure with no response attached returns
        ``(None, "", "")``.
    :rtype: tuple[int | None, str, str]
    """
    response = getattr(exc, "response", None)
    if response is None:
        return None, "", ""
    code = ""
    try:
        parsed = response.json()
        if isinstance(parsed, dict):
            code = parsed.get("code") or ""
    except ValueError:
        pass
    return response.status_code, code, response.text or ""


def submit_job(
    base_url: str,
    headers: dict[str, str],
    job_input: dict[str, Any],
    label: str = "",
) -> str:
    """Submit one job and return its id, without waiting for it.

    Makes exactly one request. A blip is the next daemon tick's
    problem, not this call's: an in-call retry loop would sleep on the
    serial daemon loop and delay every other task.

    The caller must already have recorded that a request went out, and
    named the key to look for. A lost answer is only recoverable if
    something points at the result object.

    :param base_url: From :func:`endpoint_config`.
    :param headers: From :func:`endpoint_config`.
    :param job_input: The ``input`` payload. Must carry ``action``.
    :param label: What to call this job in log lines.
    :returns: RunPod's job id.
    :rtype: str
    :raises RunpodEndpointBusy: If the endpoint is not accepting work.
        Costs the row no attempt.
    :raises RunpodTransientError: On a transport failure (the job may
        exist, so the caller must probe the result key rather than
        resubmit) or a 5xx (the job was refused, so resubmit).
    :raises RunpodError: On any other rejection, or a response with no
        job id.
    """
    logger.info(
        "runpod %s: POST %s/run input=%s",
        label or job_input.get("action", "job"),
        base_url,
        _redact_urls(job_input),
    )
    try:
        response = requests.post(
            f"{base_url}/run",
            headers=headers,
            json={"input": job_input},
            timeout=_HTTP_TIMEOUT,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        status_code, error_code, text = _submit_error_detail(exc)
        detail = f" (HTTP {status_code} body: {text[:500]})" if text else ""

        if status_code is None:
            # No response at all. RunPod may have accepted the job and
            # lost the answer, so the work may be running and its PUT
            # may still land. Resubmitting could pay for it twice.
            raise RunpodTransientError(
                f"runpod submit got no answer: {exc}",
                error_code=UNANSWERED_ERROR_CODE,
            ) from exc

        # 429 belongs with 409, not with the other 4xx: both are the
        # endpoint declining work for a reason that has nothing to do
        # with this job, and both clear on their own. Failing a shard
        # for good over one rate limit is wildly disproportionate.
        if status_code in (409, 429) or error_code in TRANSIENT_SUBMIT_CODES:
            raise RunpodEndpointBusy(
                f"runpod endpoint is not accepting work (HTTP "
                f"{status_code} {error_code or '?'}): {exc}",
                error_code=error_code or "ENDPOINT_PAUSED",
            ) from exc

        if status_code >= 500:
            # Refused with an answer: nothing was accepted and nothing
            # will be uploaded, so another attempt is safe at once.
            raise RunpodTransientError(
                f"runpod submit failed{detail}: {exc}",
                error_code=error_code or "BAD_GATEWAY",
            ) from exc

        raise RunpodError(
            f"runpod rejected the job{detail}: {exc}",
            error_code=error_code or "SUBMIT_REJECTED",
        ) from exc

    try:
        body = response.json()
    except ValueError:
        raise RunpodError(
            f"runpod /run returned a non-JSON body: {response.text[:200]!r}",
            error_code="BAD_RESPONSE",
        ) from None

    job_id = body.get("id") if isinstance(body, dict) else None
    if not job_id:
        raise RunpodError(
            f"runpod /run returned no job id: {body!r}",
            error_code="BAD_RESPONSE",
        )
    logger.info(
        "runpod %s job %s submitted",
        label or job_input.get("action", "job"),
        job_id,
    )
    return str(job_id)


def poll_once(
    base_url: str,
    headers: dict[str, str],
    job_id: str,
    label: str = "",
    result_key: str = "",
) -> PollOutcome:
    """Ask after one job once, and never sleep or raise.

    Pacing belongs to the caller, which for the daemon is its tick
    interval. A backoff in here would serialize the very batch the
    fan-out exists to run at once.

    :param base_url: From :func:`endpoint_config`.
    :param headers: From :func:`endpoint_config`.
    :param job_id: RunPod's job id.
    :param label: What to call this job in log lines.
    :param result_key: Attempt-scoped key the worker was authorized to
        write. Lets the 404 branch check S3 before writing the job off.
    :returns: What this poll learned.
    :rtype: PollOutcome
    """
    from scanning.models import JobStatus

    try:
        response = requests.get(
            f"{base_url}/status/{job_id}",
            headers=headers,
            timeout=_HTTP_TIMEOUT,
        )
        if response.status_code == 404:
            return _missing_job_outcome(job_id, result_key)
        response.raise_for_status()
        body = response.json()
    except Exception as exc:  # noqa: BLE001 - any failure means "unknown"
        # A 5xx, a network blip, a read timeout, an unparseable body: we
        # learned nothing about the job, which is not the same as
        # learning it failed. Leave the row exactly as it is.
        logger.warning(
            "runpod status poll for %s failed: %s; will ask again",
            job_id,
            exc,
        )
        return PollOutcome(status=None, error_message=str(exc)[:2000])

    if not isinstance(body, dict):
        logger.warning(
            "runpod status for %s returned a %s, expected an object; "
            "will ask again",
            job_id,
            type(body).__name__,
        )
        return PollOutcome(status=None)

    provider_status = str(body.get("status") or "")
    logger.debug("poll runpod job %s -> %s", job_id, provider_status)

    if provider_status == "COMPLETED":
        return _completed_outcome(body, job_id, label)
    if provider_status in ("FAILED", "TIMED_OUT", "CANCELLED"):
        return _failed_outcome(body, job_id, provider_status)

    # IN_QUEUE, IN_PROGRESS, or anything unrecognised. An unknown status
    # reads as "still at work" rather than as a failure: RunPod adding a
    # state must not fail every job that is merely in it.
    return PollOutcome(
        status=(
            JobStatus.IN_QUEUE
            if provider_status == "IN_QUEUE"
            else JobStatus.IN_PROGRESS
        ),
        provider_status=provider_status,
    )


def _missing_job_outcome(job_id: str, result_key: str) -> PollOutcome:
    """Classify a 404 from ``/status``.

    The job record is gone: RunPod either discarded it after exhausting
    its internal retries, or it aged past the retention window. The work
    itself may well have finished, and the worker's output outlives the
    record because it went to its own S3 key. So check the key before
    throwing away a run we already paid for.

    The key is scoped to run, shard **and attempt**
    (``s3_sync.s3_job_attempt_key``), so presence needs no freshness
    window: anything there was written by this attempt.

    Every S3 error is swallowed rather than raised. The fallback --
    write the attempt off and submit a fresh one -- is always safe,
    whereas an exception here would abort the sweep for every other job
    on the tick. Worth naming: ``head_object`` on a missing key answers
    ``403 AccessDenied`` rather than 404 when the caller lacks
    ``s3:ListBucket``, and swallowing keeps that IAM shape merely
    unhelpful rather than pathological.

    :param job_id: The job RunPod no longer knows about.
    :param result_key: The key it was authorized to write, if any.
    :returns: ``COMPLETED`` if the output is there, else a retriable
        ``EXPIRED``.
    :rtype: PollOutcome
    """
    from scanning import s3_sync
    from scanning.models import JobStatus

    present = False
    if result_key:
        try:
            present = s3_sync.object_exists(result_key)
        except Exception:
            logger.warning(
                "could not check %s after a 404 for job %s; treating the "
                "job as lost",
                result_key,
                job_id,
                exc_info=True,
            )

    if present:
        logger.info(
            "runpod job %s is gone (HTTP 404) but its result is on S3 "
            "(%s); harvesting instead of resubmitting",
            job_id,
            result_key,
        )
        return PollOutcome(
            status=JobStatus.COMPLETED,
            provider_status="NOT_FOUND",
            output={"result_key": result_key},
            confirmed_by="s3_head",
        )

    return PollOutcome(
        status=JobStatus.EXPIRED,
        provider_status="NOT_FOUND",
        error_code="JOB_NOT_FOUND",
        error_message=(
            f"RunPod job {job_id} not found (HTTP 404 from /status), and "
            "no output of its own on S3."
        ),
        retriable=True,
    )


def _completed_outcome(body: dict, job_id: str, label: str) -> PollOutcome:
    """Classify a ``COMPLETED`` status body.

    RunPod reports a handler that returned an ``error`` key as FAILED,
    so a COMPLETED job did its work. What is left to check is that the
    output has the shape we can read.

    :param body: The parsed ``/status`` response.
    :param job_id: RunPod's job id.
    :param label: What to call this job in the log line.
    :returns: A ``COMPLETED`` outcome, or a terminal failure when the
        output is unreadable.
    :rtype: PollOutcome
    """
    from scanning.models import JobStatus

    output = body.get("output")
    if not isinstance(output, dict):
        return PollOutcome(
            status=JobStatus.FAILED,
            provider_status="COMPLETED",
            error_code="BAD_OUTPUT",
            error_message=(
                f"RunPod job {job_id} completed with a "
                f"{type(output).__name__} output, expected an object"
            ),
        )

    logger.info(
        "runpod %s job %s COMPLETED in %sms (worker %sms, %s page(s))",
        label or "job",
        job_id,
        body.get("executionTime", "?"),
        output.get("duration_ms", "?"),
        output.get("page_count", "?"),
    )
    return PollOutcome(
        status=JobStatus.COMPLETED,
        provider_status="COMPLETED",
        output=output,
    )


def _failed_outcome(
    body: dict, job_id: str, provider_status: str
) -> PollOutcome:
    """Classify a ``FAILED`` / ``TIMED_OUT`` / ``CANCELLED`` body.

    The RunPod SDK (``rp_job.py::run_job``) pops ``error`` out of the
    handler's return dict and puts it at the top level of the result,
    so a handler returning ``{"error": ..., "error_code": ...}`` arrives
    as ``{"output": {"error_code": ...}, "error": ...}``. The code
    therefore survives inside ``output``, which is what separates a
    retryable worker fault from a bad input.

    An absent ``output`` means the failure happened outside the handler
    -- a RunPod platform error, a worker crash, an internal timeout.
    Those are infrastructure, so ``FAILED`` with no code is retryable.
    ``TIMED_OUT`` and ``CANCELLED`` are not: something decided this job
    should stop, and repeating it invites the same decision.

    :param body: The parsed ``/status`` response.
    :param job_id: RunPod's job id.
    :param provider_status: RunPod's own status string.
    :returns: A terminal outcome, marked retriable where another
        attempt could plausibly land differently.
    :rtype: PollOutcome
    """
    from scanning.models import JobStatus

    output = body.get("output")
    error_code = ""
    if isinstance(output, dict):
        error_code = str(output.get("error_code") or "")
    message = str(body.get("error") or "")

    if error_code:
        retriable = error_code in TRANSIENT_ERROR_CODES
    else:
        retriable = provider_status == "FAILED"

    return PollOutcome(
        status=JobStatus.FAILED,
        provider_status=provider_status,
        error_code=error_code or provider_status,
        error_message=(
            f"RunPod job {job_id} {provider_status}"
            + (f": {message}" if message else "")
        ),
        retriable=retriable,
    )


def cancel_job(base_url: str, headers: dict[str, str], job_id: str) -> None:
    """Stop paying for a job nobody will read the output of.

    Best effort, and it never raises: a cancel that fails must not
    abort the sweep that called it. Unlike doctor, this is real money
    -- a GPU worker bills for as long as it runs.

    :param base_url: From :func:`endpoint_config`.
    :param headers: From :func:`endpoint_config`.
    :param job_id: RunPod's job id. A blank id is a no-op.
    :return: None.
    """
    if not job_id:
        return
    try:
        requests.post(
            f"{base_url}/cancel/{job_id}",
            headers=headers,
            timeout=_HTTP_TIMEOUT,
        )
        logger.info("runpod job %s cancelled", job_id)
    except Exception:
        logger.warning("runpod job %s cancel failed", job_id, exc_info=True)
