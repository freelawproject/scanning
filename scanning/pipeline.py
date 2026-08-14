"""Driving a scan through steps that don't all finish in one call.

The advance half of the batch cycle (#156). ``services.py`` still holds
the pipeline as straight-line code, because that is the readable way to
write it; what changes is that a step whose work is on a GPU no longer
blocks until it comes back. :func:`await_stage` either returns the
result or raises :class:`Awaiting`, and :func:`advance_scan` catches
that and parks the scan until a later tick.

So the pipeline is re-entered from the top rather than resumed at a
remembered offset. Every step decides for itself whether its output
already exists -- which is what ``run_full_pipeline`` has always done
to survive a daemon killed on deploy, now carried far enough that an
unfinished GPU job is just another step whose output isn't ready.
Nothing about "what runs next" lives in a call stack, so a daemon that
dies between two steps costs nothing but the tick.

The cost is that re-entry re-runs the cheap local steps: making the
output directory, checking bitonal.pdf is there, re-reading a result
object off S3. Each is seconds against the minutes of a GPU job, and
it buys a pipeline with no state to corrupt.

Turned on with ``DAEMON_BATCH_JOBS``. With the flag off, every step
takes the blocking path it takes today.
"""

from __future__ import annotations

import logging

from django.utils import timezone

from scanning import jobs
from scanning.models import (
    IN_FLIGHT_JOB_STATUSES,
    ExternalJob,
    JobEngine,
    JobProvider,
    JobStage,
    JobStatus,
    QueuedAction,
    Scan,
    Status,
)
from scanning.providers import get_provider

logger = logging.getLogger(__name__)


class Awaiting(Exception):
    """A step's external work is not finished; unwind and try later.

    Control flow, not failure. It must reach :func:`advance_scan`
    intact, so the pipeline's own ``except Exception`` handlers let it
    through rather than marking the scan ERROR.

    :ivar stage: The stage being waited on, for the progress message.
    """

    def __init__(self, message, stage=""):
        super().__init__(message)
        self.stage = stage


def await_stage(
    scan,
    stage,
    engine,
    provider,
    input_key,
    manifest=None,
    shard_count=1,
    opinion=None,
) -> list[dict]:
    """Return a stage's results, or raise :class:`Awaiting`.

    Creates the stage's jobs the first time it is called and finds the
    same ones on every later pass, so calling it repeatedly is how a
    re-entered pipeline discovers that a step it started has finished.

    :param scan: The Scan the work belongs to.
    :param stage: A :class:`~scanning.models.JobStage` value.
    :param engine: A :class:`~scanning.models.JobEngine` value.
    :param provider: A :class:`~scanning.models.JobProvider` value.
    :param input_key: S3 key of the document to send.
    :param manifest: Engine arguments stored on the row.
    :param shard_count: How many jobs to split the target into.
    :param opinion: The OpinionScan, for opinion-level stages.
    :returns: One payload per shard, in shard order.
    :rtype: list[dict]
    :raises Awaiting: If any of the stage's jobs is still outstanding.
    :raises RuntimeError: If any of them failed terminally. The stage
        cannot be completed without it, and the retry budget is
        already spent, so the scan needs a person.
    """
    stage_jobs = jobs.ensure_jobs(
        scan,
        stage,
        engine,
        provider,
        input_key=input_key,
        manifest=manifest,
        shard_count=shard_count,
        opinion=opinion,
    )

    dead = [j for j in stage_jobs if j.status in _DEAD_STATUSES]
    if dead:
        raise RuntimeError(
            f"{stage} failed for scan {scan.pk}: "
            + "; ".join(
                f"{j.error_code or j.status}: {j.error_message}" for j in dead
            )
        )

    waiting = [j for j in stage_jobs if j.status in _OUTSTANDING_STATUSES]
    if waiting:
        raise Awaiting(
            f"{stage}: {len(waiting)} of {len(stage_jobs)} job(s) still "
            "running",
            stage=stage,
        )

    return [_payload_of(job) for job in stage_jobs]


#: Statuses that mean this stage will not finish on its own.
_DEAD_STATUSES = frozenset(
    {JobStatus.FAILED, JobStatus.CANCELLED, JobStatus.EXPIRED}
)

#: Statuses that mean the stage is still going.
_OUTSTANDING_STATUSES = frozenset({JobStatus.PENDING} | IN_FLIGHT_JOB_STATUSES)


def _payload_of(job: ExternalJob) -> dict:
    """Read one finished job's result, marking it consumed the first time.

    Fetched on every pass rather than cached anywhere, because the
    result object outlives the job record and re-reading it is the
    cheapest way to have no fourth copy of the truth. A CONSUMED job is
    re-read too: the pipeline may have died after applying it, and the
    object is still exactly what it applied.

    :param job: A completed or consumed row.
    :returns: The stage's payload.
    :rtype: dict
    """
    payload = get_provider(job.provider).fetch_result(
        job, {"result_key": job.result_key}
    )
    if job.status != JobStatus.CONSUMED:
        ExternalJob.objects.filter(
            pk=job.pk, status=JobStatus.COMPLETED
        ).update(status=JobStatus.CONSUMED, consumed_at=timezone.now())
        logger.info("job %s consumed", job.pk)
    return payload


