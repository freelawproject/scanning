"""The blocking cards of the text review as rows (``OpinionCard``).

Three steps, each its own writer:

1. :func:`sync` writes the rows of one opinion from its ensemble
   document, inside the build's transaction, beside the findings. The
   cards are ``blocking_review.group_cards``, the ones the blocking
   review shows, each with its reason (``blocking_types``).
2. :func:`answer` stores a volunteer's answer on a row, and nothing
   else: no edit, no build.
3. :func:`apply` writes the stored answers of one opinion as the review
   page's own text edits and builds the text again, then dismisses a
   page's finding whose every open card was kept as shown.

The text has one writer, ``OpinionEdit`` through
``opinion_edits.supersede``, so an applied answer has the Undo, the
rerun and the ``UNRESOLVED_EDIT`` card of an edit made on the page.
"""

from __future__ import annotations

import json
import logging
from collections import Counter, defaultdict

from django.db import transaction
from django.utils import timezone

from scanning import blocking_review, blocking_types, ensemble, opinion_edits
from scanning.models import (
    Opinion,
    OpinionCard,
    OpinionCheck,
    OpinionEdit,
    OpinionFinding,
    OpinionReviewStatus,
)

logger = logging.getLogger(__name__)

#: The checks whose cards are rows: the words and blocks of the vote.
CARD_CHECKS = (OpinionCheck.NO_MAJORITY, OpinionCheck.SINGLE_ENGINE)

#: A row that waits on a person: no answer, or one that went stale.
WAITING = (OpinionCard.Status.OPEN, OpinionCard.Status.STALE)

#: A row the next build still knows: everything but an applied answer.
LIVE = (
    OpinionCard.Status.OPEN,
    OpinionCard.Status.ANSWERED,
    OpinionCard.Status.STALE,
)

#: How many rounds of edits and builds one opinion's apply runs. A
#: block whose open words are answered in part takes one word a round,
#: and a block holds at most ``blocking_review.MANY_OPEN_WORDS``.
MAX_ROUNDS = blocking_review.MANY_OPEN_WORDS + 2


def key_of(card: dict) -> str:
    """Return the address of one card: the key a rerun matches it by.

    The page, the block's box to the point, the kind, and for a word
    its base position, its place in a run and whether it is a gap. A
    group id is a place on the page and moves with a drop, so it is no
    part of the key.
    """
    box = ",".join(str(round(v)) for v in card["box_pt"])
    return ":".join(
        str(part)
        for part in (
            card["page_in_opinion"],
            box,
            card["kind"],
            card.get("position"),
            card.get("run_index"),
            int(bool(card.get("gap"))),
        )
    )


def _readings_of(card: dict) -> str:
    """Return what the engines read at a card, the test of a stale answer."""
    return json.dumps(
        card.get("readings") or card.get("text"), sort_keys=True
    )


def _open_pages(opinion: Opinion) -> dict[tuple[int, str], int]:
    """Return the open findings of the card checks, by page and check."""
    return {
        (page, check): pk
        for pk, page, check in OpinionFinding.objects.filter(
            opinion=opinion,
            check_name__in=CARD_CHECKS,
            dismissal__isnull=True,
        ).values_list("pk", "page_in_opinion", "check_name")
    }


def _check_of(card: dict) -> str:
    if card["kind"] == blocking_review.SINGLE:
        return OpinionCheck.SINGLE_ENGINE
    return OpinionCheck.NO_MAJORITY


