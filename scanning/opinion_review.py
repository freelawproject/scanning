"""The approval of an opinion's text, and its frozen output (#375).

The third review closes here. A curator approves the text of one
opinion once no blocking card is open, and the approval writes the one
object the final XML and the tagger (#310) read: the approved text, a
flow of paragraphs and a list of footnotes (``paragraphs``).

**The gate** is :func:`blocking_findings`, the one rule for "the
approval waits": an ERROR card no dismissal answers, or a card no
dismissal can answer (``UNDISMISSABLE_OPINION_CHECKS``: the two stale
checks, whose way out is a new run of ``opinions.create_rows``, and an
unresolved human edit, whose way out is its Undo). A warning card never
blocks. The view, the review page and the list badge read this rule,
and none of them writes a second copy.

**The write** is the order of ``ensemble.write``: the object first,
the row second. The object goes to a key of its own, outside the glue
prefix (:func:`approved_key`), so no re-glue and no delete of a
replaced ensemble document (#376) reaches it, and nothing overwrites
it. The row moves by a compare-and-swap over the status and the three
revisions the curator saw, under a lock, with the blocking cards read
again inside it: a dismissal is not a revision, and a rebuild between
the read and the swap can write a card.

The gate of the status lives here and in the view alike, the rule of
every refused write: :func:`approve_text` refuses on its own, so a
caller that is not the view cannot skip it.
"""

import logging

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from scanning import apply, ensemble, paragraphs, s3_sync
from scanning.models import (
    UNDISMISSABLE_OPINION_CHECKS,
    Issue,
    Opinion,
    OpinionFinding,
    OpinionReviewStatus,
)

logger = logging.getLogger(__name__)

#: The least version of the ensemble document the approval reads: 8
#: gives every drop its place in the reading order, which the join rule
#: needs to keep two blocks apart when a redaction went between them.
MIN_DOCUMENT_SCHEMA = 8

#: Why an approval or a rewrite is refused. The view owns the words.
CLOSED = "closed"
NOT_WRITTEN = "not_written"
NOT_BUILT = "not_built"
STALE_PAGE = "stale_page"
BLOCKED = "blocked"
OLD_DOCUMENT = "old_document"
NO_PAGE_NUMBERS = "no_page_numbers"
BUCKET = "bucket"
MOVED = "moved"


class ApprovalRefused(Exception):
    """The approval did not happen, and ``code`` says why."""

    def __init__(self, code: str, detail: str = "", count: int = 0):
        super().__init__(detail or code)
        self.code = code
        self.count = count


def blocks(finding: OpinionFinding) -> bool:
    """Return whether a card of this check and severity blocks.

    The rule of :func:`blocking_findings` for one row the caller holds,
    so the review page sorts its cards by the same rule.

    :param finding: The card.
    :returns: Whether the approval waits on it while it is open.
    :rtype: bool
    """
    return (
        finding.severity == Issue.Severity.ERROR
        or finding.check_name in UNDISMISSABLE_OPINION_CHECKS
    )


def blocking_filter() -> Q:
    """Return the filter of an open blocking card, the shape of :func:`blocks`.

    ``opinions.finding_counts`` counts them for one page of the list
    with it, so the badge and the gate read one rule.

    :returns: A ``Q`` over ``OpinionFinding``.
    """
    return Q(dismissal__isnull=True) & (
        Q(severity=Issue.Severity.ERROR)
        | Q(check_name__in=UNDISMISSABLE_OPINION_CHECKS)
    )


def blocking_findings(opinion: Opinion):
    """Return the open cards the approval of one opinion waits on.

    **The one rule** of the gate (#375). A card of an undismissable
    check has no dismissal, so it is open while it stands.

    :param opinion: The opinion.
    :returns: A queryset of ``OpinionFinding`` rows.
    """
    return OpinionFinding.objects.filter(opinion=opinion).filter(
        blocking_filter()
    )


def approved_key(
    opinion: Opinion,
    edit_revision: int,
    join_rule: int | None = None,
) -> str:
    """Return the S3 key of the approved text of one approval.

    Outside the ``r{n}/`` glue prefix. The edit revision is in the key,
    because a reopen plus one edit gives a second approval at the same
    glue revision, and the join rule is in it, because
    ``rewrite_approved_text`` writes the same approval again under a
    new rule. So no two texts share a key.

    :param opinion: The row, at the revision of the approval.
    :param edit_revision: The edit revision of the approved text.
    :param join_rule: The rule of the text; None is the rule of this
        code, read at the call.
    :returns: The key.
    :rtype: str
    """
    if join_rule is None:
        join_rule = paragraphs.JOIN_RULE
    prefix = s3_sync.s3_processing_prefix(opinion.scan)
    return (
        f"{prefix}jobs/opinions/{opinion.first_printed_page}."
        f"{opinion.index_in_page}/approved/r{opinion.glue_revision}."
        f"e{edit_revision}.j{join_rule}.json"
    )


