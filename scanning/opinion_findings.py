"""The curator's answers to the findings of review 3 (#334, #419).

A finding row is written again at every rebuild of the ensemble, so a
dismissal is its own row (``OpinionFindingDismissal``) that names its
target by address: the opinion, the page and the check. The rebuild
lands a standing dismissal on the new card (``ensemble.rebuild_findings``),
and these two writers set and clear the FK of the card that stands now,
so the page shows the answer with no rebuild.

Nothing deletes a dismissal. A curator takes one back with
``withdrawn_at``. A card of ``STALE_OPINION_CHECKS`` is a fact about the
row, not a judgement, and an ``UNRESOLVED_EDIT`` card is a decision of
a person the text does not hold (#376), so neither takes a dismissal
(``UNDISMISSABLE_OPINION_CHECKS``).

The gate of the status lives in the view, the rule of every refused
write: this module answers for the rows alone.

``Opinion.open_findings`` is the count of the cards with no dismissal,
stored on the row so the opinions list sorts by it off an index.
:func:`stamp_open` is its one writer, and every writer of a finding row
calls it after its write: the two here, ``ensemble.rebuild_findings``
and ``opinions.create_rows``. A test pins that set of modules.
"""

from django.db import transaction
from django.db.models import Count, F, OuterRef, Subquery
from django.db.models.functions import Coalesce
from django.utils import timezone

from scanning.models import (
    UNDISMISSABLE_OPINION_CHECKS,
    Opinion,
    OpinionFinding,
    OpinionFindingDismissal,
)


def stamp_open(opinions_qs) -> int:
    """Write ``Opinion.open_findings`` again, from the finding rows.

    A count of the rows as they stand and never a step up or down, so a
    second call writes nothing and a missed one is repaired by the next
    (or by the ``stamp_open_findings`` command). Only a row whose count
    moved is written: the column is indexed, and the creation calls
    this for a whole volume.

    Two writers of one opinion's findings at once can each count
    before the other commits. The ensemble and the dismissal hold the
    opinion's row lock, so they take turns; the creation does not, and
    its stamp can be one write behind until the next one.

    :param opinions_qs: The opinions to count, a queryset of
        ``Opinion``.
    :returns: How many rows were written.
    :rtype: int
    """
    counted = (
        OpinionFinding.objects.filter(
            opinion=OuterRef("pk"), dismissal__isnull=True
        )
        .order_by()
        .values("opinion")
        .annotate(n=Count("pk"))
        .values("n")
    )
    return (
        opinions_qs.order_by()
        .annotate(open_now=Coalesce(Subquery(counted), 0))
        .exclude(open_findings=F("open_now"))
        .update(open_findings=F("open_now"))
    )


class UndismissableOpinionFinding(ValueError):
    """The finding is a fact about the row and takes no dismissal."""


def dismiss(
    opinion: Opinion, finding: OpinionFinding, user
) -> OpinionFindingDismissal:
    """Dismiss a finding: a row at its address, and the FK set at once.

    A second dismissal of the same address answers the standing row and
    writes nothing. The opinion row is locked for the check and the
    write, because the standing key is unique and two tabs press the
    button at once.

    :param opinion: The opinion of the finding.
    :param finding: The finding row.
    :param user: The curator. May be None.
    :returns: The standing dismissal.
    :raises UndismissableOpinionFinding: for a stale finding.
    """
    if finding.check_name in UNDISMISSABLE_OPINION_CHECKS:
        raise UndismissableOpinionFinding(finding.check_name)
    with transaction.atomic():
        Opinion.objects.select_for_update().filter(pk=opinion.pk).first()
        row = OpinionFindingDismissal.objects.filter(
            opinion=opinion,
            page_in_opinion=finding.page_in_opinion,
            check_name=finding.check_name,
            withdrawn_at__isnull=True,
        ).first()
        if row is None:
            row = OpinionFindingDismissal.objects.create(
                opinion=opinion,
                page_in_opinion=finding.page_in_opinion,
                check_name=finding.check_name,
                dismissed_by=user,
            )
        # By the address and not by the pk the view read: a rebuild of
        # the ensemble holds the same lock, deletes the cards and writes
        # them again, and a pk read before the lock can name a card
        # that is gone (#419).
        OpinionFinding.objects.filter(
            opinion=opinion,
            page_in_opinion=finding.page_in_opinion,
            check_name=finding.check_name,
        ).update(dismissal=row)
        stamp_open(Opinion.objects.filter(pk=opinion.pk))
    return row


def restore(opinion: Opinion, finding: OpinionFinding) -> bool:
    """Take back the dismissal of a finding, and clear the FK.

    :param opinion: The opinion of the finding.
    :param finding: The finding row.
    :returns: Whether a dismissal stood.
    """
    if finding.dismissal_id is None:
        return False
    now = timezone.now()
    with transaction.atomic():
        # The lock of :func:`dismiss`, so the two stamps take turns.
        Opinion.objects.select_for_update().filter(pk=opinion.pk).first()
        stamped = OpinionFindingDismissal.objects.filter(
            pk=finding.dismissal_id,
            opinion=opinion,
            withdrawn_at__isnull=True,
        ).update(withdrawn_at=now, date_modified=now)
        OpinionFinding.objects.filter(
            dismissal_id=finding.dismissal_id
        ).update(dismissal=None)
        stamp_open(Opinion.objects.filter(pk=opinion.pk))
    return bool(stamped)
