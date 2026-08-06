"""RunPod Serverless client for offloading GPU steps.

Two public entry points: :func:`detect` and :func:`analyze`. Both take
a local ``pdf_path`` and, when ``settings.RUNPOD_ENABLED`` is true,
ship the work out to a RunPod Serverless endpoint; otherwise they
fall back to calling blackletter in-process so dev / tests / staging
keep working without RunPod credentials.

Remote mode flow:

1. Upload the PDF to S3 under the scan's processing prefix if it's
   not already there (idempotent via ``head_object``).
2. Generate a presigned GET URL for the input, and a presigned PUT
   URL for the result key the worker will write to.
3. ``POST /run`` to the RunPod endpoint with
   ``{"input": {"action": ..., "pdf_url": ..., "result_url": ...}}``.
4. Poll ``GET /status/{id}`` with exponential backoff until a
   terminal state (``COMPLETED`` / ``FAILED`` / ``TIMED_OUT`` /
   ``CANCELLED``). On timeout, ``POST /cancel/{id}`` so we stop
   paying.
5. Read the result object off S3, validate its envelope, and return
   the payload merged with the job's timing metadata.

Job status stays the authority for whether a job finished: we poll to
a terminal state, then pull the object. The one exception is a 404
from ``/status`` (the job aged out of RunPod's retention window):
there the key we hold in memory is checked with a ``head_object``
before the run is thrown away and resubmitted.

Why the payload travels through S3 rather than inline in the job
response: RunPod caps a response at ~20 MB, which is the reason we
can't return per-word bounding boxes, and it discards the response
entirely once the ~30 min retention window closes. An S3 object has
neither limit and outlives both.

Result keys are per scan and action rather than per run, so a re-run
overwrites its predecessor instead of leaving orphans behind. Nothing
is read without first checking the object was written after the
reading job was submitted, which is what keeps a reused key from
serving a previous attempt's output.

Inline results remain supported and are still what a worker returns
when ``result_url`` is absent from the input, which is the case in
dev / CI without AWS credentials and with worker images predating the
contract. Rolling the worker image back is therefore enough to revert
to inline delivery, with no daemon change.
"""

from __future__ import annotations

import json
import logging
import tempfile
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import boto3
import requests
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from django.conf import settings

if TYPE_CHECKING:
    from scanning.models import Scan

logger = logging.getLogger(__name__)


class RunpodError(RuntimeError):
    """Raised on terminal RunPod failure or exhausted retries."""


class RunpodTransientError(RunpodError):
    """Retriable RunPod failure.

    Callers in ``scanning/services.py`` catch this specifically and
    re-queue the scan to ``Status.QUEUED`` so the next daemon tick
    retries on a (hopefully) different worker. Contrast with the base
    ``RunpodError`` which signals a terminal failure worth marking
    the scan as ``ERROR``.
    """


# Error codes the handler emits that the client translates into a
# ``RunpodTransientError`` (retry) rather than ``RunpodError`` (fail).
#
# ``RESULT_UPLOAD_FAILED`` / ``RESULT_URL_EXPIRED``: the compute
# succeeded but the worker couldn't deliver it, so nothing was written
# and there is nothing to harvest. Resubmitting is the only recovery,
# and it mints a fresh presigned PUT -- which is what an expired
# signature needs, since the dead URL can never be reused.
#
# Deliberately absent: ``RESULT_UPLOAD_REJECTED``, which S3 returns for
# a misconfiguration (wrong signing region, a bucket policy demanding
# headers we don't send). Every retry re-runs the GPU work, pays for
# it, and fails identically, so that one stays terminal.
_TRANSIENT_ERROR_CODES = {
    "NO_GPU",
    "RESULT_UPLOAD_FAILED",
    "RESULT_URL_EXPIRED",
}

# Version of the result envelope this client understands. Must match
# ``RESULT_SCHEMA_VERSION`` in ``scanning/runpod/handler.py``.
RESULT_SCHEMA_VERSION = 1

# Slack allowed between our clock and S3's when deciding whether a
# result object was written by the run that's reading it. Generous
# enough that a skewed clock never discards a good result, far short
# of the minutes between a job and its retry.
_RESULT_CLOCK_SKEW = timedelta(seconds=60)

# RunPod ``/run`` submit-time error codes that mean "the endpoint can't
# take work right now" rather than "this job is bad" -- e.g. the
# endpoint is scaled to zero (``max_workers=0``), which RunPod reports
# as HTTP 409 ``ENDPOINT_PAUSED``. Classified (along with any 409) as a
# ``RunpodTransientError`` so the scan re-queues instead of failing
# outright; the daemon's ``retry_count`` cap still escalates to
# ``ERROR_MAX_RETRIES`` if the endpoint stays down.
_TRANSIENT_SUBMIT_CODES = {"ENDPOINT_PAUSED"}


# Friendly labels for progress-callback strings shown in the UI. The
# raw action names (``detect`` / ``analyze``) are what the client
# sends on the wire; the labels here are what scanners see.
_ACTION_LABELS = {
    "detect": "YOLO (detect)",
    "analyze": "OCR (analyze)",
}