def _printed(opinion: Opinion) -> dict[int, str]:
    """Read the approved page number of every page of the opinion's run.

    :param opinion: The row, with ``apply_run`` and ``scan``.
    :returns: ``{page_index: number}``.
    :raises ApprovalRefused: When the run or its map cannot be read.
    """
    if opinion.apply_run is None:
        raise ApprovalRefused(NO_PAGE_NUMBERS, "the opinion has no apply run")
    try:
        printed = apply.load_printed_pages(opinion.scan, opinion.apply_run)
    except apply.ApplyError as exc:
        raise ApprovalRefused(NO_PAGE_NUMBERS, str(exc)) from exc
    return apply.printed_numbers(printed)


def _document(opinion: Opinion) -> dict:
    """Read the stamped ensemble document, the text the curator saw.

    :param opinion: The row.
    :returns: The document.
    :raises ApprovalRefused: When it is missing, not readable, or older
        than :data:`MIN_DOCUMENT_SCHEMA`.
    """
    try:
        document = ensemble.read_document(opinion)
    except ensemble.TransientFault as exc:
        raise ApprovalRefused(BUCKET, str(exc)) from exc
    except ensemble.EnsembleError as exc:
        raise ApprovalRefused(NOT_WRITTEN, str(exc)) from exc
    if (document.get("schema_version") or 0) < MIN_DOCUMENT_SCHEMA:
        raise ApprovalRefused(
            OLD_DOCUMENT,
            f"the ensemble document is schema "
            f"{document.get('schema_version')}",
        )
    return document


def _put(key: str, text: dict) -> bool:
    """Write the approved text once, and say whether this call wrote it.

    A key that holds an object already holds the same text: the same
    revisions and the same rule give the same flow. That object is
    kept, so nothing overwrites an approved text.

    :param key: :func:`approved_key`.
    :param text: The approved object.
    :returns: Whether this call uploaded the object.
    :raises ApprovalRefused: When the bucket did not answer.
    """
    try:
        if s3_sync.object_exists(key):
            return False
    except Exception as exc:
        raise ApprovalRefused(BUCKET, f"the HEAD of {key} failed: {exc}")
    if not s3_sync.upload_json_object(key, text):
        raise ApprovalRefused(BUCKET, f"the upload to {key} failed")
    return True


def _drop_unnamed(opinion: Opinion, key: str) -> None:
    """Delete an object a lost swap wrote, unless the row names it."""
    named = (
        Opinion.objects.filter(pk=opinion.pk)
        .values_list("approved_text_key", flat=True)
        .first()
    )
    if named != key:
        s3_sync.delete_objects([key])


def check_gate(
    opinion: Opinion, glue_revision=None, edit_revision=None
) -> None:
    """Refuse an approval of a row the curator cannot approve now.

    :param opinion: The row, as read.
    :param glue_revision: The glue revision of the page the curator
        saw; None skips the check.
    :param edit_revision: The edit revision of that page; None skips it.
    :raises ApprovalRefused: With the first reason that holds.
    """
    if opinion.status != OpinionReviewStatus.READY_FOR_TEXT_REVIEW:
        raise ApprovalRefused(CLOSED)
    if not ensemble.is_written(opinion):
        raise ApprovalRefused(NOT_WRITTEN)
    if opinion.edit_revision != opinion.ensemble_edit_revision:
        # An edit the text does not hold yet: its build failed, so the
        # text is not what the curator decided.
        raise ApprovalRefused(NOT_BUILT)
    if (
        glue_revision is not None and glue_revision != opinion.glue_revision
    ) or (
        edit_revision is not None
        and edit_revision != opinion.ensemble_edit_revision
    ):
        raise ApprovalRefused(STALE_PAGE)
    count = blocking_findings(opinion).count()
    if count:
        raise ApprovalRefused(BLOCKED, count=count)


