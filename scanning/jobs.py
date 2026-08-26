"""Lifecycle of the external jobs a scan's shards are converted by.

Everything the daemon needs to decide what to submit, confirm, retry or
write off lives on :class:`~scanning.models.ExternalJob` rows, never in
a Python call stack. That is what makes an interrupted daemon
resumable: the next tick re-reads the rows and carries on.

One stage and one provider so far -- ``CONVERT`` on doctor (issue
#176) -- but the submit/confirm split, the compare-and-swap writes and
the attempt bookkeeping are shaped so dots.mocr on RunPod (#147) and
the batching work (#156) slot in rather than rewrite this.

Three properties are load-bearing and easy to break:

- **Every write is a compare-and-swap** (:func:`_write`), so no lock is
  held across an HTTP call. The other writer is the web process, not a
  second daemon: a user's cancel and the admin re-queue both call
  :func:`abandon_open` from a request, and the loser's update simply
  matches nothing.
- **A resubmission bumps ``attempt``**, re-addressing the result
  object. Doctor finishes a conversion after we stop listening, so an
  abandoned attempt uploads *after* we gave up on it; one key shared
  across attempts would let that late object be harvested as the new
  attempt's output.
- **A failed row is not a failed volume.** :func:`retry_dead` picks one
  back up on the next tick, up to ``DOCTOR_MAX_ATTEMPTS``, so a shard
  doctor could not rasterize in time costs its own retries rather than
  the whole run's.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import timedelta

from botocore.exceptions import BotoCoreError, ClientError
from django.conf import settings
from django.db import transaction
from django.db.models import F, Max, OuterRef, Subquery
from django.utils import timezone

from scanning import doctor_client, s3_sync
from scanning.models import (
    DEAD_JOB_STATUSES,
    IN_FLIGHT_JOB_STATUSES,
    OPEN_JOB_STATUSES,
    ExternalJob,
    JobEngine,
    JobProvider,
    JobStage,
    JobStatus,
    Status,
)

logger = logging.getLogger(__name__)

#: The dead statuses :func:`retry_dead` picks back up, which is
#: ``DEAD_JOB_STATUSES`` minus CANCELLED. A cancel is a person's
#: decision -- a user cancelling a volume, an admin re-queue abandoning
#: the run -- and the daemon must not undo it. EXPIRED belongs here: it
#: is our own lost answer, not a job doctor rejected.
RETRYABLE_DEAD_STATUSES = frozenset(
    {
        JobStatus.FAILED,
        JobStatus.EXPIRED,
    }
)


@dataclass
class SubmitSummary:
    """What one submit tick did.

    :ivar submitted: Handed to doctor and answered successfully.
    :ivar failed: Written off (terminal, or out of attempts).
    :ivar retried: Sent back to PENDING for another attempt.
    :ivar unanswered: Left in flight, for the confirm pass to judge.
    :ivar skipped: Claimed by another writer mid-tick.
    """

    submitted: int = 0
    failed: int = 0
    retried: int = 0
    unanswered: int = 0
    skipped: int = 0


@dataclass
class SweepSummary:
    """What one confirm tick did.

    :ivar completed: In-flight rows whose result object turned up.
    :ivar retried: Past deadline with nothing at their key.
    :ivar failed: Out of attempts, or stranded past the queue ceiling.
    :ivar pending: Still waiting, inside their deadline.
    :ivar errors: Rows whose check itself failed (an S3 problem).
    """

    completed: int = 0
    retried: int = 0
    failed: int = 0
    pending: int = 0
    errors: int = 0


# ── deadlines ───────────────────────────────────────────────────────
def queue_deadline(waiting_since):
    """Return the ceiling for a job that has not been submitted yet.

    :param waiting_since: When the row started waiting.
    :returns: The wall-clock time the row is written off at.
    """
    return waiting_since + timedelta(
        seconds=int(settings.DAEMON_JOB_MAX_QUEUE_SECONDS)
    )


def attempt_deadline(submitted_at):
    """Return the ceiling for one submitted attempt.

    Deliberately not derived from the page count: doctor enforces its
    own per-page and total budgets, so this bounds how long we wait for
    an *answer* that may already be lost, not the conversion itself.

    :param submitted_at: When the attempt was handed to the provider.
    :returns: The wall-clock time the attempt is written off at.
    """
    return submitted_at + timedelta(
        seconds=int(settings.DOCTOR_JOB_DEADLINE_SECONDS)
    )


# ── row writes ──────────────────────────────────────────────────────
def _write(job: ExternalJob, **fields) -> bool:
    """Compare-and-swap ``job``'s row on its current status.

    The status is both the state and the lock: matching on the one we
    last read means a writer that already moved the row makes this
    update match nothing, rather than the two taking turns overwriting
    each other. Nothing is locked across the HTTP call that produced the
    outcome, which is the point.

    The writer to guard against today is the web process: a user's
    cancel (``views_process.cancel_processing``) and the admin re-queue
    both call :func:`abandon_open` mid-tick, and a daemon that wrote
    PENDING over their CANCELLED would convert a shard nobody wants.
    One daemon runs, so daemon-against-daemon is not the case being
    handled -- but this is also what makes a second replica safe if one
    is ever deployed.

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


