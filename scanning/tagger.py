"""The case-law block tagger stage: one RunPod job per approved opinion.

The text review (#375) writes the approved text of an opinion once, at
``Opinion.approved_text_key``. This stage sends the body of that text to
the ``caselaw-block-tagger`` worker (``scanning/runpod-caselaw-tagger/``)
and stores what it says: the character spans of the caption fields and
the opinion skeleton (party, docket number, court, judges, disposition,
...), placed on the paragraphs of the approved text.

**The approved text is the one input** (#272). The stage reads no
``OpinionText`` row, no ensemble document, no detection, no redaction
and no boundary: the text review already took out what the redactions
cover, cut the opinion at its boundaries, put the footnotes apart, and
joined the paragraphs a column or a page cut. ``markup.project`` writes
the tagger's HTML from the ``body`` of the approved object, and its
offset map places every span back (``markup.lift_span``).

**One opinion is one job.** The worker tags each sequence alone, one
case in each, and the text review approves one opinion at a time, so a
volume job would wait for its last approval and tag the volume again at
every reopen.

The pieces, in the shape of ``dots_mocr.py`` and ``yolo.py``:

- :func:`ensure_tag_jobs` writes the input under the opinion's own
  prefix, addressed by the digest of its text, and creates **one** row
  at ``TAG``/``CASELAW_TAGGER``/``RUNPOD`` with ``opinion`` set,
  through ``jobs.ensure_run_jobs``. The identity is the digest and
  :data:`PROJECTION_VERSION`, so a second approval of the same text
  reuses the paid run.
- The daemon's submit wave presigns the input for a GET and the result
  key for a PUT and posts :func:`build_payload`; the poll, the
  deadlines, the retries and the cancel are the shared machinery.
- :func:`finish_ready_runs`, on the collect tick, lifts the spans onto
  the approved text (:func:`glue_run`), writes them beside the input,
  stamps the ledger (:func:`is_written`) and consumes the row. The
  result object is kept.

Nothing enqueues this stage on a tick. The button of the review page
(``views_api.start_caselaw_tagger``) is the one caller of
:func:`ensure_tag_jobs`, pinned by ``TestKnownEnqueuePaths``.
"""

from __future__ import annotations

import hashlib
import logging

from botocore.exceptions import BotoCoreError, ClientError
from django.conf import settings
from django.utils import timezone

from scanning import jobs, markup, paragraphs, runpod_client, s3_sync
from scanning.models import (
    DEAD_JOB_STATUSES,
    IN_FLIGHT_JOB_STATUSES,
    ExternalJob,
    JobEngine,
    JobProvider,
    JobStage,
    JobStatus,
    Opinion,
    OpinionReviewStatus,
)

logger = logging.getLogger(__name__)

#: The handler action, the one the worker answers.
ACTION = "tag"

STAGE = JobStage.TAG
ENGINE = JobEngine.CASELAW_TAGGER

#: Bumped whenever ``markup.project`` writes another text for the same
#: approved body. Part of every row's identity, so a projection change
#: starts a new run rather than reusing spans over another text.
PROJECTION_VERSION = 2

#: The characters of tagger input one page allowance of the running-job
#: deadline covers (``jobs.runpod_execution_deadline``). A page of a
#: two-column reporter holds about 3,000 characters of text.
CHARS_PER_PAGE = 3000

#: Version of the spans document :func:`glue_run` writes.
SPANS_SCHEMA = 1

#: How many times the glue of one row is retried before it is left
#: alone, the loud-then-quiet shape of the dots.mocr glue.
GLUE_MAX_ATTEMPTS = 3

#: How many rows one collect tick glues. A glue is two reads and one
#: write of small JSON objects, so the cap only bounds a long backlog.
GLUES_PER_TICK = 50

#: The states :func:`state` answers, for the button and its view.
NONE = "none"
RUNNING = "running"
DONE = "done"
STALE = "stale"
FAILED = "failed"


class TaggerInputError(Exception):
    """The input of an opinion could not be built."""


class TaggerGlueError(Exception):
    """A completed run could not be placed on its approved text."""


class TextMoved(TaggerGlueError):
    """The approved text is not the text the run read.

    A fact about the opinion, not a fault of the glue: a new approval
    wrote another text after the press. The row is consumed without a
    stamp, and the button offers the tag again.
    """


