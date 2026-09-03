"""Lifecycle of the external jobs a scan's shards are handed to.

Everything the daemon needs to decide what to submit, confirm, retry or
write off lives on :class:`~scanning.models.ExternalJob` rows, never in
a Python call stack. That is what makes an interrupted daemon
resumable: the next tick re-reads the rows and carries on.

Two providers, and they differ in *shape* rather than only in
transport:

- ``CONVERT`` on doctor (issue #176) is **synchronous**. The response is
  the completion signal. There is nothing to poll and nothing to cancel,
  and a lost response is recovered with an S3 HEAD on the result key.
- ``ANALYZE`` and ``DETECT`` on RunPod (issues #190 and #195) are
  **asynchronous**. Submitting returns a job id, ``GET /status``
  reports progress, and cancelling matters because a graphics
  processing unit (GPU) job bills while it runs.

Those two RunPod stages are two *engines*, and each is a separate
serverless endpoint with its own image, its own GPU class and its own
worker pool. What differs is small and it is tabulated once, in
:class:`RunpodEngine`: an endpoint id, three caps and a payload
builder. Everything else about them is shared.

That difference is carried by plain branches on ``job.provider``, not by
a provider abstraction. The deliberate trade: about 600 of the lines
here are provider-agnostic and stay in one place, and only the submit
call and the in-flight check fork. **Do not answer a third provider by
copying a wave or a sweep** -- Mistral is the point to promote these
branches to a real interface, because it changes the shape again (it
runs at the opinion-level ``EXTRACT`` stage with no shard fan-out, it
has rate limits rather than a worker pool, and it has no presigned PUT
at all). Until then, branching keeps the shared lines shared.

Four properties are load-bearing and easy to break:

- **Every write is a compare-and-swap** (:func:`_write`), so no lock is
  held across an HTTP call. The other writer is the web process, not a
  second daemon: the admin re-queue, the admin scan deletion and the
  two start buttons all call into this module from a request, and the
  loser's update simply matches nothing.
- **A resubmission bumps ``attempt``**, re-addressing the result
  object. Doctor finishes a conversion after we stop listening, and
  RunPod's worker PUTs whether or not we are still reading, so an
  abandoned attempt uploads *after* we gave up on it; one key shared
  across attempts would let that late object be harvested as the new
  attempt's output.
- **Queue time is not run time.** A RunPod endpoint with a narrow worker
  pool queues the excess by design, so a row keeps the generous queue
  ceiling until ``/status`` first reports ``IN_PROGRESS``, and only then
  takes an execution budget. Charging queue time to a run budget would
  cancel the tail of every batch, resubmit it to the back of the same
  queue, and eventually fail a volume for being popular.
- **A job nothing will read must be cancelled.** Doctor's cancel is a
  no-op, so this was free to ignore while doctor was the only provider.
  A RunPod job left running bills for nothing, so every path that writes
  a row off calls :func:`_cancel_provider_job`.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import timedelta

from botocore.exceptions import BotoCoreError, ClientError
from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from scanning import doctor_client, runpod_client, s3_sync
from scanning.models import (
    DEAD_JOB_STATUSES,
    IN_FLIGHT_JOB_STATUSES,
    OPEN_JOB_STATUSES,
    ExternalJob,
    JobEngine,
    JobProvider,
    JobStage,
    JobStatus,
)

logger = logging.getLogger(__name__)


@dataclass
class SubmitSummary:
    """What one submit tick did.

    :ivar submitted: Handed to a provider and accepted.
    :ivar failed: Written off (terminal, or out of attempts).
    :ivar retried: Sent back to PENDING for another attempt.
    :ivar deferred: Returned to PENDING with no attempt spent, because
        the endpoint said it cannot take work right now.
    :ivar unanswered: Left in flight, for the confirm pass to judge.
    :ivar skipped: Claimed by another writer mid-tick.
    """

    submitted: int = 0
    failed: int = 0
    retried: int = 0
    deferred: int = 0
    unanswered: int = 0
    skipped: int = 0


@dataclass
class SweepSummary:
    """What one confirm tick did.

    :ivar completed: In-flight rows whose provider reported them done.
    :ivar retried: Past deadline, or failed retryably.
    :ivar failed: Out of attempts, or stranded past the queue ceiling.
    :ivar pending: Still waiting, inside their deadline.
    :ivar errors: Rows whose check itself failed (an S3 problem).
    """

    completed: int = 0
    retried: int = 0
    failed: int = 0
    pending: int = 0
    errors: int = 0


# ── per-row provider knobs ──────────────────────────────────────────
# One `if` each, deliberately. Something has to map a row to its own
# limits whether or not a provider abstraction exists, and these are
# that map. Add a branch per provider; do not fan them out into the
# call sites.
def _is_runpod(job: ExternalJob) -> bool:
    """Return whether this row runs on RunPod.

    :param job: The row to classify.
    :returns: True for a RunPod row.
    :rtype: bool
    """
    return job.provider == JobProvider.RUNPOD


# ── per-engine RunPod knobs ─────────────────────────────────────────
# One provider, several engines, and each engine is its own serverless
# endpoint with its own image, its own graphics processing unit (GPU)
# class and its own worker pool. So the endpoint, the caps and the
# payload are per engine, while everything else here stays per provider.
#
# This is a table, not a provider layer. The module docstring says why a
# third *provider* must promote the branches rather than copy a wave;
# a second engine on RunPod is a smaller thing -- a payload builder, an
# endpoint id and a cap -- and this is where those three live.
class UnknownRunpodEngine(runpod_client.RunpodError):
    """A RunPod row names an engine this module cannot serve.

    A subclass of ``RunpodError`` on purpose: the poll and the cancel
    already treat that as "not the job's fault, say so once and move
    on", which is what an unservable row needs. Only the two
    ``ensure_*_jobs`` wrappers create rows, so this is an internal
    fault, never an operator's.
    """


@dataclass(frozen=True)
class RunpodEngine:
    """What one RunPod engine needs that the others do not.

    Settings are held by **name** and read with ``getattr`` at call
    time. A value captured when this table is built would ignore
    ``override_settings``, which every test in this area uses.

    :ivar engine: The :class:`~scanning.models.JobEngine` value.
    :ivar stage: The :class:`~scanning.models.JobStage` it runs at.
    :ivar endpoint_setting: Name of the setting holding its endpoint id.
    :ivar concurrency_setting: Name of the setting capping one wave.
    :ivar attempts_setting: Name of the setting capping its attempts.
    :ivar seconds_per_page_setting: Name of the setting adding a
        per-page allowance to a *running* job's budget.
    :ivar is_enabled: Whether this engine may be dispatched at all.
    :ivar build_payload: Builds the ``input`` dict for one shard.
    :ivar label: What a log line calls this engine.
    """

    engine: str
    stage: str
    endpoint_setting: str
    concurrency_setting: str
    attempts_setting: str
    seconds_per_page_setting: str
    is_enabled: Callable[[], bool]
    build_payload: Callable[[ExternalJob, str, str], dict]
    label: str


def _runpod_engines() -> dict[str, RunpodEngine]:
    """Return every RunPod engine this deploy knows, keyed by engine.

    Rebuilt on each call, and deliberately not cached: the two entries
    read their functions off the engine modules at build time, so a test
    that patches ``dots_mocr.enabled`` reaches this table too.

    The imports are inside the function because both engine modules
    import this one.

    :returns: The engine table.
    :rtype: dict[str, RunpodEngine]
    """
    from scanning import dots_mocr, yolo

    return {
        JobEngine.DOTS_MOCR: RunpodEngine(
            engine=JobEngine.DOTS_MOCR,
            stage=JobStage.ANALYZE,
            endpoint_setting="RUNPOD_DOTSMOCR_ENDPOINT_ID",
            concurrency_setting="DOTS_MOCR_MAX_CONCURRENCY",
            attempts_setting="DOTS_MOCR_MAX_ATTEMPTS",
            seconds_per_page_setting="DOTS_MOCR_SECONDS_PER_PAGE",
            is_enabled=dots_mocr.enabled,
            build_payload=dots_mocr.build_payload,
            label="dots.mocr",
        ),
        JobEngine.BLACKLETTER: RunpodEngine(
            engine=JobEngine.BLACKLETTER,
            stage=JobStage.DETECT,
            endpoint_setting="RUNPOD_YOLO_ENDPOINT_ID",
            concurrency_setting="YOLO_MAX_CONCURRENCY",
            attempts_setting="YOLO_MAX_ATTEMPTS",
            seconds_per_page_setting="YOLO_SECONDS_PER_PAGE",
            is_enabled=yolo.enabled,
            build_payload=yolo.build_payload,
            label="YOLO",
        ),
    }


def _runpod_engine(job: ExternalJob) -> RunpodEngine:
    """Return the table entry that serves this row.

    :param job: A RunPod row.
    :returns: Its engine's entry.
    :rtype: RunpodEngine
    :raises UnknownRunpodEngine: If the table has no entry for it.
    """
    spec = _runpod_engines().get(job.engine)
    if spec is None:
        raise UnknownRunpodEngine(
            f"job {job.pk} runs {job.engine} on RunPod, but that engine "
            "has no endpoint, no caps and no payload here"
        )
    return spec


def _engine_setting(job: ExternalJob, field: str):
    """Return one per-engine setting's current value for a row.

    :param job: A RunPod row.
    :param field: Which name field of the entry to read.
    :returns: The setting's value.
    """
    return getattr(settings, getattr(_runpod_engine(job), field))


def _max_attempts(job: ExternalJob) -> int:
    """Return how many submissions this row gets before it is failed.

    :param job: The row to look up.
    :returns: The attempt ceiling.
    :rtype: int
    """
    if _is_runpod(job):
        return int(_engine_setting(job, "attempts_setting"))
    return int(settings.DOCTOR_MAX_ATTEMPTS)


def _presigned_ttl(job: ExternalJob) -> int:
    """Return the lifetime of the URLs this row's worker is handed.

    :param job: The row to look up.
    :returns: Seconds.
    :rtype: int
    """
    if _is_runpod(job):
        return int(settings.RUNPOD_PRESIGNED_TTL)
    return int(settings.DOCTOR_PRESIGNED_TTL)


def _result_suffix(job: ExternalJob) -> str:
    """Return the extension of the object this row's worker writes.

    :param job: The row to look up.
    :returns: ``".pdf"`` for a conversion, ``".json"`` for a read.
    :rtype: str
    """
    return ".json" if _is_runpod(job) else ".pdf"


def _result_content_type(job: ExternalJob) -> str:
    """Return the content type the result PUT must be signed with.

    Signed into the presigned URL, so this and the header the worker
    sends must agree exactly. A mismatch is a 403 the worker reports as
    an expired signature.

    :param job: The row to look up.
    :returns: The content type.
    :rtype: str
    """
    if _is_runpod(job):
        return runpod_client.RESULT_CONTENT_TYPE
    return doctor_client.RESULT_CONTENT_TYPE


def _runpod_endpoint(job: ExternalJob) -> str:
    """Return the RunPod endpoint id serving this row's engine.

    Per engine, not per account: each engine is its own serverless
    endpoint with its own worker pool and its own scaling.

    :param job: A RunPod row.
    :returns: The endpoint id, or ``""`` when the engine has none.
    :rtype: str
    :raises UnknownRunpodEngine: If no table entry serves the row.
    """
    return _engine_setting(job, "endpoint_setting")


def _cancel_provider_job(job: ExternalJob) -> None:
    """Stop paying for a job whose output nobody will read.

    Called from every path that writes a row off, because a provider
    that has not finished keeps working otherwise. Doctor has no cancel
    endpoint, so its branch is a no-op -- the request is already over,
    and its conversion is CPU seconds rather than money.

    Best effort and never raises: this runs inside a sweep that must
    still judge every other row on the tick.

    :param job: The row being written off.
    :return: None.
    """
    if job.external_id:
        _cancel_job_id(job, job.external_id)


def _cancel_job_id(job: ExternalJob, job_id: str) -> None:
    """Cancel one provider job id on ``job``'s endpoint.

    Takes the id as an argument rather than reading the row, because the
    one case that most needs cancelling is a job whose id never reached
    the row: a submit that succeeded after a cancel took the row away.

    :param job: The row the job was submitted for; names the endpoint.
    :param job_id: The provider's job id.
    :return: None.
    """
    if not (_is_runpod(job) and job_id):
        return
    try:
        base_url, headers = runpod_client.endpoint_config(
            _runpod_endpoint(job)
        )
    except runpod_client.RunpodError:
        logger.warning(
            "cannot cancel job %s: its endpoint is not configured", job.pk
        )
        return
    runpod_client.cancel_job(base_url, headers, job_id)


# ── deadlines ───────────────────────────────────────────────────────
def queue_deadline(waiting_since):
    """Return how long a job may wait before a worker starts it.

    Queue time is not run time and must not be measured against the same
    budget. A RunPod endpoint is deliberately given a narrow worker pool
    -- that cap is what keeps the cost down -- so the tail of a fan-out
    sitting in its queue is the design working, not a wedged job. A
    run-sized timeout applied from submission would cancel it, resubmit
    it to the back of the same queue, and eventually fail a volume for
    being popular.

    A ceiling still exists, generously, so a job a provider has
    accepted and never starts cannot park its scan forever. It measures
    the **provider's** queue only, never ours. A row that waits in our
    own queue carries no clock at all: an admitted backlog may lawfully
    wait longer than any ceiling, and a clock that started at creation
    once expired 2000+ rows unsubmitted and sank 29 volumes to ERROR
    (issue #218).

    Stamped **once per attempt**, at the attempt's first claim
    (:func:`submit_deadline_fields`) -- the moment the row is handed to
    the provider. Nothing after that first claim moves it. A re-claim
    after a defer does not, and a defer does not, or an endpoint that
    is paused or saturated for good would push the ceiling out on every
    tick and hold a scan forever. A retry clears it, so the next
    attempt's first claim restarts the wait. Only the crossing into
    ``IN_PROGRESS`` replaces it, with the run budget.

    :param waiting_since: When the attempt was handed to the provider.
    :returns: The wall-clock time the row is written off at.
    """
    return waiting_since + timedelta(
        seconds=int(settings.DAEMON_JOB_MAX_QUEUE_SECONDS)
    )


def doctor_attempt_deadline(submitted_at):
    """Return the ceiling for one submitted conversion.

    Deliberately not derived from the page count: doctor enforces its
    own per-page and total budgets, so this bounds how long we wait for
    an *answer* that may already be lost, not the conversion itself.

    :param submitted_at: When the attempt was handed to doctor.
    :returns: The wall-clock time the attempt is written off at.
    """
    return submitted_at + timedelta(
        seconds=int(settings.DOCTOR_JOB_DEADLINE_SECONDS)
    )


def runpod_execution_deadline(job: ExternalJob, started_at):
    """Return how long a running RunPod job may run for.

    Measured from the moment the provider reported the job actually
    running, never from submission -- see :func:`queue_deadline`. A base
    timeout plus an allowance for the pages this shard covers, read off
    the row's own ``input_manifest`` rather than divided out of the
    volume, so a short tail shard is not given a full shard's budget.

    The per-page allowance is the engine's own: a detection pass and a
    full page read do not take the same time per page.

    :param job: The row that has started.
    :param started_at: When the provider reported it running.
    :returns: The wall-clock time the attempt is written off at.
    """
    pages = job.input_manifest.get("page_count") or 0
    return started_at + timedelta(
        seconds=int(settings.RUNPOD_REQUEST_TIMEOUT)
        + pages * float(_engine_setting(job, "seconds_per_page_setting"))
    )


def submit_deadline_fields(job: ExternalJob, submitted_at) -> dict:
    """Return the deadline field a claim should write, if any.

    Doctor takes its flat answer budget straight away, because its
    response *is* the completion: there is no queue to distinguish, and
    the clock that matters starts when the request goes out.

    A RunPod row takes the queue ceiling here, at the attempt's
    **first** claim -- the moment it leaves our queue for the
    provider's. A row that already carries one is written **nothing**:
    the only way back to a claim with a ceiling intact is a defer, and
    re-stamping there would restart the wait on every tick, so an
    endpoint paused for good would hold a scan forever. Only the
    crossing into ``IN_PROGRESS`` (:func:`_record_progress`) replaces
    the ceiling with a run budget.

    :param job: The row being submitted.
    :param submitted_at: Submission timestamp.
    :returns: Fields to merge into the claim's write.
    :rtype: dict
    """
    if _is_runpod(job):
        if job.deadline is not None:
            return {}
        return {"deadline": queue_deadline(submitted_at)}
    return {"deadline": doctor_attempt_deadline(submitted_at)}


# ── row writes ──────────────────────────────────────────────────────
def _write(job: ExternalJob, **fields) -> bool:
    """Compare-and-swap ``job``'s row on its current status.

    The status is both the state and the lock: matching on the one we
    last read means a writer that already moved the row makes this
    update match nothing, rather than the two taking turns overwriting
    each other. Nothing is locked across the HTTP call that produced the
    outcome, which is the point.

    The writer to guard against today is the web process: the dots.mocr
    start button, the admin re-queue and the admin scan deletion all
    call into this module from a request, and a daemon that wrote
    PENDING over their CANCELLED would run a shard nobody wants. The
    user cancel was a fourth such writer until #219 deleted it as
    unreachable, and whatever replaces it will be one again -- so the
    guard stays whether or not a cancel exists. One daemon runs, so
    daemon-against-daemon is not the case being handled -- but this is
    also what makes a second replica safe if one is ever deployed.

    On success the in-memory instance is updated too, so a caller can
    chain another :func:`_write` against the new status.

    :param job: The row to update; its current ``status`` is the guard.
    :param fields: Field values to write.
    :returns: True if this writer won the row.
    :rtype: bool
    """
    updated = ExternalJob.objects.filter(pk=job.pk, status=job.status).update(
        **fields
    )
    if not updated:
        logger.info(
            "job %s (%s/%s shard %s) moved out of %s under us; skipping",
            job.pk,
            job.stage,
            job.engine,
            job.shard_index,
            job.status,
        )
        return False
    for name, value in fields.items():
        setattr(job, name, value)
    return True


def _complete(
    job: ExternalJob,
    output: dict | None,
    now,
    confirmed_by: str = "",
) -> bool:
    """Mark a job COMPLETED, keeping the provider's summary.

    COMPLETED, not CONSUMED: the provider being finished is not us
    having applied the result, and calling the row done here is how a
    finished job's output gets dropped.

    :param job: The row to complete.
    :param output: The provider's JSON summary, or ``None`` for a job
        whose response was lost and whose object we found on S3 instead.
    :param now: Completion timestamp.
    :param confirmed_by: How we learned the job was done, when the
        caller knows better than ``output`` says. A RunPod job whose
        record has expired is recovered by probing the result key, and
        the poll synthesises a truthy ``output`` for it, so inferring
        this would record that recovery as a normal provider answer and
        hide from a reader that the job record was already gone.
    :returns: Whether the write landed.
    :rtype: bool
    """
    meta = dict(job.provider_meta or {})
    meta["output"] = output
    meta["confirmed_by"] = confirmed_by or (
        "response" if output else "s3_head"
    )
    written = _write(
        job,
        status=JobStatus.COMPLETED,
        completed_at=now,
        last_polled_at=now,
        error_code="",
        error_message="",
        provider_meta=meta,
    )
    if written:
        # Both halves of the wall clock in one line, so the timings this
        # stage is judged on can be read off the logs instead of
        # reconstructed from timestamps in SQL. Ours is what the shard
        # cost us end to end; the provider's ``duration_ms`` is the work
        # alone, so the gap between them is queue and transport.
        _log_completion(job, output, now)
        _log_failed_pages(job, output)
        _log_run_complete(job)
    return written


def _log_completion(job: ExternalJob, output: dict | None, now) -> None:
    """Log how long one shard took, and how much of that was the worker's.

    :param job: The row just completed.
    :param output: The provider's summary, when its response reached us.
    :param now: Completion timestamp.
    :return: None.
    """
    elapsed = (
        (now - job.submitted_at).total_seconds() if job.submitted_at else None
    )
    detail = ""
    if output:
        duration_ms = output.get("duration_ms")
        # Doctor reports ``pages``; the dots.mocr worker reports
        # ``page_count``. Same number, two spellings.
        pages = output.get("pages") or output.get("page_count")
        parts = []
        if isinstance(duration_ms, int | float):
            parts.append(f"worker {duration_ms / 1000:.1f}s")
        if pages:
            parts.append(f"{pages} page(s)")
        if parts:
            detail = f" ({', '.join(parts)})"
    logger.info(
        "%s/%s shard %d/%d of scan %s completed in %s%s, confirmed by %s",
        job.stage,
        job.engine,
        job.shard_index + 1,
        job.shard_count,
        job.scan_id,
        f"{elapsed:.1f}s" if elapsed is not None else "an unknown time",
        detail,
        job.provider_meta.get("confirmed_by", "?"),
    )


def _log_failed_pages(job: ExternalJob, output: dict | None) -> None:
    """Warn about pages a worker could not read, in volume numbering.

    A shard that produced most of its pages is a success, not a retry:
    the worker itself retries a page that gave no output, once, on a
    thresholded render (the two-rung ladder of issue #238), so a page
    still failed here is one two renders could not read. The
    page-number adapter (issue #149) reads it as ``detected=None`` and
    interpolates, and re-running 99 good pages to try a third time is
    poor value. But the gap has to be visible, and it has to name pages
    of the *volume* -- the worker counts from zero inside the shard it
    was given.

    The pages a retry rung saved, and the pages whose broken layout
    JSON the repair of issue #242 gave back, are logged at INFO, so the
    ladder's recovery rate and the repair's reach can be read off the
    logs. A page whose answer was not layout JSON and which nothing
    repaired (``filtered``: text but no cell, so no page number either)
    is a **WARNING**, like a failed page: issue #242 asks for it by
    name, because each such page is a new shape of the fault and the
    log line is what makes the next one visible. The numbers survive in
    ``provider_meta["output"]`` either way; ``_complete`` stores the
    whole summary.

    :param job: The row just completed.
    :param output: The provider's summary.
    :return: None.
    """
    output = output or {}
    from_page = job.input_manifest.get("from_page")

    def _where(pages: list) -> str:
        if isinstance(from_page, int):
            # Shard-local and 0-based on the wire, volume and 1-based
            # here.
            volume = [
                from_page + page + 1 for page in pages if isinstance(page, int)
            ]
            return f"volume page(s) {volume}"
        return f"shard page(s) {pages}"

    for field, verb, level in (
        ("recovered_pages", "recovered %d page(s) on a retry", logging.INFO),
        (
            "repaired_pages",
            "repaired the layout JSON of %d page(s)",
            logging.INFO,
        ),
        (
            "filtered_pages",
            "answered %d page(s) with layout JSON nothing could repair",
            logging.WARNING,
        ),
    ):
        pages = output.get(field)
        if pages and isinstance(pages, list):
            logger.log(
                level,
                "%s/%s shard %d/%d of scan %s " + verb + ": %s",
                job.stage,
                job.engine,
                job.shard_index + 1,
                job.shard_count,
                job.scan_id,
                len(pages),
                _where(pages),
            )
    failed = output.get("failed_pages")
    if not failed or not isinstance(failed, list):
        return
    logger.warning(
        "%s/%s shard %d/%d of scan %s could not read %d of %s page(s): %s",
        job.stage,
        job.engine,
        job.shard_index + 1,
        job.shard_count,
        job.scan_id,
        len(failed),
        job.input_manifest.get("page_count", "?"),
        _where(failed),
    )


def _log_run_complete(job: ExternalJob) -> None:
    """Log the stage total once, when the last shard of a run lands.

    Fires from :func:`_complete`, which is a compare-and-swap write, so
    exactly one caller can be the last to move a row. That is what makes
    this log once per run without a marker field.

    Only the timings are reported. Whether anything *applies* the run's
    results is the consumer's business: the bitonal stage merges from
    ``bitonal.finish_ready_scans``, and dots.mocr has no consumer yet
    (issue #190 stops at COMPLETED, and the merge follows in #149).

    :param job: The row just completed.
    :return: None.
    """
    # Not ``OPEN_JOB_STATUSES``: COMPLETED belongs to that set on
    # purpose (the provider is done, we have applied nothing), and a run
    # of COMPLETED rows is exactly the case this logs. What must be
    # empty is the work still to come.
    rows = live_run(job.scan_id, job.stage, job.engine)
    unfinished = {JobStatus.PENDING} | IN_FLIGHT_JOB_STATUSES
    if not rows or any(row.status in unfinished for row in rows):
        return
    started = min(
        (row.date_created for row in rows if row.date_created), default=None
    )
    if started is None:
        return
    elapsed = (timezone.now() - started).total_seconds()
    pages = sum(row.input_manifest.get("page_count") or 0 for row in rows)
    rate = f", {pages / elapsed:.1f} pages/s" if pages and elapsed > 0 else ""
    logger.info(
        "%s/%s run %d done for scan %s: %d shard(s), %d page(s) in %.1fs%s",
        job.stage,
        job.engine,
        job.run,
        job.scan_id,
        len(rows),
        pages,
        elapsed,
        rate,
    )


def _failure_location(job: ExternalJob, details: dict | None = None) -> str:
    """Describe where in the volume a shard's failure happened.

    A shard index alone sends a reader to S3 with the manifest to find
    out which pages it covers, so the row's own ``input_manifest``
    answers that here. Doctor's ``page_number`` (its issue #245) narrows
    it from the shard to the page, and ``pixels`` says whether that page
    is merely enormous.

    Page numbers are reported 1-based, as a viewer shows them, while
    ``from_page``/``to_page`` are fitz indexes -- hence the ``+ 1``.

    :param job: The row that failed.
    :param details: Doctor's ``FAILURE_DETAIL_KEYS``, when it sent them.
    :returns: A bracketed clause, or ``""`` when the row knows nothing.
    :rtype: str
    """
    manifest = job.input_manifest or {}
    from_page = manifest.get("from_page")
    to_page = manifest.get("to_page")
    if not isinstance(from_page, int) or not isinstance(to_page, int):
        return ""

    parts = [f"volume pages {from_page + 1}-{to_page + 1}"]
    details = details or {}
    page_number = details.get("page_number")
    if isinstance(page_number, int) and page_number >= 1:
        parts.append(f"failed on volume page {from_page + page_number}")
    pixels = details.get("pixels")
    # A raster size is meaningful only against the resolution it was
    # rendered at, and that is doctor's conversion parameter.
    if isinstance(pixels, int) and not _is_runpod(job):
        parts.append(f"{pixels} pixel(s) at {settings.DOCTOR_BITONAL_DPI} dpi")
    return f" [{'; '.join(parts)}]"


def _fail(
    job: ExternalJob,
    error_code: str,
    message: str,
    details: dict | None = None,
) -> bool:
    """Write a job off for good, and stop paying for it.

    :param job: The row to fail.
    :param error_code: Provider (or local) error code.
    :param message: Human-readable detail, truncated.
    :param details: Doctor's per-page failure fields, when it sent them.
    :returns: Whether the write landed.
    :rtype: bool
    """
    location = _failure_location(job, details)
    logger.error(
        "job %s (scan %s %s/%s shard %s) failed after %d attempt(s): %s %s%s",
        job.pk,
        job.scan_id,
        job.stage,
        job.engine,
        job.shard_index,
        job.attempt,
        error_code,
        message[:200],
        location,
    )
    written = _write(
        job,
        status=JobStatus.FAILED,
        error_code=error_code[:64],
        error_message=f"{message}{location}"[:2000],
    )
    if written:
        _cancel_provider_job(job)
    return written


def _defer(job: ExternalJob, error_code: str, message: str, now) -> str:
    """Return a job to PENDING without spending an attempt.

    For the one answer that says nothing about the job: the endpoint is
    not accepting work. Burning a row's retry budget there would fail a
    volume for being submitted while an endpoint was scaled to zero.

    **The deadline is left exactly as it was.** The first claim stamped
    it when it handed the row to the provider, and the endpoint
    declining is that wait going on, not a new one. Clearing or
    re-stamping it here would push the ceiling out on every tick, and
    an endpoint paused for good would hold a scan forever instead of
    failing it -- which is the one outcome the ceiling is there to
    prevent.

    :param job: The row to hand back.
    :param error_code: Why it was not sent.
    :param message: Human-readable detail.
    :param now: Current time. Unused, and kept so every outcome helper
        reads the same at its call site.
    :returns: ``"deferred"`` or ``"skipped"``.
    :rtype: str
    """
    logger.info(
        "job %s (scan %s shard %s) not sent (%s); leaving it pending",
        job.pk,
        job.scan_id,
        job.shard_index,
        error_code,
    )
    ok = _write(
        job,
        status=JobStatus.PENDING,
        result_key="",
        external_id="",
        submitted_at=None,
        error_code=error_code[:64],
        error_message=message[:2000],
    )
    return "deferred" if ok else "skipped"


def _retry_or_fail(
    job: ExternalJob,
    error_code: str,
    message: str,
    now,
    details: dict | None = None,
) -> str:
    """Send a job back for another attempt, or write it off.

    A retry mutates the row rather than inserting one, so
    :meth:`~scanning.models.ExternalJob.push_attempt` preserves the
    previous attempt first. Clearing ``result_key`` makes the next
    submit mint a fresh attempt-scoped one, which is also what makes an
    expired signature self-healing rather than fatal.

    The deadline is cleared, not re-stamped: a new attempt is a new
    wait, and its ceiling is stamped when the attempt's first claim
    hands it to the provider. A row carrying the previous attempt's
    deadline would be swept straight back out by :func:`sweep_jobs`,
    which writes off any PENDING row already past its queue ceiling.

    The previous attempt is cancelled on the way out. It may still be
    running -- a deadline write-off is exactly the case where it is --
    and two attempts of one shard running at once is paid twice.

    :param job: The row to retry.
    :param error_code: Why the previous attempt did not stick.
    :param message: Human-readable detail.
    :param now: Current time.
    :param details: Doctor's per-page failure fields, when it sent them.
    :returns: ``"retried"``, ``"failed"``, or ``"skipped"``.
    :rtype: str
    """
    if job.attempt >= _max_attempts(job):
        failed = _fail(job, error_code, message, details)
        return "failed" if failed else "skipped"

    logger.warning(
        "job %s (scan %s shard %s) attempt %d failed (%s); retrying",
        job.pk,
        job.scan_id,
        job.shard_index,
        job.attempt,
        error_code,
    )
    _cancel_provider_job(job)
    job.push_attempt(save=False)
    ok = _write(
        job,
        status=JobStatus.PENDING,
        attempt=job.attempt + 1,
        retry_count=job.retry_count + 1,
        result_key="",
        external_id="",
        submitted_at=None,
        completed_at=None,
        deadline=None,
        error_code=error_code[:64],
        error_message=message[:2000],
        provider_meta=job.provider_meta,
    )
    return "retried" if ok else "skipped"


# ── creating the work ───────────────────────────────────────────────
def _shard_specs(scan, manifest: dict) -> list[tuple[str, dict]]:
    """Describe the work today's shard set asks for, in page order.

    One ``(input_key, identity)`` pair per shard. The identity is what
    the row stores in ``input_manifest`` -- which pages of which volume
    the shard holds -- so a row can be checked against a later manifest.

    ``size_bytes`` is part of the identity: pages alone cannot tell a
    re-uploaded original with the same page count from the one a prior
    result was computed on, and byte size plus page count is exactly
    the fingerprint the shard manifest itself trusts. Rows written
    before this field existed match leniently in
    :func:`_still_describes` and never in :func:`_reusable_results`.

    An apply manifest (#224) names each shard's ``key`` itself, under
    ``jobs/apply/pages/``, and carries the ``edit_id`` the shard was
    built from: that is what keeps two edits' shards apart when both
    hold one page of the same size.

    :param scan: The scan the shards belong to.
    :param manifest: The shard manifest from :mod:`scanning.sharding`,
        or the apply's (:func:`scanning.apply.shard_manifest`).
    :returns: ``(key, identity)`` per shard, ordered by shard index.
    :rtype: list[tuple[str, dict]]
    """
    prefix = s3_sync.shards_prefix(scan)
    source_page_count = manifest["source"]["page_count"]
    specs = []
    for entry in sorted(manifest["shards"], key=lambda e: e["index"]):
        identity = {
            "name": entry["name"],
            "from_page": entry["from_page"],
            "to_page": entry["to_page"],
            "page_count": entry["page_count"],
            "size_bytes": entry["size_bytes"],
            "source_page_count": entry.get(
                "source_page_count", source_page_count
            ),
        }
        if "edit_id" in entry:
            identity["edit_id"] = entry["edit_id"]
        specs.append(
            (entry.get("key") or f"{prefix}{entry['name']}", identity)
        )
    return specs


def _identity_matches(stored: dict | None, identity: dict) -> bool:
    """Return whether a row's stored identity describes today's spec.

    Exact equality, with one lenient case: a row written before
    ``size_bytes`` joined the identity has every other field, and
    demanding the missing field would read every pre-deploy run as
    stale -- a re-queue would then re-pay work that is plainly the
    same. The carry path (:func:`_reusable_results`) does **not** get
    this leniency, because there the missing field is the proof.

    :param stored: The row's ``input_manifest``.
    :param identity: Today's identity for the same shard position.
    :returns: Whether they describe the same work.
    :rtype: bool
    """
    if stored == identity:
        return True
    legacy = {k: v for k, v in identity.items() if k != "size_bytes"}
    return stored == legacy


def _still_describes(
    live: list[ExternalJob], specs: list[tuple[str, dict]]
) -> bool:
    """Return whether an existing run still covers the work asked for.

    Compares the stored page ranges, not just the keys: shards are named
    by position (``0001.pdf``), so a re-cut volume with the same shard
    count produces identical keys over different pages. On keys alone
    this would degenerate to comparing counts, and the reused rows would
    process the new bytes while claiming the old ranges.

    :param live: The current run's rows, ordered by shard index.
    :param specs: What the work looks like today, from
        :func:`_shard_specs`.
    :returns: Whether the existing rows describe today's shard set.
    :rtype: bool
    """
    if len(live) != len(specs):
        return False
    return all(
        job.input_key == key
        and _identity_matches(job.input_manifest, identity)
        for job, (key, identity) in zip(live, specs, strict=True)
    )


def _is_reusable(live: list[ExternalJob]) -> bool:
    """Return whether an existing run can be picked back up as-is.

    Reusable while every row is still working or already applied. One
    dead row (failed, cancelled, expired) sinks the run: nothing will
    move it again, so it can neither finish nor be applied, and reusing
    it parks the caller behind work that is over. That is exactly what a
    cancel or an admin re-queue leaves behind -- the path that most
    needs a clean second attempt, not the corpse of the first.

    :param live: The current run's rows.
    :returns: Whether to reuse the run rather than start a new one.
    :rtype: bool
    """
    return not any(job.status in DEAD_JOB_STATUSES for job in live)


def check_result_envelope(
    scan, job: ExternalJob, envelope, action: str, error_cls: type
) -> dict:
    """Return an envelope's payload, or refuse the envelope.

    Shared by every stage that reads a worker's result object, because
    the envelope is the one thing the workers agree on: the same
    ``RESULT_SCHEMA_VERSION``, written by ``runpod_common``, for every
    image. The checks close delivery faults, not model quality -- the
    object at the row's key must be an envelope this reader
    understands, written for this scan by this attempt. An unknown
    ``schema_version`` usually means a worker deployed ahead of the
    daemon, so the message names both versions rather than guessing at
    the content.

    :param scan: The scan being read.
    :param job: The row whose result the envelope claims to be.
    :param envelope: The parsed JSON found at ``job.result_key``.
    :param action: The handler action the envelope must name.
    :param error_cls: The exception class to raise, so each stage
        reports its own failure type.
    :returns: ``envelope["payload"]``.
    :rtype: dict
    :raises error_cls: If the envelope is not one this attempt should
        have produced.
    """
    if not isinstance(envelope, dict) or "payload" not in envelope:
        raise error_cls(
            f"scan {scan.pk} shard {job.shard_index}: the object at "
            f"{job.result_key} is not a result envelope"
        )
    version = envelope.get("schema_version")
    if version != runpod_client.RESULT_SCHEMA_VERSION:
        raise error_cls(
            f"scan {scan.pk} shard {job.shard_index} answered result "
            f"schema {version}; this reader knows "
            f"{runpod_client.RESULT_SCHEMA_VERSION}"
        )
    for field, expected in (
        ("action", action),
        ("scan_pk", scan.pk),
        ("result_key", job.result_key),
    ):
        got = envelope.get(field)
        if got != expected:
            raise error_cls(
                f"scan {scan.pk} shard {job.shard_index} envelope has "
                f"{field}={got!r}, expected {expected!r}"
            )
    return envelope["payload"]


def live_run(
    scan, stage: str, engine: str, apply_run=None
) -> list[ExternalJob]:
    """Return a target's current-run rows for one engine, in page order.

    The live run is the rows at ``max(run)``: a re-run keeps the
    previous run as history, and reading those as live would report work
    nobody wants any more.

    The volume run by default. The rows of an apply run (#224) are a
    target of their own -- one-page shards of the pages a curator
    changed -- and they are read only through ``apply_run``; a volume
    reader that saw them would glue a one-page shard as the volume.

    :param scan: The scan, or its pk.
    :param stage: A :class:`~scanning.models.JobStage` value.
    :param engine: A :class:`~scanning.models.JobEngine` value.
    :param apply_run: The apply run whose rows are wanted, or None for
        the volume run.
    :returns: The live run's rows ordered by shard index, or an empty
        list when the engine has never run for this target.
    :rtype: list[ExternalJob]
    """
    rows = list(
        ExternalJob.objects.filter(
            scan=scan,
            stage=stage,
            engine=engine,
            opinion=None,
            apply_run=apply_run,
        ).order_by("-run", "shard_index")
    )
    if not rows:
        return []
    return [job for job in rows if job.run == rows[0].run]


def run_summary(scan, stage: str, engine: str, apply_run=None) -> dict | None:
    """Describe a scan's live run of one engine for the process page.

    Neither GPU stage writes a scan status while it works (issue #190),
    so the rows are the only place its progress lives, and this is how a
    viewer sees it. Written once for every engine: a second copy would
    drift, and the ``open`` count below is too subtle to duplicate.

    ``open`` is what the daemon still has to do -- rows waiting to be
    submitted or in flight -- and it is what a start button refuses a
    second press on. Deliberately **not** ``OPEN_JOB_STATUSES``, which
    includes ``COMPLETED`` because the provider finishing is not us
    having applied the result. On that definition a run holding one
    failed shard beside two finished ones would read as open forever,
    and the button would refuse the re-run such a run needs.

    ``label`` is the same counts as one readable phrase, because a
    template rendering ``statuses`` directly would print a Python dict.

    :param scan: The scan (or its pk) to describe.
    :param stage: A :class:`~scanning.models.JobStage` value.
    :param engine: A :class:`~scanning.models.JobEngine` value.
    :param apply_run: The apply run to describe instead of the volume
        run (#224).
    :returns: ``{"run", "total", "done", "open", "failed", "statuses",
        "label", "error_code", "error_message"}``, or ``None`` when the
        engine has never run for this target.
    :rtype: dict | None
    """
    rows = live_run(scan, stage, engine, apply_run)
    if not rows:
        return None

    statuses = _status_counts(rows)
    failed = [
        row
        for row in rows
        if row.status in (JobStatus.FAILED, JobStatus.EXPIRED)
    ]
    done = sum(
        count
        for status, count in statuses.items()
        if status in (JobStatus.COMPLETED, JobStatus.CONSUMED)
    )
    unfinished = {JobStatus.PENDING} | IN_FLIGHT_JOB_STATUSES
    first_failure = failed[0] if failed else None
    return {
        "run": rows[0].run,
        "total": len(rows),
        "done": done,
        "open": sum(1 for row in rows if row.status in unfinished),
        "failed": len(failed),
        "statuses": statuses,
        "label": rows_label(rows),
        "error_code": first_failure.error_code if first_failure else "",
        "error_message": first_failure.error_message if first_failure else "",
    }


def _status_counts(rows: list[ExternalJob]) -> dict[str, int]:
    """Count the rows of one run by status.

    :param rows: The rows of one run.
    :returns: ``{status: count}``.
    :rtype: dict[str, int]
    """
    statuses: dict[str, int] = {}
    for row in rows:
        statuses[row.status] = statuses.get(row.status, 0) + 1
    return statuses


def rows_label(rows: list[ExternalJob]) -> str:
    """Return the status counts of some rows as one readable phrase.

    ``"2 consumed, 1 failed"``: what :func:`run_summary` shows the
    process page, and what the glued-output index (#243) shows for
    every run, live or not. A template rendering the counts dict
    directly would print a Python dict.

    :param rows: The rows of one run.
    :returns: The counts, one per status, in status order.
    :rtype: str
    """
    return ", ".join(
        f"{count} {ExternalJob(status=status).get_status_display().lower()}"
        for status, count in sorted(_status_counts(rows).items())
    )


#: The per-shard page lists a dots.mocr summary carries (shard-local
#: indexes). The first two are holes to the page-number reader; the
#: third is the pages a retry rung saved, and the fourth the pages
#: whose layout JSON broke on one character and was repaired (#242).
PAGE_LIST_NAMES = (
    "failed_pages",
    "filtered_pages",
    "recovered_pages",
    "repaired_pages",
)


def page_lists(job: ExternalJob) -> dict[str, list]:
    """Return a row's stored page lists, empty where the summary has none.

    :param job: A completed or consumed row.
    :returns: ``{name: shard-local page indexes}`` for every name in
        :data:`PAGE_LIST_NAMES`.
    :rtype: dict[str, list]
    """
    output = (job.provider_meta or {}).get("output") or {}
    return {
        name: list(output.get(name))
        if isinstance(output.get(name), list)
        else []
        for name in PAGE_LIST_NAMES
    }


def has_unread_pages(job: ExternalJob) -> bool:
    """Return whether a completed row's worker left pages unread.

    Read off the summary ``_complete`` stores in
    ``provider_meta["output"]``: the dots.mocr worker reports
    ``failed_pages`` (no answer) and ``filtered_pages`` (an answer that
    was not layout JSON) there, both as shard-local indexes. Either is
    a hole to the page-number reader, which needs a cell to place a
    number, so either counts. A row with no summary -- a carried row, a
    doctor row -- has no holes to report.

    :param job: A completed or consumed row.
    :returns: Whether either list is non-empty.
    :rtype: bool
    """
    lists = page_lists(job)
    return bool(lists["failed_pages"] or lists["filtered_pages"])


def hole_is_stable(job: ExternalJob) -> bool:
    """Return whether a re-read of this shard already gave this answer.

    The dots.mocr worker decodes greedily on a fixed render, so the
    same shard read twice gives the same output. When the previous run
    read the same shard -- same key, same identity -- and its summary
    holds the same page lists, this row *is* that re-read, and a third
    read would pay for the answer a third time. The carry then keeps
    the row and the backfill leaves the volume alone (#238).

    Only the immediately previous run is consulted: that is the run
    this row's holes were meant to close. A row with no holes is never
    "stable" in this sense -- there is nothing to re-read.

    :param job: A completed or consumed row with unread pages.
    :returns: Whether the previous run's row for the same shard holds
        identical page lists.
    :rtype: bool
    """
    if not has_unread_pages(job):
        return False
    previous = (
        ExternalJob.objects.filter(
            scan_id=job.scan_id,
            stage=job.stage,
            engine=job.engine,
            opinion=None,
            input_key=job.input_key,
            input_manifest=job.input_manifest,
            run__lt=job.run,
            status__in=(JobStatus.COMPLETED, JobStatus.CONSUMED),
        )
        .order_by("-run", "-attempt")
        .first()
    )
    return previous is not None and page_lists(previous) == page_lists(job)


def _reusable_results(
    scan, stage: str, engine: str, specs: list[tuple[str, dict]]
) -> dict[int, ExternalJob]:
    """Map today's shard indexes to prior rows whose results still serve.

    A shard's work is addressed by its ``(input_key, input_manifest)``
    identity, so a prior ``COMPLETED`` or ``CONSUMED`` row with the
    same identity holds output for exactly the bytes today's spec asks
    for -- whatever run it belongs to. The newest such row wins, and
    one S3 HEAD confirms its result object is really there: the keys
    are attempt-scoped, so presence needs no freshness window.

    The match is strict, no legacy leniency: ``size_bytes`` is the only
    field that separates today's shard from an equally-paginated shard
    of a re-uploaded original, so a row that cannot prove it gets read
    again rather than trusted.

    The HEAD never raises out of here. The carry is an optimization,
    so an S3 blip must degrade to "not carried" -- the pre-carry cost,
    in money -- and not into a failure: in the pipeline it would mark
    the scan ERROR (an S3 fault is not a ``RunpodTransientError``, so
    no retry), and in the start button it would be a 500. A shard that
    cannot prove its result is read again, with a WARNING naming the
    fault.

    Only useful for an engine whose results are *kept* after the
    apply (dots.mocr keeps them for the future smart glue; the bitonal
    merge deletes them), which is why :func:`ensure_shard_jobs` takes
    it as an opt-in.

    :param scan: The scan the shards belong to.
    :param stage: A :class:`~scanning.models.JobStage` value.
    :param engine: A :class:`~scanning.models.JobEngine` value.
    :param specs: Today's work, from :func:`_shard_specs`.
    :returns: ``{shard_index: prior_row}`` for every reusable shard.
    :rtype: dict[int, ExternalJob]
    """
    if not s3_sync.s3_active():
        return {}

    def _identity_key(input_key: str, identity) -> str:
        return f"{input_key}\n{json.dumps(identity or {}, sort_keys=True)}"

    by_identity: dict[str, ExternalJob] = {}
    prior_rows = ExternalJob.objects.filter(
        scan=scan,
        stage=stage,
        engine=engine,
        opinion=None,
        status__in=(JobStatus.COMPLETED, JobStatus.CONSUMED),
    ).order_by("-run", "-attempt")
    for row in prior_rows:
        if not row.result_key:
            continue
        if has_unread_pages(row) and not hole_is_stable(row):
            # A result with a hole is not a result to carry (#238): the
            # new run exists to read the pages the old one could not,
            # so this shard is re-paid while its clean siblings ride
            # along for free. Unless the previous run already re-read
            # this shard and got the same holes: the worker is
            # deterministic, so a third read buys the same answer.
            continue
        by_identity.setdefault(
            _identity_key(row.input_key, row.input_manifest), row
        )
    if not by_identity:
        return {}

    reusable: dict[int, ExternalJob] = {}
    for index, (input_key, identity) in enumerate(specs):
        row = by_identity.get(_identity_key(input_key, identity))
        if row is None:
            continue
        try:
            if not s3_sync.object_exists(row.result_key):
                continue
        except (BotoCoreError, ClientError) as exc:
            logger.warning(
                "carry check for scan %s %s/%s shard %d could not "
                "confirm %s (%s); the shard will be read again",
                getattr(scan, "pk", scan),
                stage,
                engine,
                index,
                row.result_key,
                exc,
            )
            continue
        reusable[index] = row
    return reusable


def ensure_shard_jobs(
    scan,
    manifest: dict,
    *,
    stage: str,
    engine: str,
    provider: str,
    reuse_results: bool = False,
    force_new_run: bool = False,
    apply_run=None,
) -> list[ExternalJob]:
    """Return the live rows for one engine over ``scan``'s shards,
    creating them if the current run does not describe today's shard set.

    Idempotent, which is what makes every re-entry path safe: a scan
    whose daemon died after the rows were created finds them again
    instead of paying for the work twice, and a second press of a start
    button is a no-op rather than a second run. When the shard set has
    moved (a re-upload, a page edit), a new run starts and the previous
    run's rows and result objects stay addressable as history.

    With ``reuse_results`` a new run does not start from zero: a shard
    whose identity is unchanged and whose prior result object is still
    on S3 (:func:`_reusable_results`) enters the run as a ``COMPLETED``
    row pointing at that object, and only the rest are ``PENDING``. So
    a run replaced for one dead shard re-pays one shard, and a re-cut
    that moved a few page ranges re-pays only the shards that moved.
    Pass it only for an engine whose results outlive the apply.

    That idempotence has to survive two callers arriving at once, which
    it did not have to before issue #190: until the dots.mocr button
    landed, the only caller was the single-threaded daemon. Two staff
    presses racing would both read no rows, both compute the same
    ``next_run``, and both insert -- so the loser is caught on
    ``unique_volume_job_per_engine_run`` and re-reads instead of
    raising. The database constraint is the serializer, which is why no
    lock is taken and none is held across the read: a lock would have to
    be, since the reuse decision spans several queries.

    :param scan: The scan to process.
    :param manifest: The committed shard manifest.
    :param stage: A :class:`~scanning.models.JobStage` value.
    :param engine: A :class:`~scanning.models.JobEngine` value.
    :param provider: A :class:`~scanning.models.JobProvider` value.
    :param reuse_results: Carry prior results forward, as described
        above. Only for an engine whose results outlive the apply.
    :param force_new_run: Start a new run even when the live one still
        describes today's shard set and is reusable. For the backfill
        of a run that completed with unread pages (#238): the live run
        is whole and consumed, so nothing else would replace it, and
        with ``reuse_results`` the carry re-pays only the shards with a
        hole. A deliberate way to spend GPU money, which is why no tick
        passes it.
    :param apply_run: The apply run (#224) the rows work for, or None
        for the volume run. The rows carry it, the live-run read is
        scoped by it, and the run number still comes from the one
        sequence of :meth:`ExternalJob.next_run`, so the unique key
        holds unchanged.
    :returns: The live run's rows, ordered by shard index.
    :rtype: list[ExternalJob]
    """
    specs = _shard_specs(scan, manifest)

    existing = list(
        ExternalJob.objects.filter(
            scan=scan,
            stage=stage,
            engine=engine,
            opinion=None,
            apply_run=apply_run,
        ).order_by("-run", "shard_index")
    )
    if existing:
        live = [job for job in existing if job.run == existing[0].run]
        if (
            not force_new_run
            and _still_describes(live, specs)
            and _is_reusable(live)
        ):
            return live
        logger.info(
            "scan %s %s/%s run %d %s (%d shard(s), statuses %s); starting "
            "a new run",
            scan.pk,
            stage,
            engine,
            live[0].run,
            "is replaced on request"
            if force_new_run
            else "cannot be picked back up",
            len(live),
            sorted({job.status for job in live}),
        )

    run = ExternalJob.next_run(scan, stage, engine)
    now = timezone.now()
    reusable = (
        _reusable_results(scan, stage, engine, specs) if reuse_results else {}
    )
    rows = []
    for index, (key, identity) in enumerate(specs):
        row = ExternalJob(
            scan=scan,
            stage=stage,
            engine=engine,
            provider=provider,
            status=JobStatus.PENDING,
            run=run,
            shard_index=index,
            shard_count=len(specs),
            apply_run=apply_run,
            input_key=key,
            # Travels with the row, so the reuse check and any later
            # merge read what was actually processed rather than a
            # manifest that may since have changed.
            input_manifest=identity,
            # No deadline: a row in our own queue has no clock. The
            # queue ceiling is stamped at the attempt's first claim,
            # when the row is handed to the provider (issue #218).
        )
        prior = reusable.get(index)
        if prior is not None:
            # Born COMPLETED: the prior attempt's result object serves
            # this identical shard, so the daemon submits nothing and
            # the merge reads the old key. The blank ``external_id``
            # keeps every cancel path a no-op on the provider.
            row.status = JobStatus.COMPLETED
            row.result_key = prior.result_key
            row.attempt = prior.attempt
            row.completed_at = prior.completed_at or now
            row.provider_meta = {
                "carried_from": {"run": prior.run, "job": prior.pk}
            }
        rows.append(row)
    try:
        with transaction.atomic():
            created = ExternalJob.objects.bulk_create(rows)
    except IntegrityError:
        # Somebody else created this exact run between our read and our
        # insert. Their rows are the run, and ours were never written
        # (bulk_create is one statement inside one transaction), so
        # re-read and hand theirs back. This is what keeps two staff
        # presses a no-op rather than a 500 for whoever lost.
        rows = live_run(scan, stage, engine, apply_run)
        logger.info(
            "scan %s %s/%s run %d was created by another writer; "
            "reusing its %d row(s)",
            scan.pk,
            stage,
            engine,
            rows[0].run if rows else run,
            len(rows),
        )
        return rows

    logger.info(
        "created %d %s/%s job(s) for scan %s at run %d",
        len(created),
        stage,
        engine,
        scan.pk,
        run,
    )
    return created


def ensure_convert_jobs(scan, manifest: dict) -> list[ExternalJob]:
    """Return the live bitonal-conversion jobs for ``scan``.

    :param scan: The scan to convert.
    :param manifest: The committed shard manifest.
    :returns: The live run's rows, ordered by shard index.
    :rtype: list[ExternalJob]
    """
    return ensure_shard_jobs(
        scan,
        manifest,
        stage=JobStage.CONVERT,
        engine=JobEngine.BITONAL,
        provider=JobProvider.DOCTOR,
    )


# ── submitting ──────────────────────────────────────────────────────
def _claim(job: ExternalJob, now) -> tuple[str, tuple[str, str] | None]:
    """Mark one job SUBMITTED and return its presigned URL pair.

    The row moves *before* the URLs are even signed, deliberately: if
    this process dies in between, the row says work may be running and
    names the key to look for. The other order would leave a completed
    job with nothing pointing at it.

    :param job: A PENDING row.
    :param now: Submission timestamp.
    :returns: ``("claimed", (input_url, output_url))``, or an outcome
        label with no URLs -- ``"skipped"`` if another writer took the
        row, ``"retried"`` / ``"failed"`` if signing failed.
    :rtype: tuple[str, tuple[str, str] | None]
    """
    result_key = s3_sync.s3_job_attempt_key(job, suffix=_result_suffix(job))
    if not _write(
        job,
        status=JobStatus.SUBMITTED,
        result_key=result_key,
        submitted_at=now,
        error_code="",
        error_message="",
        **submit_deadline_fields(job, now),
    ):
        return "skipped", None

    try:
        ttl = _presigned_ttl(job)
        return "claimed", (
            s3_sync.presign_get(job.input_key, ttl),
            s3_sync.presign_put(result_key, _result_content_type(job), ttl),
        )
    except (BotoCoreError, ClientError) as exc:
        # Nothing was sent, so this is purely local: hand the row
        # back rather than fail a volume over an S3 blip.
        return _retry_or_fail(job, "PRESIGN_FAILED", str(exc), now), None


def _claim_wave(
    pending: list[ExternalJob], now, summary: SubmitSummary
) -> list[tuple[ExternalJob, tuple[str, str]]]:
    """Claim every row a wave will send, and sign its URLs.

    Shared by both providers: the claim, the presign and the accounting
    of what fell out are identical, and only the call that follows
    differs.

    :param pending: PENDING rows this tick will send.
    :param now: Submission timestamp.
    :param summary: Counts to update for rows that fell out.
    :returns: ``(job, (input_url, output_url))`` for each claimed row.
    :rtype: list[tuple[ExternalJob, tuple[str, str]]]
    """
    claimed = []
    for job in pending:
        outcome, urls = _claim(job, now)
        if urls is None:
            setattr(summary, outcome, getattr(summary, outcome) + 1)
            continue
        claimed.append((job, urls))
    return claimed


def _room_for(queryset, limit: int, label: str) -> int:
    """Return how many more jobs of one kind may be started.

    A wave, not a queue drain: the cap bounds the jobs running at once,
    **including ones an earlier tick started and never got an answer
    for**. Those are still work the provider is doing, so claiming a
    fresh full wave every tick while it is slow would grow our load on
    it without bound, exactly when it is struggling.

    :param queryset: This provider and stage's rows.
    :param limit: The concurrency ceiling.
    :param label: What to call the work in the log line.
    :returns: How many rows to claim, possibly zero.
    :rtype: int
    """
    in_flight = queryset.filter(status__in=IN_FLIGHT_JOB_STATUSES).count()
    room = limit - in_flight
    if room < 1:
        logger.info(
            "%d %s shard(s) already in flight at a cap of %d; submitting "
            "none this tick",
            in_flight,
            label,
            limit,
        )
        return 0
    return room


def _pending_slice(queryset, room: int) -> list[ExternalJob]:
    """Return the PENDING rows a wave will claim.

    :param queryset: This provider and stage's rows.
    :param room: How many rows the cap allows.
    :returns: Rows in creation order, at most ``room`` of them.
    :rtype: list[ExternalJob]
    """
    return list(
        queryset.filter(status=JobStatus.PENDING)
        .select_related("scan", "scan__reporter")
        .order_by("id")[:room]
    )


def _apply_doctor_outcome(
    job: ExternalJob, exc: Exception | None, output: dict | None, now
) -> str:
    """Record what came back from one conversion request.

    Everything turns on whether doctor *answered*. If it did -- even
    with an error -- it is finished with that request and will never
    upload for it, so a retryable code goes back at once. If it did not
    (a read timeout, a reset mid-conversion), the conversion is probably
    still running and will still PUT, so the row stays in flight for the
    confirm pass to judge by the result key. Resubmitting there pays for
    the shard twice.

    :param job: The SUBMITTED row.
    :param exc: The exception raised by the request, if any.
    :param output: Doctor's summary on success.
    :param now: Current time.
    :returns: One of ``"submitted"``, ``"retried"``, ``"failed"``,
        ``"unanswered"``, ``"skipped"``.
    :rtype: str
    """
    if exc is None:
        return "submitted" if _complete(job, output, now) else "skipped"

    details = getattr(exc, "details", None)
    if isinstance(exc, doctor_client.DoctorTransientError):
        if exc.error_code == doctor_client.UNANSWERED_ERROR_CODE:
            return _leave_in_flight(job, exc)
        return _retry_or_fail(job, exc.error_code, str(exc), now, details)

    if isinstance(exc, doctor_client.DoctorError):
        failed = _fail(job, exc.error_code, str(exc), details)
        return "failed" if failed else "skipped"

    logger.exception(
        "unexpected error submitting job %s", job.pk, exc_info=exc
    )
    return "failed" if _fail(job, "SUBMIT_FAILED", str(exc)) else "skipped"


def _apply_runpod_outcome(
    job: ExternalJob, exc: Exception | None, job_id: str | None, now
) -> str:
    """Record what came back from one ``POST /run``.

    Unlike doctor, a successful answer is not a completion: it is a job
    id, and the row stays in flight until a poll says otherwise. The
    three failure shapes differ in what they cost the row:

    - **The endpoint is busy.** Nothing is wrong with the job, so it
      goes back to PENDING with its attempt intact.
    - **No answer at all.** RunPod may hold the job and its worker may
      still PUT, so the row stays in flight with no id. The confirm pass
      then has only the result key to go on, which costs a full deadline
      -- the price of a lost answer from a paid provider.
    - **Answered with a refusal.** Nothing was accepted and nothing will
      be uploaded, so another attempt is safe at once.

    :param job: The SUBMITTED row.
    :param exc: The exception raised by the request, if any.
    :param job_id: RunPod's job id on success.
    :param now: Current time.
    :returns: One of ``"submitted"``, ``"deferred"``, ``"retried"``,
        ``"failed"``, ``"unanswered"``, ``"skipped"``.
    :rtype: str
    """
    if exc is None:
        # Still SUBMITTED: RunPod has it, and only a poll can say more.
        written = _write(job, external_id=job_id or "")
        if not written and job_id:
            # The row was cancelled while the request was in flight, so
            # the id has nowhere to live. Cancel from here or nothing
            # ever will: `abandon_open` ran while `external_id` was
            # still blank, so its own cancel was a no-op, and the sweep
            # only looks at rows that are still open. Left alone this
            # job runs to completion and bills for output nobody will
            # read.
            logger.warning(
                "job %s was claimed by another writer while RunPod job "
                "%s was being submitted; cancelling it",
                job.pk,
                job_id,
            )
            _cancel_job_id(job, job_id)
        return "submitted" if written else "skipped"

    if isinstance(exc, runpod_client.RunpodEndpointBusy):
        return _defer(job, exc.error_code, str(exc), now)

    if isinstance(exc, runpod_client.RunpodTransientError):
        if exc.error_code == runpod_client.UNANSWERED_ERROR_CODE:
            return _leave_in_flight(job, exc)
        return _retry_or_fail(job, exc.error_code, str(exc), now)

    if isinstance(exc, runpod_client.RunpodError):
        failed = _fail(job, exc.error_code, str(exc))
        return "failed" if failed else "skipped"

    logger.exception(
        "unexpected error submitting job %s", job.pk, exc_info=exc
    )
    return "failed" if _fail(job, "SUBMIT_FAILED", str(exc)) else "skipped"


def _leave_in_flight(job: ExternalJob, exc: Exception) -> str:
    """Keep a row in flight after a lost answer, and say why.

    The provider may be working on it, and its worker may still upload,
    so the confirm pass must judge this row by its result key rather
    than a resubmission paying for the work twice.

    :param job: The SUBMITTED row.
    :param exc: The transport failure.
    :returns: ``"unanswered"``.
    :rtype: str
    """
    logger.warning(
        "job %s (scan %s shard %s) got no answer from %s (%s); leaving it "
        "in flight for the confirm pass",
        job.pk,
        job.scan_id,
        job.shard_index,
        job.provider,
        exc,
    )
    _write(
        job,
        error_code=getattr(exc, "error_code", "")[:64],
        error_message=str(exc)[:2000],
    )
    return "unanswered"


def _submit_doctor_wave(summary: SubmitSummary, limit: int | None) -> None:
    """Send one wave of pending conversions to doctor.

    The threads make HTTP calls and nothing else. Every database write
    happens on this thread, before and after the fan-out, so no worker
    thread opens a connection Django would have to clean up.

    Rows are claimed only as far as the pool can start them at once,
    which keeps a SUBMITTED row genuinely in flight: claimed rows queued
    behind a saturated pool would leave a parked scan unable to tell
    "waiting on doctor" from "waiting on us".

    :param summary: Counts to update.
    :param limit: Concurrency override; defaults to
        ``settings.DOCTOR_MAX_CONCURRENCY``.
    :return: None.
    """
    if not doctor_client.enabled():
        return
    ours = ExternalJob.objects.filter(
        provider=JobProvider.DOCTOR, stage=JobStage.CONVERT
    )
    room = _room_for(
        ours, int(limit or settings.DOCTOR_MAX_CONCURRENCY), "bitonal"
    )
    if not room:
        return
    pending = _pending_slice(ours, room)
    if not pending:
        return

    now = timezone.now()
    claimed = _claim_wave(pending, now, summary)
    if not claimed:
        return

    logger.info("submitting %d bitonal shard(s) to doctor", len(claimed))
    outcomes = []
    with ThreadPoolExecutor(max_workers=len(claimed)) as pool:
        futures = [
            (
                job,
                pool.submit(
                    doctor_client.convert_bitonal, input_url, output_url
                ),
            )
            for job, (input_url, output_url) in claimed
        ]
        for job, future in futures:
            try:
                outcomes.append((job, None, future.result()))
            except Exception as exc:  # noqa: BLE001 - classified below
                outcomes.append((job, exc, None))

    applied_at = timezone.now()
    for job, exc, output in outcomes:
        result = _apply_doctor_outcome(job, exc, output, applied_at)
        setattr(summary, result, getattr(summary, result) + 1)


def _submit_runpod_wave(summary: SubmitSummary, limit: int | None) -> None:
    """Send one wave per RunPod engine.

    Each engine counts its own in-flight rows against its own cap,
    because each is a separate serverless endpoint that scales on its
    own. So a saturated dots.mocr queue cannot starve detection, and an
    engine switched off holds nothing up.

    :param summary: Counts to update.
    :param limit: Concurrency override applied to each engine.
    :return: None.
    """
    for spec in _runpod_engines().values():
        if spec.is_enabled():
            _submit_runpod_engine_wave(spec, summary, limit)


def _submit_runpod_engine_wave(
    spec: RunpodEngine, summary: SubmitSummary, limit: int | None
) -> None:
    """Send one wave of one engine's pending shards to RunPod.

    Serially, not from a pool: ``POST /run`` returns as soon as the job
    is queued, so a wave costs a second or two rather than doctor's
    25-45s per shard. A pool here would add threads for no wall clock.

    :param spec: The engine to send for.
    :param summary: Counts to update.
    :param limit: Concurrency override; defaults to the engine's own
        concurrency setting.
    :return: None.
    """
    ours = ExternalJob.objects.filter(
        provider=JobProvider.RUNPOD,
        stage=spec.stage,
        engine=spec.engine,
    )
    cap = limit or getattr(settings, spec.concurrency_setting)
    room = _room_for(ours, int(cap), spec.label)
    if not room:
        return
    pending = _pending_slice(ours, room)
    if not pending:
        return

    now = timezone.now()
    claimed = _claim_wave(pending, now, summary)
    if not claimed:
        return

    logger.info(
        "submitting %d %s shard(s) to RunPod", len(claimed), spec.label
    )
    for job, (input_url, output_url) in claimed:
        exc: Exception | None = None
        job_id = None
        try:
            base_url, headers = runpod_client.endpoint_config(
                _runpod_endpoint(job)
            )
            job_id = runpod_client.submit_job(
                base_url,
                headers,
                spec.build_payload(job, input_url, output_url),
                label=f"{job.engine} shard {job.shard_index + 1}",
            )
        except Exception as caught:  # noqa: BLE001 - classified below
            exc = caught
        result = _apply_runpod_outcome(job, exc, job_id, timezone.now())
        setattr(summary, result, getattr(summary, result) + 1)


def submit_pending(limit: int | None = None) -> SubmitSummary:
    """Submit one wave per provider and record what happened.

    Each provider counts its own in-flight rows against its own cap, so
    one saturated endpoint cannot starve another. Doctor's ceiling is
    its replica count; each RunPod engine's is that engine's own
    serverless endpoint, which scales on its own.

    **The non-blocking waves go first, and the blocking one goes last.**
    A RunPod submit is a fast ``POST /run`` that returns as soon as the
    job is queued, while a doctor submit holds the socket open for the
    whole conversion (~25-45s per shard) -- and the daemon's scheduler
    is serial (issue #156), so whatever runs first delays everything
    after it. Ordered the other way, a wave of bitonal shards would keep
    RunPod's queue empty for a minute or more at a time, wasting exactly
    the queue depth a narrow worker pool depends on. Reversed, the GPU
    work is already queueing at RunPod while doctor converts.

    :param limit: Concurrency override applied to **each** wave.
        Defaults to each provider's own setting.
    :returns: Counts of what the tick did, summed over the waves.
    :rtype: SubmitSummary
    """
    summary = SubmitSummary()
    if not s3_sync.s3_active():
        # Every provider reads its input through a presigned GET, so
        # without S3 the shards were never uploaded: every request would
        # 404 and burn a job's whole retry budget under a misleading
        # code. Say so once instead.
        if doctor_client.enabled() or _any_runpod_engine_enabled():
            logger.error(
                "an external stage is enabled but S3 is inactive (no "
                "credentials, or DEVELOPMENT without them): shards never "
                "reached the bucket workers fetch them from, so nothing "
                "can be submitted."
            )
        return summary

    # Non-blocking first. See the note above: the serial scheduler makes
    # this ordering the difference between RunPod queueing during a
    # conversion and waiting for one.
    _submit_runpod_wave(summary, limit)
    _submit_doctor_wave(summary, limit)
    return summary


def _any_runpod_engine_enabled() -> bool:
    """Return whether any RunPod engine would dispatch a job.

    :returns: Whether at least one engine is switched on.
    :rtype: bool
    """
    return any(spec.is_enabled() for spec in _runpod_engines().values())


# ── confirming ──────────────────────────────────────────────────────
def _sweep_doctor_job(job: ExternalJob, now, summary: SweepSummary) -> None:
    """Confirm one conversion against its result object.

    The only thing that can finish a job whose response we never saw --
    a killed daemon, a redeployed pod, an abandoned read. Doctor's
    single PUT is atomic and its key unique to this attempt, so the
    object's existence is a complete answer.

    An attempt past its deadline with nothing at its key is retried.
    That costs a full deadline per lost shard, and is inherent to a
    provider with no status endpoint.

    :param job: A SUBMITTED row.
    :param now: Comparison time.
    :param summary: Counts to update.
    :return: None.
    """
    try:
        present = bool(job.result_key) and s3_sync.object_exists(
            job.result_key
        )
    except (BotoCoreError, ClientError):
        # An S3 problem is ours, not the job's: retry next tick rather
        # than read it as "produced nothing" and burn an attempt.
        logger.warning(
            "could not check %s for job %s; leaving it in flight",
            job.result_key,
            job.pk,
            exc_info=True,
        )
        summary.errors += 1
        return

    if present:
        if _complete(job, None, now):
            summary.completed += 1
        return
    if job.is_overdue(now):
        result = _retry_or_fail(
            job,
            "DEADLINE_EXCEEDED",
            f"no result at {job.result_key} by {job.deadline}",
            now,
        )
        if result in ("retried", "failed"):
            setattr(summary, result, getattr(summary, result) + 1)
        return
    _write(job, last_polled_at=now)
    summary.pending += 1


def _sweep_runpod_job(job: ExternalJob, now, summary: SweepSummary) -> None:
    """Poll one RunPod job and apply what it said.

    Polled before the deadline is checked: a job that finished just
    inside its budget must be harvested rather than written off, and one
    tick either way is free.

    A row with no ``external_id`` is one whose submit never answered.
    There is nothing to poll, so it is judged the way a doctor row is:
    the result key, then the deadline.

    :param job: An in-flight row.
    :param now: Comparison time.
    :param summary: Counts to update.
    :return: None.
    """
    if not job.external_id:
        _sweep_doctor_job(job, now, summary)
        return

    try:
        base_url, headers = runpod_client.endpoint_config(
            _runpod_endpoint(job)
        )
    except runpod_client.RunpodError as exc:
        # The engine's endpoint was unset after the job went out. Not
        # the job's fault and not fixable by retrying it, so leave it
        # alone and say so once per tick.
        logger.warning("cannot poll job %s: %s", job.pk, exc)
        summary.errors += 1
        return

    outcome = runpod_client.poll_once(
        base_url,
        headers,
        job.external_id,
        label=f"{job.engine} shard {job.shard_index + 1}",
        result_key=job.result_key,
    )

    if outcome.status is None:
        # We learned nothing, which is not the same as learning it
        # failed. Fall through to the deadline check so a job whose
        # status endpoint is permanently unhappy still ends.
        _write(job, last_polled_at=now)
        summary.pending += 1
    elif outcome.status == JobStatus.COMPLETED:
        if _complete(job, outcome.output, now, outcome.confirmed_by):
            summary.completed += 1
        return
    elif outcome.status in DEAD_JOB_STATUSES:
        if outcome.retriable:
            result = _retry_or_fail(
                job, outcome.error_code, outcome.error_message, now
            )
        else:
            result = (
                "failed"
                if _fail(job, outcome.error_code, outcome.error_message)
                else "skipped"
            )
        if result in ("retried", "failed"):
            setattr(summary, result, getattr(summary, result) + 1)
        return
    else:
        _record_progress(job, outcome, now)
        summary.pending += 1

    if job.is_overdue(now):
        summary.pending -= 1
        result = _retry_or_fail(
            job,
            "DEADLINE_EXCEEDED",
            f"still {outcome.provider_status or job.status} at {job.deadline}",
            now,
        )
        if result in ("retried", "failed"):
            setattr(summary, result, getattr(summary, result) + 1)


def _record_progress(job: ExternalJob, outcome, now) -> None:
    """Store a still-running job's state, and start its run budget.

    Crossing into ``IN_PROGRESS`` is when the deadline stops being a
    queue ceiling and becomes an execution budget: until then the job
    was waiting behind the endpoint's worker cap, which is free and
    expected. Only the *crossing* re-stamps it, or every tick would push
    the deadline out and a wedged job would never end.

    :param job: The in-flight row.
    :param outcome: The poll result.
    :param now: Current time.
    :return: None.
    """
    fields = {"status": outcome.status, "last_polled_at": now}
    if (
        outcome.status == JobStatus.IN_PROGRESS
        and job.status != JobStatus.IN_PROGRESS
    ):
        fields["deadline"] = runpod_execution_deadline(job, now)
        logger.info(
            "job %s (scan %s shard %s) started; run budget until %s",
            job.pk,
            job.scan_id,
            job.shard_index,
            fields["deadline"],
        )
    _write(job, **fields)


def sweep_jobs(now=None) -> SweepSummary:
    """Confirm in-flight jobs and write off stranded rows.

    The recovery half of the loop. For doctor it is the *only* thing
    that can finish a job whose response we never saw. For RunPod it is
    the normal path: submitting only queues the work.

    :param now: Comparison time; defaults to ``timezone.now()``.
    :returns: Counts of what the tick did.
    :rtype: SweepSummary
    """
    summary = SweepSummary()
    now = now or timezone.now()

    in_flight = ExternalJob.objects.filter(
        status__in=IN_FLIGHT_JOB_STATUSES
    ).select_related("scan", "scan__reporter")
    for job in in_flight:
        if _is_runpod(job):
            _sweep_runpod_job(job, now, summary)
        else:
            _sweep_doctor_job(job, now, summary)

    # PENDING rows that carry an expired ceiling. Since the ceiling
    # moved to the first claim (issue #218), that means an endpoint
    # that kept declining the attempt for the whole ceiling -- plus
    # rows stamped at creation before the move. A row never claimed
    # carries no deadline and waits for its engine instead: our own
    # queue is not a fault, however long it is.
    stranded = ExternalJob.objects.filter(
        status=JobStatus.PENDING,
        deadline__isnull=False,
        deadline__lt=now,
    ).select_related("scan", "scan__reporter")
    for job in stranded:
        if _fail(
            job,
            "QUEUE_TIMEOUT",
            f"never submitted; waited past {job.deadline}",
        ):
            summary.failed += 1

    return summary


def abandon_open(
    scan,
    reason: str,
    stage: str | None = None,
    engine: str | None = None,
    statuses: frozenset = OPEN_JOB_STATUSES,
    apply_run=None,
) -> int:
    """Cancel a scan's open jobs, optionally for one engine only.

    For the paths that mean "start this over" -- an admin re-queue, a
    cancel. Open rows would let a stale attempt's outcome land on a scan
    that has moved on, and would make :func:`ensure_shard_jobs` reuse
    rows whose result objects someone else is still writing.

    **Scope it, and scope it by stage.** ``COMPLETED`` counts as open,
    because the provider finishing is not us having applied the result.
    Unscoped, a re-queue of a scan whose conversion needs restarting
    would also cancel a finished dots.mocr run, and the next press of
    that button would pay RunPod for output already sitting in S3. The
    stage is the right grain because it is what a restart redoes: a
    re-queue re-runs ``run_full_pipeline``, which owns ``CONVERT``
    whichever engine serves it, and owns no part of ``ANALYZE``.

    ``statuses`` narrows the default further for a caller whose stop is
    a *retry*, not an end. The pipeline's lost-claim hand-back keeps
    ``COMPLETED`` rows: those are paid results the carry
    (:func:`_reusable_results`) re-reads on the retry, and the claim is
    lost most often to the daemon's own shutdown -- every deploy that
    catches a pipeline mid-flight -- so cancelling them would re-pay
    whole volumes routinely, not rarely.

    Each cancelled row's provider job is cancelled too, so a GPU worker
    stops billing for output nobody will read.

    :param scan: The scan (or its pk) whose jobs to cancel.
    :param reason: Recorded on each row for the audit trail.
    :param stage: Limit to one :class:`~scanning.models.JobStage`.
    :param engine: Limit to one :class:`~scanning.models.JobEngine`.
    :param statuses: The statuses to treat as open. Narrow, never
        widen: ``CONSUMED`` and the dead statuses must stay terminal.
    :param apply_run: Limit to the rows of one apply run (#224). The
        reopen of a page review cancels the run it supersedes and
        nothing else; without this scope it would take the volume
        runs down with it. The default keeps the volume-run behaviour:
        an unscoped call still reaches every row, apply rows included,
        which is what the admin scan deletion wants.
    :returns: How many rows were cancelled.
    :rtype: int
    """
    rows = ExternalJob.objects.filter(scan=scan, status__in=statuses)
    if stage is not None:
        rows = rows.filter(stage=stage)
    if engine is not None:
        rows = rows.filter(engine=engine)
    if apply_run is not None:
        rows = rows.filter(apply_run=apply_run)
    elif stage is not None or engine is not None:
        # A stage is restarted for the volume run: a re-queue re-runs
        # the pipeline, which owns no part of an apply run (#224).
        rows = rows.filter(apply_run__isnull=True)

    # Read the handles before the update: it is the only thing that says
    # what to cancel, and afterwards the rows no longer read as open.
    doomed = list(rows.filter(status__in=IN_FLIGHT_JOB_STATUSES))
    cancelled = rows.update(
        status=JobStatus.CANCELLED,
        error_code="ABANDONED",
        error_message=reason[:2000],
    )
    if cancelled:
        logger.info(
            "cancelled %d open job(s) for scan %s: %s",
            cancelled,
            getattr(scan, "pk", scan),
            reason,
        )
    for job in doomed:
        _cancel_provider_job(job)
    return cancelled
