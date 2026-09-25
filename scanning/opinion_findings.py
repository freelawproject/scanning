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
"""

from django.db import transaction
from django.utils import timezone

from scanning.models import (
    UNDISMISSABLE_OPINION_CHECKS,
    Opinion,
    OpinionFinding,
    OpinionFindingDismissal,
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
        stamped = OpinionFindingDismissal.objects.filter(
            pk=finding.dismissal_id,
            opinion=opinion,
            withdrawn_at__isnull=True,
        ).update(withdrawn_at=now, date_modified=now)
        OpinionFinding.objects.filter(
            dismissal_id=finding.dismissal_id
        ).update(dismissal=None)
    return bool(stamped)