def _complete(job: ExternalJob, output: dict | None, now) -> bool:
    """Mark a job COMPLETED, keeping the provider's summary.

    COMPLETED, not CONSUMED: the provider being finished is not us
    having applied the result, and calling the row done here is how a
    finished job's output gets dropped.

    :param job: The row to complete.
    :param output: Doctor's JSON summary, or ``None`` for a job whose
        response was lost and whose object we found on S3 instead.
    :param now: Completion timestamp.
    :returns: Whether the write landed.
    :rtype: bool
    """
    meta = dict(job.provider_meta or {})
    meta["output"] = output
    meta["confirmed_by"] = "response" if output else "s3_head"
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
        # cost us end to end; doctor's ``duration_ms`` is the conversion
        # alone, so the gap between them is queue and transport.
        _log_completion(job, output, now)
    return written


def _log_completion(job: ExternalJob, output: dict | None, now) -> None:
    """Log how long one shard took, and how much of that was doctor's.

    :param job: The row just completed.
    :param output: Doctor's summary, when its response reached us.
    :param now: Completion timestamp.
    :return: None.
    """
    elapsed = (
        (now - job.submitted_at).total_seconds() if job.submitted_at else None
    )
    detail = ""
    if output:
        duration_ms = output.get("duration_ms")
        pages = output.get("pages")
        parts = []
        if isinstance(duration_ms, int | float):
            parts.append(f"doctor {duration_ms / 1000:.1f}s")
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
    if isinstance(pixels, int):
        parts.append(f"{pixels} pixel(s) at {settings.DOCTOR_BITONAL_DPI} dpi")
    return f" [{'; '.join(parts)}]"


