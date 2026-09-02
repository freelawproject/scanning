"""The dots.mocr stage: what to submit, and what a run looks like.

dots.mocr reads the **original** shards, not the converted ones: it
wants the greyscale scan its layout model was trained on, and the
bitonal pass is a display artifact. Both stages therefore fan out over
the one shard set ``sharding.ensure_shards`` cut from the original
(issue #164), which is why neither has to cut its own.

One job per shard, tracked on ``ExternalJob`` rows
(:mod:`scanning.jobs`) at ``ANALYZE``/``DOTS_MOCR``/``RUNPOD``. The
stage is forced rather than chosen: the ``job_opinion_matches_stage``
constraint allows only ``CONVERT``, ``DETECT`` and ``ANALYZE`` at volume
level, and ``ANALYZE`` is what the page-number adapter (issue #149)
consumes.

The run itself ends when every shard answers, with the rows at
``COMPLETED`` -- the provider is done, and we have applied nothing yet.
The glue (issue #202) follows on the daemon's collect tick:
:func:`finish_ready_runs` reads each row's ``input_manifest``, offsets
each page by its shard's ``from_page``, writes one volume-level JSON to
a run-scoped S3 key, and flips the rows to ``CONSUMED``. The per-shard
result objects are deliberately **kept**: a future smart glue over page
inserts and deletes re-reads them, so nothing may delete a raw input.
Reading page numbers out of the glued JSON is issue #149.

Who starts it: the upload pipeline (``services.run_full_pipeline``,
issue #207) creates the rows for every new scan, next to the convert
rows and gated the same way (``services._can_analyze``). The staff-only
button (``views_process.start_dots_mocr``) remains as the manual way
in: a re-run over an edited volume, or a backfill for scans uploaded
while the stage was button-only (#190). Either way the web process and
the pipeline only write rows; the daemon submits, polls and retries
them.
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
    Scan,
    Status,
)

logger = logging.getLogger(__name__)

#: Handler action on the worker image. Its only one.
ACTION = "parse"

#: Render resolution. Matches ``DOCTOR_BITONAL_DPI`` deliberately, so a
#: cell's bounding box describes the same pixel space as everything else
#: in the corpus -- which is what lets issue #149 reuse the head/foot
#: band constants from ai-research without rescaling.
#:
#: A module constant rather than a setting: there is no operational
#: reason to retune this per deploy, and a one-off experiment writes
#: ``{"dpi": 400}`` onto the row's ``input_manifest`` instead.
DPI = 200

#: Prompt mode. ``prompt_layout_all_en`` returns both ``cells`` (bounding
#: box, category and text per region) and ``md``. Issue #149 needs the
#: cells to find the running-head region and the text to read the page
#: number out of it, so the cheaper layout-only and text-only modes do
#: not serve it.
PROMPT_MODE = "prompt_layout_all_en"

#: Per-row tuning keys this stage reads off ``input_manifest``, so an
#: experiment can override them without a deploy. Everything else there
#: describes the shard and must not be treated as a knob.
TUNING_KEYS = ("dpi", "prompt_mode")

#: Version of the glued volume document. Independent of the worker's
#: ``RESULT_SCHEMA_VERSION``: the envelopes are the wire format, this is
#: the stored one, and they change for different reasons.
GLUE_SCHEMA_VERSION = 1

#: How many times :func:`finish_ready_runs` tries the glue before it
#: gives up on a scan. The glue is the one local step, so a failure is
#: not the provider's fault and retries no job -- but the pass runs
#: every collect tick, and an unbounded retry of a deterministic bug
#: would re-download every shard result every 15 seconds, forever.
GLUE_MAX_ATTEMPTS = 3

#: Prefix of the glue's scratch directory in the system temp dir.
#: ``cleanup_processing_tmp`` sweeps leaked ones (a SIGKILL mid-glue
#: skips the ``TemporaryDirectory`` cleanup), so the name is shared
#: rather than inlined (#215).
GLUE_TMP_PREFIX = "dotsmocr-"

#: How many times :func:`apply_ready_runs` tries the apply (#149/#204)
#: before it gives up on a run, for the same reason as
#: ``GLUE_MAX_ATTEMPTS``: the pass runs every collect tick, and an
#: unbounded retry of a deterministic bug would recompute -- and log --
#: forever.
APPLY_MAX_ATTEMPTS = 3

#: The statuses the apply pass acts on: the two parks it takes to
#: READY, and READY itself for a recompute. One tuple, because
#: ``reapply_page_numbers`` must offer the pass exactly the scans the
#: pass will read -- a scan outside them would hold a cleared stamp
#: that nothing reads.
APPLY_STATUSES = (
    Status.AWAITING_VALIDATION,
    Status.PENDING_REVIEW,
    Status.READY_FOR_PAGE_COMPLETENESS_REVIEW,
)


class DotsMocrGlueError(Exception):
    """A completed dots.mocr run could not be glued into one document."""


def enabled() -> bool:
    """Return whether dots.mocr jobs may be dispatched.

    Both switches, because they fail differently: ``DOTS_MOCR_ENABLED``
    is the operator's decision to spend money on this stage, and
    :func:`runpod_client.enabled` covers the account credentials and
    this engine's own endpoint id.

    :returns: Whether the stage should run.
    :rtype: bool
    """
    return bool(
        settings.DOTS_MOCR_ENABLED
        and runpod_client.enabled(settings.RUNPOD_DOTSMOCR_ENDPOINT_ID)
    )


def build_payload(job: ExternalJob, input_url: str, output_url: str) -> dict:
    """Return the RunPod ``input`` payload for one shard.

    ``result_key`` travels beside ``result_url`` so the worker can name
    the object it wrote in its response summary, and we can check that
    it wrote where it was authorized to. The URL is the capability; the
    key is the label.

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
        "prompt_mode": PROMPT_MODE,
        "dpi": DPI,
        **tuning,
    }