# ── Switches and keys ───────────────────────────────────────────────
def enabled() -> bool:
    """Return whether tagger jobs may be dispatched.

    Both switches, like the other engines: ``TAGGER_ENABLED`` is the
    operator's decision to spend money on this stage, and
    :func:`runpod_client.enabled` covers the account credentials and
    this engine's own endpoint id.

    :returns: Whether the stage should run.
    :rtype: bool
    """
    return bool(
        settings.TAGGER_ENABLED
        and runpod_client.enabled(settings.RUNPOD_TAGGER_ENDPOINT_ID)
    )


def prefix(opinion: Opinion) -> str:
    """Return the prefix every object of this stage lives under.

    Beside ``approved/`` and keyed by the invariant identity of the
    opinion (#350), not by ``glue_revision``: the input comes from the
    approval, not from a glue.

    :param opinion: The opinion, with ``scan``.
    :returns: ``{processing_prefix}jobs/opinions/{first}.{index}/tag/``.
    :rtype: str
    """
    return (
        f"{s3_sync.s3_processing_prefix(opinion.scan)}jobs/opinions/"
        f"{opinion.first_printed_page}.{opinion.index_in_page}/tag/"
    )


def input_key(opinion: Opinion, digest: str) -> str:
    """Return the key of the input document with this text digest.

    Addressed by content, not by run: the row's identity compares the
    key, and a key that carried the run number would read every call
    as new work.

    :param opinion: The opinion.
    :param digest: From :func:`text_digest`.
    :returns: The key.
    :rtype: str
    """
    return f"{prefix(opinion)}input-{digest[:16]}.json"


def spans_key(opinion: Opinion, run: int, approved_key: str) -> str:
    """Return the key of the spans of one run over one approved text.

    The run and the approved text both name it: a second approval of
    the same text reuses the run, and its spans are placed on the new
    object without writing over the spans of the first.

    :param opinion: The opinion.
    :param run: The run number.
    :param approved_key: The ``approved_text_key`` the spans are over.
    :returns: The key.
    :rtype: str
    """
    text = hashlib.sha256(approved_key.encode("utf-8")).hexdigest()[:12]
    return f"{prefix(opinion)}spans-r{run}-{text}.json"


def sequence_id(opinion: Opinion) -> str:
    """Return the id the input gives the one sequence of an opinion.

    :param opinion: The opinion.
    :returns: ``{scan}:{first}.{index}``.
    :rtype: str
    """
    return (
        f"{opinion.scan_id}:{opinion.first_printed_page}."
        f"{opinion.index_in_page}"
    )


