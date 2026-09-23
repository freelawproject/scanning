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

The glues are the three of #368, and they follow the Mistral stage
(#245) rather than dots.mocr, because the read starts by hand and
later than the review-1 approval:

- the volume document (:func:`finish_ready_runs`), in the page space of
  the original;
- the corrected volume's document (:func:`finish_ready_applies`), in
  the page space of one apply run, named on ``ApplyRun.surya_key``;
- one document per opinion, which ``opinion_ocr`` writes from the
  second one. Surya is one entry of ``opinion_ocr.ENGINES`` there and
  no other code.

No review state reads any of them. ``ApplyRun.surya_key`` is outside
``is_glued`` and ``is_complete``, so a volume nobody read with Surya
opens review 2 as it always did.
"""

from __future__ import annotations

import json
import logging
import tempfile
import time
from pathlib import Path

from django.conf import settings
from django.utils import timezone

from scanning import dots_mocr, jobs, runpod_client, s3_sync
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
#: reason to retune this per deploy. A one-off experiment writes
#: ``{"dpi": 400}`` onto the row's ``input_manifest`` instead, and
#: **that costs a new run**: ``input_manifest`` is the shard identity,
#: so the edited row no longer describes today's shard set
#: (``jobs._still_describes``) and cannot be carried
#: (``jobs._reusable_results``). The next ``ensure_extract_jobs`` opens
#: run n+1 and re-pays that shard. For an experiment that is the point
#: -- a read at another resolution is a second read -- but it is never
#: free.
DPI = 200

#: Pages one worker reads at once against its own inference server. The
#: worker's own default is the same 16, the value the ai-research kit
#: measured as good on a 24 GB card; it is sent so the number this repo
#: asks for is readable here rather than on an endpoint's env page.
NUM_THREADS = 16

#: Per-row tuning keys this stage reads off ``input_manifest``, so an
#: experiment can override them without a deploy. Everything else there
#: describes the shard and must not be treated as a knob. An override
#: re-pays the shard it names; see :data:`DPI`.
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
    scan, manifest: dict, *, force_new_run: bool = False, apply_run=None
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

    A result with an unread page is never carried, stable or not
    (``carry_stable_holes=False``). The stable-hole rule of #238 trusts
    a deterministic worker to give the same answer twice. Surya's own
    client reads a looped answer again at a temperature it raises
    itself, so a page that failed once may well read on the next
    attempt, and two unlucky runs must not freeze it as unread for
    good.

    "Unread" is ``jobs.has_unread_pages``, which reads ``failed_pages``
    and ``filtered_pages`` and nothing else. The worker also reports
    ``empty_pages`` -- a page it read twice and got no block from --
    and this stage does **not** treat one as a hole: a blank page is
    rare but real, and re-paying a shard for one would never converge.
    Such a page is carried, and what an empty page means is the glue's
    question. The files index lists them so a reader checks (#364).

    :param scan: The scan to read.
    :param manifest: The committed shard manifest.
    :param force_new_run: Replace a whole, reusable live run. A
        deliberate way to spend GPU money; no tick passes it.
    :param apply_run: The apply run (#224) the rows work for, or None
        for the volume run.
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
        apply_run=apply_run,
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
    """Return the S3 key one run's glued volume document lives at.

    This stage's name for :func:`jobs.volume_result_key`, which holds
    the rule and the reasons.

    :param scan: The scan the run belongs to.
    :param run: The run number.
    :returns: Key of the form ``{processing_prefix}jobs/extract/surya/
        r{run}-volume.json``.
    :rtype: str
    """
    return jobs.volume_result_key(scan, JobStage.EXTRACT, JobEngine.SURYA, run)


# ── the volume document (#368) ──────────────────────────────────────
#: Version of the glued volume document. Independent of the worker's
#: own envelope version: the envelope is the wire format, this is the
#: stored one, and they change for different reasons.
GLUE_SCHEMA_VERSION = 1

#: What the read covers, recorded on every document. ``input_key``
#: names a shard of the original, and the original never changes, which
#: is what lets a later run carry a paid result for good.
SOURCE = "original"

#: How many times a run's glue may fail before the pass leaves it
#: alone. The per-shard results are kept, so a retry costs one download
#: and no GPU payment.
GLUE_MAX_ATTEMPTS = 3

#: Prefix of the glue's scratch directory in the system temp dir. The
#: bytes go through the disk because a shard result carries ``raw`` for
#: every page, which makes it the biggest object this daemon reads.
#: ``cleanup_processing_tmp`` reclaims a directory a SIGKILL orphans by
#: this prefix (#215).
GLUE_TMP_PREFIX = "suryaocr-"


class SuryaGlueError(Exception):
    """A Surya run could not be glued into a document."""


def _lost_content(page: dict) -> bool:
    """Return whether one page's answer did not reach its blocks.

    The rule of the worker (``handler._lost_content``) over the same
    fields: a div its parser refused, or a parsed entry with no block.
    It reads the counts and not the names, because a refused div nobody
    could name is still a refused div.

    The rule is here as well as in the worker because the two answer
    different questions. The worker names the pages of one shard, in
    the shard's own numbering. This names the pages of a volume, and
    the apply glue names the pages of a corrected volume, where a page
    has moved. ``test_surya_glue.TestTheLostContentRule`` pins the two
    copies against each other, over the shapes both guard.

    :param page: One page of a result or of a document.
    :returns: Whether the page lost content on the way to its blocks.
    :rtype: bool
    """
    if page.get("dropped_blocks"):
        return True
    divs = page.get("raw_divs")
    if not isinstance(divs, int):
        return False
    parsed = page.get("parsed_blocks")
    return divs > (parsed if isinstance(parsed, int) else 0)


#: The page lists of a document, and the page-dict key that puts a page
#: in each (``"error"`` and ``"fallback"`` by presence, ``"empty"`` by
#: truth, the fourth by :func:`_lost_content`).
#:
#: The names are the worker's own and **not** ``jobs.PAGE_LIST_NAMES``:
#: this stage has no filtered page and no retry rung, and it reports
#: two faults dots.mocr does not have. So ``jobs.has_unread_pages``
#: reads ``failed_pages`` alone off a Surya row, which is the rule of
#: #364: an empty page is carried and is not a hole.
#:
#: The membership rule lives here once, because the apply glue
#: renumbers the pages and must sort them again.
PAGE_LISTS = (
    ("failed_pages", lambda page: "error" in page),
    ("empty_pages", lambda page: bool(page.get("empty"))),
    ("fallback_pages", lambda page: "fallback" in page),
    ("dropped_block_pages", _lost_content),
)


def page_lists(pages: list[dict], key: str) -> dict[str, list]:
    """Sort ``pages`` into the four lists, naming each by ``key``.

    :param pages: Page dicts, shard-local or volume-level.
    :param key: The page-number field to list: ``"page_no"`` for a
        shard's pages, ``"page_index"`` for a document.
    :returns: ``{list name: page numbers}``.
    :rtype: dict[str, list]
    """
    return {
        name: [page[key] for page in pages if member(page)]
        for name, member in PAGE_LISTS
    }


def shard_pages(payload: dict) -> dict[int, dict]:
    """Turn one stored result into a page dict per page of its shard.

    **The one transform of a Surya result**, and both glues call it --
    the volume glue over a shard result, the apply glue over a one-page
    result. So a better transform is a re-glue
    (``reglue_surya_ocr``) at no GPU payment, and it is right in both
    documents at once (#245).

    The worker writes the page shape this repository stores, so the
    transform is small: it keys the pages by their page inside the
    shard, and it drops ``raw``.

    ``raw`` is the answer of the whole page as the model wrote it, and
    it is the biggest field of a result. The shard result keeps it for
    good and a reader of the answer reads that object; a copy here
    would double a document the opinion glue downloads per volume. The
    dots.mocr glue drops its own ``raw`` for the same reason.

    :param payload: The ``payload`` of a stored result envelope.
    :returns: ``{page inside the shard: page dict}``.
    :rtype: dict[int, dict]
    """
    pages: dict[int, dict] = {}
    for page in payload.get("pages") or []:
        if not isinstance(page, dict):
            continue
        page_no = page.get("page_no")
        if not isinstance(page_no, int) or isinstance(page_no, bool):
            continue
        pages[page_no] = {
            name: value for name, value in page.items() if name != "raw"
        }
    return pages


def glued_volume_key(scan) -> str | None:
    """Return the key of a scan's glued Surya document, or nothing.

    The live run is glued when every one of its rows is ``CONSUMED``:
    the glue writes the document and flips the rows in one pass. The
    twin of ``mistral_ocr.glued_volume_key``.

    :param scan: The scan (or its pk) to look up.
    :returns: The key, or None when no run is glued.
    :rtype: str | None
    """
    return jobs.glued_volume_key(scan, JobStage.EXTRACT, JobEngine.SURYA)


def merge_surya_results(scan, extract_jobs: list[ExternalJob]) -> str:
    """Glue one run's shard results into a volume document on S3.

    Glues in strict shard order and asserts the page arithmetic:
    ``page_no`` counts from zero inside a shard, so a page's volume
    index is the shard's ``from_page`` plus its ``page_no``, and its
    1-based ``pdf_page`` is that plus one. The document is in the page
    space of the **original**, as every other volume document is; the
    corrected volume's own space is :func:`glue_apply_run`.

    A page the worker could not read keeps its slot and its ``error``,
    as it does in the other two volume documents: a hole that shifted
    the pages after it would put every later page's text on the wrong
    page.

    Idempotent: it rebuilds from the result objects every time, so a
    daemon killed between the upload and the ``CONSUMED`` write glues
    again. The results are kept and nothing here deletes one.

    It writes no scan status. The stages that read a volume own no
    review state (#190, #195), and this one starts later than both.

    :param scan: The scan whose run finished.
    :param extract_jobs: The live run's rows, ordered by shard index.
    :returns: The S3 key the document was uploaded to.
    :rtype: str
    :raises SuryaGlueError: If a result is missing or malformed, or the
        page arithmetic does not add up to the volume.
    """
    if not extract_jobs:
        raise SuryaGlueError(f"scan {scan.pk} has no Surya jobs")

    expected_total = (extract_jobs[0].input_manifest or {}).get(
        "source_page_count"
    )
    run = extract_jobs[0].run
    started = time.monotonic()

    pages: list[dict] = []
    shards: list[dict] = []
    # A temp dir, not the output dir: the generic S3 sync sweeps up
    # everything there, and these are wire artifacts that stay out of
    # it.
    with tempfile.TemporaryDirectory(
        prefix=f"{GLUE_TMP_PREFIX}{scan.pk}-"
    ) as tmp:
        tmp_dir = Path(tmp)

        def _download(key: str) -> dict:
            local = tmp_dir / Path(key).name
            s3_sync.download_object(key, local)
            return json.loads(local.read_text())

        for read in jobs.read_run_shards(
            scan,
            extract_jobs,
            action=ACTION,
            error_cls=SuryaGlueError,
            download=_download,
        ):
            answered = shard_pages(read.payload)
            if sorted(answered) != list(range(read.page_count)):
                raise SuryaGlueError(
                    f"scan {scan.pk} shard {read.index} answered page(s) "
                    f"{sorted(answered)}, the shard has {read.page_count}"
                )
            for page_no in range(read.page_count):
                page_index = read.from_page + page_no
                pages.append(
                    {
                        "page_index": page_index,
                        "pdf_page": page_index + 1,
                        "shard_index": read.index,
                        **answered[page_no],
                    }
                )
            shards.append(jobs.shard_entry(read.job, read.index, TUNING_KEYS))

    if expected_total is not None and len(pages) != expected_total:
        raise SuryaGlueError(
            f"scan {scan.pk} glued to {len(pages)} page(s), the original "
            f"has {expected_total}"
        )

    document = {
        "schema_version": GLUE_SCHEMA_VERSION,
        "engine": str(JobEngine.SURYA),
        "action": ACTION,
        "scan_pk": scan.pk,
        "run": run,
        "source_page_count": expected_total,
        # Every ``bbox`` of every block lives in the page's own render,
        # which ``origin_width`` and ``origin_height`` name on the page
        # itself. So the document states the resolution and the copy of
        # the volume that was read, and no reader guesses either.
        "dpi": DPI,
        "source": SOURCE,
        "generated_at": timezone.now().isoformat(),
        "shards": shards,
        "pages": pages,
        **page_lists(pages, "page_index"),
    }
    key = glued_result_key(scan, run)
    if not s3_sync.upload_json_object(key, document):
        raise SuryaGlueError(
            f"scan {scan.pk}: the glued document could not be uploaded "
            f"to {key}"
        )
    logger.info(
        "Glued %d Surya shard(s) for scan %s into %s (%d page(s), %d "
        "unread, %d empty, %d read in block mode, %d that lost content) "
        "in %.1fs",
        len(extract_jobs),
        scan.pk,
        key,
        len(pages),
        len(document["failed_pages"]),
        len(document["empty_pages"]),
        len(document["fallback_pages"]),
        len(document["dropped_block_pages"]),
        time.monotonic() - started,
    )
    return key


def _glue_attempts(extract_jobs: list[ExternalJob]) -> int:
    """Return how many times this run's volume glue has failed.

    :param extract_jobs: The live run's rows, ordered by shard index.
    :returns: The stored attempt count, 0 when none.
    :rtype: int
    """
    return jobs.ledger_attempts(extract_jobs, jobs.glue_ledger_key())


def _record_glue_failure(
    scan, extract_jobs: list[ExternalJob], key: str, exc
) -> None:
    """Count one glue failure, and give up loudly on the last one.

    The result objects stay in S3, so a retry costs one download and no
    GPU payment. The crossing into "out of tries" is the one ERROR-level
    event; the way back after a fix is a person who clears the named
    key on the named row.

    :param scan: The scan whose glue failed.
    :param extract_jobs: The live run's rows, ordered by shard index.
    :param key: The ledger key (``"glue"``, or ``"glue:a{n}"``).
    :param exc: What the glue raised.
    :return: None.
    """
    attempts, head = jobs.bump_run_ledger(extract_jobs, key, exc)
    if attempts >= GLUE_MAX_ATTEMPTS:
        logger.exception(
            "Gluing the Surya results (%s) for scan %s failed; giving up "
            "after %d attempt(s). The shard results stay in S3; clear "
            "provider_meta['%s'] on job %s to retry.",
            key,
            scan.pk,
            attempts,
            key,
            head.pk,
        )
    else:
        logger.warning(
            "Gluing the Surya results (%s) for scan %s failed (attempt "
            "%d of %d): %s",
            key,
            scan.pk,
            attempts,
            GLUE_MAX_ATTEMPTS,
            exc,
        )


def finish_ready_runs() -> int:
    """Glue every finished Surya run into its volume document.

    Runs on the collect tick, next to ``mistral_ocr.finish_ready_runs``
    and on the same candidate rule (:func:`jobs.ready_volume_runs`). A
    glued run is all ``CONSUMED``, which is the idempotence marker,
    because this pass writes no scan status.

    It asks :func:`enabled` nothing, on purpose: the results are paid
    for and stored, so an endpoint id taken out of the environment
    after the read must not leave them unglued. What that id gates is
    spending, and this pass spends nothing.

    A glued run hands a volume still in review 1 back to the
    page-number apply (``dots_mocr.reopen_apply_after_read``, #351),
    so this document fills the pages dots.mocr left blank on the
    next tick.

    :returns: How many runs were glued and consumed.
    :rtype: int
    """
    if not s3_sync.s3_active():
        return 0

    glued = 0
    for scan, rows in jobs.ready_volume_runs(
        JobStage.EXTRACT,
        JobEngine.SURYA,
        JobProvider.RUNPOD,
        live_extract_jobs,
        _glue_attempts,
        GLUE_MAX_ATTEMPTS,
    ):
        try:
            merge_surya_results(scan, rows)
        except Exception as exc:
            _record_glue_failure(scan, rows, jobs.glue_ledger_key(), exc)
            continue
        jobs.consume_run(rows)
        glued += 1
        dots_mocr.reopen_apply_after_read(scan, str(JobEngine.SURYA))

    return glued


# ── the corrected volume (#224, #368) ───────────────────────────────
#: Which document of a corrected volume this stage writes, for the
#: shared prologue (``jobs.ready_apply_runs``).
APPLY_GLUE_TARGET = jobs.ApplyGlueTarget(
    stage=JobStage.EXTRACT,
    engine=JobEngine.SURYA,
    provider=JobProvider.RUNPOD,
    key_field="surya_key",
    run_field="surya_run",
)


def ensure_extract_apply_jobs(scan, run) -> list[ExternalJob]:
    """Return the Surya rows of one apply run, creating them if none.

    The pages a curator inserted, replaced or turned have no address in
    the original, so the volume read does not cover them: they are
    one-page shards of their own, under ``jobs/apply/pages/e{pk}.pdf``
    (#224). This is where they are read.

    Created here rather than in ``apply._ensure_rows`` for the two
    reasons of the Mistral twin (#245). The read starts long after the
    build, so the build usually has no Surya run to join. And
    ``_ensure_rows`` refuses the whole corrected volume when a stage it
    needs is off (``apply.GateClosedError``), which must never happen
    for a stage an environment may simply not pay for.

    Idempotent, like every ``ensure_*`` of this module: a second call
    hands back the live rows, and the carry gives a replacement run the
    results already paid for.

    :param scan: The scan.
    :param run: A built, standing ``ApplyRun``.
    :returns: The rows, or an empty list for a run with no edited page.
    :rtype: list[ExternalJob]
    """
    from scanning import apply

    manifest = apply.stored_shard_manifest(scan, run)
    if not manifest["shards"]:
        return []
    return ensure_extract_jobs(scan, manifest, apply_run=run)


def apply_jobs(scan, run) -> list[ExternalJob]:
    """Return one apply run's live Surya rows, in shard order.

    :param scan: The scan.
    :param run: The apply run.
    :returns: The rows, or an empty list.
    :rtype: list[ExternalJob]
    """
    return jobs.live_run(
        scan, JobStage.EXTRACT, JobEngine.SURYA, apply_run=run
    )


def apply_glue_due(
    run, rows: list[ExternalJob], volume_run: int, *, force: bool = False
) -> bool:
    """Return whether the corrected volume's Surya document can be
    written now.

    The one rule, called by the pass that writes it and by the command
    that writes it again. Four things must hold:

    - the run is built;
    - the volume run is glued (the caller's own test, passed in as its
      run number);
    - **every edited page the run's map names has a row**, and no row
      is unstarted or dead, the way ``apply.glues_due`` judges a stage.
      Without the first half a document would be written with every
      edited page marked unread -- and worse, its key would then say
      the run is done, so nothing would ever read those pages. A run
      with no edited page (a deletion alone, or an identity run) passes
      it with no row at all;
    - the document that stands is not this volume run's already, unless
      the caller asks for it anyway (``force``: a person who runs
      ``reglue_surya_ocr`` writes the document again on purpose, and
      only that last test is theirs to skip).

    :param run: The standing ``ApplyRun``.
    :param rows: The run's Surya rows (:func:`apply_jobs`).
    :param volume_run: The glued volume run's number.
    :param force: Write a document that stands for this volume run
        again.
    :returns: Whether to write the document.
    :rtype: bool
    """
    from scanning import apply

    if not run.is_built:
        return False
    if any(row.status in apply.BLOCKING_JOB_STATUSES for row in rows):
        return False
    named = set(apply.edit_page_counts(run.page_map))
    if named - {(row.input_manifest or {}).get("edit_id") for row in rows}:
        return False
    if force:
        return True
    return not (run.surya_key and run.surya_run == volume_run)


def glue_apply_run(scan, run, rows: list[ExternalJob], volume_run: int) -> str:
    """Write the corrected volume's Surya document, and name it on the
    run.

    The walk of ``apply._glue_ocr``, over the same page map: a kept
    page comes from the volume document, an edited page from that
    edit's own one-page result, every page is renumbered to its final
    page, and a page the map does not name -- a page a curator deleted
    -- is simply not walked. So the document is in the corrected
    volume's page space, and the deleted pages are gone from it.

    A run with no structural edit aliases the volume document rather
    than a copy of it, as the other two glues do for the same case.

    :param scan: The scan.
    :param run: The built, standing run.
    :param rows: The run's Surya rows.
    :param volume_run: The glued volume run's number.
    :returns: The key the document lives at.
    :rtype: str
    :raises SuryaGlueError: If an input is missing or the upload
        failed.
    """
    from scanning import apply

    volume_key = glued_result_key(scan, volume_run)
    volume = s3_sync.download_json_object(volume_key)
    if not isinstance(volume, dict) or not volume.get("pages"):
        raise SuryaGlueError(
            f"scan {scan.pk}: the glued Surya document at {volume_key} "
            f"has no pages"
        )
    page_map = run.page_map or {}
    if apply.is_identity_map(page_map):
        return volume_key

    read = apply._rows_by_edit(rows, JobStage.EXTRACT)
    edit_pages = {
        edit_id: shard_pages(apply._result_payload(scan, row, ACTION))
        for edit_id, row in read.items()
    }
    pages = apply.walk_final_pages(
        page_map,
        volume["pages"],
        edit_pages,
        missing={"blocks": [], "text": ""},
        error_cls=SuryaGlueError,
        what=f"the Surya volume document of scan {scan.pk}",
    )

    document = {
        "schema_version": GLUE_SCHEMA_VERSION,
        "engine": str(JobEngine.SURYA),
        "action": ACTION,
        "scan_pk": scan.pk,
        "run": volume_run,
        "apply_run": run.label,
        "source_page_count": page_map["final_page_count"],
        "source_fingerprint": run.source_fingerprint,
        "dpi": volume.get("dpi", DPI),
        "source": volume.get("source", SOURCE),
        "generated_at": timezone.now().isoformat(),
        "pages": pages,
        **page_lists(pages, "page_index"),
    }
    key = f"{apply.run_prefix(scan, run)}surya-volume.json"
    if not s3_sync.upload_json_object(key, document):
        raise SuryaGlueError(
            f"scan {scan.pk}: the corrected volume's Surya document "
            f"could not be uploaded to {key}"
        )
    return key


def _glue_one_apply(scan, run, volume_rows: list[ExternalJob]) -> bool:
    """Create the rows of one standing run, and glue it when it is due.

    :param scan: The scan.
    :param run: The standing run.
    :param volume_rows: The glued volume run's rows.
    :returns: Whether the document was written.
    :rtype: bool
    """
    from scanning.models import ApplyRun

    volume_run = volume_rows[0].run
    rows = apply_jobs(scan, run)
    if not rows:
        rows = ensure_extract_apply_jobs(scan, run)
    if not apply_glue_due(run, rows, volume_run):
        return False
    key = glue_apply_run(scan, run, rows, volume_run)
    ApplyRun.objects.filter(pk=run.pk).update(
        surya_key=key, surya_run=volume_run
    )
    run.surya_key, run.surya_run = key, volume_run
    jobs.consume_run(rows)
    logger.info(
        "Glued the Surya read of scan %s into %s for apply run %s "
        "(volume run %s, %d edited page(s))",
        scan.pk,
        key,
        run.label,
        volume_run,
        len(rows),
    )
    return True


def finish_ready_applies() -> int:
    """Read the edited pages of every corrected volume, and glue them.

    The second pass of this stage on the collect tick, and the twin of
    ``mistral_ocr.finish_ready_applies``. Its candidates are
    ``jobs.ready_apply_runs``, the prologue both ``EXTRACT`` stages
    share: a scan whose live Surya volume run is glued and whose
    standing apply run is built. For each one it creates the run's own
    one-page rows if it has none, and writes the corrected volume's
    document once every row has answered.

    **A volume nobody read with Surya pays nothing here.** The
    candidate is the glued volume run, which only the staff button
    starts (``views_process.start_surya_ocr``), so this pass creates a
    paid row only for a volume somebody chose to read.

    Not part of ``apply.glues_due`` on purpose (#245, #368). That
    trigger takes a scan in ``PAGE_COMPLETENESS_REVIEW_DONE`` alone,
    and this read starts later than that status, so an arm there would
    miss the normal case.

    Unlike :func:`finish_ready_runs` this pass does ask
    :func:`enabled`, because it may create a row, and a row is GPU
    money. So an environment with no endpoint id writes no corrected
    volume's document either, even for the identity run that would need
    no row; the tick writes it as soon as the endpoint returns.

    :returns: How many corrected volumes were glued.
    :rtype: int
    """
    if not s3_sync.s3_active() or not enabled():
        return 0

    glued = 0
    for candidate in jobs.ready_apply_runs(
        APPLY_GLUE_TARGET, live_extract_jobs, GLUE_MAX_ATTEMPTS
    ):
        try:
            if _glue_one_apply(
                candidate.scan, candidate.run, candidate.volume_rows
            ):
                glued += 1
        except Exception as exc:
            _record_glue_failure(
                candidate.scan,
                candidate.volume_rows,
                candidate.ledger_key,
                exc,
            )

    return glued
