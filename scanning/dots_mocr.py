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

**This module creates and describes work; it never applies a result.**
Issue #190 deliberately ends when every shard answers, with the rows at
``COMPLETED`` -- the provider is done, and we have applied nothing.
The merge follows in #149: read each row's ``input_manifest``, offset
each page by its shard's ``from_page``, write one volume-level JSON, and
flip the rows to ``CONSUMED``. Nothing is at risk in the meantime,
because each result object sits at an attempt-scoped key and each row
keeps the page range it covered.

Who starts it: a staff-only button (``views_process.start_dots_mocr``),
not the daemon. That is the point of #190 -- the stage costs real
graphics processing unit (GPU) money per press, so a person decides
while it is being debugged. The web process only writes rows; the daemon
submits, polls and retries them.
"""

from __future__ import annotations

import logging

from django.conf import settings

from scanning import jobs, runpod_client
from scanning.models import (
    OPEN_JOB_STATUSES,
    ExternalJob,
    JobEngine,
    JobProvider,
    JobStage,
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


def ensure_analyze_jobs(scan, manifest: dict) -> list[ExternalJob]:
    """Return the live dots.mocr jobs for ``scan``, creating them if
    the current run does not describe today's shard set.

    Idempotent, so a second press of the start button is a no-op rather
    than a second run over shards already read. A run holding a dead row
    (failed, cancelled, expired) is replaced instead, since nothing will
    move it again.

    :param scan: The scan to read.
    :param manifest: The committed shard manifest.
    :returns: The live run's rows, ordered by shard index.
    :rtype: list[ExternalJob]
    """
    return jobs.ensure_shard_jobs(
        scan,
        manifest,
        stage=JobStage.ANALYZE,
        engine=JobEngine.DOTS_MOCR,
        provider=JobProvider.RUNPOD,
    )


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

    The stage writes no scan status by design (issue #190), so the rows
    are the only place its progress lives and this is how a viewer sees
    it. ``open`` is what the start button refuses a second press on.

    :param scan: The scan (or its pk) to describe.
    :returns: ``{"run", "total", "done", "open", "failed", "statuses",
        "error_code", "error_message"}``, or ``None`` when the stage has
        never run for this scan.
    :rtype: dict | None
    """
    rows = live_analyze_jobs(scan)
    if not rows:
        return None

    statuses: dict[str, int] = {}
    for row in rows:
        statuses[row.status] = statuses.get(row.status, 0) + 1

    failed = [row for row in rows if row.status in ("failed", "expired")]
    done = sum(
        count
        for status, count in statuses.items()
        if status in ("completed", "consumed")
    )
    first_failure = failed[0] if failed else None
    return {
        "run": rows[0].run,
        "total": len(rows),
        "done": done,
        "open": sum(1 for row in rows if row.status in OPEN_JOB_STATUSES),
        "failed": len(failed),
        "statuses": statuses,
        "error_code": first_failure.error_code if first_failure else "",
        "error_message": first_failure.error_message if first_failure else "",
    }
