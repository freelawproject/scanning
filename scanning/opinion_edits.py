"""The human edits of the text of review 3 (#376).

A curator changes four things of an opinion's text: the text of one
block, the section of one block (the body or the footnotes), the order
of the blocks of one section of one page, and the blockquote of one
block, whole or over one span of its text (#419). Each change is one
``OpinionEdit`` row, and the ensemble applies the standing rows at
every build (``ensemble.build_page``), so the text, the document and
the cards hold them.

**The address plus a copy of the box.** The id of a group is its place
on the page, and a build can give the same block another one. So a row
names its page by the durable address of the page and its block by a
copy of the block's box, and ``ensemble.land_edits`` lands it on the
group of each build.

**One standing row per target.** A box copy takes no unique key, so
:func:`supersede` finds the standing row of the same target by the
same land rule, withdraws it and writes the new row with ``replaces``,
under a lock on the opinion. Nothing deletes a row. :func:`withdraw`
is the Undo: the block goes back to the engines' text, and an older
edit this one superseded does not come back, the rule of review 2.

Every write raises ``Opinion.edit_revision`` in its own transaction,
and the ensemble swaps on it (``ensemble.write``).

The gate of the status and the refusals live in the views, the rule of
every refused write: this module answers for the rows alone.
"""

import re

from django.db import transaction
from django.db.models import F
from django.utils import timezone

from scanning import ensemble
from scanning.models import Opinion, OpinionEdit

#: The longest text of one block a curator may write. A block is one
#: paragraph, and the longest the engines read are some thousand
#: characters.
MAX_TEXT_CHARS = 20000

_SPACE_RUN = re.compile(r"\s+")


def fold(text: str) -> str:
    """Fold a curator's text to the whitespace rule of an engine's.

    One space between two words, a ``\\n`` where the run held a line
    break, and no whitespace at the edges: the rule of
    ``markup.Parsed.text``, or ``ensemble.plain`` moves every offset.

    :param text: What the curator typed.
    :returns: The folded text.
    :rtype: str
    """
    return _SPACE_RUN.sub(
        lambda match: "\n" if "\n" in match.group() else " ",
        text.replace("\r\n", "\n").replace("\r", "\n").strip(),
    )


def snap_span(text: str, start: int, end: int) -> tuple[int, int] | None:
    """Move a selection of a block's text to the edges of its words.

    A selection by hand starts and ends inside a word or on a space;
    the quote takes whole words (#419). ``start`` goes back to the
    start of its word and ``end`` forward to the end of its word, and
    the whitespace at the two edges is left out.

    :param text: The text of the block, as the page shows it.
    :param start: The first selected character.
    :param end: The character after the last selected one.
    :returns: ``(start, end)``, or None when no word is selected.
    :rtype: tuple[int, int] | None
    """
    start = max(0, min(start, len(text)))
    end = max(start, min(end, len(text)))
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    if start >= end:
        return None
    while start > 0 and not text[start - 1].isspace():
        start -= 1
    while end < len(text) and not text[end].isspace():
        end += 1
    return start, end


def _standing(opinion: Opinion, kind: str, address: tuple):
    """Return the standing rows of one kind at one page address."""
    source_edit_id, source_page = address
    return list(
        OpinionEdit.objects.filter(
            opinion=opinion,
            kind=kind,
            source_edit_id=source_edit_id,
            source_page=source_page,
            withdrawn_at__isnull=True,
        ).order_by("pk")
    )


def _target_of(
    opinion: Opinion, kind: str, address: tuple, box, section: str
) -> OpinionEdit | None:
    """Return the standing row the new row replaces, or None.

    A block edit replaces the row of the same kind that lands on the
    same box. An order edit replaces the row of the same section.
    """
    rows = _standing(opinion, kind, address)
    if kind == OpinionEdit.Kind.ORDER:
        return next((row for row in rows if row.section == section), None)
    landed = ensemble.land_edits(
        [box], [{"box_pt": row.box_pt} for row in rows]
    )
    return rows[landed[0]] if 0 in landed else None


def supersede(opinion: Opinion, user, **fields) -> OpinionEdit:
    """Write one edit, and withdraw the standing row of its target.

    :param opinion: The opinion.
    :param user: The curator.
    :param fields: The fields of the row: ``kind``, ``source_edit_id``,
        ``source_page``, ``page_in_opinion``, ``glue_revision``, and
        the ones of its kind.
    :returns: The new row.
    :rtype: OpinionEdit
    """
    kind = fields["kind"]
    address = (fields.get("source_edit_id"), fields.get("source_page"))
    now = timezone.now()
    with transaction.atomic():
        Opinion.objects.select_for_update().filter(pk=opinion.pk).first()
        old = _target_of(
            opinion,
            kind,
            address,
            fields.get("box_pt"),
            fields.get("section", ""),
        )
        if old is not None:
            OpinionEdit.objects.filter(pk=old.pk).update(
                withdrawn_at=now, withdrawn_by=user, date_modified=now
            )
        row = OpinionEdit.objects.create(
            opinion=opinion, created_by=user, replaces=old, **fields
        )
        Opinion.objects.filter(pk=opinion.pk).update(
            edit_revision=F("edit_revision") + 1
        )
    return row


def withdraw(opinion: Opinion, edit: OpinionEdit, user) -> bool:
    """Take back one standing edit: the Undo.

    :param opinion: The opinion of the edit.
    :param edit: The row.
    :param user: The curator.
    :returns: Whether the row stood.
    :rtype: bool
    """
    now = timezone.now()
    with transaction.atomic():
        Opinion.objects.select_for_update().filter(pk=opinion.pk).first()
        stamped = OpinionEdit.objects.filter(
            pk=edit.pk, opinion=opinion, withdrawn_at__isnull=True
        ).update(withdrawn_at=now, withdrawn_by=user, date_modified=now)
        if stamped:
            Opinion.objects.filter(pk=opinion.pk).update(
                edit_revision=F("edit_revision") + 1
            )
    return bool(stamped)


def swapped_order(groups: list[dict], group_id: int, step: int) -> list | None:
    """Return the boxes of one section with one block moved one place.

    :param groups: The groups of one section of one page, in the order
        of the document.
    :param group_id: The id of the block that moves.
    :param step: -1 for up, 1 for down.
    :returns: The box copies in the new order, or None when the block
        is at the edge of its section or is not in it.
    :rtype: list | None
    """
    ids = [group["id"] for group in groups]
    if group_id not in ids:
        return None
    here = ids.index(group_id)
    there = here + step
    if there < 0 or there >= len(groups):
        return None
    boxes = [group["box_pt"] for group in groups]
    boxes[here], boxes[there] = boxes[there], boxes[here]
    return boxes