# Input fields holding a presigned URL. Masked before logging.
_SIGNED_URL_FIELDS = ("pdf_url", "result_url")


def _redact_urls(body: dict[str, Any]) -> dict[str, Any]:
    """Return a shallow copy of the RunPod submit body with any
    presigned URL under ``input`` replaced by ``***``.

    Presigned URLs grant time-limited access to a single S3 object:
    read for ``pdf_url``, **write** for ``result_url``. Neither should
    land in persistent logs (even at DEBUG), so we mask them before
    passing the body to ``logger``. ``result_key`` is left visible --
    it's the key alone, no capability attached, and it's the thing
    you need in a log line to find a job's output later.

    :param body: The ``{"input": {...}}`` dict about to be POSTed.
    :returns: A masked shallow copy safe to log.
    :rtype: dict
    """
    redacted = {**body}
    inp = redacted.get("input")
    if isinstance(inp, dict):
        masked = {field: "***" for field in _SIGNED_URL_FIELDS if field in inp}
        if masked:
            redacted["input"] = {**inp, **masked}
    return redacted


# ── Public API ──────────────────────────────────────────────────────
ProgressCallback = Callable[[int | None, int | None, str], None]


def detect(
    scan: Scan,
    pdf_path: str | Path,
    models: list[str] | None = None,
    confidence: float = 0.20,
    progress_callback: ProgressCallback | None = None,
) -> list[dict]:
    """Run YOLO detection on a PDF, remotely or in-process.

    :param scan: The Scan owning this job (used for S3 pathing and
        Sentry tagging on the worker side).
    :param pdf_path: Local filesystem path to the input PDF. In
        remote mode the file is uploaded to S3 under the scan's
        processing prefix if it's not already there.
    :param models: YOLO model sizes to run. Defaults to all three.
    :param confidence: Minimum confidence threshold.
    :param progress_callback: Optional
        ``callable(current, total, message)`` for progress updates.
        Remote mode emits ``(None, None, status)`` for a handful of
        coarse events (submit / queued / state). Local mode doesn't
        fire the callback (``blackletter.api.detect`` has no callback
        parameter; its progress is printed to stdout, where the
        caller can capture it if needed).
    :returns: Merged detection list (same shape as
        ``blackletter.api.detect``'s return value).
    :rtype: list[dict]
    :raises RunpodError: On terminal remote failure.
    """
    models = models or ["small", "medium", "large"]

    if not _remote_enabled():
        return _detect_local(pdf_path, models, confidence)

    pdf_url = _ensure_presigned_url(scan, pdf_path)
    result = _invoke(
        action="detect",
        scan=scan,
        payload={
            "pdf_url": pdf_url,
            "models": models,
            "confidence": confidence,
        },
        progress_callback=progress_callback,
    )
    return result["detections"]


def analyze(
    scan: Scan,
    pdf_path: str | Path,
    exp_start: int | None,
    exp_end: int | None,
    max_pages: int = 9999,
    num_workers: int | None = None,
    progress_callback: ProgressCallback | None = None,
) -> dict:
    """Run PaddleOCR + YOLO page-number analysis on a PDF.

    Always returns ``{"results": [...]}`` regardless of mode; the
    daemon recomputes ``seq_issues`` / ``duplicates`` /
    ``missing_pages`` from ``results`` via
    ``_rebuild_issues_from_results``, so the extra fields aren't
    needed over the wire.

    :param scan: The Scan owning this job.
    :param pdf_path: Local filesystem path to the input PDF.
    :param exp_start: Expected first page number (or ``None``).
    :param exp_end: Expected last page number (or ``None``).
    :param max_pages: Upper bound on pages to process.
    :param num_workers: ``multiprocessing.Pool`` size for local mode.
        Ignored in remote mode (the worker always runs with
        ``num_workers=1`` to avoid the paddle+fork segfault).
    :param progress_callback: Optional
        ``callable(current, total, message)``. In local mode the
        callback is passed through to blackletter and fires once per
        page; in remote mode only coarse ``(None, None, status)``
        events fire (submit / queued / state) because there's no
        per-page stream across the HTTP boundary.
    :returns: ``{"results": list[dict]}``.
    :rtype: dict
    :raises RunpodError: On terminal remote failure.
    """
    if not _remote_enabled():
        return _analyze_local(
            pdf_path,
            exp_start,
            exp_end,
            max_pages,
            num_workers=num_workers,
            progress_callback=progress_callback,
        )

    pdf_url = _ensure_presigned_url(scan, pdf_path)
    result = _invoke(
        action="analyze",
        scan=scan,
        payload={
            "pdf_url": pdf_url,
            "exp_start": exp_start,
            "exp_end": exp_end,
            "max_pages": max_pages,
        },
        progress_callback=progress_callback,
    )
    return {"results": result["results"]}


# ── Local fallback ──────────────────────────────────────────────────
def _remote_enabled() -> bool:
    """Return True if remote RunPod dispatch should be used.

    :returns: Whether the remote path is active.
    :rtype: bool
    """
    return bool(settings.RUNPOD_ENABLED)