def await_detect(scan, pdf_path) -> list[dict]:
    """Run YOLO detection as a batched job.

    :param scan: The Scan being processed.
    :param pdf_path: Local path to the PDF to detect on.
    :returns: The merged detection list.
    :rtype: list[dict]
    :raises Awaiting: If the job has not finished.
    """
    from scanning import runpod_client

    payloads = await_stage(
        scan,
        JobStage.DETECT,
        JobEngine.BLACKLETTER,
        JobProvider.RUNPOD,
        input_key=runpod_client.ensure_input_key(scan, pdf_path),
        manifest={"models": ["small", "medium", "large"], "confidence": 0.20},
    )
    return payloads[0]["detections"]


def await_analyze(scan, pdf_path) -> list[dict]:
    """Run PaddleOCR page-number analysis as a batched job.

    :param scan: The Scan being processed.
    :param pdf_path: Local path to the PDF to analyze.
    :returns: The per-page results list.
    :rtype: list[dict]
    :raises Awaiting: If the job has not finished.
    """
    from scanning import runpod_client

    payloads = await_stage(
        scan,
        JobStage.ANALYZE,
        JobEngine.BLACKLETTER,
        JobProvider.RUNPOD,
        input_key=runpod_client.ensure_input_key(scan, pdf_path),
        manifest={
            "exp_start": scan.start_page or 1,
            "exp_end": scan.end_page,
            "max_pages": 9999,
        },
    )
    return payloads[0]["results"]


def advance_scan(scan_pk: int) -> str:
    """Run a scan's queued action as far as it will go this tick.

    The single entry point for moving a scan, whether it was just
    claimed off the queue or has been parked waiting on a GPU. Both
    call this, and it does the same thing either way: run the action
    from the top and see how far it gets.

    :param scan_pk: The scan to advance.
    :returns: The status the scan was left in.
    :rtype: str
    """
    from scanning import services

    scan = Scan.objects.filter(pk=scan_pk).first()
    if scan is None:
        logger.warning("cannot advance scan %s: it is gone", scan_pk)
        return ""

    action = scan.queued_action or QueuedAction.FULL_PIPELINE
    runner = _DISPATCH.get(action)
    if runner is None:
        logger.error("unknown queued_action %r for scan %s", action, scan_pk)
        Scan.objects.filter(pk=scan_pk).update(
            status=Status.ERROR,
            progress_message=f"Unknown action: {action}",
        )
        return Status.ERROR

    try:
        getattr(services, runner)(scan_pk)
    except Awaiting as exc:
        # Not a failure: the scan is mid-pipeline with its work on a
        # provider. Park it so nothing holds it and no process-death
        # timeout applies; the advance tick picks it up once its jobs
        # land.
        logger.info("scan %s is waiting on %s", scan_pk, exc.stage or "jobs")
        # Guarded like every other transition here: a scan cancelled or
        # re-queued while this tick was running has been moved on
        # purpose, and parking it would resurrect it.
        parked = Scan.objects.filter(
            pk=scan_pk, status=Status.PROCESSING
        ).update(
            status=Status.AWAITING,
            progress_message=str(exc)[:255],
        )
        if not parked:
            logger.info(
                "scan %s moved out of PROCESSING while it ran; leaving it "
                "to whoever moved it",
                scan_pk,
            )
        return Status.AWAITING if parked else ""
    except Exception:
        # The action handles its own failures; this is the backstop for
        # one raised before it could. Guarded on PROCESSING so it never
        # stomps a scan another process has already moved.
        logger.exception("scan %s (%s) failed", scan_pk, action)
        Scan.objects.filter(pk=scan_pk, status=Status.PROCESSING).update(
            status=Status.ERROR,
            progress_message="Unexpected error (check logs)",
        )
        return Status.ERROR

    return (
        Scan.objects.filter(pk=scan_pk)
        .values_list("status", flat=True)
        .first()
        or ""
    )


#: Queued action to the ``services`` function that runs it. Held by
#: name rather than by reference so importing this module does not drag
#: in ``services`` (and through it blackletter) on every caller.
_DISPATCH = {
    QueuedAction.FULL_PIPELINE: "run_full_pipeline",
    QueuedAction.VALIDATE: "run_validate_with_bitonal",
    QueuedAction.DETECT: "run_detect",
    QueuedAction.REPROCESS: "run_reprocess",
    QueuedAction.GENERATE_FILES: "run_generate_files",
}


def resumable_scans():
    """Return scans parked on jobs that have all landed.

    A scan is resumable when nothing it is waiting for is outstanding:
    every job either finished, or failed in a way that has run out of
    retries. Both want the pipeline re-entered -- one to apply the
    result, the other to mark the scan ERROR with the reason.

    :returns: Scans ready for another pass.
    :rtype: QuerySet
    """
    return Scan.objects.filter(status=Status.AWAITING).exclude(
        jobs__status__in=_OUTSTANDING_STATUSES
    )