def ensure_analyze_jobs(
    scan, manifest: dict, *, force_new_run: bool = False
) -> list[ExternalJob]:
    """Return the live dots.mocr jobs for ``scan``, creating them if
    the current run does not describe today's shard set.

    Idempotent, so a second press of the start button is a no-op rather
    than a second run over shards already read. A run holding a dead row
    (failed, cancelled, expired) is replaced instead, since nothing will
    move it again.

    A replacement run does not re-read shards already read:
    ``reuse_results`` carries a prior result forward whenever the
    shard's identity is unchanged and its result object is still on S3
    (``jobs._reusable_results``). This engine can carry, because its
    per-shard results are deliberately kept after the apply; the
    bitonal merge deletes its results, so the convert stage must not.
    A result with unread pages is never carried (#238), which is what
    makes ``force_new_run`` the backfill for those: the new run re-pays
    the shards with a hole and nothing else.

    :param scan: The scan to read.
    :param manifest: The committed shard manifest.
    :param force_new_run: Replace a whole, reusable live run. Only the
        ``reread_failed_pages`` command passes it.
    :returns: The live run's rows, ordered by shard index.
    :rtype: list[ExternalJob]
    """
    return jobs.ensure_shard_jobs(
        scan,
        manifest,
        stage=JobStage.ANALYZE,
        engine=JobEngine.DOTS_MOCR,
        provider=JobProvider.RUNPOD,
        reuse_results=True,
        force_new_run=force_new_run,
    )


def shards_with_holes(rows: list[ExternalJob]) -> list[ExternalJob]:
    """Return the rows of a run whose worker left pages unread.

    :param rows: A run's rows.
    :returns: Those with a non-empty ``failed_pages`` in their summary.
    :rtype: list[ExternalJob]
    """
    return [row for row in rows if jobs.has_failed_pages(row)]


def live_analyze_jobs(scan) -> list[ExternalJob]:
    """Return a scan's current-run dots.mocr rows, in page order.

    :param scan: The scan (or its pk) to look up.
    :returns: The live run's rows, or an empty list when the stage has
        never run for this scan.
    :rtype: list[ExternalJob]
    """
    return jobs.live_run(scan, JobStage.ANALYZE, JobEngine.DOTS_MOCR)