def _detect_local(
    pdf_path: str | Path, models: list[str], confidence: float
) -> list[dict]:
    """In-process fallback for :func:`detect`.

    Uses a scratch ``TemporaryDirectory`` for blackletter's
    ``detections.json`` side-effect; the caller gets the return value
    only, so the tmp directory is disposed after.

    :param pdf_path: Local PDF path.
    :param models: YOLO model sizes.
    :param confidence: Confidence threshold.
    :returns: Detection list.
    :rtype: list[dict]
    """
    from blackletter.api import detect as bl_detect

    with tempfile.TemporaryDirectory(prefix="detect-local-") as tmp:
        return bl_detect(
            str(pdf_path), tmp, models=models, confidence=confidence
        )


def _analyze_local(
    pdf_path: str | Path,
    exp_start: int | None,
    exp_end: int | None,
    max_pages: int,
    num_workers: int | None,
    progress_callback: ProgressCallback | None,
) -> dict:
    """In-process fallback for :func:`analyze`.

    :param pdf_path: Local PDF path.
    :param exp_start: Expected first page number.
    :param exp_end: Expected last page number.
    :param max_pages: Upper bound on pages to process.
    :param num_workers: Pool size (passed through to blackletter).
    :param progress_callback: Per-page callback (passed through).
    :returns: ``{"results": list[dict]}``.
    :rtype: dict
    """
    from blackletter.analyze import analyze_pdf as bl_analyze_pdf

    kwargs: dict[str, Any] = {
        "exp_start": exp_start,
        "exp_end": exp_end,
        "max_pages": max_pages,
    }
    if num_workers is not None:
        kwargs["num_workers"] = num_workers
    if progress_callback is not None:
        kwargs["progress_callback"] = progress_callback

    result = bl_analyze_pdf(str(pdf_path), **kwargs)
    return {"results": result["results"]}


# ── Remote: S3 presigning ───────────────────────────────────────────
# Content type the worker PUTs its result with. Signed into the URL,
# so the two must agree exactly -- see :func:`_presign_result_put`.
_RESULT_CONTENT_TYPE = "application/json"


def _s3() -> Any:
    """Return an S3 client pinned to SigV4 and the bucket's region.

    Both halves matter, and neither is the ambient default:

    - **Signature version.** A bare ``boto3.client("s3")`` inherits
      whatever the local AWS config resolves to, and some environments
      still resolve to SigV2. That's invisible for a presigned GET (the
      worker sends no signed headers when downloading) but fatal for
      the result PUT: SigV2 folds ``Content-Type`` into the string to
      sign, so a URL signed without it and a request sent with it
      disagree -- ``SignatureDoesNotMatch``.
    - **Region.** SigV4 encodes the region into the credential scope,
      so signing for the wrong one is rejected outright with
      ``AuthorizationQueryParametersError``. Unset, boto3 assumes
      ``us-east-1``. SigV2 had no such field, which is why this only
      became load-bearing alongside the version pin.

    :returns: A boto3 S3 client.
    """
    return boto3.client(
        "s3",
        region_name=settings.AWS_S3_REGION_NAME,
        config=Config(signature_version="s3v4"),
    )


def _ensure_presigned_url(scan: Scan, pdf_path: str | Path) -> str:
    """Upload ``pdf_path`` to the scan's S3 processing prefix if
    missing, then return a presigned GET URL.

    Uses the PDF's basename as the S3 key suffix, so if the file is
    already under the scan's processing prefix (the common case for
    bitonal.pdf / *.original.pdf), the ``head_object`` check succeeds
    and no upload happens.

    :param scan: The Scan owning the file.
    :param pdf_path: Local filesystem path.
    :returns: Presigned GET URL.
    :rtype: str
    :raises FileNotFoundError: If the file isn't in S3 and isn't
        present locally.
    :raises RunpodError: If S3 credentials are missing.
    """
    from scanning import s3_sync
    from scanning.utils import has_s3_credentials

    if not has_s3_credentials():
        raise RunpodError(
            "RUNPOD_ENABLED is true but no AWS credentials are configured; "
            "cannot generate a presigned URL for the worker."
        )

    bucket = settings.AWS_PRIVATE_STORAGE_BUCKET_NAME
    key = f"{s3_sync.s3_processing_prefix(scan)}{Path(pdf_path).name}"
    s3 = _s3()

    # Only "the object isn't there yet" should trigger an upload. Any
    # other ClientError (bad credentials, wrong region, access denied,
    # bucket missing) means S3 is misconfigured and must surface, not
    # get swallowed by a fall-through to upload_file().
    try:
        s3.head_object(Bucket=bucket, Key=key)
    except ClientError as exc:
        err_code = exc.response.get("Error", {}).get("Code", "")
        if err_code not in ("404", "NoSuchKey"):
            raise
        local = Path(pdf_path)
        if not local.is_file():
            raise FileNotFoundError(
                f"{pdf_path} not present locally and missing from "
                f"s3://{bucket}/{key}; cannot run GPU step."
            ) from exc
        logger.info(
            "uploading %s to s3://%s/%s before presign",
            local.name,
            bucket,
            key,
        )
        s3.upload_file(str(local), bucket, key)

    ttl = int(settings.RUNPOD_PRESIGNED_TTL)
    return s3.generate_presigned_url(
        "get_object",
        Params={"Bucket": bucket, "Key": key},
        ExpiresIn=ttl,
    )


