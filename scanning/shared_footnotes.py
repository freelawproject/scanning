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

The gate of the status lives in the views, the rule of every refused
write: this module answers for the rows alone.
"""

from django.db import transaction
from django.db.models import F
from django.utils import timezone

from scanning.models import (
    KeptFootnotes,
    Opinion,
    OpinionReviewStatus,
    Scan,
)


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


def _reglue(opinion: Opinion) -> None:
    """Raise the revision, so every glue is written from the new masks."""
    Opinion.objects.filter(pk=opinion.pk).exclude(
        status=OpinionReviewStatus.TEXT_REVIEW_DONE
    ).update(glue_revision=F("glue_revision") + 1, ocr_glue_attempts=0)


def keep(opinion: Opinion, user) -> bool:
    """Keep the first-page footnotes as the opinion's own.

    A standing row of another address (the opinion moved) is withdrawn
    first: it applies to nothing, and one row stands per opinion.

    :param opinion: The opinion.
    :param user: Who decided.
    :returns: False when a standing row applies already.
    :rtype: bool
    """
    with transaction.atomic():
        locked = Opinion.objects.select_for_update().get(pk=opinion.pk)
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


def give_back(opinion: Opinion, user) -> bool:
    """Withdraw the kept row: the mask takes the footnotes again.

    :param opinion: The opinion.
    :param user: Who decided.
    :returns: False when no row stands.
    :rtype: bool
    """
    with transaction.atomic():
        locked = Opinion.objects.select_for_update().get(pk=opinion.pk)
        row = standing(locked)
        if row is None:
            return False
        row.withdrawn_at = timezone.now()
        row.withdrawn_by = user
        row.save(update_fields=["withdrawn_at", "withdrawn_by"])
        if applies(row, locked):
            _reglue(locked)
    return True
