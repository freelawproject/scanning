"""The generalized YOLO detection stage: what to submit, and what a run
looks like.

Detection reads the **original** shards, never the converted ones. The
``bl_warm`` checkpoint was trained on greyscale renders, and its large
region classes collapse on 1-bit pages: issue #167 measured caption F1
falling from 0.99 to 0.25. dots.mocr wants the same input for the same
reason, so both stages fan out over the one shard set
``sharding.ensure_shards`` cut from the original (issue #164), and
neither has to cut its own.

One job per shard, tracked on ``ExternalJob`` rows
(:mod:`scanning.jobs`) at ``DETECT``/``BLACKLETTER``/``RUNPOD``. The
worker image is issue #194; this module is the caller it was waiting
for.

The run ends when every shard answers, with the rows at ``COMPLETED``
-- the provider is done, and we have applied nothing. There is
deliberately **no** glue and no ``CONSUMED`` here: issue #196 reads the
per-shard results, offsets each ``page_index`` by its shard's own
``from_page``, and turns the rows into ``Detection`` records and
redaction geometry. Until it lands, a finished run is a set of JSON
objects in S3 and nothing else.

Who starts it: the staff-only button
(``views_process.start_yolo_detect``), and nothing else. The pipeline
does **not** enqueue it. That is the whole point of this issue -- the
stage must be exercised on a few volumes before it runs over the corpus
(#211) -- and it is structural rather than a promise, because
:func:`ensure_detect_jobs` is the only creator of ``DETECT`` rows and an
abstract syntax tree (AST) test pins its caller set. The web process
only writes rows; the daemon submits, polls and retries them.
"""

from __future__ import annotations

import json
import logging
import tempfile
import time
from pathlib import Path

from django.conf import settings
from django.utils import timezone