# ── Remote: result delivery via S3 ──────────────────────────────────
def _results_to_s3_enabled() -> bool:
    """Return True if the worker should PUT its result to S3.

    Credentials are the only question: without them there's nothing to
    presign with, so the worker has to answer inline. Remote mode
    already requires them for the input PDF's presigned GET, so in
    practice this is only ever false alongside ``RUNPOD_ENABLED=False``
    -- but the S3 path checks for itself rather than assuming.

    :returns: Whether to hand the worker a presigned PUT.
    :rtype: bool
    """
    from scanning.utils import has_s3_credentials

    return has_s3_credentials()


def _presign_result_put(scan: Scan, action: str) -> tuple[str, str]:
    """Return the result key for this scan + action and a PUT for it.

    The key is computed here, before submission, rather than discovered
    afterwards: a presigned PUT covers exactly one object and a prefix
    cannot be presigned, so it has to be known at submit time anyway.
    That also means the daemon can find the object later by name --
    ``head_object`` on a known key, not a search.

    Deliberately not run-scoped. One object per scan and action means a
    re-run overwrites its predecessor rather than accumulating orphans
    nobody will ever read. The staleness that invites (reading an old
    attempt's output) is handled by checking the object's write time
    against this run's submission, in :func:`_result_object_is_fresh`.

    The URL's TTL is ``RUNPOD_PRESIGNED_TTL`` (1 day by default), which
    has to outlive queue time plus execution; ``RUNPOD_REQUEST_TIMEOUT``
    (30 min) sits well inside it, so we cancel a wedged job long before
    its signature dies.

    ``ContentType`` is signed deliberately. The worker sends that header
    on the PUT, and whether S3 folds it into the signature depends on
    the signature version -- SigV2 always does, SigV4 only if it was
    signed. Signing it makes the two sides agree either way, and leaves
    the stored object correctly typed instead of octet-stream. It does
    mean the header and this constant must stay in lockstep, which is
    why the worker sends exactly ``_RESULT_CONTENT_TYPE``.

    :param scan: The Scan owning the job.
    :param action: Handler action / pipeline stage.
    :returns: ``(result_key, presigned_put_url)``.
    :rtype: tuple[str, str]
    """
    from scanning import s3_sync

    bucket = settings.AWS_PRIVATE_STORAGE_BUCKET_NAME
    key = s3_sync.s3_job_result_key(scan, action)
    url = _s3().generate_presigned_url(
        "put_object",
        Params={
            "Bucket": bucket,
            "Key": key,
            "ContentType": _RESULT_CONTENT_TYPE,
        },
        ExpiresIn=int(settings.RUNPOD_PRESIGNED_TTL),
    )
    return key, url


def _result_object_is_fresh(key: str, submitted_at: datetime) -> bool:
    """Return True if ``key`` holds an object *this* run wrote.

    Two questions, one ``head_object``, because they share an answer
    shape:

    - Is anything there? A single PUT is atomic, so a partial upload
      never becomes a gettable object: presence means the whole result
      is present.
    - Is it ours? Result keys are per scan and action, not per run, so
      the object at one can be a leftover from an earlier attempt.
      Anything written before we submitted this job is one of those and
      must not be read as this job's output.

    :param key: The result key.
    :param submitted_at: When this job was handed to RunPod (UTC).
    :returns: Whether a result from this run is present.
    :rtype: bool
    """
    bucket = settings.AWS_PRIVATE_STORAGE_BUCKET_NAME
    try:
        head = _s3().head_object(Bucket=bucket, Key=key)
    except ClientError as exc:
        err_code = exc.response.get("Error", {}).get("Code", "")
        if err_code in ("404", "NoSuchKey", "NotFound"):
            return False
        # Anything else (access denied, wrong region) is a real S3
        # problem: don't report it as "the worker produced nothing".
        raise

    last_modified = head.get("LastModified")
    if last_modified is None:
        # Real S3 always sends it. If something else doesn't, fall back
        # to plain presence rather than discarding a good result.
        return True
    return last_modified >= submitted_at - _RESULT_CLOCK_SKEW


def _result_survived_the_job(
    result_key: str | None, submitted_at: datetime | None
) -> bool:
    """Return True if a finished result is waiting despite a lost job.

    The probe :func:`_poll` runs when ``/status`` 404s. Deliberately
    swallows every S3 error instead of propagating: the caller's
    fallback (re-queue and resubmit) is always safe, whereas an
    exception raised here lands in ``_poll``'s request ``try`` and gets
    retried as if it were a status-poll blip -- a loop that can only
    spin to the deadline, since ``/status`` will keep 404ing.

    The 403-not-404 case is worth naming: ``head_object`` on a missing
    key answers ``403 AccessDenied`` rather than 404 when the caller
    lacks ``s3:ListBucket``. Swallowing keeps that IAM shape merely
    unhelpful rather than pathological.

    :param result_key: Key the worker was told to write to, if any.
    :param submitted_at: When this job was submitted (UTC).
    :returns: Whether this run's result is on S3.
    :rtype: bool
    """
    if not result_key or not submitted_at:
        return False
    try:
        return _result_object_is_fresh(result_key, submitted_at)
    except Exception:
        logger.warning(
            "could not check %s for a finished result; treating the job "
            "as lost and re-queueing",
            result_key,
            exc_info=True,
        )
        return False