def run_summary(scan) -> dict | None:
    """Describe a scan's dots.mocr run for the process page.

    See :func:`jobs.run_summary`, which every engine shares.

    :param scan: The scan (or its pk) to describe.
    :returns: The summary dict, or ``None`` when the stage has never
        run for this scan.
    :rtype: dict | None
    """
    return jobs.run_summary(scan, JobStage.ANALYZE, JobEngine.DOTS_MOCR)


def glued_result_key(scan, run: int) -> str:
    """Return the S3 key one run's glued volume document lives at.

    Under ``jobs/`` on purpose: that prefix is already excluded from the
    generic processing sync in both directions, and the admin delete
    already sweeps it. Scoped to the run, like the per-attempt result
    keys, so a re-run leaves the previous glue addressable instead of
    stomping it.

    :param scan: The scan the run belongs to.
    :param run: The run number.
    :returns: Key of the form ``{processing_prefix}jobs/analyze/
        dots_mocr/r{run}-volume.json``.
    :rtype: str
    """
    return (
        f"{s3_sync.s3_processing_prefix(scan)}{s3_sync.JOB_RESULTS_SUBDIR}"
        f"{JobStage.ANALYZE}/{JobEngine.DOTS_MOCR}/r{run}-volume.json"
    )


def _check_envelope(scan, job: ExternalJob, envelope) -> dict:
    """Return an envelope's payload, or refuse the envelope.

    The checks close delivery faults, not model quality: the object at
    the row's key must be an envelope this reader understands, written
    for this scan by this attempt. An unknown ``schema_version`` usually
    means a worker deployed ahead of the daemon, so the message names
    both versions rather than guessing at the content.

    :param scan: The scan being glued.
    :param job: The row whose result the envelope claims to be.
    :param envelope: The parsed JSON found at ``job.result_key``.
    :returns: ``envelope["payload"]``.
    :rtype: dict
    :raises DotsMocrGlueError: If the envelope is not one this attempt
        should have produced.
    """
    if not isinstance(envelope, dict) or "payload" not in envelope:
        raise DotsMocrGlueError(
            f"scan {scan.pk} shard {job.shard_index}: the object at "
            f"{job.result_key} is not a result envelope"
        )
    version = envelope.get("schema_version")
    if version != runpod_client.RESULT_SCHEMA_VERSION:
        raise DotsMocrGlueError(
            f"scan {scan.pk} shard {job.shard_index} answered result "
            f"schema {version}; this reader knows "
            f"{runpod_client.RESULT_SCHEMA_VERSION}"
        )
    for field, expected in (
        ("action", ACTION),
        ("scan_pk", scan.pk),
        ("result_key", job.result_key),
    ):
        got = envelope.get(field)
        if got != expected:
            raise DotsMocrGlueError(
                f"scan {scan.pk} shard {job.shard_index} envelope has "
                f"{field}={got!r}, expected {expected!r}"
            )
    return envelope["payload"]