def sync(opinion: Opinion, document: dict) -> Counter:
    """Write the card rows of one opinion from its document.

    Called by ``ensemble.write`` after the findings, in its transaction.
    A card of a page whose finding is open is a row, matched by
    :func:`key_of`: a new card is an ``OPEN`` row; a matched row takes
    the card as it is now, and an answer whose readings moved is
    ``STALE``; a row no card matches goes, unless its answer was
    applied, which stays as the record of what was written.

    :param opinion: The row.
    :param document: The document the build is stamping.
    :returns: Counts of what was written.
    :rtype: Counter
    """
    counts: Counter = Counter()
    open_pages = _open_pages(opinion)
    schema = document.get("schema_version")
    fresh: dict[str, dict] = {}
    for page in document.get("pages") or []:
        if page.get("error"):
            continue
        number = page["page_in_opinion"]
        for group in page.get("groups") or []:
            for card in blocking_review.group_cards(
                page, number, group, schema
            ):
                if (number, _check_of(card)) not in open_pages:
                    continue
                fresh[key_of(card)] = card
    # Locked to the end of the build's transaction: an answer stored
    # between this read and the write below would be written back as
    # the row was. ``answer`` takes the same lock, so it waits.
    rows = {
        row.key: row
        for row in OpinionCard.objects.select_for_update().filter(
            opinion=opinion
        )
    }
    gone = [
        row.pk
        for key, row in rows.items()
        if key not in fresh and row.status != OpinionCard.Status.APPLIED
    ]
    if gone:
        OpinionCard.objects.filter(pk__in=gone).delete()
        counts["deleted"] = len(gone)
    created, updated = [], []
    for key, card in fresh.items():
        row = rows.get(key)
        if row is None:
            created.append(
                OpinionCard(
                    opinion=opinion,
                    page_in_opinion=card["page_in_opinion"],
                    key=key,
                    check_name=_check_of(card),
                    kind=card["kind"],
                    reason=card["type"],
                    card=card,
                )
            )
            continue
        if row.status == OpinionCard.Status.ANSWERED and _readings_of(
            row.card
        ) != _readings_of(card):
            row.status = OpinionCard.Status.STALE
            row.note = "The engines read this place again; check the answer."
        elif row.status == OpinionCard.Status.APPLIED:
            # The answer was written and the place is open still: the
            # edit was taken back, or it judged another word.
            row.status = OpinionCard.Status.OPEN
            row.answer = None
            row.edit = None
            row.applied_at = None
            row.note = "The written answer did not settle this place."
        row.card = card
        row.reason = card["type"]
        row.check_name = _check_of(card)
        updated.append(row)
    if created:
        OpinionCard.objects.bulk_create(created)
        counts["created"] = len(created)
    if updated:
        OpinionCard.objects.bulk_update(
            updated,
            [
                "card",
                "reason",
                "check_name",
                "status",
                "answer",
                "edit",
                "applied_at",
                "note",
            ],
        )
        counts["kept"] = len(updated)
    return counts


# ---------------------------------------------------------------------------
# The answer
# ---------------------------------------------------------------------------


class CardClosed(Exception):
    """The card was applied or is gone: no answer goes on it."""


def answer(row: OpinionCard, user, value: str | None) -> OpinionCard:
    """Store an answer on one row, or take it back with ``None``.

    The value is what the card's place should read: a word card's
    words, a gap's words ("" for nothing), a block's whole text. The
    text it makes is :func:`compose`, run by the apply against the text
    of that day. An applied row takes no answer: its Undo is the edit's.

    :raises CardClosed: for an applied row.
    """
    with transaction.atomic():
        row = OpinionCard.objects.select_for_update().get(pk=row.pk)
        if row.status == OpinionCard.Status.APPLIED:
            raise CardClosed(row.pk)
        if value is None:
            row.status = OpinionCard.Status.OPEN
            row.answer = None
            row.answered_by = None
            row.answered_at = None
        else:
            row.status = OpinionCard.Status.ANSWERED
            row.answer = value
            row.answered_by = user
            row.answered_at = timezone.now()
        row.note = ""
        row.save(
            update_fields=[
                "status",
                "answer",
                "answered_by",
                "answered_at",
                "note",
                "date_modified",
            ]
        )
    return row


def compose(card: dict, value: str, text: str | None = None) -> str:
    """Return the block text one answer makes, the review page's rule.

    A word goes in place of the card's span, a gap's words go into the
    gap with the text around it kept, and a block card's value is the
    whole text. The twin of ``choose`` in ``blocking_review.js``.

    :param card: The card.
    :param value: The answer.
    :param text: The block text to answer in, the card's own by default.
    :returns: The folded text.
    """
    text = card["text"] if text is None else text
    if card["kind"] != blocking_review.WORD:
        return opinion_edits.fold(value)
    start = card["start"]
    if card.get("gap"):
        return opinion_edits.fold(f"{text[:start]} {value} {text[start:]}")
    return opinion_edits.fold(
        text[:start] + value + text[start + card["length"] :]
    )


