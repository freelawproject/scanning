"""The export of an opinion's final XML, the file CourtListener reads (#408).

``casebody.build`` merges the approved text (#375) and the tagger's
spans (#272) into one XML document. The review page builds it at each
request (#432). This module stores the same build, once per approved
and tagged opinion, at a key CourtListener builds from the two ids
alone:

    export/{scan pk}/{opinion pk}.xml

in the private bucket, outside ``processing/``. CourtListener's
``import_scanned_opinions`` lists ``export/{scan}/`` for a volume
and reads one key for an opinion; it copies each file into its own
storage (``filepath_xml_scan``), so the bucket is read by one read-only
user and no public URL points at it.

**``export/`` holds the outputs and nothing else.** CourtListener's
read-only user gets ``s3:GetObject`` on ``export/*`` and ``s3:ListBucket``
limited to the ``export/`` prefix, so it reaches no unredacted page: the
original, the shards and the engine results stay under ``processing/``.
Only this module writes under ``export/``.

**The redacted PDF goes here too, in a later PR.** It is now at
``opinion_pdf.key``, inside the scan's processing prefix and under a
revision, so the policy above cannot reach it, and CourtListener cannot
build its key from the ids. The design:

- The key is ``export/{scan pk}/{opinion pk}.pdf``, beside the XML.
- The pass copies the PDF server side (``s3_sync.copy_object``) from
  ``opinion_pdf.key(opinion, redacted_pdf_revision)``. A copy, never a
  move: a re-glue and the prune of old revisions (#452) must not change
  the file CourtListener reads.
- The ledger is a second pair of fields: ``final_pdf_revision`` (the
  ``redacted_pdf_revision`` the copy holds; null when no object is
  stored) and ``final_pdf_attempts``. The PDF is owed when the opinion
  is ``TEXT_REVIEW_DONE``, ``opinion_pdf.is_written`` holds, and the
  stamp is not ``redacted_pdf_revision``. So every new PDF of an
  approved opinion is copied again, whatever wrote it.
- The XML and the PDF are owed apart: the PDF waits for the approval
  alone, the XML for the tagger too. The pass copies the PDF first, so
  an XML in the listing has its PDF beside it.
- A reopen deletes both objects, the rule of the XML. A missing source
  PDF counts on ``final_pdf_attempts``; an S3 fault counts nothing. The
  stamp after the copy can be unconditional, because the one exporter
  (:func:`exporter_lock`) knows which revision it copied; a row that is
  gone takes its object with it.
- :func:`parse_key` and :func:`orphan_keys` read both extensions, each
  against its own stamp. The admin sweep of ``export/{scan}/`` and the
  command take both with no change.

Today nothing writes a new PDF for an approved opinion
(``opinions.create_rows`` and the OCR re-glue skip
``TEXT_REVIEW_DONE``): a redaction change reaches it through a reopen
and a new approval.

**The key is stable, so the export writes over it.** Every other glue
of an opinion writes a new key per revision. This one cannot: the
reader builds the key from the ids. The ledger on the row says what the
object holds: ``final_xml_tag_key`` is the ``tag_key`` it was built
from, and ``final_xml_schema`` the ``casebody.SCHEMA``. The spans key
names one approved text and one tagger run, so a new approval, a
``rewrite_approved_text`` and a new run all make the row due again, and
a new ``SCHEMA`` makes every row due (:func:`is_written`).

**The pass writes an object only for an exportable opinion**
(:func:`exportable`: the text review is done and the spans are over the
approved text), **and deletes it only when the text is no longer
approved** (a reopen or an error: the status left ``TEXT_REVIEW_DONE``).
A reopen keeps both keys (#375), so the deletion reads the status. A
``rewrite_approved_text`` moves the approved key and leaves the spans
behind, and the object it keeps is still a whole document: the text the
curator approved, under the old join rule, and the spans over that
text. It stays until a tagger run of the new text writes the new one. A
rewrite that keeps the body carries the spans with the key
(``opinion_review.rewrite_text``), and the spans key alone would then
read the old object as current: the rewrite sets ``final_xml_schema``
to None, so the pass writes the new footnotes. A listing of the prefix
is then the list of what CourtListener may import.

**The window of a reopen.** The deletion runs on the tick, so a reopen,
an edit and a new approval inside one blocked tick (the Mistral wave
holds the loop for minutes) leave the old object listed: the status is
``TEXT_REVIEW_DONE`` again, and the spans are over the old text, the
state of a rewrite that dropped the spans. The ledger cannot tell the
two apart, so the object stays until the tagger press places the spans
on the new text, which writes it over. CourtListener may import the
old text in that window; the next import reads the new one.

**A stamp names every object.** ``final_xml_tag_key`` is not blank
whenever an object may be at the key, so no object is left with no row
to delete it. A ``final_xml_schema`` of None beside a stamp says "an
object may be there, of unknown content": the row is owed again.

**One exporter at a time.** S3 keeps the last PUT, and the stamp is a
second write, so two exporters of one row could stamp the new inputs
over the old object. The pass takes a Postgres advisory lock
(:data:`EXPORT_LOCK`) that the tick and the command share: the tick
skips while the command holds it, and the command refuses while the
tick holds it. The web process
changes the inputs and never exports, and the compare-and-swap of the
stamp answers it.

**The pass** (:func:`export_due`) is pass fifteen of the collect tick:
two small JSON reads and one small PUT per row, the work of the glues.
The object is written first and the row second, by a compare-and-swap
over the inputs it read. A fault of the objects (a missing key, spans
that do not fit the text) counts on ``final_xml_attempts`` and stops at
:data:`MAX_ATTEMPTS`, loud then quiet; an S3 fault counts nothing and
waits for the next tick, and a database fault goes to the tick's own
retry. It writes no opinion status and no scan status: the approval
stands whatever the export does.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager

from botocore.exceptions import BotoCoreError, ClientError
from django.db import DatabaseError, connection
from django.db.models import Case, F, Q, QuerySet, Value, When

from scanning import casebody, s3_sync, tagger
from scanning.models import Opinion, OpinionReviewStatus

logger = logging.getLogger(__name__)

#: The ``ContentType`` of a stored document.
CONTENT_TYPE = "application/xml; charset=utf-8"

#: Faults of the objects before the pass leaves a row alone. New spans
#: reset the count (``tagger.glue_run``), and so does the command.
MAX_ATTEMPTS = 3

#: How many rows one tick writes or deletes. A row is two reads and one
#: write of small objects, so the cap only bounds a long backlog.
EXPORTS_PER_TICK = 50

#: The key of the Postgres advisory lock the pass holds: one exporter at
#: a time (the tick or the command). Any constant no other code takes.
EXPORT_LOCK = 408_001


class FinalXmlError(Exception):
    """The objects of an opinion do not give its final XML.

    A fact about the row: it counts on ``final_xml_attempts``.
    """


class TransientFault(Exception):
    """S3 did not answer. The row waits for the next tick and counts
    nothing."""


# ── The key and the ledger ──────────────────────────────────────────
def key(opinion: Opinion) -> str:
    """Return the key of an opinion's exported final XML.

    The one rule of the key: ``export/{scan}/{opinion}.xml``, from
    the two primary keys alone, because CourtListener builds it.

    :param opinion: The opinion.
    :returns: The key.
    :rtype: str
    """
    return f"{s3_sync.EXPORT_PREFIX}{opinion.scan_id}/{opinion.pk}.xml"


def exportable(opinion: Opinion) -> bool:
    """Return whether an opinion's final XML may be stored.

    The text review is done and the spans are over the approved text
    the row names now (``tagger.is_written``). The query twin is
    :func:`_exportable_q`.

    :param opinion: The row.
    :rtype: bool
    """
    return (
        opinion.status == OpinionReviewStatus.TEXT_REVIEW_DONE
        and tagger.is_written(opinion)
    )


def _exportable_q() -> Q:
    return (
        Q(status=OpinionReviewStatus.TEXT_REVIEW_DONE)
        & ~Q(approved_text_key="")
        & ~Q(tag_key="")
        & Q(tagged_text_key=F("approved_text_key"))
    )


def is_written(opinion: Opinion) -> bool:
    """Return whether the stored final XML is the one the row owes.

    The one rule (#408): the opinion is :func:`exportable`, and the
    object was built from its spans under this ``casebody.SCHEMA``.

    :param opinion: The row.
    :rtype: bool
    """
    return (
        exportable(opinion)
        and opinion.final_xml_tag_key == opinion.tag_key
        and opinion.final_xml_schema == casebody.SCHEMA
    )


def is_stored(opinion: Opinion) -> bool:
    """Return whether an object may be at the opinion's key.

    A stamp names every object (see the module docstring), so this is
    the stamp. The object may be older than the row's inputs: after a
    ``rewrite_approved_text`` it holds the text approved before.

    :param opinion: The row.
    :rtype: bool
    """
    return bool(opinion.final_xml_tag_key)


def parse_key(object_key: str) -> tuple[int, int] | None:
    """Return the scan and opinion pks a key of :func:`key` names.

    :param object_key: A key under ``export/``.
    :returns: ``(scan_pk, opinion_pk)``, or None for a key of another
        shape.
    :rtype: tuple[int, int] | None
    """
    rest = object_key.removeprefix(s3_sync.EXPORT_PREFIX)
    scan, _, name = rest.partition("/")
    opinion, dot, ext = name.partition(".")
    if rest == object_key or not dot or ext != "xml":
        return None
    if not (scan.isdigit() and opinion.isdigit()):
        return None
    return int(scan), int(opinion)


def orphan_keys(prefix: str = s3_sync.EXPORT_PREFIX) -> list[str]:
    """Return the objects under the prefix that no stamp names.

    The backup of the admin sweep (#408): a key of another shape, of an
    opinion that does not exist or is of another scan, or of a row with
    no stamp. A row with a stamp is the pass's own (it deletes it when
    the review is open again). Read under :func:`exporter_lock`, or an
    export between its PUT and its stamp reads as an orphan.

    :param prefix: ``export/`` or one scan's part of it.
    :returns: The keys.
    :rtype: list[str]
    :raises TransientFault: When the listing failed: a caller deletes
        what this returns, so a fault is never "nothing there".
    """
    keys = s3_sync.list_keys(prefix)
    if keys is None:
        raise TransientFault(f"could not list {prefix}")
    named = {k: parse_key(k) for k in keys}
    stamped = {
        (scan_id, pk)
        for pk, scan_id in Opinion.objects.filter(
            pk__in=[ids[1] for ids in named.values() if ids]
        )
        .exclude(final_xml_tag_key="")
        .values_list("pk", "scan_id")
    }
    return [k for k, ids in named.items() if ids not in stamped]


# ── The build ───────────────────────────────────────────────────────
def _read(object_key: str):
    try:
        return s3_sync.download_json_object(object_key)
    except ClientError as exc:
        if s3_sync.is_missing_object(exc):
            raise FinalXmlError(f"{object_key} is not in the bucket") from exc
        raise TransientFault(f"could not read {object_key}: {exc}") from exc
    except BotoCoreError as exc:
        raise TransientFault(f"could not read {object_key}: {exc}") from exc
    except ValueError as exc:
        raise FinalXmlError(f"{object_key} is not JSON: {exc}") from exc


#: The reporters whose citation eyecite did not read, logged once per
#: process: the final XML is built at every request and every export.
_UNREAD_REPORTERS: set[str] = set()


def main_citation(opinion: Opinion) -> str:
    """Return the citation of an opinion in its scan's reporter (#435).

    A reporter missing from ``Reporter.CITE_MAP`` gives a name eyecite
    does not read, and CourtListener's importer drops the citation, so
    the miss is logged for a developer to add the abbreviation, once
    per reporter (:data:`_UNREAD_REPORTERS`).

    :param opinion: The opinion, with ``scan__reporter``.
    :rtype: str
    """
    scan = opinion.scan
    citation = casebody.main_citation(
        scan.volume, scan.reporter.cite_name, opinion.first_printed_page
    )
    short_name = scan.reporter.short_name
    if (
        short_name not in _UNREAD_REPORTERS
        and casebody.full_citation(citation) is None
    ):
        _UNREAD_REPORTERS.add(short_name)
        logger.error(
            "%s: eyecite does not read the citation %r; add the reporter "
            "%r to Reporter.CITE_MAP",
            opinion,
            citation,
            short_name,
        )
    return citation


def render(opinion: Opinion) -> tuple[str, dict]:
    """Build the final XML of an opinion from its two objects.

    The one build: the review page (#432) and the export call it, so
    the file a person reads is the file CourtListener reads.

    :param opinion: A row whose spans exist (``tagger.is_written``).
    :returns: The XML, and the spans object for the page's legend.
    :rtype: tuple[str, dict]
    :raises FinalXmlError: When an object is missing or of another
        shape, or the spans do not fit the text.
    :raises TransientFault: When S3 did not answer.
    """
    try:
        approved = tagger.check_approved(
            _read(opinion.approved_text_key), opinion.approved_text_key
        )
    except tagger.TaggerInputError as exc:
        raise FinalXmlError(str(exc)) from exc
    tags = _read(opinion.tag_key)
    if not isinstance(tags, dict):
        raise FinalXmlError(f"{opinion.tag_key} is not a spans object")
    try:
        xml = casebody.build(
            approved,
            tags,
            main_citation(opinion),
            ids={"scan-id": opinion.scan_id, "opinion-id": opinion.pk},
        )
    except casebody.CasebodyError as exc:
        raise FinalXmlError(str(exc)) from exc
    return xml, tags


# ── The writers ─────────────────────────────────────────────────────
def export(opinion: Opinion) -> bool:
    """Store the final XML of one exportable opinion and stamp the row.

    The object first, then a compare-and-swap over the inputs the build
    read. When the row moved during the build, the object holds old
    inputs: the row is marked "content unknown" (:func:`_mark_unknown`),
    and the next pass writes it again or deletes it. A row that is gone
    takes its object with it.

    :param opinion: An :func:`exportable` row.
    :returns: Whether the row was stamped.
    :rtype: bool
    :raises FinalXmlError: See :func:`render`.
    :raises TransientFault: When S3 did not answer.
    """
    tag_key = opinion.tag_key
    approved_key = opinion.approved_text_key
    xml, _tags = render(opinion)
    object_key = key(opinion)
    if not s3_sync.upload_bytes_object(
        object_key, xml.encode("utf-8"), CONTENT_TYPE
    ):
        raise TransientFault(f"could not write {object_key}")
    stamped = Opinion.objects.filter(
        pk=opinion.pk,
        status=OpinionReviewStatus.TEXT_REVIEW_DONE,
        approved_text_key=approved_key,
        tagged_text_key=approved_key,
        tag_key=tag_key,
    ).update(
        final_xml_tag_key=tag_key,
        final_xml_schema=casebody.SCHEMA,
        final_xml_attempts=0,
    )
    if not stamped:
        if not _mark_unknown(opinion, tag_key):
            # The row is gone (a scan deletion): no stamp can name the
            # object, so no pass would delete it.
            s3_sync.delete_object(object_key)
        logger.info(
            "%s of scan %s: the row moved during the export of %s; "
            "the next pass writes it again or deletes it",
            opinion,
            opinion.scan_id,
            object_key,
        )
        return False
    opinion.final_xml_tag_key = tag_key
    opinion.final_xml_schema = casebody.SCHEMA
    opinion.final_xml_attempts = 0
    logger.info(
        "%s of scan %s: exported the final XML to %s",
        opinion,
        opinion.scan_id,
        object_key,
    )
    return True


def _mark_unknown(opinion: Opinion, tag_key: str) -> bool:
    """Say that the object of a row holds content no stamp describes.

    The schema goes to None, so :func:`is_written` is false and the row
    is owed again; the stamp stays, or takes ``tag_key`` when it was
    blank, so a deletion still finds the object. Unconditional on
    purpose: the one exporter wrote the object last, and the worst a
    mark costs is one more write.

    :param opinion: The row.
    :param tag_key: The spans key the object was built from.
    :returns: Whether the row still exists.
    :rtype: bool
    """
    return bool(
        Opinion.objects.filter(pk=opinion.pk).update(
            final_xml_tag_key=Case(
                When(final_xml_tag_key="", then=Value(tag_key)),
                default=F("final_xml_tag_key"),
            ),
            final_xml_schema=None,
        )
    )


def withdraw(opinion: Opinion) -> None:
    """Delete the stored final XML of a row whose text is not approved.

    The object first, then the stamp, by a compare-and-swap over the
    stamp the row held.

    :param opinion: A row with a ``final_xml_tag_key``.
    :raises TransientFault: When S3 did not take the delete.
    """
    object_key = key(opinion)
    if not s3_sync.delete_object(object_key):
        raise TransientFault(f"could not delete {object_key}")
    Opinion.objects.filter(
        pk=opinion.pk, final_xml_tag_key=opinion.final_xml_tag_key
    ).update(final_xml_tag_key="", final_xml_schema=None)
    opinion.final_xml_tag_key = ""
    opinion.final_xml_schema = None
    logger.info(
        "%s of scan %s: deleted the final XML at %s (status %s)",
        opinion,
        opinion.scan_id,
        object_key,
        opinion.status,
    )


def _record_failure(opinion: Opinion, exc: Exception) -> None:
    attempts = opinion.final_xml_attempts + 1
    Opinion.objects.filter(pk=opinion.pk).update(final_xml_attempts=attempts)
    opinion.final_xml_attempts = attempts
    log = logger.error if attempts >= MAX_ATTEMPTS else logger.warning
    log(
        "%s of scan %s: the final XML was not exported (attempt %d of %d): %s",
        opinion,
        opinion.scan_id,
        attempts,
        MAX_ATTEMPTS,
        exc,
    )


# ── The pass ────────────────────────────────────────────────────────
def withdrawn_rows(opinions: QuerySet | None = None) -> QuerySet:
    """Return the rows whose stored object must go: the text review is
    no longer done.

    Not "not exportable": a rewrite of the approved text leaves the
    spans behind, and the object of the old text stays until a tagger
    run of the new one (see the module docstring).

    :param opinions: The rows to look in; every opinion by default.
    :rtype: QuerySet
    """
    opinions = Opinion.objects.all() if opinions is None else opinions
    return (
        opinions.exclude(final_xml_tag_key="")
        .exclude(status=OpinionReviewStatus.TEXT_REVIEW_DONE)
        .order_by("pk")
    )


def owed_rows(opinions: QuerySet | None = None) -> QuerySet:
    """Return the exportable rows whose object is missing or old.

    The query twin of "exportable and not :func:`is_written`", the
    rows at the attempt cap included.

    :param opinions: The rows to look in; every opinion by default.
    :rtype: QuerySet
    """
    opinions = Opinion.objects.all() if opinions is None else opinions
    return (
        opinions.filter(_exportable_q())
        .exclude(
            final_xml_tag_key=F("tag_key"), final_xml_schema=casebody.SCHEMA
        )
        .order_by("-approved_at", "-pk")
    )


def due_rows(opinions: QuerySet | None = None) -> QuerySet:
    """Return the owed rows the pass still tries: under the attempt cap.

    :param opinions: The rows to look in; every opinion by default.
    :rtype: QuerySet
    """
    return owed_rows(opinions).filter(final_xml_attempts__lt=MAX_ATTEMPTS)


@contextmanager
def exporter_lock() -> Iterator[bool]:
    """Hold the one exporter's advisory lock, if no one else holds it.

    A session lock and not a transaction lock: the pass makes S3 calls,
    and a transaction around them would hold the row locks of every
    stamp until the pass ends. Nobody waits for it.

    :returns: (as the context value) Whether this call holds the lock.
    """
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_try_advisory_lock(%s)", [EXPORT_LOCK])
        held = cursor.fetchone()[0]
    try:
        yield held
    finally:
        if held:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_unlock(%s)", [EXPORT_LOCK])


def export_due(
    limit: int | None = EXPORTS_PER_TICK, opinions: QuerySet | None = None
) -> int:
    """Delete the objects that must go, then write the ones owed.

    Pass fifteen of the collect tick. The deletions go first: a
    reopened opinion must leave the listing CourtListener reads before
    the newest approvals are written. Nothing runs while another
    exporter holds :func:`exporter_lock`.

    :param limit: The most rows one call writes or deletes; None for no
        cap (the command).
    :param opinions: The rows to look in; every opinion by default.
    :returns: How many objects were written or deleted.
    :rtype: int
    """
    done = run_locked(limit, opinions)
    if done is None:
        logger.info("Another exporter holds the lock; the pass waits")
        return 0
    return done


def run_locked(
    limit: int | None = EXPORTS_PER_TICK, opinions: QuerySet | None = None
) -> int | None:
    """Run the pass under :func:`exporter_lock`.

    :param limit: See :func:`export_due`.
    :param opinions: See :func:`export_due`.
    :returns: How many objects were written or deleted, or None when
        another exporter holds the lock. 0 when S3 is off.
    :rtype: int | None
    """
    if not s3_sync.s3_active():
        return 0
    with exporter_lock() as held:
        if not held:
            return None
        return export_rows(limit, opinions)


def export_rows(limit: int | None, opinions: QuerySet | None) -> int:
    """Delete the objects that must go, then write the ones owed.

    The body of the pass. Call it under :func:`exporter_lock` alone.

    :param limit: See :func:`export_due`.
    :param opinions: See :func:`export_due`.
    :returns: How many objects were written or deleted.
    :rtype: int
    """
    done = 0
    for opinion in withdrawn_rows(opinions)[:limit]:
        try:
            withdraw(opinion)
        except TransientFault as exc:
            logger.warning("%s: %s; the next tick tries again", opinion, exc)
            continue
        done += 1
    left = None if limit is None else limit - done
    if left is not None and left <= 0:
        return done
    for opinion in due_rows(opinions).select_related("scan__reporter")[:left]:
        try:
            wrote = export(opinion)
        except TransientFault as exc:
            logger.warning("%s: %s; the next tick tries again", opinion, exc)
            continue
        except FinalXmlError as exc:
            _record_failure(opinion, exc)
            continue
        except DatabaseError:
            # The tick retries a database fault; it says nothing of the
            # row, so it counts nothing.
            raise
        except Exception as exc:
            # A fault of this code must not stop the tick at this row on
            # every pass: it counts like a fault of the objects.
            logger.exception("%s: the export raised", opinion)
            _record_failure(opinion, exc)
            continue
        done += wrote
    return done