def _fetch_result(key: str, submitted_at: datetime) -> dict:
    """Read and parse the JSON envelope the worker wrote.

    Heads the object first: "the job reported success but left nothing
    from this run behind" is a different (and more interesting) failure
    than a body that won't parse, and saying so plainly beats a raw
    botocore ``NoSuchKey``. Read straight into memory -- the payload is
    parsed JSON we hand to the caller, so it never touches the daemon's
    disk or the ``/tmp`` tree the cleanup command sweeps.

    :param key: The result key.
    :param submitted_at: When this job was submitted (UTC), used to
        reject an earlier attempt's leftover object.
    :returns: The parsed envelope.
    :rtype: dict
    :raises RunpodTransientError: If the object is missing, stale,
        unreadable or unparseable. Re-running the job is the only
        recovery, and the daemon's transient-retry cap still bounds
        that.
    """
    bucket = settings.AWS_PRIVATE_STORAGE_BUCKET_NAME
    try:
        fresh = _result_object_is_fresh(key, submitted_at)
    except (BotoCoreError, ClientError) as exc:
        # Throttling, expired instance credentials, a region blip: the
        # result may well be sitting there intact, so this must not
        # escape as a bare botocore error. Anything that isn't a
        # RunpodTransientError marks the whole scan ERROR.
        raise RunpodTransientError(
            f"could not check job result at s3://{bucket}/{key}: {exc}"
        ) from exc
    if not fresh:
        raise RunpodTransientError(
            f"job reported success but no result from this run at "
            f"s3://{bucket}/{key}"
        )
    try:
        obj = _s3().get_object(Bucket=bucket, Key=key)
        return json.loads(obj["Body"].read())
    except (BotoCoreError, ClientError, ValueError) as exc:
        # BotoCoreError covers the streaming failures a mid-read
        # connection drop raises (ResponseStreamingError, ReadTimeout),
        # which are not ClientErrors.
        raise RunpodTransientError(
            f"could not read job result from s3://{bucket}/{key}: {exc}"
        ) from exc


def _validate_envelope(
    envelope: Any, scan: Scan, action: str, key: str, job_id: str
) -> dict:
    """Check a result envelope describes the job we're consuming.

    The envelope is self-describing precisely so a file from another
    run, another action, or a future schema fails loudly here instead
    of being consumed as this job's result.

    Two classes of mismatch, deliberately classified differently:

    - ``action`` / ``scan_pk`` / a malformed envelope are **terminal**.
      They mean the contract is broken, and re-running would produce
      the same mismatch.
    - ``job_id`` and ``schema_version`` are **transient**. A foreign
      ``job_id`` means we're looking at an earlier attempt's object at
      this (deliberately reused) key, which a re-run replaces. A future
      ``schema_version`` means the worker image is deployed ahead of
      the daemon, which resolves itself when the daemon catches up --
      terminal there would ``ERROR`` every in-flight scan over a deploy
      ordering, each needing a manual re-queue.

    :param envelope: Parsed JSON from the result object.
    :param scan: The Scan the job belongs to.
    :param action: The action we submitted.
    :param key: The key it was read from (for the error message).
    :param job_id: The RunPod job we're consuming the result of.
    :returns: The validated payload dict.
    :rtype: dict
    :raises RunpodError: On a terminal mismatch.
    :raises RunpodTransientError: On a mismatch a re-run can fix.
    """

    def fail(reason: str) -> RunpodError:
        return RunpodError(f"invalid job result at {key}: {reason}")

    if not isinstance(envelope, dict):
        raise fail(f"expected a JSON object, got {type(envelope).__name__}")

    version = envelope.get("schema_version")
    if version != RESULT_SCHEMA_VERSION:
        raise RunpodTransientError(
            f"job result at {key} has schema_version {version!r}, expected "
            f"{RESULT_SCHEMA_VERSION} (worker image ahead of the daemon?)"
        )
    if envelope.get("action") != action:
        raise fail(f"action {envelope.get('action')!r}, expected {action!r}")
    if envelope.get("scan_pk") != scan.pk:
        raise fail(
            f"scan_pk {envelope.get('scan_pk')!r}, expected {scan.pk!r}"
        )

    # The authoritative "is this ours?" check. ``_result_object_is_fresh``
    # gets there first and more cheaply, but its clock comparison is
    # coarse: a scan re-queued on the daemon's 5 s tick resubmits well
    # inside the skew allowance, so a leftover object can look fresh.
    # The job id can't.
    envelope_job = envelope.get("job_id")
    if envelope_job and envelope_job != job_id:
        raise RunpodTransientError(
            f"job result at {key} was written by job {envelope_job!r}, not "
            f"{job_id!r}; it is an earlier attempt's output"
        )

    payload = envelope.get("payload")
    if not isinstance(payload, dict):
        raise fail("payload is missing or not an object")

    return payload


