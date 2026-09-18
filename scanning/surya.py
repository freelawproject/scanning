"""The Surya stage: what to submit, and what a run looks like.

Surya OCR 2 reads the **original** shards, the set ``sharding``
cut from the original (#164) and the one dots.mocr, YOLO and Mistral
read. So the stage waits for no review state and for no redacted
volume: what it needs exists from the moment the pipeline cut the
shards, and the pages are unredacted, which is what a reader of the
headnote brackets needs (#303).

One job per shard, tracked on ``ExternalJob`` rows
(:mod:`scanning.jobs`) at ``EXTRACT``/``SURYA``/``RUNPOD``. ``EXTRACT``
takes either shape (``models.EITHER_LEVEL_STAGES``): a shard row with
no opinion, which is this one, or an opinion row for an engine that
reads opinion PDFs. ``engine`` is part of the unique key, so these rows
and the Mistral rows of the same stage never collide.

The provider is RunPod, so this module is one table entry and nothing
more: ``jobs._runpod_engines`` holds the endpoint, the caps and the
payload builder, and the shared wave claims, signs, submits, polls and
retries the rows. The worker is ``scanning/runpod-surya/`` (#320): it
downloads the shard from a presigned GET, renders each page at
:data:`DPI`, reads it whole, and PUTs one JSON envelope to the key the
claim signed.

Who starts it: the staff-only button (``views_process.start_surya_ocr``,
#364), and nothing else. No tick and no pipeline arm creates a Surya
row, because every row is paid GPU work.

**There is no glue yet.** A finished run sits at ``COMPLETED`` with its
per-shard results on S3, which is where the glue of the next pull
request reads them. No review state and no apply reads ``EXTRACT``, so
an unglued run holds nothing up.
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
ACTION = "ocr"

#: Render resolution. The same 200 dpi the other stages use, so a block
#: box describes the same pixel space as the dots.mocr cells, the
#: Mistral blocks and the detection rows: US Letter is 1700x2200 in all
#: four. The worker renders at this value with PyMuPDF and surya scales
#: its boxes back to that image, so nothing rescales later.
#:
#: A module constant rather than a setting: there is no operational
#: reason to retune this per deploy, and a one-off experiment writes
#: ``{"dpi": 400}`` onto the row's ``input_manifest`` instead.
DPI = 200

#: Pages one worker reads at once against its own inference server. The
#: worker's own default is the same 16, the value the ai-research kit
#: measured as good on a 24 GB card; it is sent so the number this repo
#: asks for is readable here rather than on an endpoint's env page.
NUM_THREADS = 16

#: Per-row tuning keys this stage reads off ``input_manifest``, so an
#: experiment can override them without a deploy. Everything else there
#: describes the shard and must not be treated as a knob.
#:
#: No decode parameter belongs here, ever. The worker refuses
#: ``temperature``, ``top_p``, ``max_tokens`` and
#: ``max_completion_tokens`` with ``BAD_INPUT``: what the model sees is
#: what the kit measured (#320).
TUNING_KEYS = ("dpi", "num_threads")


def enabled() -> bool:
    """Return whether Surya jobs may be dispatched.

    Both switches, because they fail differently: ``SURYA_ENABLED`` is
    the operator's decision to spend money on this stage, and
    :func:`runpod_client.enabled` covers the account credentials and
    this engine's own endpoint id. The endpoint id is blank until the
    endpoint of #320 exists, so a deploy that has not created it yet
    answers false here and the button refuses.

    :returns: Whether the stage should run.
    :rtype: bool
    """
    return bool(
        settings.SURYA_ENABLED
        and runpod_client.enabled(settings.RUNPOD_SURYA_ENDPOINT_ID)
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
        "dpi": DPI,
        "num_threads": NUM_THREADS,
        **tuning,
    }


def ensure_extract_jobs(
    scan, manifest: dict, *, force_new_run: bool = False
) -> list[ExternalJob]:
    """Return the live Surya jobs for ``scan``, creating them if the
    current run does not describe today's shard set.

    Idempotent, so a second press of the button is a no-op rather than
    a second run over shards already read. A run holding a dead row
    (failed, cancelled, expired) is replaced instead, since nothing will
    move it again.

    A replacement run does not re-read shards already read: a shard
    whose identity is unchanged and whose result object is still on S3
    enters the run as a ``COMPLETED`` row pointing at that object
    (``jobs._reusable_results``). This engine can carry, because its
    per-shard results are kept for the glue.

    A result with a hole is never carried, stable or not
    (``carry_stable_holes=False``). The stable-hole rule of #238 trusts
    a deterministic worker to give the same answer twice. Surya's own
    client reads a looped answer again at a temperature it raises
    itself, so a page that failed once may well read on the next
    attempt, and two unlucky runs must not freeze it as unread for
    good.

    :param scan: The scan to read.
    :param manifest: The committed shard manifest.
    :param force_new_run: Replace a whole, reusable live run. A
        deliberate way to spend GPU money; no tick passes it.
    :returns: The live run's rows, ordered by shard index.
    :rtype: list[ExternalJob]
    """
    return jobs.ensure_shard_jobs(
        scan,
        manifest,
        stage=JobStage.EXTRACT,
        engine=JobEngine.SURYA,
        provider=JobProvider.RUNPOD,
        reuse_results=True,
        force_new_run=force_new_run,
        carry_stable_holes=False,
    )


def live_extract_jobs(scan) -> list[ExternalJob]:
    """Return the current run's Surya rows for ``scan``, in page order.

    :param scan: The scan, or its pk.
    :returns: The rows, or an empty list.
    :rtype: list[ExternalJob]
    """
    return jobs.live_run(scan, JobStage.EXTRACT, JobEngine.SURYA)


def run_summary(scan) -> dict | None:
    """Describe a scan's live Surya run for the process page.

    :param scan: The scan (or its pk) to describe.
    :returns: See :func:`jobs.run_summary`, or ``None``.
    :rtype: dict | None
    """
    return jobs.run_summary(scan, JobStage.EXTRACT, JobEngine.SURYA)


def glued_result_key(scan, run: int) -> str:
    """Return the S3 key this run's glued volume document will live at.

    Nothing writes that object yet: the glue is the next pull request
    (#364 leaves it out on purpose). The named wrapper exists now
    because a reader that lists the outputs holds a callable per output
    (``views_process.GLUED_OUTPUTS``), and the volume route answers
    "not glued yet" for a key with no object.

    :param scan: The scan the run belongs to.
    :param run: The run number.
    :returns: The key.
    :rtype: str
    """
    return jobs.volume_result_key(scan, JobStage.EXTRACT, JobEngine.SURYA, run)