def approve_text(
    opinion: Opinion, user, glue_revision=None, edit_revision=None
) -> str:
    """Approve the text of one opinion, and write the approved text.

    :param opinion: The row, with ``scan`` and ``apply_run``.
    :param user: The curator.
    :param glue_revision: The glue revision of the page the curator saw.
    :param edit_revision: The edit revision of that page.
    :returns: The key of the approved text.
    :rtype: str
    :raises ApprovalRefused: When the gate refuses, when an input does
        not load, or when the row moved during the write.
    """
    check_gate(opinion, glue_revision, edit_revision)
    document = _document(opinion)
    printed = _printed(opinion)
    now = timezone.now()
    text = paragraphs.approved_document(
        document,
        printed,
        getattr(user, "username", "") or "",
        now.isoformat(),
    )
    key = approved_key(opinion, opinion.ensemble_edit_revision)
    wrote = _put(key, text)

    try:
        with transaction.atomic():
            Opinion.objects.select_for_update().filter(pk=opinion.pk).first()
            count = blocking_findings(opinion).count()
            if count:
                raise ApprovalRefused(BLOCKED, count=count)
            moved = Opinion.objects.filter(
                pk=opinion.pk,
                status=OpinionReviewStatus.READY_FOR_TEXT_REVIEW,
                glue_revision=opinion.glue_revision,
                edit_revision=opinion.edit_revision,
                ensemble_edit_revision=opinion.ensemble_edit_revision,
                ensemble_revision=opinion.ensemble_revision,
            ).update(
                status=OpinionReviewStatus.TEXT_REVIEW_DONE,
                approved_at=now,
                approved_by=user if getattr(user, "pk", None) else None,
                approved_text_key=key,
            )
            if not moved:
                raise ApprovalRefused(MOVED)
    except ApprovalRefused:
        if wrote:
            _drop_unnamed(opinion, key)
        raise
    logger.info(
        "%s of scan %s: %s approved the text, written at %s",
        opinion,
        opinion.scan_id,
        user,
        key,
    )
    return key


def reopen_text(opinion: Opinion, user) -> bool:
    """Take an approved opinion back to the text review.

    A compare-and-swap ``TEXT_REVIEW_DONE`` to ``READY_FOR_TEXT_REVIEW``
    that clears who approved and when. ``approved_text_key`` stays until
    the next approval, so a tagger run of that text stays valid for it.
    The gate of the staff lives in the view.

    :param opinion: The row.
    :param user: The staff reader, for the log.
    :returns: Whether this call moved the row.
    :rtype: bool
    """
    moved = Opinion.objects.filter(
        pk=opinion.pk, status=OpinionReviewStatus.TEXT_REVIEW_DONE
    ).update(
        status=OpinionReviewStatus.READY_FOR_TEXT_REVIEW,
        approved_at=None,
        approved_by=None,
    )
    if moved:
        logger.info(
            "%s of scan %s: %s reopened the text review",
            opinion,
            opinion.scan_id,
            user,
        )
    return bool(moved)


def rewrite_text(opinion: Opinion) -> str | None:
    """Write the approved text of one opinion again, under this rule.

    The approval stands: the curator approved the groups of the stamped
    ensemble document, and a new :data:`paragraphs.JOIN_RULE` only
    joins them otherwise. That document stays in the bucket, because an
    approved row is never built again. The new object keeps who
    approved and when, and the row moves to it by a compare-and-swap
    over the key it held.

    :param opinion: An approved row, with ``scan`` and ``apply_run``.
    :returns: The new key, or None when the row holds it already.
    :raises ApprovalRefused: When the row is not approved, when an input
        does not load, or when the row moved during the write.
    """
    if opinion.status != OpinionReviewStatus.TEXT_REVIEW_DONE:
        raise ApprovalRefused(CLOSED)
    key = approved_key(opinion, opinion.ensemble_edit_revision)
    if key == opinion.approved_text_key:
        return None
    document = _document(opinion)
    printed = _printed(opinion)
    text = paragraphs.approved_document(
        document,
        printed,
        opinion.approved_by.username if opinion.approved_by else "",
        opinion.approved_at.isoformat() if opinion.approved_at else "",
    )
    wrote = _put(key, text)
    moved = Opinion.objects.filter(
        pk=opinion.pk,
        status=OpinionReviewStatus.TEXT_REVIEW_DONE,
        approved_text_key=opinion.approved_text_key,
    ).update(approved_text_key=key)
    if not moved:
        if wrote:
            _drop_unnamed(opinion, key)
        raise ApprovalRefused(MOVED)
    logger.info(
        "%s of scan %s: the approved text was written again at %s "
        "(join rule %s)",
        opinion,
        opinion.scan_id,
        key,
        paragraphs.JOIN_RULE,
    )
    return key