def text_digest(text: str) -> str:
    """Return the digest of the text the tagger reads.

    :param text: ``markup.project(...).text``.
    :returns: A hex SHA-256.
    :rtype: str
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def build_payload(job: ExternalJob, input_url: str, output_url: str) -> dict:
    """Return the RunPod ``input`` payload for one opinion.

    :param job: The claimed row. Its ``result_key`` is already set.
    :param input_url: Presigned GET of the input document.
    :param output_url: Presigned PUT for the result, signed with
        ``runpod_client.RESULT_CONTENT_TYPE``.
    :returns: The ``input`` dict to POST.
    :rtype: dict
    """
    return {
        "action": ACTION,
        "scan_pk": job.scan_id,
        "input_url": input_url,
        "result_url": output_url,
        "result_key": job.result_key,
    }


# ── The input ───────────────────────────────────────────────────────
def load_approved(key: str) -> dict:
    """Read an approved text, and refuse a shape this module does not know.

    :param key: An ``approved_text_key``.
    :returns: The object of ``paragraphs.approved_document``.
    :rtype: dict
    :raises TaggerInputError: When the object is missing, not readable,
        or of another ``APPROVED_SCHEMA``.
    """
    try:
        approved = s3_sync.download_json_object(key)
    except (BotoCoreError, ClientError, ValueError) as exc:
        raise TaggerInputError(f"could not read {key}: {exc}") from exc
    if not isinstance(approved, dict):
        raise TaggerInputError(f"{key} is not an approved text")
    if approved.get("schema") != paragraphs.APPROVED_SCHEMA:
        raise TaggerInputError(
            f"{key} is an approved text of schema {approved.get('schema')}; "
            f"the tagger reads schema {paragraphs.APPROVED_SCHEMA}"
        )
    return approved


def project(approved: dict) -> markup.Projection:
    """Return the tagger's text of an approved object: its body alone.

    The footnotes are not sent (#399), and the page table, which holds
    the running heads, is not either.

    :param approved: From :func:`load_approved`.
    :returns: ``markup.project`` of the body.
    :rtype: markup.Projection
    """
    return markup.project(approved.get("body") or [])


def ensure_tag_jobs(
    opinion: Opinion, *, force_new_run: bool = False
) -> list[ExternalJob]:
    """Return the live tagger row of an opinion, creating it if the
    current run does not describe the approved text.

    Writes the input unless an object with that digest is already there,
    and creates one row whose identity is the digest of the text and
    :data:`PROJECTION_VERSION`. Idempotent through
    ``jobs.ensure_run_jobs``: the same text gives the same key and
    identity, so a second call reuses the run, also after a second
    approval of the same text; a run holding a dead row is replaced,
    and a prior result for the same identity is carried
    (``reuse_results``), since the result objects are kept.

    :param opinion: An approved opinion, with ``scan``.
    :param force_new_run: Start a new run even when the live one still
        describes the text.
    :returns: The live run's rows (one).
    :rtype: list[ExternalJob]
    :raises TaggerInputError: If the opinion is not approved, or the
        input cannot be built or written.
    """
    if (
        opinion.status != OpinionReviewStatus.TEXT_REVIEW_DONE
        or not opinion.approved_text_key
    ):
        raise TaggerInputError(f"{opinion} has no approved text")
    if not s3_sync.s3_active():
        raise TaggerInputError(
            "the tagger needs S3: the worker reads its input from the "
            "bucket through a presigned URL"
        )
    approved = load_approved(opinion.approved_text_key)
    projection = project(approved)
    if not projection.text:
        raise TaggerInputError(f"{opinion}: the approved text has no body")
    digest = text_digest(projection.text)
    key = input_key(opinion, digest)
    if not s3_sync.object_exists(key):
        document = {
            "sequences": [
                {"id": sequence_id(opinion), "text": projection.text}
            ]
        }
        if not s3_sync.upload_json_object(key, document):
            raise TaggerInputError(f"{opinion}: could not write {key}")
        logger.info(
            "%s of scan %s: wrote tagger input %s (%d paragraphs, %d chars)",
            opinion,
            opinion.scan_id,
            key,
            len(projection.paragraphs),
            len(projection.text),
        )
    identity = {
        "digest": digest,
        "projection": PROJECTION_VERSION,
        "chars": len(projection.text),
        "paragraphs": len(projection.paragraphs),
        # What the running-job deadline multiplies by
        # (``jobs.runpod_execution_deadline``). From the text and never
        # from the page table: ``_still_describes`` compares the whole
        # identity, so a count the digest does not fix would start a paid
        # run for a second approval of the same text.
        "page_count": max(1, -(-len(projection.text) // CHARS_PER_PAGE)),
    }
    return jobs.ensure_run_jobs(
        opinion.scan,
        [(key, identity)],
        stage=STAGE,
        engine=ENGINE,
        provider=JobProvider.RUNPOD,
        fingerprint=opinion.scan.source_fingerprint or "",
        reuse_results=True,
        force_new_run=force_new_run,
        opinion=opinion,
    )


def live_tag_jobs(opinion: Opinion) -> list[ExternalJob]:
    """Return the opinion's live tagger run.

    :param opinion: The opinion.
    :returns: The rows (one), or an empty list.
    :rtype: list[ExternalJob]
    """
    return jobs.live_run(opinion.scan_id, STAGE, ENGINE, opinion=opinion)


# ── The ledger ──────────────────────────────────────────────────────
def is_written(opinion: Opinion) -> bool:
    """Return whether the spans of the approved text exist.

    The one rule (#272): the spans at ``tag_key`` were placed on the
    object the opinion names now. A reopen keeps ``approved_text_key``,
    so the spans stay valid for it (#375); a new approval and
    ``rewrite_approved_text`` write another key.

    :param opinion: The row.
    :returns: Whether ``tagged_text_key`` is the approved key.
    :rtype: bool
    """
    return bool(
        opinion.approved_text_key
        and opinion.tag_key
        and opinion.tagged_text_key == opinion.approved_text_key
    )


def _glue_attempts(row: ExternalJob) -> int:
    return int(
        ((row.provider_meta or {}).get("glue") or {}).get("attempts") or 0
    )


def state(opinion: Opinion) -> str:
    """Return what the tagger has done with the approved text.

    :param opinion: The row.
    :returns: :data:`DONE` when :func:`is_written`; :data:`RUNNING`
        while the live row is open or waits for its glue; :data:`FAILED`
        when it is dead or its glue gave up; :data:`STALE` when a run
        read an earlier approved text; :data:`NONE` before any run.
    :rtype: str
    """
    if is_written(opinion):
        return DONE
    rows = live_tag_jobs(opinion)
    if not rows:
        return NONE
    row = rows[0]
    if row.status in DEAD_JOB_STATUSES:
        return FAILED
    if row.status == JobStatus.COMPLETED:
        return FAILED if _glue_attempts(row) >= GLUE_MAX_ATTEMPTS else RUNNING
    if row.status == JobStatus.CONSUMED:
        return STALE
    return RUNNING


# ── The glue ────────────────────────────────────────────────────────
def glue_run(opinion: Opinion, row: ExternalJob) -> str:
    """Place a completed run's spans on the approved text, and stamp it.

    Reads the approved key the row holds **now**, not the one of the
    press, and projects it again: the digest of that text must be the
    digest the run read, and then its offset map places every span
    exactly. A second approval of the same text (a reopen, or a new
    join rule that joins nothing new) is placed by the same call, with
    no new job.

    :param opinion: The opinion.
    :param row: Its live row, ``COMPLETED`` or ``CONSUMED``.
    :returns: The key written.
    :rtype: str
    :raises TextMoved: If the approved text is not the text the run read.
    :raises TaggerGlueError: If the result or the approved text cannot
        be read, or the write fails.
    """
    approved_key = (
        Opinion.objects.filter(pk=opinion.pk)
        .values_list("approved_text_key", flat=True)
        .first()
    )
    if not approved_key:
        raise TextMoved(f"{opinion} has no approved text any more")
    try:
        approved = load_approved(approved_key)
    except TaggerInputError as exc:
        raise TaggerGlueError(str(exc)) from exc
    projection = project(approved)
    identity = row.input_manifest or {}
    if text_digest(projection.text) != identity.get("digest"):
        raise TextMoved(
            f"{opinion}: the approved text {approved_key} is not the text "
            f"run {row.run} read"
        )
    if not row.result_key:
        raise TaggerGlueError(f"{opinion} run {row.run} has no result key")
    try:
        envelope = s3_sync.download_json_object(row.result_key)
    except (BotoCoreError, ClientError, ValueError) as exc:
        raise TaggerGlueError(
            f"{opinion} run {row.run}: could not read {row.result_key}: {exc}"
        ) from exc
    payload = jobs.check_result_envelope(
        opinion.scan, row, envelope, ACTION, TaggerGlueError
    )
    sequences = payload.get("sequences")
    if not isinstance(sequences, list) or len(sequences) != 1:
        raise TaggerGlueError(
            f"{opinion} run {row.run}: the result carries "
            f"{len(sequences) if isinstance(sequences, list) else 'no'} "
            "sequence(s), not one"
        )
    sequence = sequences[0]
    if not isinstance(sequence, dict):
        raise TaggerGlueError(
            f"{opinion} run {row.run}: the sequence is not an object"
        )
    spans = []
    for span in sequence.get("spans") or []:
        if not (
            isinstance(span, dict)
            and isinstance(span.get("start"), int)
            and isinstance(span.get("end"), int)
            and isinstance(span.get("label"), str)
        ):
            raise TaggerGlueError(
                f"{opinion} run {row.run}: a span has no integer start and "
                f"end or no label: {str(span)[:200]}"
            )
        for part in markup.lift_span(projection, span["start"], span["end"]):
            spans.append({**part, "label": span["label"]})
    document = {
        "schema": SPANS_SCHEMA,
        "engine": str(ENGINE),
        "scan": opinion.scan_id,
        "opinion": {
            "first_printed_page": opinion.first_printed_page,
            "index_in_page": opinion.index_in_page,
        },
        "approved_text_key": approved_key,
        "run": row.run,
        "input_key": row.input_key,
        "digest": identity.get("digest"),
        "projection": identity.get("projection"),
        "model": payload.get("model"),
        "max_tokens": payload.get("max_tokens"),
        # The spans in the offsets of the approved body: ``paragraph``
        # indexes ``body``, and ``start``/``end`` index its ``text``.
        "spans": spans,
        # The worker's own answer, verbatim, in the offsets of the input.
        "raw": sequence,
        "generated_at": timezone.now().isoformat(),
    }
    key = spans_key(opinion, row.run, approved_key)
    if not s3_sync.upload_json_object(key, document):
        raise TaggerGlueError(
            f"{opinion} run {row.run}: could not write {key}"
        )
    Opinion.objects.filter(pk=opinion.pk).update(
        tag_key=key, tagged_text_key=approved_key
    )
    opinion.tag_key = key
    opinion.tagged_text_key = approved_key
    logger.info(
        "%s of scan %s: tagger run %d placed %d span(s) -> %s",
        opinion,
        opinion.scan_id,
        row.run,
        len(spans),
        key,
    )
    return key


def _record_glue_failure(row: ExternalJob, exc: Exception) -> None:
    """Count a glue failure on the row, loud at the crossing into "out
    of tries" and quiet after, the dots.mocr glue's shape."""
    meta = dict(row.provider_meta or {})
    glue = dict(meta.get("glue") or {})
    glue["attempts"] = int(glue.get("attempts") or 0) + 1
    glue["last_error"] = str(exc)[:500]
    glue["last_attempt_at"] = timezone.now().isoformat()
    meta["glue"] = glue
    ExternalJob.objects.filter(pk=row.pk).update(provider_meta=meta)
    log = (
        logger.error
        if glue["attempts"] >= GLUE_MAX_ATTEMPTS
        else logger.warning
    )
    log(
        "scan %s tagger row %s: glue failed (attempt %d of %d): %s",
        row.scan_id,
        row.pk,
        glue["attempts"],
        GLUE_MAX_ATTEMPTS,
        exc,
    )


def _consume(row: ExternalJob) -> None:
    ExternalJob.objects.filter(pk=row.pk, status=JobStatus.COMPLETED).update(
        status=JobStatus.CONSUMED, consumed_at=timezone.now()
    )


def place(opinion: Opinion, row: ExternalJob) -> str:
    """Place a finished row's spans now, in the request of a press.

    The press after :func:`ensure_tag_jobs` hands back a row that needs
    no job: ``COMPLETED`` (a result carried from an earlier run of the
    same text, or a row whose glue gave up on the tick) or ``CONSUMED``
    (a second approval of the same text). The result is paid for, so
    the retry is this press and not a new job (the rule of #336). A
    fault counts on the row like a fault of the tick, and the next
    press tries again.

    :param opinion: The opinion.
    :param row: Its live row, ``COMPLETED`` or ``CONSUMED``.
    :returns: The key written.
    :rtype: str
    :raises TaggerGlueError: If the glue fails; :class:`TextMoved` when
        the approved text moved, after the row is consumed.
    """
    try:
        key = glue_run(opinion, row)
    except TextMoved:
        _consume(row)
        raise
    except TaggerGlueError as exc:
        if row.status == JobStatus.COMPLETED:
            _record_glue_failure(row, exc)
        raise
    _consume(row)
    return key


def finish_ready_runs(limit: int = GLUES_PER_TICK) -> int:
    """Glue the finished tagger rows onto their approved texts.

    A row is ready when it is ``COMPLETED`` and is its opinion's live
    row; an older run's row is left alone. A row whose approved text
    moved (:class:`TextMoved`) is consumed without a stamp, because the
    result still describes the text it read. Writes no scan status and
    no opinion status: the spans are the output, and what reads them is
    the assembly step (#408), not the review flow.

    :param limit: The most rows this call glues.
    :returns: How many rows were glued and consumed.
    :rtype: int
    """
    if not s3_sync.s3_active():
        return 0
    unfinished = {JobStatus.PENDING} | IN_FLIGHT_JOB_STATUSES
    rows = (
        ExternalJob.objects.filter(
            stage=STAGE,
            engine=ENGINE,
            provider=JobProvider.RUNPOD,
            status=JobStatus.COMPLETED,
            opinion__isnull=False,
        )
        .select_related("opinion", "opinion__scan")
        .order_by("completed_at", "pk")
    )
    glued = 0
    tried = 0
    for row in rows:
        # In Python and not in the query: a row with no ``glue`` key is
        # NULL to a JSON lookup, and an ``exclude`` would drop it too.
        if _glue_attempts(row) >= GLUE_MAX_ATTEMPTS:
            continue
        if tried >= limit:
            break
        tried += 1
        live = live_tag_jobs(row.opinion)
        if not live or live[0].pk != row.pk or live[0].status in unfinished:
            continue
        try:
            glue_run(row.opinion, row)
        except TextMoved as exc:
            logger.info("%s; the result is kept and not placed", exc)
            _consume(row)
            continue
        except Exception as exc:
            _record_glue_failure(row, exc)
            continue
        _consume(row)
        glued += 1
    return glued