def _harvest(
    output: dict,
    scan: Scan,
    action: str,
    result_key: str | None,
    submitted_at: datetime,
    job_id: str,
) -> dict:
    """Resolve a job's output into the payload the caller expects.

    Two shapes arrive here. One carries ``result_key`` and no payload
    (the S3 path): read the object, validate it, and merge the payload
    over the job's timing metadata. The other carries the payload
    itself (the inline path, used when we sent no ``result_url`` or
    when an older worker image ignored it): pass it straight through.

    :param output: The handler's ``output`` dict.
    :param scan: The Scan owning the job.
    :param action: The action we submitted.
    :param result_key: The key we presigned, or ``None`` if we asked
        for an inline result.
    :param submitted_at: When this job was submitted (UTC).
    :param job_id: The RunPod job whose result we're consuming.
    :returns: Output dict guaranteed to carry the action's payload.
    :rtype: dict
    :raises RunpodError: If the worker named a key we didn't ask for,
        or the envelope fails validation.
    """
    key = output.get("result_key")
    if not key:
        if result_key:
            logger.info(
                "worker returned %s inline despite result_url (older "
                "image?); using the response payload",
                action,
            )
        return output

    if key != result_key:
        # The worker can only write where the signature lets it, so a
        # response naming a different object -- or naming one at all
        # when we asked for an inline result -- is describing something
        # we didn't ask for. Don't fetch it.
        raise RunpodError(
            f"worker reported result_key {key!r} but the job was "
            f"presigned for {result_key!r}"
        )

    payload = _validate_envelope(
        _fetch_result(key, submitted_at), scan, action, key, job_id
    )
    logger.info(
        "harvested %s result from s3 key=%s (%s bytes)",
        action,
        key,
        output.get("bytes", "?"),
    )
    return {**output, **payload}


# ── Remote: invocation + polling ────────────────────────────────────
def _invoke(
    action: str,
    scan: Scan,
    payload: dict[str, Any],
    progress_callback: ProgressCallback | None,
) -> dict:
    """Submit a RunPod job and poll until terminal state.

    :param action: ``"detect"`` or ``"analyze"``.
    :param scan: Scan owning the job (``scan.pk`` is passed through
        so the handler can tag Sentry events).
    :param payload: Remaining ``input`` fields (e.g. ``pdf_url``,
        action args). Merged with ``action`` and ``scan_pk``.
    :param progress_callback: Optional coarse progress emitter.
    :returns: The handler's ``output`` dict, with the payload merged
        in from S3 when the worker delivered it that way.
    :rtype: dict
    :raises RunpodError: On terminal failure, handler error, or
        exhausted retries.
    """
    endpoint = settings.RUNPOD_ENDPOINT_ID
    api_key = settings.RUNPOD_API_KEY
    if not endpoint or not api_key:
        raise RunpodError("RUNPOD_ENDPOINT_ID / RUNPOD_API_KEY not configured")

    base_url = f"https://api.runpod.ai/v2/{endpoint}"
    headers = {"Authorization": f"Bearer {api_key}"}
    job_input: dict[str, Any] = {
        "action": action,
        "scan_pk": scan.pk,
        **payload,
    }

    result_key: str | None = None
    if _results_to_s3_enabled():
        result_key, result_url = _presign_result_put(scan, action)
        job_input["result_key"] = result_key
        job_input["result_url"] = result_url

    body = {"input": job_input}

    deadline = time.monotonic() + int(settings.RUNPOD_REQUEST_TIMEOUT)
    max_retries = int(settings.RUNPOD_MAX_RETRIES)

    # Wall clock, unlike ``deadline``: it's compared against an S3
    # object's ``LastModified`` to tell this run's result from an
    # earlier attempt's. Taken before the submit so a slow /run call
    # can only make the window wider, never miss a real result.
    submitted_at = datetime.now(UTC)

    job_id = _submit(
        base_url, headers, body, action, max_retries, progress_callback
    )
    output = _poll(
        base_url,
        headers,
        job_id,
        action,
        deadline,
        progress_callback,
        result_key=result_key,
        submitted_at=submitted_at,
    )
    return _harvest(output, scan, action, result_key, submitted_at, job_id)


def _submit_error_detail(exc: Exception) -> tuple[int | None, str, str]:
    """Pull the HTTP status and body out of a failed ``/run`` request.

    RunPod error responses carry a JSON body such as
    ``{"status":409,"title":"Conflict","detail":"Endpoint is paused
    (max_workers=0)...","code":"ENDPOINT_PAUSED"}``. ``_submit`` logs
    that body (otherwise ``raise_for_status`` discards it) and uses the
    ``code`` / status to classify the failure.

    :param exc: The exception raised by ``requests`` / ``_submit``.
    :returns: ``(status_code, runpod_code, body_text)``. Transport-level
        failures (no response attached) return ``(None, "", "")``.
    :rtype: tuple[int | None, str, str]
    """
    resp = getattr(exc, "response", None)
    if resp is None:
        return None, "", ""
    text = resp.text or ""
    code = ""
    try:
        parsed = resp.json()
        if isinstance(parsed, dict):
            code = parsed.get("code") or ""
    except ValueError:
        pass
    return resp.status_code, code, text