def is_keep(row: OpinionCard) -> bool:
    """Return whether an answer leaves the text as the card shows it."""
    return compose(row.card, row.answer or "") == opinion_edits.fold(
        row.card["text"]
    )


def _all_answers(group_text: str, rows: list[OpinionCard]) -> str:
    """Return the block text every answer of one block makes together.

    Right to left, so an answer never moves the offsets of the ones
    before it; each card's span is in the same text, the group's.
    """
    text = group_text
    for row in sorted(rows, key=lambda r: r.card["start"], reverse=True):
        card = row.card
        start = card["start"]
        if card.get("gap"):
            text = f"{text[:start]} {row.answer} {text[start:]}"
        else:
            text = text[:start] + row.answer + text[start + card["length"] :]
    return opinion_edits.fold(text)


# ---------------------------------------------------------------------------
# The apply
# ---------------------------------------------------------------------------


def _stale(rows: list[OpinionCard], note: str, dry_run: bool) -> None:
    if dry_run:
        return
    OpinionCard.objects.filter(pk__in=[r.pk for r in rows]).update(
        status=OpinionCard.Status.STALE, note=note
    )


def _edit_for(
    rows: list[OpinionCard], block: list[OpinionCard], group: dict
) -> tuple[str, list | None, list[OpinionCard]] | None:
    """Return ``(text, span, rows it answers)`` of one block, or None.

    Every open card of the block answered: one edit of the whole block
    with no span, the review page's block judgement, which settles the
    kept words with the changed ones. Otherwise the first changed word
    alone, with its span, as the word card of the blocking review sends
    it; the build keeps the block's other open words open, and the next
    round takes the next answer. Kept words alone write no edit: a page
    of them is a dismissal.
    """
    changed = [r for r in rows if not is_keep(r)]
    if not changed:
        return None
    word = rows[0].kind == blocking_review.WORD
    if not word:
        row = changed[0]
        return compose(row.card, row.answer), None, [row]
    if len(rows) == len(block):
        return _all_answers(group["text"], rows), None, rows
    row = changed[0]
    start = row.card["start"]
    end = start if row.card.get("gap") else start + row.card["length"]
    return compose(row.card, row.answer), [start, end], [row]


def _write_edit(
    opinion: Opinion, page: dict, group: dict, text: str, span, user
) -> OpinionEdit:
    """Write one text edit as ``edit_opinion_text`` writes it."""
    address = ensemble._address(page)
    human = group.get("human") or {}
    return opinion_edits.supersede(
        opinion,
        user,
        kind=OpinionEdit.Kind.TEXT,
        source_edit_id=address[0],
        source_page=address[1],
        page_in_opinion=page["page_in_opinion"],
        box_pt=group["box_pt"],
        section=group.get("section") or ensemble.BODY,
        base_text=human.get("base_text") or group["text"],
        text=text,
        span=span,
        glue_revision=opinion.glue_revision,
    )


def _refusal(group: dict | None, text: str) -> str:
    """Return why the review page would refuse this edit, or ``""``."""
    from scanning import markup

    if group is None:
        return "Its block is no longer in the text."
    if group.get("level") is None:
        return "The engines now read its block alike."
    if group.get("kind") in (markup.TABLE, markup.FIGURE):
        return "A table or a picture takes no text edit."
    if not text:
        return "The answer leaves the block empty."
    if len(text) > opinion_edits.MAX_TEXT_CHARS:
        return "The answer is longer than a block may be."
    return ""


