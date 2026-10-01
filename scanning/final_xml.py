"""The export of an opinion's final XML, the file CourtListener reads (#408).

``casebody.build`` merges the approved text (#375) and the tagger's
spans (#272) into one XML document. The review page builds it at each
request (#432). This module stores the same build, once per approved
and tagged opinion, at a key CourtListener builds from the two ids
alone:

    final-xml/{scan pk}/{opinion pk}.xml

in the private bucket, outside ``processing/``. CourtListener's
``import_scanned_opinions`` lists ``final-xml/{scan}/`` for a volume
and reads one key for an opinion; it copies each file into its own
storage (``filepath_xml_scan``), so the bucket is read by one read-only
user and no public URL points at it.

**The key is stable, so the export writes over it.** Every other glue
of an opinion writes a new key per revision. This one cannot: the
reader builds the key from the ids. The ledger on the row says what the
object holds: ``final_xml_tag_key`` is the ``tag_key`` it was built
from, and ``final_xml_schema`` the ``casebody.SCHEMA``. The spans key
names one approved text and one tagger run, so a new approval, a
``rewrite_approved_text`` and a new run all make the row due again, and
a new ``SCHEMA`` makes every row due (:func:`is_written`).

**An object exists only for an exportable opinion** (:func:`exportable`:
the text review is done and the spans are over the approved text). A
reopen keeps both keys (#375), so the pass also deletes the object of
a row that stopped being exportable and clears its stamp. A listing of
the prefix is then the list of what CourtListener may import.

**The pass** (:func:`export_due`) is pass fifteen of the collect tick:
two small JSON reads and one small PUT per row, the work of the glues.
The object is written first and the row second, by a compare-and-swap
over the inputs it read. A fault of the objects (a missing key, spans
that do not fit the text) counts on ``final_xml_attempts`` and stops at
:data:`MAX_ATTEMPTS`, loud then quiet; an S3 fault counts nothing and
waits for the next tick. It writes no opinion status and no scan
status: the approval stands whatever the export does.
"""

from __future__ import annotations

import logging

from botocore.exceptions import BotoCoreError, ClientError
from django.db.models import F, Q, QuerySet

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

    The one rule of the key: ``final-xml/{scan}/{opinion}.xml``, from
    the two primary keys alone, because CourtListener builds it.

    :param opinion: The opinion.
    :returns: The key.
    :rtype: str
    """
    return f"{s3_sync.FINAL_XML_PREFIX}{opinion.scan_id}/{opinion.pk}.xml"


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
            ids={"scan-id": opinion.scan_id, "opinion-id": opinion.pk},
        )
    except casebody.CasebodyError as exc:
        raise FinalXmlError(str(exc)) from exc
    return xml, tags


# ── The writers ─────────────────────────────────────────────────────
def export(opinion: Opinion) -> bool:
    """Store the final XML of one exportable opinion and stamp the row.

    The object first, then a compare-and-swap over the inputs the build
    read. When the row moved during the build, the stamp is not written:
    the next pass writes the new inputs, or deletes an object that no
    stamp names.

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
        fresh = Opinion.objects.filter(pk=opinion.pk).first()
        if fresh is None or (
            not exportable(fresh) and not fresh.final_xml_tag_key
        ):
            # No stamp names the object, so no pass would delete it.
            s3_sync.delete_object(object_key)
        logger.info(
            "%s of scan %s: the row moved during the export of %s; "
            "the next pass writes it again",
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


def withdraw(opinion: Opinion) -> None:
    """Delete the stored final XML of a row that is not exportable.

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
    """Return the rows whose stored object must go.

    :param opinions: The rows to look in; every opinion by default.
    :rtype: QuerySet
    """
    opinions = Opinion.objects.all() if opinions is None else opinions
    return (
        opinions.exclude(final_xml_tag_key="")
        .exclude(_exportable_q())
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


def export_due(
    limit: int | None = EXPORTS_PER_TICK, opinions: QuerySet | None = None
) -> int:
    """Delete the objects that must go, then write the ones owed.

    Pass fifteen of the collect tick. The deletions go first: a
    reopened opinion must leave the listing CourtListener reads before
    the newest approvals are written.

    :param limit: The most rows one call writes or deletes; None for no
        cap (the command).
    :param opinions: The rows to look in; every opinion by default.
    :returns: How many objects were written or deleted.
    :rtype: int
    """
    if not s3_sync.s3_active():
        return 0
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
    for opinion in due_rows(opinions)[:left]:
        try:
            wrote = export(opinion)
        except TransientFault as exc:
            logger.warning("%s: %s; the next tick tries again", opinion, exc)
            continue
        except FinalXmlError as exc:
            _record_failure(opinion, exc)
            continue
        except Exception as exc:
            # A fault of this code must not stop the tick at this row on
            # every pass: it counts like a fault of the objects.
            logger.exception("%s: the export raised", opinion)
            _record_failure(opinion, exc)
            continue
        done += wrote
    return done
