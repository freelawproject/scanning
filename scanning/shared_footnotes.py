"""The footnotes of a first page the opinion before ends on (#457).

Two opinions share a page when one ends on it and the next starts on
it. blackletter's masks over the neighbour stop above the footnotes,
so the earlier opinion's notes, printed at the foot of that page, went
into the later opinion's text and its redacted PDF.
``boundaries.outside_rects`` masks them on the later opinion's first
page, and both the OCR glue and the PDF pass read those masks.

A later opinion can print a note of its own on that page (a star note
on its caption, a first note of its text). The ensemble writes a
``SHARED_FOOTNOTES`` card for every page whose notes the mask took, an
ERROR when the opinion's own text there carries a footnote mark, and a
person answers it here: :func:`keep` lifts the mask for the opinion,
:func:`give_back` puts it back. Each write raises the opinion's
``glue_revision``, so the OCR glue, the PDF and the ensemble are
written again from the new masks, the rule of ``opinion_ocr.reglue``.

The views refuse an opinion that is not ready for the text review,
and :func:`keep` and :func:`give_back` read the status again under the
lock (:class:`FootnotesClosed`): an approval that lands between the two
would leave a decision on a text nobody wrote again. Each write also
withdraws a standing dismissal of the card, which named the other
answer.
"""

from django.db import transaction
from django.db.models import F
from django.utils import timezone

from scanning.models import (
    KeptFootnotes,
    Opinion,
    OpinionCheck,
    OpinionFinding,
    OpinionFindingDismissal,
    OpinionReviewStatus,
    Scan,
)


class FootnotesClosed(Exception):
    """The opinion is not ready for the text review under the lock."""


#: What :func:`give_back` did. A row of another first page lifted no
#: mask, so its withdrawal writes nothing again, and the answer must
#: not say a rewrite is coming.
GAVE_BACK = "gave_back"
WITHDREW_STALE = "withdrew_stale"
NOTHING_STANDING = "nothing_standing"


def applies(row: KeptFootnotes, opinion: Opinion) -> bool:
    """Return whether a kept row names the opinion's first page now.

    :param row: The standing row.
    :param opinion: The opinion, as it is now.
    :returns: Whether the address of the row is the first page.
    :rtype: bool
    """
    return (
        row.source_edit_id == opinion.start_source_edit_id
        and row.source_page == opinion.start_source_page
    )


def standing(opinion: Opinion) -> KeptFootnotes | None:
    """Return the standing row of the opinion, applied or not."""
    return KeptFootnotes.objects.filter(
        opinion=opinion, withdrawn_at__isnull=True
    ).first()


def kept(opinion: Opinion) -> bool:
    """Return whether a person kept the first-page footnotes.

    **The one rule** the OCR glue, the PDF pass and the card read.

    :param opinion: The opinion.
    :returns: Whether a standing row applies.
    :rtype: bool
    """
    row = standing(opinion)
    return row is not None and applies(row, opinion)


def kept_on_shared_page(opinion: Opinion) -> bool:
    """Return whether a kept row lifts a mask the opinion would have.

    :func:`kept`, on an opinion whose first page the opinion before
    still ends on (``boundaries.shared_first_pages``). The card of a
    kept page reads it: a row on a page nobody shares lifts nothing,
    and a card would say what is not so.

    :param opinion: The opinion.
    :returns: Whether the kept row lifts a mask.
    :rtype: bool
    """
    from scanning import boundaries

    boundary = opinion.boundary
    return (
        boundary is not None
        and kept(opinion)
        and boundary.pk
        in boundaries.shared_first_pages(opinion.scan, [boundary])
    )


def kept_boundaries(scan: Scan) -> set[int]:
    """Return the boundary pks whose first-page footnotes are kept.

    For the step-2 viewer, which draws the masks of every boundary: an
    opinion links its boundary, and a kept row of that opinion lifts
    the footnote mask of that boundary, the masks the PDF has.

    :param scan: The scan.
    :returns: The pks.
    :rtype: set[int]
    """
    kept_pks = set()
    for row in KeptFootnotes.objects.filter(
        opinion__scan=scan,
        withdrawn_at__isnull=True,
        opinion__boundary__isnull=False,
    ).select_related("opinion"):
        if applies(row, row.opinion):
            kept_pks.add(row.opinion.boundary_id)
    return kept_pks


def _locked(opinion: Opinion) -> Opinion:
    """Lock the opinion, and refuse one that is not ready for the review.

    :raises FootnotesClosed: When the status moved since the view read it.
    """
    locked = Opinion.objects.select_for_update().get(pk=opinion.pk)
    if locked.status != OpinionReviewStatus.READY_FOR_TEXT_REVIEW:
        raise FootnotesClosed(locked.status)
    return locked


def _reglue(opinion: Opinion) -> None:
    """Raise the revision, and reopen the card's dismissal.

    The revision rises so every glue is written from the new masks. A
    dismissal of the card said "the notes are the opinion before's",
    and this write changes that answer, so it stands no more: the card
    the new build writes is open, and an ERROR one holds the approval
    again.
    """
    Opinion.objects.filter(pk=opinion.pk).update(
        glue_revision=F("glue_revision") + 1, ocr_glue_attempts=0
    )
    now = timezone.now()
    dismissals = OpinionFindingDismissal.objects.filter(
        opinion=opinion,
        check_name=OpinionCheck.SHARED_FOOTNOTES,
        withdrawn_at__isnull=True,
    )
    # The rule of ``opinion_findings.restore``: the card loses its FK
    # too, so the strip counts it open before the next build.
    OpinionFinding.objects.filter(dismissal__in=dismissals).update(
        dismissal=None
    )
    dismissals.update(withdrawn_at=now, date_modified=now)


def keep(opinion: Opinion, user) -> bool:
    """Keep the first-page footnotes as the opinion's own.

    A standing row of another address (the opinion moved) is withdrawn
    first: it applies to nothing, and one row stands per opinion.

    :param opinion: The opinion.
    :param user: Who decided.
    :returns: False when a standing row applies already.
    :rtype: bool
    :raises FootnotesClosed: When the opinion is not ready for the review.
    """
    with transaction.atomic():
        locked = _locked(opinion)
        row = standing(locked)
        if row is not None and applies(row, locked):
            return False
        if row is not None:
            row.withdrawn_at = timezone.now()
            row.withdrawn_by = user
            row.save(update_fields=["withdrawn_at", "withdrawn_by"])
        KeptFootnotes.objects.create(
            opinion=locked,
            source_edit_id=locked.start_source_edit_id,
            source_page=locked.start_source_page,
            created_by=user,
        )
        _reglue(locked)
    return True


def give_back(opinion: Opinion, user) -> str:
    """Withdraw the kept row: the mask takes the footnotes again.

    A row of another first page lifted nothing, so it is withdrawn and
    nothing is written again.

    :param opinion: The opinion.
    :param user: Who decided.
    :returns: :data:`GAVE_BACK` when the glues are written again,
        :data:`WITHDREW_STALE` for a row of another first page, or
        :data:`NOTHING_STANDING`.
    :rtype: str
    :raises FootnotesClosed: When the opinion is not ready for the review.
    """
    with transaction.atomic():
        locked = _locked(opinion)
        row = standing(locked)
        if row is None:
            return NOTHING_STANDING
        row.withdrawn_at = timezone.now()
        row.withdrawn_by = user
        row.save(update_fields=["withdrawn_at", "withdrawn_by"])
        if not applies(row, locked):
            return WITHDREW_STALE
        _reglue(locked)
    return GAVE_BACK