def merge_dotsmocr_results(scan, analyze_jobs: list[ExternalJob]) -> str:
    """Glue one run's shard payloads into a volume document on S3.

    Glues in strict shard order and asserts the page arithmetic:
    ``page_no`` counts from zero inside a shard, so a page's volume
    index is the shard's ``from_page`` plus its ``page_no``, and its
    1-based ``pdf_page`` is that plus one. The glue is naive by design
    (issue #202): a page the worker could not read keeps its slot with
    its ``error``, and page inserts and deletes are a later, smarter
    glue that will re-read the per-shard objects this one leaves in
    place.

    Idempotent: it rebuilds from the result objects every time, so a
    daemon killed between the upload and the CONSUMED write just glues
    again.

    :param scan: The scan whose run finished.
    :param analyze_jobs: The live run's rows, ordered by shard index.
    :returns: The S3 key the document was uploaded to.
    :rtype: str
    :raises DotsMocrGlueError: If a result is missing, malformed, or
        the page arithmetic does not add up to the volume.
    """
    if not analyze_jobs:
        raise DotsMocrGlueError(f"scan {scan.pk} has no dots.mocr jobs")

    expected_total = analyze_jobs[0].input_manifest.get("source_page_count")
    run = analyze_jobs[0].run
    started = time.monotonic()

    pages: list[dict] = []
    shards: list[dict] = []
    next_page = 0
    # A temp dir, not the output dir: the generic S3 sync sweeps up
    # everything there, and these are wire artifacts that stay out of it.
    with tempfile.TemporaryDirectory(
        prefix=f"{GLUE_TMP_PREFIX}{scan.pk}-"
    ) as tmp:
        tmp_dir = Path(tmp)
        for index, job in enumerate(analyze_jobs):
            if job.shard_index != index:
                raise DotsMocrGlueError(
                    f"scan {scan.pk} shard sequence breaks at position "
                    f"{index}: job {job.pk} covers shard {job.shard_index}"
                )
            if not job.result_key:
                raise DotsMocrGlueError(
                    f"scan {scan.pk} shard {index} has no result key"
                )
            manifest = job.input_manifest or {}
            from_page = manifest.get("from_page")
            page_count = manifest.get("page_count")
            if from_page != next_page or not isinstance(page_count, int):
                raise DotsMocrGlueError(
                    f"scan {scan.pk} shard {index} covers pages from "
                    f"{from_page}, expected {next_page}"
                )

            local = tmp_dir / f"{index:04d}.json"
            s3_sync.download_object(job.result_key, local)
            payload = _check_envelope(scan, job, json.loads(local.read_text()))
            shard_pages = sorted(
                payload.get("pages") or [],
                key=lambda page: page.get("page_no", -1),
            )
            answered = [page.get("page_no") for page in shard_pages]
            if answered != list(range(page_count)):
                raise DotsMocrGlueError(
                    f"scan {scan.pk} shard {index} answered page(s) "
                    f"{answered}, the shard has {page_count}"
                )
            for page in shard_pages:
                page_index = from_page + page["page_no"]
                pages.append(
                    {
                        "page_index": page_index,
                        "pdf_page": page_index + 1,
                        "shard_index": index,
                        **page,
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
            }
            tuning = {
                key: manifest[key] for key in TUNING_KEYS if key in manifest
            }
            if tuning:
                entry["tuning"] = tuning
            shards.append(entry)
            next_page += page_count

    if expected_total is not None and len(pages) != expected_total:
        raise DotsMocrGlueError(
            f"scan {scan.pk} glued to {len(pages)} page(s), the original "
            f"has {expected_total}"
        )

    document = {
        "schema_version": GLUE_SCHEMA_VERSION,
        "engine": str(JobEngine.DOTS_MOCR),
        "action": ACTION,
        "scan_pk": scan.pk,
        "run": run,
        "source_page_count": expected_total,
        "dpi": DPI,
        "prompt_mode": PROMPT_MODE,
        "generated_at": timezone.now().isoformat(),
        "shards": shards,
        "pages": pages,
        "failed_pages": [
            page["page_index"] for page in pages if "error" in page
        ],
        # Two more lists for the survey of #238, neither read by the
        # apply: a filtered page has no cells and so no page number
        # either, and a recovered page is one a retry rung saved.
        "filtered_pages": [
            page["page_index"] for page in pages if page.get("filtered")
        ],
        "recovered_pages": [
            page["page_index"] for page in pages if "recovered_by" in page
        ],
    }
    key = glued_result_key(scan, run)
    if not s3_sync.upload_json_object(key, document):
        raise DotsMocrGlueError(
            f"scan {scan.pk}: the glued document could not be uploaded "
            f"to {key}"
        )
    logger.info(
        "Glued %d dots.mocr shard(s) for scan %s into %s (%d page(s), "
        "%d unread, %d filtered, %d recovered on a retry) in %.1fs",
        len(analyze_jobs),
        scan.pk,
        key,
        len(pages),
        len(document["failed_pages"]),
        len(document["filtered_pages"]),
        len(document["recovered_pages"]),
        time.monotonic() - started,
    )
    return key


def _glue_attempts(analyze_jobs: list[ExternalJob]) -> int:
    """Return how many times this run's glue has already failed.

    Kept on the first row's ``provider_meta`` rather than a field:
    the counter describes the run, any row of it can carry that, and
    ``input_manifest`` is off limits (``_still_describes`` compares it
    exactly, so an added key would read as a stale run).

    :param analyze_jobs: The live run's rows, ordered by shard index.
    :returns: The stored attempt count, 0 when none.
    :rtype: int
    """
    meta = analyze_jobs[0].provider_meta or {}
    return int((meta.get("glue") or {}).get("attempts") or 0)


def _record_glue_failure(scan, analyze_jobs: list[ExternalJob], exc) -> None:
    """Count one glue failure, and give up loudly on the last one.

    The rows stay ``COMPLETED`` either way: the results are paid for and
    still in S3, so the retry costs a download rather than a RunPod run.
    The crossing into "out of tries" is the one ERROR-level event -- the
    pass runs every tick, and logging every skip after it would bury
    Sentry in duplicates of a failure that is not going to change. The
    way back after a fix is a person clearing ``provider_meta["glue"]``
    on the named row.

    :param scan: The scan whose glue failed.
    :param analyze_jobs: The live run's rows, ordered by shard index.
    :param exc: What the glue raised.
    :return: None.
    """
    head = analyze_jobs[0]
    meta = head.provider_meta or {}
    glue = dict(meta.get("glue") or {})
    attempts = int(glue.get("attempts") or 0) + 1
    glue.update(
        {
            "attempts": attempts,
            "last_error": str(exc)[:500],
            "last_attempt_at": timezone.now().isoformat(),
        }
    )
    meta["glue"] = glue
    head.provider_meta = meta
    head.save(update_fields=["provider_meta"])
    if attempts >= GLUE_MAX_ATTEMPTS:
        logger.exception(
            "Gluing dots.mocr results for scan %s failed; giving up after "
            "%d attempt(s). The rows stay COMPLETED and the results stay "
            "in S3; clear provider_meta['glue'] on job %s to retry.",
            scan.pk,
            attempts,
            head.pk,
        )
    else:
        logger.warning(
            "Gluing dots.mocr results for scan %s failed (attempt %d of "
            "%d): %s",
            scan.pk,
            attempts,
            GLUE_MAX_ATTEMPTS,
            exc,
        )


def finish_ready_runs() -> int:
    """Glue every finished dots.mocr run into its volume document.

    Runs after the confirm pass, next to ``bitonal.finish_ready_scans``.
    A run is finished when no row of it is waiting to be submitted or in
    flight, and none is dead: a dead row means the run can never cover
    the volume, ``run_summary`` already shows it on the process page,
    and the way forward is the staff button opening a fresh run.

    Like the reading stage itself, this pass writes **no scan status**
    -- that invariant belongs to issue #190, and it is what keeps a
    volume browsable while it is read. With no status to latch on, the
    rows are the idempotence marker: a glued run is all ``CONSUMED``,
    so it drops out of the candidate query and becomes
    :func:`apply_ready_runs`'s input. The per-shard results are kept,
    which is what makes the retry after a crashed tick safe.

    :returns: How many runs were glued and consumed.
    :rtype: int
    """
    if not s3_sync.s3_active():
        return 0

    scan_ids = (
        Scan.objects.filter(
            jobs__stage=JobStage.ANALYZE,
            jobs__engine=JobEngine.DOTS_MOCR,
            jobs__provider=JobProvider.RUNPOD,
            jobs__status=JobStatus.COMPLETED,
        )
        .values_list("pk", flat=True)
        .distinct()
    )
    unfinished = {JobStatus.PENDING} | IN_FLIGHT_JOB_STATUSES
    glued = 0
    for scan in Scan.objects.filter(pk__in=list(scan_ids)).select_related(
        "reporter"
    ):
        rows = live_analyze_jobs(scan)
        if not rows:
            continue
        if any(row.status in unfinished for row in rows):
            continue
        if any(row.status in DEAD_JOB_STATUSES for row in rows):
            continue
        if not any(row.status == JobStatus.COMPLETED for row in rows):
            # The candidate row belongs to an older run; the live one
            # has nothing to apply.
            continue
        if _glue_attempts(rows) >= GLUE_MAX_ATTEMPTS:
            continue

        try:
            merge_dotsmocr_results(scan, rows)
        except Exception as exc:
            _record_glue_failure(scan, rows, exc)
            continue

        # No delete of the shard results here, deliberately: the future
        # smart glue over page inserts and deletes re-reads them.
        ExternalJob.objects.filter(
            pk__in=[row.pk for row in rows],
            status=JobStatus.COMPLETED,
        ).update(status=JobStatus.CONSUMED, consumed_at=timezone.now())
        glued += 1

    return glued


def _apply_state(analyze_jobs: list[ExternalJob]) -> dict:
    """Return the run's apply bookkeeping (#149/#204).

    Kept on the first row's ``provider_meta`` like the glue counter,
    and for the same reasons: the state describes the run, the rows
    are per run so a fresh OCR run starts clean, and
    ``input_manifest`` is off limits (``_still_describes`` compares it
    exactly).

    :param analyze_jobs: The live run's rows, ordered by shard index.
    :returns: The stored state: ``applied_at``, ``attempts``,
        ``last_error``, ``last_attempt_at``; empty when never tried.
    :rtype: dict
    """
    meta = analyze_jobs[0].provider_meta or {}
    return dict(meta.get("apply") or {})


def _write_apply_state(analyze_jobs: list[ExternalJob], state: dict) -> None:
    """Persist the run's apply bookkeeping on the first row.

    :param analyze_jobs: The live run's rows, ordered by shard index.
    :param state: The state to store.
    :return: None.
    """
    head = analyze_jobs[0]
    meta = head.provider_meta or {}
    meta["apply"] = state
    head.provider_meta = meta
    head.save(update_fields=["provider_meta"])


def _record_apply_failure(scan, analyze_jobs: list[ExternalJob], exc) -> None:
    """Count one apply failure, and give up loudly on the last one.

    Mirrors :func:`_record_glue_failure`: the glued document and the
    shard results stay in S3, so the retry costs one small download.
    The crossing into "out of tries" is the one ERROR-level event; the
    way back after a fix is a person clearing ``provider_meta["apply"]``
    on the named row.

    :param scan: The scan whose apply failed.
    :param analyze_jobs: The live run's rows, ordered by shard index.
    :param exc: What the apply raised.
    :return: None.
    """
    state = _apply_state(analyze_jobs)
    attempts = int(state.get("attempts") or 0) + 1
    state.update(
        {
            "attempts": attempts,
            "last_error": str(exc)[:500],
            "last_attempt_at": timezone.now().isoformat(),
        }
    )
    _write_apply_state(analyze_jobs, state)
    if attempts >= APPLY_MAX_ATTEMPTS:
        logger.exception(
            "Applying the dots.mocr results for scan %s failed; giving up "
            "after %d attempt(s). The glued document stays in S3; clear "
            "provider_meta['apply'] on job %s to retry.",
            scan.pk,
            attempts,
            analyze_jobs[0].pk,
        )
    else:
        logger.warning(
            "Applying the dots.mocr results for scan %s failed (attempt "
            "%d of %d): %s",
            scan.pk,
            attempts,
            APPLY_MAX_ATTEMPTS,
            exc,
        )


def apply_ready_runs() -> int:
    """Apply every glued run: page numbers, Issues, and the READY edge.

    The apply step of issues #149/#204, run right after the glue on the
    collect tick. It reads the glued volume JSON (its S3 presence is a
    checked precondition), rebuilds ``Scan.ocr_results``, recomputes
    the Issues, and moves the scan to
    ``READY_FOR_PAGE_COMPLETENESS_REVIEW``.

    Deliberately **not** daemon-queued work (#212): the computation is
    seconds of local work that does not change what the scan is, so it
    never transits QUEUED/PROCESSING -- the scan stays in the review
    flow throughout, there is no claim for a cancel to race, and no
    scan-wide retry budget is spent. The one status write is a single
    compare-and-swap on the review edge, inside
    ``services.run_compute_issues``.

    Which scans, and why:

    - ``AWAITING_VALIDATION`` (the normal park) and the legacy
      ``PENDING_REVIEW`` (staff ran OCR on it deliberately) take the
      edge to READY. A failure leaves the status alone, so the pass
      retries next tick, up to ``APPLY_MAX_ATTEMPTS``.
    - ``READY_FOR_PAGE_COMPLETENESS_REVIEW`` is a recompute after a
      fresh OCR run: data only, status untouched. ``applied_at`` on
      the run is what keeps a recompute from looping every tick.
    - ``AWAITING`` is deferred, not skipped: the pass leaves no mark,
      so the scan is picked up on the tick after the bitonal merge
      parks it. A cancelled, errored or approved scan is deferred the
      same way and comes back only through the admin re-queue, which
      parks it in ``AWAITING_VALIDATION``.

    :returns: How many scans were applied.
    :rtype: int
    """
    if not s3_sync.s3_active():
        return 0

    scan_ids = (
        Scan.objects.filter(
            jobs__stage=JobStage.ANALYZE,
            jobs__engine=JobEngine.DOTS_MOCR,
            jobs__provider=JobProvider.RUNPOD,
            jobs__status=JobStatus.CONSUMED,
        )
        .values_list("pk", flat=True)
        .distinct()
    )
    applied = 0
    for scan in Scan.objects.filter(
        pk__in=list(scan_ids), status__in=APPLY_STATUSES
    ).select_related("reporter"):
        rows = live_analyze_jobs(scan)
        if not rows:
            continue
        if any(row.status != JobStatus.CONSUMED for row in rows):
            # The candidate row belongs to an older run; the live one
            # is not glued yet.
            continue
        state = _apply_state(rows)
        if state.get("applied_at"):
            continue
        if int(state.get("attempts") or 0) >= APPLY_MAX_ATTEMPTS:
            continue

        from scanning import services

        try:
            done = services.run_compute_issues(
                scan, glued_result_key(scan, rows[0].run)
            )
        except Exception as exc:
            _record_apply_failure(scan, rows, exc)
            continue

        if done:
            state["applied_at"] = timezone.now().isoformat()
            _write_apply_state(rows, state)
            applied += 1

    return applied


def reopen_apply(scan, dry_run: bool = False) -> bool:
    """Let the collect tick apply a glued run a second time (#228).

    ``applied_at`` is what stops :func:`apply_ready_runs` from looping
    every tick, so a change to how the page numbers are read reaches
    no volume that already applied. Clearing it hands the volume back
    to the pass, which re-reads the *stored* glued document: no GPU
    time, no new run, and the numbers a curator typed survive, because
    ``page_edits.overlay_page_numbers`` writes them over the machine
    output on every apply.

    The whole apply state goes, not only the stamp: a second apply is
    a fresh attempt and deserves the full ``APPLY_MAX_ATTEMPTS``
    budget.

    The caller decides which scans may take it, from
    ``APPLY_STATUSES``. A scan outside them would simply keep the
    cleared state until an admin re-queue parks it back in the review
    flow.

    The write is not a compare-and-swap, and needs none, although
    ``apply_ready_runs`` stamps ``applied_at`` onto a state it read
    seconds earlier. The two cannot interleave: this function writes
    only when ``applied_at`` is set, and a run whose ``applied_at`` is
    set is one the pass skips before it does any work. So there is
    never a stale state to overwrite -- for the pass to be working, the
    stamp must already be absent, and then there is nothing here to
    clear.

    :param scan: The scan whose live run should apply again.
    :param dry_run: Answer whether the run qualifies, and write
        nothing.
    :returns: Whether a glued, applied run was handed back.
    :rtype: bool
    """
    rows = live_analyze_jobs(scan)
    if not rows or any(row.status != JobStatus.CONSUMED for row in rows):
        return False
    if not _apply_state(rows).get("applied_at"):
        return False
    if not dry_run:
        _write_apply_state(rows, {})
    return True