def _dismiss_kept(opinion: Opinion, dry_run: bool, counts: Counter) -> None:
    """Dismiss each page finding whose every live card was kept as shown."""
    from scanning import opinion_findings

    live = defaultdict(list)
    for row in OpinionCard.objects.filter(opinion=opinion, status__in=LIVE):
        live[(row.page_in_opinion, row.check_name)].append(row)
    findings = {
        (f.page_in_opinion, f.check_name): f
        for f in OpinionFinding.objects.filter(
            opinion=opinion, check_name__in=CARD_CHECKS, dismissal__isnull=True
        )
    }
    for place, rows in live.items():
        finding = findings.get(place)
        if finding is None:
            continue
        if not all(
            r.status == OpinionCard.Status.ANSWERED and is_keep(r) for r in rows
        ):
            continue
        counts["pages_dismissed"] += 1
        if dry_run:
            continue
        opinion_findings.dismiss(opinion, finding, rows[-1].answered_by)
        OpinionCard.objects.filter(pk__in=[r.pk for r in rows]).update(
            status=OpinionCard.Status.APPLIED,
            applied_at=timezone.now(),
            note="Kept as shown: the page's card was dismissed.",
        )


def apply(opinion: Opinion, dry_run: bool = False) -> Counter:
    """Write the stored answers of one opinion, and build its text.

    One round writes at most one edit per block, then builds the text
    once (``ensemble.rerun``), which writes the rows again through
    :func:`sync`; the next round reads the new document. When a round
    writes nothing, the kept pages are dismissed. A row whose block the
    review page would refuse is ``STALE`` with the reason.

    :param opinion: The row.
    :param dry_run: Count what would be written and write nothing.
    :returns: Counts: ``edits``, ``answers``, ``pages_dismissed``,
        ``stale``, ``rounds``, and ``skipped`` with the reason.
    :rtype: Counter
    """
    counts: Counter = Counter()
    for _round in range(MAX_ROUNDS):
        opinion.refresh_from_db()
        if opinion.status != OpinionReviewStatus.READY_FOR_TEXT_REVIEW:
            counts["skipped: not in text review"] += 1
            return counts
        if not ensemble.is_written(opinion):
            counts["skipped: text not written"] += 1
            return counts
        if opinion.edit_revision != opinion.ensemble_edit_revision:
            counts["skipped: an edit is not built yet"] += 1
            return counts
        answered = list(
            OpinionCard.objects.filter(
                opinion=opinion, status=OpinionCard.Status.ANSWERED
            )
        )
        if not answered:
            break
        counts["rounds"] += 1
        document = ensemble.read_document(opinion)
        pages = {p["page_in_opinion"]: p for p in document.get("pages") or []}
        blocks = defaultdict(list)
        for row in OpinionCard.objects.filter(opinion=opinion, status__in=LIVE):
            blocks[(row.page_in_opinion, row.card["group_id"])].append(row)
        by_block = defaultdict(list)
        for row in answered:
            by_block[(row.page_in_opinion, row.card["group_id"])].append(row)
        wrote = 0
        for (number, group_id), rows in by_block.items():
            page = pages.get(number)
            group = next(
                (
                    g
                    for g in (page or {}).get("groups") or []
                    if g["id"] == group_id
                ),
                None,
            )
            if group is None:
                counts["stale"] += len(rows)
                _stale(rows, _refusal(None, ""), dry_run)
                continue
            planned = _edit_for(rows, blocks[(number, group_id)], group)
            if planned is None:
                continue
            text, span, settled = planned
            why = _refusal(group, text)
            if not why and text == group["text"]:
                continue
            if why:
                counts["stale"] += len(settled)
                _stale(settled, why, dry_run)
                continue
            counts["edits"] += 1
            counts["answers"] += len(settled)
            if dry_run:
                continue
            edit = _write_edit(
                opinion, page, group, text, span, settled[0].answered_by
            )
            OpinionCard.objects.filter(pk__in=[r.pk for r in settled]).update(
                status=OpinionCard.Status.APPLIED,
                edit=edit,
                applied_at=timezone.now(),
                note="",
            )
            wrote += 1
        if dry_run or not wrote:
            break
        ensemble.rerun(opinion)
    _dismiss_kept(opinion, dry_run, counts)
    return counts


def reasons() -> list[tuple[str, str]]:
    """Return the reasons a card can carry, hardest first, with labels."""
    return list(blocking_types.TYPES)