def _submit(
    base_url: str,
    headers: dict[str, str],
    body: dict[str, Any],
    action: str,
    max_retries: int,
    progress_callback: ProgressCallback | None,
) -> str:
    """POST to ``/run`` with retry on transport errors.

    :returns: The RunPod job id.
    :rtype: str
    :raises RunpodTransientError: If the endpoint reports it cannot
        accept work (HTTP 409 / ``ENDPOINT_PAUSED``), so the scan
        re-queues rather than failing.
    :raises RunpodError: If all retries fail for any other reason or the
        response is malformed.
    """
    last_exc: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            if progress_callback:
                progress_callback(
                    None, None, f"Submitting {action} to RunPod..."
                )
            r = requests.post(
                f"{base_url}/run", headers=headers, json=body, timeout=30
            )
            r.raise_for_status()
            submit = r.json()
            job_id = submit.get("id")
            if not job_id:
                raise RunpodError(
                    f"RunPod /run returned no job id: {submit!r}"
                )
            logger.info(
                "runpod %s job %s submitted (input=%s)",
                action,
                job_id,
                _redact_urls(body).get("input"),
            )
            if progress_callback:
                label = _ACTION_LABELS.get(action, action)
                progress_callback(
                    None, None, f"{label}: queued — RunPod job {job_id}"
                )
            return job_id
        except Exception as exc:
            last_exc = exc
            status_code, err_code, text = _submit_error_detail(exc)
            body_note = (
                f" (HTTP {status_code} body: {text[:500]})" if text else ""
            )

            # A paused / 409 endpoint won't start accepting work within
            # the in-call backoff window, so don't burn the remaining
            # attempts: raise transient immediately and let the daemon
            # re-queue the scan on a later tick.
            if status_code == 409 or err_code in _TRANSIENT_SUBMIT_CODES:
                logger.warning(
                    "runpod submit failed: %s%s; endpoint not accepting "
                    "work, re-queueing",
                    exc,
                    body_note,
                )
                raise RunpodTransientError(
                    f"RunPod endpoint not accepting work (HTTP "
                    f"{status_code}): {exc}"
                ) from exc

            sleep_for = 2**attempt
            logger.warning(
                "runpod submit attempt %d/%d failed: %s%s; sleeping %ds",
                attempt + 1,
                max_retries + 1,
                exc,
                body_note,
                sleep_for,
            )
            if attempt < max_retries:
                time.sleep(sleep_for)

    raise RunpodError(
        f"failed to submit RunPod job after {max_retries + 1} attempts: {last_exc}"
    )