from scanning import jobs, runpod_client, s3_sync
from scanning.models import (
    DEAD_JOB_STATUSES,
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

logger = logging.getLogger(__name__)

#: Handler action on the worker image. Its only one.
ACTION = "detect"

#: Weights the worker loads. Only ``bl_warm.pt`` is baked into the
#: image, and ``api.detect`` calls ``ensure_weights`` itself, so any
#: other name would reach Hugging Face from inside a paid job. The
#: worker refuses an unbaked name up front
#: (``handler._missing_weights``); running the legacy small/medium/large
#: trio again is a rebuild, not a changed input.
MODELS = ["bl_warm"]

#: Score below which a detection is dropped. blackletter's own
#: ``CONFIDENCE_THRESHOLD``. Low on purpose: the per-class gates that
#: decide what survives are applied later, by the redaction work (#196),
#: off the ``found_by`` provenance each row carries.
CONFIDENCE = 0.20

#: Per-row tuning keys this stage reads off ``input_manifest``, so an
#: experiment can override them without a deploy. Everything else there
#: describes the shard and must not be treated as a knob.
TUNING_KEYS = ("models", "confidence")

#: Render resolution of the worker, recorded on the merged document.
#: blackletter fixes it at 200 and :func:`build_payload` sends no
#: ``dpi`` field, so this is a statement about the pixel space every
#: bounding box is measured in, not a knob. It matches
#: ``DOCTOR_BITONAL_DPI`` and ``dots_mocr.DPI``.
DPI = 200

#: Version of the merged volume document. Independent of the worker's
#: ``RESULT_SCHEMA_VERSION``: the envelopes are the wire format, this is
#: the stored one, and they change for different reasons.
MERGE_SCHEMA_VERSION = 1

#: How many times :func:`finish_ready_runs` tries the merge before it
#: gives up on a run. The merge is local work, so a failure is not the
#: provider's fault and retries no job -- but the pass runs every
#: collect tick, and an unbounded retry of a deterministic bug would
#: re-download every shard result every 15 seconds, forever.
MERGE_MAX_ATTEMPTS = 3

#: Prefix of the merge's scratch directory in the system temp dir.
#: ``cleanup_processing_tmp`` sweeps leaked ones (a SIGKILL mid-merge
#: skips the ``TemporaryDirectory`` cleanup), so the name is shared
#: rather than inlined (#215).
MERGE_TMP_PREFIX = "yolodetect-"

#: How many times the apply may run before the pass gives up on a run.
#: Same reason as ``MERGE_MAX_ATTEMPTS``: :func:`queue_ready_runs` runs
#: every collect tick, and an unbounded retry of a deterministic bug
#: would queue an hour of page rendering, forever.
APPLY_MAX_ATTEMPTS = 3

#: The one status the apply is queued from. Review 2 follows review 1,
#: so a merged run waits for the approval rather than overtaking it.
#: Every other status is deferred, never marked: a scan approved into
#: step 3, errored or re-queued comes back through the admin re-queue.
APPLY_STATUS = Status.PAGE_COMPLETENESS_REVIEW_DONE


class DetectMergeError(Exception):
    """A completed detection run could not be merged into one document."""


def enabled() -> bool:
    """Return whether detection jobs may be dispatched.

    Both switches, because they fail differently: ``YOLO_ENABLED`` is
    the operator's decision to spend money on this stage, and
    :func:`runpod_client.enabled` covers the account credentials and
    this engine's own endpoint id.

    :returns: Whether the stage should run.
    :rtype: bool
    """
    return bool(
        settings.YOLO_ENABLED
        and runpod_client.enabled(settings.RUNPOD_YOLO_ENDPOINT_ID)
    )


def build_payload(job: ExternalJob, input_url: str, output_url: str) -> dict:
    """Return the RunPod ``input`` payload for one shard.

    ``result_key`` travels beside ``result_url`` so the worker can name
    the object it wrote in its response summary, and we can check that
    it wrote where it was authorized to. The URL is the capability; the
    key is the label.

    There is deliberately no ``dpi`` field. blackletter fixes the render
    resolution at 200, which matches ``DOCTOR_BITONAL_DPI`` and the
    dots.mocr constant, so every bounding box in the corpus describes
    one pixel space. There is no ``max_pages`` field either: the worker
    refuses a shard over its own ceiling, and a partial detection merged
    as a whole volume is worse than a failure.

    :param job: The claimed row. Its ``result_key`` is already set.
    :param input_url: Presigned GET of the shard PDF.
    :param output_url: Presigned PUT for the result JSON, signed with
        ``runpod_client.RESULT_CONTENT_TYPE``.
    :returns: The ``input`` dict to POST.
    :rtype: dict
    """
    tuning = {
        key: job.input_manifest[key]
        for key in TUNING_KEYS
        if key in (job.input_manifest or {})
    }
    return {
        "action": ACTION,
        # The worker tags its Sentry events with this.
        "scan_pk": job.scan_id,
        "pdf_url": input_url,
        "result_url": output_url,
        "result_key": job.result_key,
        "models": list(MODELS),
        "confidence": CONFIDENCE,
        **tuning,
    }


def ensure_detect_jobs(scan, manifest: dict) -> list[ExternalJob]:
    """Return the live detection jobs for ``scan``, creating them if the
    current run does not describe today's shard set.

    Idempotent, so a second press of the start button is a no-op rather
    than a second run over shards already read. A run holding a dead row
    (failed, cancelled, expired) is replaced instead, since nothing will
    move it again.

    A replacement run does not re-detect shards already detected:
    ``reuse_results`` carries a prior result forward whenever the
    shard's identity is unchanged and its result object is still on S3
    (``jobs._reusable_results``). This engine can carry, because nothing
    deletes its per-shard results -- #196 reads them, and a re-read must
    never cost a second paid run. The bitonal merge deletes its results,
    so the convert stage must not carry.

    :param scan: The scan to detect over.
    :param manifest: The committed shard manifest.
    :returns: The live run's rows, ordered by shard index.
    :rtype: list[ExternalJob]
    """
    return jobs.ensure_shard_jobs(
        scan,
        manifest,
        stage=JobStage.DETECT,
        engine=JobEngine.BLACKLETTER,
        provider=JobProvider.RUNPOD,
        reuse_results=True,
    )


def live_detect_jobs(scan) -> list[ExternalJob]:
    """Return a scan's current-run detection rows, in page order.

    :param scan: The scan (or its pk) to look up.
    :returns: The live run's rows, or an empty list when the stage has
        never run for this scan.
    :rtype: list[ExternalJob]
    """
    return jobs.live_run(scan, JobStage.DETECT, JobEngine.BLACKLETTER)


def run_summary(scan) -> dict | None:
    """Describe a scan's detection run for the process page.

    See :func:`jobs.run_summary`, which every engine shares.

    :param scan: The scan (or its pk) to describe.
    :returns: The summary dict, or ``None`` when the stage has never
        run for this scan.
    :rtype: dict | None
    """
    return jobs.run_summary(scan, JobStage.DETECT, JobEngine.BLACKLETTER)


def merged_result_key(scan, run: int) -> str:
    """Return the S3 key one run's merged volume document lives at.

    Under ``jobs/`` on purpose: that prefix is already excluded from the
    generic processing sync in both directions, and the admin delete
    already sweeps it. Scoped to the run, like the per-attempt result
    keys, so a re-run leaves the previous document addressable instead
    of stomping it.

    :param scan: The scan the run belongs to.
    :param run: The run number.
    :returns: Key of the form ``{processing_prefix}jobs/detect/
        blackletter/r{run}-volume.json``.
    :rtype: str
    """
    return (
        f"{s3_sync.s3_processing_prefix(scan)}{s3_sync.JOB_RESULTS_SUBDIR}"
        f"{JobStage.DETECT}/{JobEngine.BLACKLETTER}/r{run}-volume.json"
    )


def _check_envelope(scan, job: ExternalJob, envelope) -> dict:
    """Return an envelope's payload, or refuse the envelope.

    A thin wrapper over :func:`jobs.check_result_envelope`, which every
    stage that reads a worker's result shares. This one binds the
    action and the error type of this stage.

    :param scan: The scan being merged.
    :param job: The row whose result the envelope claims to be.
    :param envelope: The parsed JSON found at ``job.result_key``.
    :returns: ``envelope["payload"]``.
    :rtype: dict
    :raises DetectMergeError: If the envelope is not one this attempt
        should have produced.
    """
    return jobs.check_result_envelope(
        scan, job, envelope, ACTION, DetectMergeError
    )


def merge_detect_results(scan, detect_jobs: list[ExternalJob]) -> str:
    """Merge one run's shard payloads into a volume document on S3.

    Merges in strict shard order and asserts the page arithmetic:
    ``page_index`` counts from zero inside a shard, so a page's volume
    index is the shard's ``from_page`` plus that value, and its 1-based
    ``pdf_page`` is the volume index plus one. That is the same offset
    the dots.mocr glue applies (#202), over the same shard set.

    The check is weaker than the dots.mocr one, and it has to be: that
    payload lists every page, so a lost page is visible, while this one
    lists detections only, and a page with no detection reads exactly
    like a page the worker skipped. What can be checked is checked --
    the shard sequence, the shard's own page count as the worker saw
    it, and every page index inside its shard.

    Idempotent: it rebuilds from the result objects every time, so a
    daemon killed between the upload and the ``CONSUMED`` write simply
    merges again.

    ``found_by`` rides along on every detection, deliberately. The
    confidence gates are per model family since blackletter #73
    (``label_confidence(label, bl_warm)``), and that provenance is how
    every reader downstream picks the right family.

    :param scan: The scan whose run finished.
    :param detect_jobs: The live run's rows, ordered by shard index.
    :returns: The S3 key the document was uploaded to.
    :rtype: str
    :raises DetectMergeError: If a result is missing or malformed, or
        the page arithmetic does not add up to the volume.
    """
    if not detect_jobs:
        raise DetectMergeError(f"scan {scan.pk} has no detection jobs")

    expected_total = detect_jobs[0].input_manifest.get("source_page_count")
    run = detect_jobs[0].run
    started = time.monotonic()

    detections: list[dict] = []
    shards: list[dict] = []
    models: list[str] = []
    next_page = 0
    # A temp dir, not the output dir: the generic S3 sync sweeps up
    # everything there, and these are wire artifacts that stay out of it.
    with tempfile.TemporaryDirectory(
        prefix=f"{MERGE_TMP_PREFIX}{scan.pk}-"
    ) as tmp:
        tmp_dir = Path(tmp)
        for index, job in enumerate(detect_jobs):
            if job.shard_index != index:
                raise DetectMergeError(
                    f"scan {scan.pk} shard sequence breaks at position "
                    f"{index}: job {job.pk} covers shard {job.shard_index}"
                )
            if not job.result_key:
                raise DetectMergeError(
                    f"scan {scan.pk} shard {index} has no result key"
                )
            manifest = job.input_manifest or {}
            from_page = manifest.get("from_page")
            page_count = manifest.get("page_count")
            if from_page != next_page or not isinstance(page_count, int):
                raise DetectMergeError(
                    f"scan {scan.pk} shard {index} covers pages from "
                    f"{from_page}, expected {next_page}"
                )

            local = tmp_dir / f"{index:04d}.json"
            s3_sync.download_object(job.result_key, local)
            payload = _check_envelope(scan, job, json.loads(local.read_text()))

            answered = payload.get("page_count")
            if answered != page_count:
                raise DetectMergeError(
                    f"scan {scan.pk} shard {index} read {answered} page(s), "
                    f"the shard has {page_count}"
                )
            shard_models = list(payload.get("models") or [])
            if not models:
                models = shard_models
            elif shard_models != models:
                raise DetectMergeError(
                    f"scan {scan.pk} shard {index} ran {shard_models}, "
                    f"shard 0 ran {models}"
                )

            for row in payload.get("detections") or []:
                page_no = row.get("page_index")
                if not isinstance(page_no, int) or not (
                    0 <= page_no < page_count
                ):
                    raise DetectMergeError(
                        f"scan {scan.pk} shard {index} has a detection on "
                        f"page {page_no}, the shard has {page_count}"
                    )
                page_index = from_page + page_no
                detections.append(
                    {
                        **row,
                        "page_index": page_index,
                        "pdf_page": page_index + 1,
                        "shard_index": index,
                    }
                )

            entry = {
                "name": manifest.get("name"),
                "index": index,
                "from_page": from_page,
                "to_page": manifest.get("to_page"),
                "page_count": page_count,
                "attempt": job.attempt,
                "result_key": job.result_key,
                "duration_ms": payload.get("duration_ms"),
            }
            tuning = {
                key: manifest[key] for key in TUNING_KEYS if key in manifest
            }
            if tuning:
                entry["tuning"] = tuning
            shards.append(entry)
            next_page += page_count

    if expected_total is not None and next_page != expected_total:
        raise DetectMergeError(
            f"scan {scan.pk} merged {next_page} page(s), the original has "
            f"{expected_total}"
        )

    document = {
        "schema_version": MERGE_SCHEMA_VERSION,
        "engine": str(JobEngine.BLACKLETTER),
        "action": ACTION,
        "scan_pk": scan.pk,
        "run": run,
        "source_page_count": expected_total,
        # The apply refuses a document cut from another original. The
        # shard identity ties the *rows* to today's bytes, but the
        # document outlives the run that wrote it.
        "source_fingerprint": scan.source_fingerprint,
        "dpi": DPI,
        "models": models,
        "confidence": CONFIDENCE,
        "generated_at": timezone.now().isoformat(),
        "shards": shards,
        "detections": detections,
        "pages_with_detections": len(
            {row["page_index"] for row in detections}
        ),
    }
    key = merged_result_key(scan, run)
    if not s3_sync.upload_json_object(key, document):
        raise DetectMergeError(
            f"scan {scan.pk}: the merged document could not be uploaded "
            f"to {key}"
        )
    logger.info(
        "Merged %d detection shard(s) for scan %s into %s (%d detection(s) "
        "over %d page(s)) in %.1fs",
        len(detect_jobs),
        scan.pk,
        key,
        len(detections),
        document["pages_with_detections"],
        time.monotonic() - started,
    )
    return key


def load_merged_document(scan, run: int) -> dict:
    """Read one run's merged document, and refuse a stale one.

    :param scan: The scan the run belongs to.
    :param run: The run number.
    :returns: The stored document.
    :rtype: dict
    :raises DetectMergeError: If the document is absent or unreadable,
        carries another reader's version, or describes another
        original.
    """
    key = merged_result_key(scan, run)
    try:
        document = s3_sync.download_json_object(key)
    except Exception as exc:
        raise DetectMergeError(
            f"scan {scan.pk}: the merged document at {key} could not be "
            f"read: {exc}"
        ) from exc
    if not isinstance(document, dict):
        raise DetectMergeError(
            f"scan {scan.pk}: the object at {key} is not a document"
        )
    version = document.get("schema_version")
    if version != MERGE_SCHEMA_VERSION:
        raise DetectMergeError(
            f"scan {scan.pk} merged document is schema {version}; this "
            f"reader knows {MERGE_SCHEMA_VERSION}"
        )
    stored = document.get("source_fingerprint") or ""
    # A blank fingerprint on either side is a volume sharded before the
    # stamp existed, and it matches anything -- the rule the page edits
    # already use (#214).
    if (
        stored
        and scan.source_fingerprint
        and stored != scan.source_fingerprint
    ):
        raise DetectMergeError(
            f"scan {scan.pk} merged document describes another original "
            f"({stored} against {scan.source_fingerprint}); detect again"
        )
    return document


def _merge_attempts(detect_jobs: list[ExternalJob]) -> int:
    """Return how many times this run's merge has already failed.

    Kept on the first row's ``provider_meta`` rather than a field: the
    counter describes the run, any row of it can carry that, and
    ``input_manifest`` is off limits (``_still_describes`` compares it
    exactly, so an added key would read as a stale run).

    :param detect_jobs: The live run's rows, ordered by shard index.
    :returns: The stored attempt count, 0 when none.
    :rtype: int
    """
    meta = detect_jobs[0].provider_meta or {}
    return int((meta.get("merge") or {}).get("attempts") or 0)


def _record_merge_failure(scan, detect_jobs: list[ExternalJob], exc) -> None:
    """Count one merge failure, and give up loudly on the last one.

    The rows stay ``COMPLETED`` either way: the results are paid for and
    still in S3, so the retry costs a download rather than a RunPod run.
    The crossing into "out of tries" is the one ERROR-level event -- the
    pass runs every tick, and a log line on every skip after it would
    bury Sentry in duplicates of a failure that is not going to change.
    The way back after a fix is a person who clears
    ``provider_meta["merge"]`` on the named row.

    :param scan: The scan whose merge failed.
    :param detect_jobs: The live run's rows, ordered by shard index.
    :param exc: What the merge raised.
    :return: None.
    """
    head = detect_jobs[0]
    meta = head.provider_meta or {}
    merge = dict(meta.get("merge") or {})
    attempts = int(merge.get("attempts") or 0) + 1
    merge.update(
        {
            "attempts": attempts,
            "last_error": str(exc)[:500],
            "last_attempt_at": timezone.now().isoformat(),
        }
    )
    meta["merge"] = merge
    head.provider_meta = meta
    head.save(update_fields=["provider_meta"])
    if attempts >= MERGE_MAX_ATTEMPTS:
        logger.exception(
            "Merging the detection results for scan %s failed; giving up "
            "after %d attempt(s). The rows stay COMPLETED and the results "
            "stay in S3; clear provider_meta['merge'] on job %s to retry.",
            scan.pk,
            attempts,
            head.pk,
        )
    else:
        logger.warning(
            "Merging the detection results for scan %s failed (attempt %d "
            "of %d): %s",
            scan.pk,
            attempts,
            MERGE_MAX_ATTEMPTS,
            exc,
        )


def finish_ready_runs() -> int:
    """Merge every finished detection run into its volume document.

    Runs on the collect tick, next to ``dots_mocr.finish_ready_runs``. A
    run is finished when no row of it waits to be submitted or is in
    flight, and none is dead: a dead row means the run can never cover
    the volume, ``run_summary`` already shows it on the process page,
    and the way forward is the staff button, which opens a fresh run.

    Like the detection stage itself, this pass writes **no scan
    status** (#195). With no status to latch on, the rows are the
    idempotence marker: a merged run is all ``CONSUMED``, so it drops
    out of the candidate query and becomes :func:`queue_ready_runs`'s
    input.

    **The per-shard results stay.** Issue #196 asks for that directly:
    a page insert or a page replacement computes the merge again from
    them. The bitonal merge deletes its results, and this stage must
    not copy that.

    :returns: How many runs were merged and consumed.
    :rtype: int
    """
    if not s3_sync.s3_active():
        return 0

    scan_ids = (
        Scan.objects.filter(
            jobs__stage=JobStage.DETECT,
            jobs__engine=JobEngine.BLACKLETTER,
            jobs__provider=JobProvider.RUNPOD,
            jobs__status=JobStatus.COMPLETED,
        )
        .values_list("pk", flat=True)
        .distinct()
    )
    unfinished = {JobStatus.PENDING} | IN_FLIGHT_JOB_STATUSES
    merged = 0
    for scan in Scan.objects.filter(pk__in=list(scan_ids)).select_related(
        "reporter"
    ):
        rows = live_detect_jobs(scan)
        if not rows:
            continue
        if any(row.status in unfinished for row in rows):
            continue
        if any(row.status in DEAD_JOB_STATUSES for row in rows):
            continue
        if not any(row.status == JobStatus.COMPLETED for row in rows):
            # The candidate row belongs to an older run; the live one
            # has nothing to merge.
            continue
        if _merge_attempts(rows) >= MERGE_MAX_ATTEMPTS:
            continue

        try:
            merge_detect_results(scan, rows)
        except Exception as exc:
            _record_merge_failure(scan, rows, exc)
            continue

        ExternalJob.objects.filter(
            pk__in=[row.pk for row in rows],
            status=JobStatus.COMPLETED,
        ).update(status=JobStatus.CONSUMED, consumed_at=timezone.now())
        merged += 1

    return merged


def apply_state(detect_jobs: list[ExternalJob]) -> dict:
    """Return the run's apply bookkeeping.

    Kept on the first row's ``provider_meta`` like the merge counter,
    and for the same reasons: the state describes the run, the rows are
    per run so a fresh detection run starts clean, and
    ``input_manifest`` is off limits.

    The fields: ``queued_at`` while the daemon owes the scan a run of
    the apply, ``attempts`` and ``last_error`` for the failures, and
    ``applied_at`` once it worked.

    :param detect_jobs: The live run's rows, ordered by shard index.
    :returns: The stored state; empty when the apply never ran.
    :rtype: dict
    """
    meta = detect_jobs[0].provider_meta or {}
    return dict(meta.get("apply") or {})


def write_apply_state(detect_jobs: list[ExternalJob], state: dict) -> None:
    """Persist the run's apply bookkeeping on the first row.

    :param detect_jobs: The live run's rows, ordered by shard index.
    :param state: The state to store.
    :return: None.
    """
    head = detect_jobs[0]
    meta = head.provider_meta or {}
    meta["apply"] = state
    head.provider_meta = meta
    head.save(update_fields=["provider_meta"])


def record_apply_start(detect_jobs: list[ExternalJob]) -> None:
    """Drop the queue claim, because the apply runs now.

    The claim is what stops :func:`queue_ready_runs` from queueing the
    same scan on every 15-second tick. It must go when the work starts,
    or a failed apply would never reach the queue again.

    :param detect_jobs: The live run's rows, ordered by shard index.
    :return: None.
    """
    state = apply_state(detect_jobs)
    state.pop("queued_at", None)
    write_apply_state(detect_jobs, state)


def record_apply_success(detect_jobs: list[ExternalJob]) -> None:
    """Stamp the run as applied, so nothing queues it again.

    :param detect_jobs: The live run's rows, ordered by shard index.
    :return: None.
    """
    state = apply_state(detect_jobs)
    state.pop("queued_at", None)
    state["applied_at"] = timezone.now().isoformat()
    write_apply_state(detect_jobs, state)


def record_apply_failure(scan, detect_jobs: list[ExternalJob], exc) -> None:
    """Count one apply failure, and give up loudly on the last one.

    Mirrors :func:`_record_merge_failure`: the merged document and the
    shard results stay in S3, so a retry costs one small download and
    the page rendering, never a second paid run. The crossing into "out
    of tries" is the one ERROR-level event; the way back after a fix is
    a person who clears ``provider_meta["apply"]`` on the named row.

    :param scan: The scan whose apply failed.
    :param detect_jobs: The live run's rows, ordered by shard index.
    :param exc: What the apply raised.
    :return: None.
    """
    state = apply_state(detect_jobs)
    attempts = int(state.get("attempts") or 0) + 1
    state.pop("queued_at", None)
    state.update(
        {
            "attempts": attempts,
            "last_error": str(exc)[:500],
            "last_attempt_at": timezone.now().isoformat(),
        }
    )
    write_apply_state(detect_jobs, state)
    if attempts >= APPLY_MAX_ATTEMPTS:
        logger.exception(
            "Applying the detection results for scan %s failed; giving up "
            "after %d attempt(s). The merged document stays in S3; clear "
            "provider_meta['apply'] on job %s to retry.",
            scan.pk,
            attempts,
            detect_jobs[0].pk,
        )
    else:
        logger.warning(
            "Applying the detection results for scan %s failed (attempt %d "
            "of %d): %s",
            scan.pk,
            attempts,
            APPLY_MAX_ATTEMPTS,
            exc,
        )


def queue_ready_runs() -> int:
    """Queue the apply for every merged run that has none.

    The collect tick's last pass, and a trigger rather than the work
    itself. It writes one status and returns.

    **The apply must not run here.** The tick runs every 15 seconds and
    the daemon scheduler is serial (#156), while the geometry the apply
    computes renders every page of the volume three times: 83 seconds
    for a volume of 1364 pages, measured. A pass that did that inline
    would stop every submit and every poll in the daemon for minutes.
    So this queues ``COMPUTE_REDACTIONS``, and ``process_next_scan``
    runs it where the long stages already run.

    That is the one place this differs from the page-number apply
    (#204), which stays off the queue because it is seconds of work
    over a JSON file.

    Only ``PAGE_COMPLETENESS_REVIEW_DONE`` is taken, with a
    compare-and-swap: review 2 follows review 1, and a scan that is
    approved, errored or busy is deferred without a mark, so it comes
    back when it holds that status again.

    :returns: How many scans were queued.
    :rtype: int
    """
    if not s3_sync.s3_active():
        return 0

    scan_ids = (
        Scan.objects.filter(
            jobs__stage=JobStage.DETECT,
            jobs__engine=JobEngine.BLACKLETTER,
            jobs__provider=JobProvider.RUNPOD,
            jobs__status=JobStatus.CONSUMED,
        )
        .values_list("pk", flat=True)
        .distinct()
    )
    queued = 0
    for scan in Scan.objects.filter(
        pk__in=list(scan_ids), status=APPLY_STATUS
    ).select_related("reporter"):
        rows = live_detect_jobs(scan)
        if not rows:
            continue
        if any(row.status != JobStatus.CONSUMED for row in rows):
            # The candidate row belongs to an older run; the live one
            # is not merged yet.
            continue
        state = apply_state(rows)
        if state.get("applied_at"):
            continue
        # ``queued_at`` is an audit stamp, never a guard. A scan whose
        # apply is really pending has left this status, so the filter
        # above excludes it; a scan back in this status with the stamp
        # still on it lost its claim (an admin re-queue takes that
        # path), and it must be queued again rather than stranded
        # without its geometry.
        if int(state.get("attempts") or 0) >= APPLY_MAX_ATTEMPTS:
            continue

        claimed = Scan.objects.filter(pk=scan.pk, status=APPLY_STATUS).update(
            status=Status.QUEUED,
            queued_action=QueuedAction.COMPUTE_REDACTIONS,
            progress_message=(
                "Detection finished. The redactions are queued for "
                "computation."
            ),
            progress_current=0,
            progress_total=0,
        )
        if not claimed:
            # Somebody moved the scan between the read and the write.
            # No mark: the next tick sees it again.
            continue
        state["queued_at"] = timezone.now().isoformat()
        write_apply_state(rows, state)
        logger.info(
            "Queued the redaction computation for scan %s (detection run %s)",
            scan.pk,
            rows[0].run,
        )
        queued += 1

    return queued
