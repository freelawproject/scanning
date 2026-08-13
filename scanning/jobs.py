"""The lifecycle of an :class:`~scanning.models.ExternalJob` row.

The collect half of the batch cycle (#156). Nothing here knows what a
job computes or which scan is waiting on it: it asks every in-flight
job's provider what happened, writes the answer to the row, and stops.
Deciding what a finished job unblocks belongs to the advance phase,
which reads these rows on its own tick.

Where the phase boundary falls is :class:`JobStatus`'s own split.
``COMPLETED`` means the provider says the work is done; ``CONSUMED``
means we have read the result and applied it. This module only ever
raises a job to ``COMPLETED``. That leaves the payload on S3 under the
job's ``result_key`` rather than in memory or in a column, so a daemon
killed between the two phases loses nothing: the next advance tick
finds a completed job and fetches its result exactly as it would have.

Three properties the sweep has to hold, all of which follow from the
daemon holding many jobs at once rather than one:

- **Failure isolation.** One job's provider blowing up must not end
  the tick for the others, so every job is swept inside its own
  ``try``.
- **No lock held across a network call.** Polls happen outside any
  transaction, and each write is guarded on the status the poll was
  taken under, so a concurrent writer is never stomped.
- **Nothing in a call stack.** A poll that learns nothing leaves the
  row exactly as it was; the next tick re-derives what to do from the
  same columns.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from scanning.models import ExternalJob, JobStatus
from scanning.providers import get_provider

logger = logging.getLogger(__name__)


@dataclass
class SubmitSummary:
    """What one submit sweep did.

    :ivar submitted: Jobs handed to a provider this tick.
    :ivar deferred: Jobs left ``PENDING`` because the provider said it
        could not take work right now. Not an error: the next tick
        tries again, and the job's retry budget is untouched.
    :ivar failed: Jobs that could not be submitted at all.
    """

    submitted: int = 0
    deferred: int = 0
    failed: int = 0

    def __str__(self):
        return (
            f"submitted {self.submitted}, deferred {self.deferred}, "
            f"failed {self.failed}"
        )


def ensure_jobs(
    scan,
    stage,
    engine,
    provider,
    input_key,
    manifest=None,
    shard_count=1,
    opinion=None,
) -> list[ExternalJob]:
    """Return this stage's live jobs, creating them if it has not run.

    The pipeline's way of saying "this stage needs to happen" without
    knowing whether it already has. Called on every pass through a
    step, it creates rows the first time and finds the same ones after,
    which is what lets the pipeline be re-entered from the top rather
    than resumed at a remembered offset.

    Rows are created ``PENDING``; :func:`submit_pending` hands them
    over on a later tick. Splitting creation from submission means a
    daemon killed between the two leaves rows that describe work to do
    rather than jobs nobody is watching.

    :param scan: The Scan the work belongs to.
    :param stage: A :class:`~scanning.models.JobStage` value.
    :param engine: A :class:`~scanning.models.JobEngine` value.
    :param provider: A :class:`~scanning.models.JobProvider` value.
    :param input_key: S3 key of the document to send. Presigned into a
        GET at submit time, not now, so a job that waits out a long
        queue does not carry an expired signature.
    :param manifest: Engine arguments (models, page range, ...), stored
        on the row and replayed on every resubmission.
    :param shard_count: How many jobs to split the target into.
    :param opinion: The OpinionScan, for the opinion-level stages.
    :returns: The stage's jobs at its current run, in shard order.
    :rtype: list[ExternalJob]
    """
    existing = list(
        ExternalJob.objects.filter(
            scan=scan, stage=stage, engine=engine, opinion=opinion
        ).order_by("-run", "shard_index")
    )
    if existing:
        live_run = existing[0].run
        return [job for job in existing if job.run == live_run]

    run = ExternalJob.next_run(scan, stage, engine, opinion=opinion)
    with transaction.atomic():
        created = ExternalJob.objects.bulk_create(
            [
                ExternalJob(
                    scan=scan,
                    opinion=opinion,
                    stage=stage,
                    engine=engine,
                    provider=provider,
                    status=JobStatus.PENDING,
                    run=run,
                    shard_index=index,
                    shard_count=shard_count,
                    input_key=input_key,
                    input_manifest=manifest or {},
                )
                for index in range(shard_count)
            ]
        )
    logger.info(
        "created %d %s/%s job(s) for scan %s at run %d",
        len(created),
        stage,
        engine,
        scan.pk,
        run,
    )
    return created


def submit_pending(limit=None, now=None) -> SubmitSummary:
    """Hand every pending job to its provider, without waiting on any.

    The batch submit. Nothing here blocks: each job is handed over and
    the loop moves to the next, so a batch of ten volumes reaches the
    endpoint in the time one used to, and the endpoint's own worker cap
    decides how many run at once.

    :param limit: Most jobs to submit this tick, or None for all.
    :param now: Submission time; defaults to ``timezone.now()``.
    :returns: Counts for the tick.
    :rtype: SubmitSummary
    """
    now = now or timezone.now()
    summary = SubmitSummary()

    pending = ExternalJob.objects.filter(
        status=JobStatus.PENDING
    ).select_related("scan")
    if limit:
        pending = pending[:limit]

    for job in pending:
        _submit(job, summary, now)
    return summary


def _submit(job: ExternalJob, summary: SubmitSummary, now) -> None:
    """Submit one job and record where it went.

    :param job: The pending row.
    :param summary: Counters to update.
    :param now: The tick's timestamp.
    """
    from scanning import s3_sync
    from scanning.runpod_client import RunpodTransientError

    # Assigned before the call, because the provider presigns a PUT for
    # exactly this key and cannot be told about it afterwards.
    job.result_key = s3_sync.s3_job_attempt_key(job)

    try:
        payload = _build_payload(job)
        receipt = get_provider(job.provider).submit(job, payload)
    except RunpodTransientError as exc:
        # The endpoint cannot take work right now (paused, scaled to
        # zero). Leaving the row PENDING retries on the next tick
        # without spending the retry budget, which is reserved for
        # failures of the work itself.
        logger.warning(
            "job %s deferred, provider not accepting work: %s", job.pk, exc
        )
        summary.deferred += 1
        return
    except Exception as exc:
        logger.exception("submitting job %s failed", job.pk)
        _write(
            job,
            status=JobStatus.FAILED,
            error_code="SUBMIT_FAILED",
            error_message=str(exc)[:2000],
        )
        summary.failed += 1
        return

    _write(
        job,
        status=JobStatus.SUBMITTED,
        external_id=receipt.external_id,
        result_key=receipt.result_key,
        submitted_at=receipt.submitted_at,
        deadline=_deadline_for(job, receipt.submitted_at),
        last_polled_at=None,
        completed_at=None,
        error_code="",
        error_message="",
    )
    summary.submitted += 1
    logger.info(
        "job %s submitted to %s as %s",
        job.pk,
        job.provider,
        receipt.external_id,
    )


def _build_payload(job: ExternalJob) -> dict:
    """Build the provider input for one job.

    The durable half lives on the row (``input_key``,
    ``input_manifest``); the presigned GET is minted here, at submit
    time, so a job that waited out a long queue is not handed a
    signature that expired while it waited.

    :param job: The row being submitted.
    :returns: The provider's ``input`` fields.
    :rtype: dict
    :raises ValueError: If the row names no input document.
    """
    from scanning import runpod_client

    if not job.input_key:
        raise ValueError(f"job {job.pk} names no input document")

    payload = dict(job.input_manifest or {})
    payload["pdf_url"] = runpod_client.presign_input_get(job.input_key)
    return payload


def _deadline_for(job: ExternalJob, submitted_at):
    """Return the wall-clock ceiling for one job.

    A base timeout plus an allowance for the pages this job covers.
    Per job rather than per scan, so one wedged shard is cancelled and
    resubmitted without stalling the siblings it fanned out alongside.

    :param job: The row being submitted.
    :param submitted_at: When it was handed over.
    :returns: When to give up on it.
    """
    pages = (job.scan.page_count or 0) / max(job.shard_count, 1)
    return submitted_at + timedelta(
        seconds=int(settings.RUNPOD_REQUEST_TIMEOUT)
        + pages * float(settings.DAEMON_JOB_SECONDS_PER_PAGE)
    )


@dataclass
class CollectSummary:
    """What one sweep did, for the command's output and for tests.

    :ivar polled: Jobs a status call was made for.
    :ivar completed: Jobs the provider reported finished this tick.
    :ivar retried: Jobs sent back to ``PENDING`` for another submit.
    :ivar failed: Jobs that reached a terminal failure.
    :ivar cancelled: Jobs cancelled for running past their deadline.
    :ivar unchanged: Jobs whose status call told us nothing, either
        because the job is still running or because the call failed.
    :ivar errors: Jobs whose sweep raised. Counted rather than fatal;
        the rest of the tick still runs.
    """

    polled: int = 0
    completed: int = 0
    retried: int = 0
    failed: int = 0
    cancelled: int = 0
    unchanged: int = 0
    errors: int = 0

    def __str__(self):
        return (
            f"polled {self.polled}, completed {self.completed}, "
            f"retried {self.retried}, failed {self.failed}, "
            f"cancelled {self.cancelled}, unchanged {self.unchanged}, "
            f"errors {self.errors}"
        )


def collect_once(now=None) -> CollectSummary:
    """Sweep every in-flight job once and record what each provider said.

    Polling runs before the deadline check on purpose. A job can be
    both past its deadline and already finished, and harvesting a
    result we have already paid for beats cancelling it a second
    before we would have read it.

    :param now: Comparison time; defaults to ``timezone.now()``.
    :returns: Counts for the tick.
    :rtype: CollectSummary
    """
    now = now or timezone.now()
    summary = CollectSummary()

    for job in ExternalJob.objects.in_flight():
        _sweep(job, summary, now)

    # Re-queried, so anything the sweep above just finished is already
    # excluded and does not get cancelled on its way out.
    for job in ExternalJob.objects.overdue(now):
        _time_out(job, summary, now)

    return summary


def _sweep(job: ExternalJob, summary: CollectSummary, now) -> None:
    """Poll one job and apply what came back.

    :param job: The in-flight row.
    :param summary: Counters to update.
    :param now: The tick's timestamp.
    """
    try:
        outcome = get_provider(job.provider).poll(job)
    except Exception:
        # Isolation: an unimplemented provider, a malformed row, a
        # client bug. Log it and leave the row untouched for the next
        # tick rather than ending the sweep for everything else.
        logger.exception("polling job %s failed", job.pk)
        summary.errors += 1
        return

    summary.polled += 1

    if outcome.status is None:
        # The call failed and told us nothing about the job. Stamping
        # last_polled_at is still worth it: it separates "nobody has
        # looked" from "we looked and could not reach the provider".
        _write(job, last_polled_at=now)
        summary.unchanged += 1
        return

    if outcome.status == JobStatus.COMPLETED:
        _complete(job, outcome, summary, now)
        return

    if outcome.status in (
        JobStatus.FAILED,
        JobStatus.CANCELLED,
        JobStatus.EXPIRED,
    ):
        _retry_or_finish(job, outcome.status, outcome, summary, now)
        return

    # Still queued or running. Record the provider's own state so the
    # advance phase can show it without asking again.
    if _write(job, status=outcome.status, last_polled_at=now):
        logger.debug(
            "job %s -> %s (%s)",
            job.pk,
            outcome.status,
            outcome.provider_status,
        )
    summary.unchanged += 1


def _complete(job, outcome, summary: CollectSummary, now) -> None:
    """Raise a finished job to ``COMPLETED``, or fail it if unreadable.

    A job whose payload came back inline has nowhere to survive the
    tick: the advance phase re-reads from ``result_key``, and a column
    is the wrong home for a result measured in megabytes. That only
    happens without S3 credentials, where the pipeline uses the
    synchronous path anyway, so it is a misconfiguration rather than a
    case to support -- and a loud failure beats silently dropping work
    we have already paid for.

    :param job: The finished row.
    :param outcome: What the poll returned.
    :param summary: Counters to update.
    :param now: The tick's timestamp.
    """
    if not job.result_key:
        logger.error(
            "job %s completed with no result_key; the async collector "
            "cannot hold an inline payload across a tick",
            job.pk,
        )
        _write(
            job,
            status=JobStatus.FAILED,
            completed_at=now,
            last_polled_at=now,
            error_code="NO_RESULT_KEY",
            error_message=(
                "Completed without a presigned result key, so the payload "
                "did not outlive the job. Batch submission requires S3 "
                "result delivery."
            ),
        )
        summary.failed += 1
        return

    logger.info(
        "job %s completed (%s/%s on %s)",
        job.pk,
        job.stage,
        job.engine,
        job.provider,
    )
    _write(
        job,
        status=JobStatus.COMPLETED,
        completed_at=now,
        last_polled_at=now,
        error_code="",
        error_message="",
    )
    summary.completed += 1


def _time_out(job: ExternalJob, summary: CollectSummary, now) -> None:
    """Cancel a job that ran past its deadline, then retry or finish it.

    Per job rather than per scan: one wedged shard is cancelled and
    resubmitted without stalling the siblings it fanned out alongside.

    :param job: The overdue row.
    :param summary: Counters to update.
    :param now: The tick's timestamp.
    """
    logger.warning(
        "job %s passed its deadline (%s); cancelling", job.pk, job.deadline
    )
    try:
        get_provider(job.provider).cancel(job)
    except Exception:
        # Best effort. Failing to cancel costs money, not correctness,
        # and the row still has to move out of flight.
        logger.exception("cancelling overdue job %s failed", job.pk)

    summary.cancelled += 1
    _retry_or_finish(
        job,
        JobStatus.CANCELLED,
        _TimedOut(job),
        summary,
        now,
    )


class _TimedOut:
    """Stand-in outcome for a deadline the provider never reported.

    A cancelled-for-lateness job is retriable: it was not rejected, it
    just did not finish, and a fresh worker often does.

    :ivar error_code: Always ``DEADLINE_EXCEEDED``.
    :ivar error_message: Names the deadline that passed.
    :ivar retriable: Always True.
    """

    retriable = True
    error_code = "DEADLINE_EXCEEDED"

    def __init__(self, job):
        self.error_message = (
            f"Cancelled after passing its deadline of {job.deadline}."
        )


def _retry_or_finish(
    job: ExternalJob, terminal_status, outcome, summary: CollectSummary, now
) -> None:
    """Send a failed job back for another submit, or leave it terminal.

    A retry mutates the row rather than inserting one, so everything
    the previous attempt held has to be preserved first
    (:meth:`ExternalJob.push_attempt`) and then cleared. ``result_key``
    is cleared with the rest: the next submit mints one scoped to the
    new attempt, which is what stops an abandoned worker's late upload
    from being read as the current attempt's output.

    :param job: The row that failed.
    :param terminal_status: Status to settle on if it is not retried.
    :param outcome: Anything carrying ``retriable`` / ``error_code`` /
        ``error_message``.
    :param summary: Counters to update.
    :param now: The tick's timestamp.
    """
    max_retries = int(settings.RUNPOD_MAX_TRANSIENT_RETRIES)
    if not outcome.retriable or job.retry_count >= max_retries:
        logger.error(
            "job %s is terminal (%s): %s",
            job.pk,
            terminal_status,
            outcome.error_message,
        )
        _write(
            job,
            status=terminal_status,
            completed_at=now,
            last_polled_at=now,
            error_code=outcome.error_code,
            error_message=outcome.error_message,
        )
        summary.failed += 1
        return

    logger.warning(
        "job %s failed retriably (%d/%d), re-queueing for submit: %s",
        job.pk,
        job.retry_count + 1,
        max_retries,
        outcome.error_message,
    )
    # push_attempt records the row as it stands, so stamp the failure
    # onto it first. The polled status goes back afterwards: it is what
    # the database still holds, and what _write guards on.
    polled_status = job.status
    job.status = terminal_status
    job.error_code = outcome.error_code
    job.error_message = outcome.error_message
    attempts = job.push_attempt(save=False)
    job.status = polled_status

    _write(
        job,
        status=JobStatus.PENDING,
        attempt=job.attempt + 1,
        retry_count=job.retry_count + 1,
        external_id="",
        result_key="",
        submitted_at=None,
        completed_at=None,
        deadline=None,
        last_polled_at=now,
        error_code=outcome.error_code,
        error_message=outcome.error_message,
        provider_meta={**job.provider_meta, "attempts": attempts},
    )
    summary.retried += 1


def _write(job: ExternalJob, **fields) -> bool:
    """Write ``fields`` to ``job``, unless something else moved it first.

    Guarded on the status the row was polled under. Polls happen
    outside a transaction -- holding a row lock across an HTTP call
    would serialize the sweep it exists to parallelize -- so the guard
    is what keeps a concurrent admin re-queue or a second daemon
    replica from being silently overwritten.

    :param job: The row to update, as it was read.
    :param fields: Column values to write.
    :returns: Whether the row was still where we left it.
    :rtype: bool
    """
    updated = ExternalJob.objects.filter(pk=job.pk, status=job.status).update(
        **fields
    )
    if not updated:
        logger.info(
            "job %s moved out of %s while it was being polled; "
            "leaving it to whoever moved it",
            job.pk,
            job.status,
        )
    return bool(updated)