def _fail(
    job: ExternalJob,
    error_code: str,
    message: str,
    details: dict | None = None,
) -> bool:
    """Write a job off for good.

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
    return _write(
        job,
        status=JobStatus.FAILED,
        error_code=error_code[:64],
        error_message=f"{message}{location}"[:2000],
    )


def _back_to_pending(
    job: ExternalJob, error_code: str, message: str, now
) -> bool:
    """Put a row back in the queue for a fresh attempt.

    A retry mutates the row rather than inserting one, so
    :meth:`~scanning.models.ExternalJob.push_attempt` preserves the
    previous attempt first. Clearing ``result_key`` makes the next
    submit mint a fresh attempt-scoped one, which is also what makes an
    expired signature self-healing rather than fatal.

    The deadline is re-stamped from ``now``: a row carrying the previous
    attempt's deadline would be swept straight back out by
    :func:`sweep_jobs`, which writes off any PENDING row already past
    its queue ceiling.

    Sole writer of this transition, shared by the submit path
    (:func:`_retry_or_fail`) and the next-tick recovery
    (:func:`retry_dead`).

    :param job: The row to re-queue.
    :param error_code: Why the previous attempt did not stick.
    :param message: Human-readable detail.
    :param now: Current time.
    :returns: Whether this writer won the row.
    :rtype: bool
    """
    job.push_attempt(save=False)
    return _write(
        job,
        status=JobStatus.PENDING,
        attempt=job.attempt + 1,
        retry_count=job.retry_count + 1,
        result_key="",
        external_id="",
        submitted_at=None,
        completed_at=None,
        deadline=queue_deadline(now),
        error_code=error_code[:64],
        error_message=message[:2000],
        provider_meta=job.provider_meta,
    )


def _retry_or_fail(
    job: ExternalJob,
    error_code: str,
    message: str,
    now,
    details: dict | None = None,
) -> str:
    """Send a job back for another attempt, or write it off.

    :param job: The row to retry.
    :param error_code: Why the previous attempt did not stick.
    :param message: Human-readable detail.
    :param now: Current time.
    :param details: Doctor's per-page failure fields, when it sent them.
    :returns: ``"retried"``, ``"failed"``, or ``"skipped"``.
    :rtype: str
    """
    if job.attempt >= int(settings.DOCTOR_MAX_ATTEMPTS):
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
    ok = _back_to_pending(job, error_code, message, now)
    return "retried" if ok else "skipped"


# ── creating the work ───────────────────────────────────────────────
def _shard_specs(scan, manifest: dict) -> list[tuple[str, dict]]:
    """Describe the work today's shard set asks for, in page order.

    One ``(input_key, identity)`` pair per shard. The identity is what
    the row stores in ``input_manifest`` -- which pages of which volume
    the shard holds -- so a row can be checked against a later manifest.

    :param scan: The scan the shards belong to.
    :param manifest: The shard manifest from :mod:`scanning.sharding`.
    :returns: ``(key, identity)`` per shard, ordered by shard index.
    :rtype: list[tuple[str, dict]]
    """
    prefix = s3_sync.shards_prefix(scan)
    source_page_count = manifest["source"]["page_count"]
    return [
        (
            f"{prefix}{entry['name']}",
            {
                "name": entry["name"],
                "from_page": entry["from_page"],
                "to_page": entry["to_page"],
                "page_count": entry["page_count"],
                "source_page_count": source_page_count,
            },
        )
        for entry in sorted(manifest["shards"], key=lambda e: e["index"])
    ]


def _still_describes(
    live: list[ExternalJob], specs: list[tuple[str, dict]]
) -> bool:
    """Return whether an existing run still covers the work asked for.

    Compares the stored page ranges, not just the keys: shards are named
    by position (``0001.pdf``), so a re-cut volume with the same shard
    count produces identical keys over different pages. On keys alone
    this would degenerate to comparing counts, and the reused rows would
    convert the new bytes while claiming the old ranges -- which the
    merge catches, but only after paying for every conversion.

    :param live: The current run's rows, ordered by shard index.
    :param specs: What the work looks like today, from
        :func:`_shard_specs`.
    :returns: Whether the existing rows describe today's shard set.
    :rtype: bool
    """
    if len(live) != len(specs):
        return False
    return all(
        job.input_key == key and job.input_manifest == identity
        for job, (key, identity) in zip(live, specs, strict=True)
    )


def _is_reusable(live: list[ExternalJob]) -> bool:
    """Return whether an existing run can be picked back up as-is.

    Reusable while every row is still working or already applied. One
    dead row (failed, cancelled, expired) sinks the run: nothing will
    move it again, so it can neither finish nor be merged, and reusing
    it parks the scan behind work that is over. That is exactly what a
    cancel or an admin re-queue leaves behind -- the path that most
    needs a clean second attempt, not the corpse of the first.

    :param live: The current run's rows.
    :returns: Whether to reuse the run rather than start a new one.
    :rtype: bool
    """
    return not any(job.status in DEAD_JOB_STATUSES for job in live)


def ensure_convert_jobs(scan, manifest: dict) -> list[ExternalJob]:
    """Return the live bitonal-conversion jobs for ``scan``, creating them
    if the current run does not describe today's shard set.

    Idempotent, which is what makes the re-queue path safe: a scan whose
    daemon died after the rows were created finds them again instead of
    paying for a second conversion. When the shard set has moved (a
    re-upload, a page edit), a new run starts and the previous run's
    rows and result objects stay addressable as history.

    :param scan: The scan to convert.
    :param manifest: The committed shard manifest.
    :returns: The live run's rows, ordered by shard index.
    :rtype: list[ExternalJob]
    """
    specs = _shard_specs(scan, manifest)

    existing = list(
        ExternalJob.objects.filter(
            scan=scan,
            stage=JobStage.CONVERT,
            engine=JobEngine.BITONAL,
            opinion=None,
        ).order_by("-run", "shard_index")
    )
    if existing:
        live = [job for job in existing if job.run == existing[0].run]
        if _still_describes(live, specs) and _is_reusable(live):
            return live
        logger.info(
            "scan %s convert run %d cannot be picked back up (%d shard(s), "
            "statuses %s); starting a new run",
            scan.pk,
            live[0].run,
            len(live),
            sorted({job.status for job in live}),
        )

    run = ExternalJob.next_run(scan, JobStage.CONVERT, JobEngine.BITONAL)
    now = timezone.now()
    with transaction.atomic():
        created = ExternalJob.objects.bulk_create(
            [
                ExternalJob(
                    scan=scan,
                    stage=JobStage.CONVERT,
                    engine=JobEngine.BITONAL,
                    provider=JobProvider.DOCTOR,
                    status=JobStatus.PENDING,
                    run=run,
                    shard_index=index,
                    shard_count=len(specs),
                    input_key=key,
                    # Travels with the row, so the reuse check and the
                    # merge read what was actually converted rather
                    # than a manifest that may since have changed.
                    input_manifest=identity,
                    deadline=queue_deadline(now),
                )
                for index, (key, identity) in enumerate(specs)
            ]
        )
    logger.info(
        "created %d bitonal conversion job(s) for scan %s at run %d",
        len(created),
        scan.pk,
        run,
    )
    return created


# ── submitting ──────────────────────────────────────────────────────
def _claim(job: ExternalJob, now) -> tuple[str, tuple[str, str] | None]:
    """Mark one job SUBMITTED and return its presigned URL pair.

    The row moves *before* the URLs are even signed, deliberately: if
    this process dies in between, the row says a conversion may be
    running and names the key to look for. The other order would leave a
    completed conversion with nothing pointing at it.

    :param job: A PENDING row.
    :param now: Submission timestamp.
    :returns: ``("claimed", (input_url, output_url))``, or an outcome
        label with no URLs -- ``"skipped"`` if another writer took the
        row, ``"retried"`` / ``"failed"`` if signing failed.
    :rtype: tuple[str, tuple[str, str] | None]
    """
    result_key = s3_sync.s3_job_attempt_key(job, suffix=".pdf")
    if not _write(
        job,
        status=JobStatus.SUBMITTED,
        result_key=result_key,
        submitted_at=now,
        deadline=attempt_deadline(now),
        error_code="",
        error_message="",
    ):
        return "skipped", None

    try:
        ttl = int(settings.DOCTOR_PRESIGNED_TTL)
        return "claimed", (
            s3_sync.presign_get(job.input_key, ttl),
            s3_sync.presign_put(
                result_key, doctor_client.RESULT_CONTENT_TYPE, ttl
            ),
        )
    except (BotoCoreError, ClientError) as exc:
        # Nothing was sent, so this is purely local: hand the row
        # back rather than fail a volume over an S3 blip.
        return _retry_or_fail(job, "PRESIGN_FAILED", str(exc), now), None


def _apply_submit_outcome(
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
            logger.warning(
                "job %s (scan %s shard %s) got no answer from doctor (%s); "
                "leaving it in flight for the confirm pass",
                job.pk,
                job.scan_id,
                job.shard_index,
                exc,
            )
            _write(
                job,
                error_code=exc.error_code[:64],
                error_message=str(exc)[:2000],
            )
            return "unanswered"
        return _retry_or_fail(job, exc.error_code, str(exc), now, details)

    if isinstance(exc, doctor_client.DoctorError):
        failed = _fail(job, exc.error_code, str(exc), details)
        return "failed" if failed else "skipped"

    logger.exception(
        "unexpected error submitting job %s", job.pk, exc_info=exc
    )
    return "failed" if _fail(job, "SUBMIT_FAILED", str(exc)) else "skipped"


def submit_pending(limit: int | None = None) -> SubmitSummary:
    """Submit one wave of pending conversions and record what happened.

    A wave, not a queue drain: ``limit`` bounds the conversions running
    at once, including ones an earlier tick started and never got an
    answer for. Those are still work doctor is doing, so claiming a
    fresh full wave every tick while it is slow would grow our load on
    it without bound, exactly when it is struggling.

    Rows are claimed only as far as the pool can start them at once,
    which keeps a SUBMITTED row genuinely in flight: claimed rows queued
    behind a saturated pool would leave a parked scan unable to tell
    "waiting on doctor" from "waiting on us".

    The threads make HTTP calls and nothing else. Every database write
    happens on this thread, before and after the fan-out, so no worker
    thread opens a connection Django would have to clean up.

    :param limit: Ceiling on concurrent conversions; defaults to
        ``settings.DOCTOR_MAX_CONCURRENCY``.
    :returns: Counts of what the tick did.
    :rtype: SubmitSummary
    """
    summary = SubmitSummary()
    if not doctor_client.enabled():
        return summary

    if not s3_sync.s3_active():
        # Doctor reads its input through a presigned GET, so without
        # S3 the shards were never uploaded: every request would 404
        # and burn a job's whole retry budget under a misleading code.
        # Say so once instead.
        logger.error(
            "DOCTOR_ENABLED is set but S3 is inactive (no credentials, or "
            "DEVELOPMENT without them): shards never reached the bucket "
            "doctor fetches them from, so nothing can be submitted."
        )
        return summary

    limit = int(limit or settings.DOCTOR_MAX_CONCURRENCY)
    ours = ExternalJob.objects.filter(
        provider=JobProvider.DOCTOR, stage=JobStage.CONVERT
    )
    in_flight = ours.filter(status__in=IN_FLIGHT_JOB_STATUSES).count()
    room = limit - in_flight
    if room < 1:
        logger.info(
            "%d bitonal shard(s) already in flight at a cap of %d; "
            "submitting none this tick",
            in_flight,
            limit,
        )
        return summary

    pending = list(
        ours.filter(status=JobStatus.PENDING)
        .select_related("scan", "scan__reporter")
        .order_by("id")[:room]
    )
    if not pending:
        return summary

    now = timezone.now()
    claimed = []
    for job in pending:
        outcome, urls = _claim(job, now)
        if urls is None:
            setattr(summary, outcome, getattr(summary, outcome) + 1)
            continue
        claimed.append((job, urls))
    if not claimed:
        return summary

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
        result = _apply_submit_outcome(job, exc, output, applied_at)
        setattr(summary, result, getattr(summary, result) + 1)
    return summary


# ── recovering ──────────────────────────────────────────────────────
def can_retry(job: ExternalJob) -> bool:
    """Return whether :func:`retry_dead` will pick this dead row up.

    Read by :func:`scanning.bitonal.finish_ready_scans` to tell a shard
    that is merely between attempts from one that is out of them: the
    first must leave its scan waiting in AWAITING, the second is what
    ERRORs the volume.

    :param job: A row of the scan's live run.
    :returns: Whether the row is dead but still owed an attempt.
    :rtype: bool
    """
    return job.status in RETRYABLE_DEAD_STATUSES and job.attempt < int(
        settings.DOCTOR_MAX_ATTEMPTS
    )


def retry_dead(now=None) -> int:
    """Send failed conversions back for another attempt (issue #187).

    Doctor reports a page it could not rasterize in time with the same
    finality as a corrupt PDF, so the submit path writes the row off
    after one attempt (:func:`_record_outcome`). One such shard used to
    strand a whole volume: the scan went to ERROR, and the only way out
    was an admin re-queue, which abandons every sibling that already
    converted and pays for all of them again.

    So the daemon picks the row back up instead, and the run keeps its
    converted siblings. ``DOCTOR_MAX_ATTEMPTS`` bounds it, so a page
    that always fails still ends as a failure -- three conversions
    later, which for bitonal is CPU seconds (see
    :mod:`scanning.doctor_client`). Without that bound this would
    convert a defective page on every tick, forever.

    Only rows of a *live* run qualify. A superseded run's failure
    belongs to a shard set that no longer exists, and reviving it would
    submit a shard key the current manifest does not list.

    And only for a scan that still wants the conversion: AWAITING, where
    it is waiting for exactly this, or ERROR, where the volume was
    written off and a retry is the way back. A cancelled scan is the
    case that matters -- ``abandon_open`` cannot cancel a row that
    already failed, so without this filter the daemon would convert a
    shard of a volume somebody stopped.

    Doctor's ``CONVERSION_TIMEOUT`` (its issue #245, PR #246) is merged
    but not necessarily released; until it is, a timeout and a corrupt
    page both arrive here as ``CONVERSION_FAILED`` and nothing can tell
    them apart. Once it is running in production, narrow this to the
    codes that deserve it: that one and ``TRANSIENT_ERROR_CODES``.

    :param now: Comparison time; defaults to ``timezone.now()``.
    :returns: How many rows went back to PENDING.
    :rtype: int
    """
    now = now or timezone.now()
    ours = ExternalJob.objects.filter(
        provider=JobProvider.DOCTOR,
        stage=JobStage.CONVERT,
        engine=JobEngine.BITONAL,
        opinion=None,
    )
    # Grouped by scan alone because the filter above already pins the
    # rest of a run's identity -- and pins ``opinion`` as the literal
    # None a volume-level row carries, which an OuterRef could not: that
    # would compile to ``= NULL``, true of nothing.
    latest_run = (
        ours.filter(scan_id=OuterRef("scan_id"))
        .values("scan_id")
        .annotate(latest=Max("run"))
        .values("latest")[:1]
    )
    dead = (
        ours.filter(
            status__in=RETRYABLE_DEAD_STATUSES,
            scan__status__in=(Status.AWAITING, Status.ERROR),
        )
        .annotate(latest_run=Subquery(latest_run))
        .filter(run=F("latest_run"))
        .select_related("scan", "scan__reporter")
        .order_by("scan_id", "shard_index")
    )

    retried = 0
    for job in dead:
        if not can_retry(job):
            continue
        logger.info(
            "job %s (scan %s shard %d/%d) failed on attempt %d (%s); "
            "sending it back for another attempt",
            job.pk,
            job.scan_id,
            job.shard_index + 1,
            job.shard_count,
            job.attempt,
            job.error_code or "?",
        )
        if _back_to_pending(job, job.error_code, job.error_message, now):
            retried += 1
    return retried


# ── confirming ──────────────────────────────────────────────────────
def sweep_jobs(now=None) -> SweepSummary:
    """Confirm in-flight conversions and write off stranded rows.

    The recovery half of the loop, and the only thing that can finish a
    job whose response we never saw -- a killed daemon, a redeployed
    pod, an abandoned read. Doctor's single PUT is atomic and its key
    unique to this attempt, so the object's existence is a complete
    answer.

    An attempt past its deadline with nothing at its key is retried.
    That costs a full deadline per lost shard, and is inherent to a
    provider with no status endpoint.

    :param now: Comparison time; defaults to ``timezone.now()``.
    :returns: Counts of what the tick did.
    :rtype: SweepSummary
    """
    summary = SweepSummary()
    now = now or timezone.now()

    in_flight = ExternalJob.objects.filter(
        status=JobStatus.SUBMITTED,
        provider=JobProvider.DOCTOR,
        stage=JobStage.CONVERT,
    ).select_related("scan", "scan__reporter")
    for job in in_flight:
        try:
            present = bool(job.result_key) and s3_sync.object_exists(
                job.result_key
            )
        except (BotoCoreError, ClientError):
            # An S3 problem is ours, not the job's: retry next tick
            # rather than read it as "produced nothing" and burn an
            # attempt.
            logger.warning(
                "could not check %s for job %s; leaving it in flight",
                job.result_key,
                job.pk,
                exc_info=True,
            )
            summary.errors += 1
            continue

        if present:
            if _complete(job, None, now):
                summary.completed += 1
            continue
        if job.is_overdue(now):
            result = _retry_or_fail(
                job,
                "DEADLINE_EXCEEDED",
                f"no result at {job.result_key} by {job.deadline}",
                now,
            )
            if result in ("retried", "failed"):
                setattr(summary, result, getattr(summary, result) + 1)
            continue
        _write(job, last_polled_at=now)
        summary.pending += 1

    # Rows never submitted at all -- a provider switched off after they
    # were created, a submit loop that never ran. Without this their
    # scan waits in AWAITING forever.
    stranded = ExternalJob.objects.filter(
        status=JobStatus.PENDING,
        provider=JobProvider.DOCTOR,
        stage=JobStage.CONVERT,
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


def abandon_open(scan, reason: str) -> int:
    """Cancel a scan's open conversion jobs.

    For the paths that mean "start this over" -- an admin re-queue, a
    cancel. Open rows would let a stale attempt's outcome land on a scan
    that has moved on, and would make :func:`ensure_convert_jobs` reuse
    rows whose result objects someone else is still writing.

    :param scan: The scan (or its pk) whose jobs to cancel.
    :param reason: Recorded on each row for the audit trail.
    :returns: How many rows were cancelled.
    :rtype: int
    """
    cancelled = ExternalJob.objects.filter(
        scan=scan, status__in=OPEN_JOB_STATUSES
    ).update(
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
    return cancelled
