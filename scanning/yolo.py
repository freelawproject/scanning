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

import logging

from django.conf import settings

from scanning import jobs, runpod_client
from scanning.models import (
    ExternalJob,
    JobEngine,
    JobProvider,
    JobStage,
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