def _poll(
    base_url: str,
    headers: dict[str, str],
    job_id: str,
    action: str,
    deadline: float,
    progress_callback: ProgressCallback | None,
    result_key: str | None = None,
    submitted_at: datetime | None = None,
) -> dict:
    """Poll ``/status/{job_id}`` until terminal, with backoff.

    Poll cadence starts at 1 s, doubles each idle tick, capped at 15 s.
    Exceeding ``deadline`` triggers ``/cancel/{job_id}`` and raises.

    Every poll logs at DEBUG (enable with ``SCANNING_LOG_LEVEL=DEBUG``);
    status transitions log at INFO.

    :param result_key: Key the worker was told to PUT its result to, if
        any. Lets the 404 branch check S3 before giving up.
    :param submitted_at: When the job was submitted (UTC), used to tell
        this run's result from an earlier attempt's.
    :returns: The handler's ``output`` dict on COMPLETED, or a
        ``{"result_key": ...}`` stand-in when the job record is gone
        but its result object is on S3.
    :rtype: dict
    :raises RunpodError: On FAILED / TIMED_OUT / CANCELLED / deadline
        exceeded / malformed output.
    :raises RunpodTransientError: On HTTP 404 from ``/status`` with no
        result on S3 (the job aged out of RunPod's retention window or
        was discarded after internal retries; the inputs are still on
        S3 so a fresh submit will re-run the work).
    """
    # Happy-path cadence: 1 s, 2 s, 4 s, 8 s, 15 s, 15 s, ... Advances
    # on every successful poll that returns a non-terminal status.
    poll_sleep_s = 1.0
    # Error-retry cadence: independent of poll_sleep_s so a single
    # 5xx or network blip doesn't permanently slow down happy-path
    # polling after the underlying issue resolves.
    error_sleep_s = 1.0
    last_status: str | None = None
    label = _ACTION_LABELS.get(action, action)

    while True:
        if time.monotonic() > deadline:
            _cancel(base_url, headers, job_id)
            raise RunpodError(
                f"RunPod job {job_id} exceeded RUNPOD_REQUEST_TIMEOUT"
            )

        try:
            r = requests.get(
                f"{base_url}/status/{job_id}", headers=headers, timeout=30
            )
            # 404 from /status means the job is gone: either RunPod
            # discarded it after exhausting their internal retries, or
            # the result aged past the retention window (30 min async).
            #
            # The work itself may still have finished. When the worker
            # was given a presigned PUT, its output outlives the job
            # record, so check the key before throwing away a run we
            # already paid for. Only an object written after we
            # submitted counts: the key is per scan and action, so an
            # older one is a previous attempt's leftover.
            if r.status_code == 404:
                if _result_survived_the_job(result_key, submitted_at):
                    logger.info(
                        "runpod job %s is gone (HTTP 404) but its result "
                        "is on S3 (%s); harvesting instead of resubmitting",
                        job_id,
                        result_key,
                    )
                    return {"result_key": result_key}
                raise RunpodTransientError(
                    f"RunPod job {job_id} not found (HTTP 404 from "
                    "/status). Re-queueing so the next daemon tick "
                    "submits a fresh job."
                )
            r.raise_for_status()
            body = r.json()
        except RunpodError:
            # Don't swallow the 404-derived RunpodTransientError above.
            raise
        except Exception as exc:
            # Transient status-poll error (5xx, network blip, timeout):
            # keep trying until deadline. Uses its own backoff counter
            # so happy-path poll cadence is unaffected once the blip
            # resolves.
            logger.warning(
                "runpod status poll for %s failed: %s; retrying", job_id, exc
            )
            time.sleep(min(error_sleep_s, 10))
            error_sleep_s = min(error_sleep_s * 2, 15)
            continue

        # Status poll succeeded: reset the error backoff so the next
        # transient blip starts fresh rather than inheriting the
        # previous escalation.
        error_sleep_s = 1.0

        status = body.get("status")
        logger.debug("poll runpod job %s -> %s", job_id, status)

        if status == "COMPLETED":
            output = body.get("output")
            if not isinstance(output, dict):
                raise RunpodError(
                    f"RunPod job {job_id} returned non-dict output: {output!r}"
                )
            execution_ms = body.get("executionTime", "?")
            action_ms = output.get("duration_ms", "?")
            model_durations = output.get("model_durations_ms")
            detail_parts = [f"action={action_ms}ms"]
            if model_durations:
                detail_parts += [
                    f"{m}={ms}ms" for m, ms in model_durations.items()
                ]
            logger.info(
                "runpod %s job %s COMPLETED in %sms (%s)",
                action,
                job_id,
                execution_ms,
                ", ".join(detail_parts),
            )
            return output

        if status in ("FAILED", "TIMED_OUT", "CANCELLED"):
            err = body.get("error") or ""
            # The RunPod SDK (rp_job.py::run_job) pops ``error`` from
            # the handler's return dict and places it at the top level
            # of the result payload before POSTing to RunPod. For example,
            # a handler returning:
            #
            #   {"error": "bad input", "error_code": "BAD_INPUT", ...}
            #
            # becomes:
            #
            #   {"output": {"error_code": "BAD_INPUT", ...}, "error": "bad input"}
            #
            # RunPod marks this FAILED with ``error`` at the top level.
            # ``error_code`` survives inside ``output``, so we can still
            # distinguish transient errors (re-queue) from terminal ones
            # (mark scan ERROR).
            #
            # When ``output`` is absent the failure is a RunPod platform
            # error (e.g. "job timed out after 1 retries", worker crash).
            # Those are infrastructure problems, not handler logic failures,
            # so re-queue rather than permanently failing the scan.
            err_code = (body.get("output") or {}).get("error_code") or ""
            if err_code:
                exc_cls = (
                    RunpodTransientError
                    if err_code in _TRANSIENT_ERROR_CODES
                    else RunpodError
                )
            elif status == "FAILED":
                exc_cls = RunpodTransientError
            else:
                exc_cls = RunpodError
            raise exc_cls(
                f"RunPod job {job_id} {status}" + (f": {err}" if err else "")
            )

        # IN_QUEUE / IN_PROGRESS / unknown — keep polling.
        if status and status != last_status:
            logger.info("runpod job %s -> %s", job_id, status)
            if progress_callback:
                progress_callback(
                    None,
                    None,
                    f"{label}: {status} — RunPod job {job_id}",
                )
            last_status = status

        time.sleep(min(poll_sleep_s, 15))
        poll_sleep_s = min(poll_sleep_s * 2, 15)


def _cancel(base_url: str, headers: dict[str, str], job_id: str) -> None:
    """Best-effort cancel so we stop paying for a wedged job.

    :param base_url: RunPod endpoint base URL.
    :param headers: Authorization header dict.
    :param job_id: RunPod job id.
    """
    try:
        requests.post(
            f"{base_url}/cancel/{job_id}", headers=headers, timeout=15
        )
        logger.info("runpod job %s cancelled", job_id)
    except Exception:
        logger.warning("runpod job %s cancel failed", job_id, exc_info=True)
