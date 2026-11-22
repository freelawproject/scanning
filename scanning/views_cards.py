"""The card rows of the text review: walked by reason, answered, kept.

The volunteers' page over ``OpinionCard``: the open cards of a reason
across the corpus (or of a volume), one section per opinion, each
answer stored on its row and nothing else. ``apply_card_answers``
writes the answers into the text later (``opinion_cards.apply``).
"""

from __future__ import annotations

import json
import logging

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Count, Q
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.http import urlencode
from django.views.decorators.http import require_POST

from scanning import blocking_types, opinion_cards, volunteers
from scanning.models import (
    Opinion,
    OpinionCard,
    OpinionReviewStatus,
    Scan,
)

logger = logging.getLogger(__name__)

#: The opinions of one page of the walk.
OPINIONS_PER_PAGE = 25

#: How long the reason counts of the whole corpus are kept, in seconds.
COUNTS_TTL = 120

#: The statuses a filter of the page names.
STATUS_FILTERS = {
    "open": opinion_cards.WAITING,
    "answered": (OpinionCard.Status.ANSWERED,),
    "applied": (OpinionCard.Status.APPLIED,),
    "flagged": (OpinionCard.Status.FLAGGED,),
}

#: Which cards the level filter keeps: the ones a person must answer,
#: or the minor ones the next build makes warnings (``OpinionCard.minor``).
LEVEL_FILTERS = {
    # What holds an approval now: the word and one-engine cards.
    "blocking": Q(check_name__in=opinion_cards.CARD_CHECKS),
    # The minor warnings, and the blocking cards the next build makes
    # minor warnings (``OpinionCard.minor``).
    "minor": Q(minor=True),
}

CARD_GONE_MESSAGE = (
    "This card is no longer open: its answer was written into the text, "
    "or the engines read its place again. Reload the page."
)
ANSWER_BAD_REQUEST_MESSAGE = "The answer could not be read."
ANSWER_SAVED_MESSAGE = (
    "Answer saved. It goes into the text when the answers are applied."
)
ANSWER_CLEARED_MESSAGE = "Answer taken back. The card is open again."


def _whole_number(value) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def _filters(request: HttpRequest) -> dict:
    """Return the page's filters, as the query string names them."""
    reason = request.GET.get("reason", "")
    status = request.GET.get("status", "open")
    scan = _whole_number(request.GET.get("scan"))
    volume = _whole_number(request.GET.get("volume"))
    level = request.GET.get("level", "")
    return {
        "reason": reason if reason in blocking_types.LABELS else "",
        "level": level if level in LEVEL_FILTERS else "",
        "status": status if status in STATUS_FILTERS else "open",
        "scan": str(scan) if scan is not None else "",
        "volume": str(volume) if volume is not None else "",
        # A volunteer sees their own cards and no others
        # (``scanning.volunteers``): the filter is forced, not offered.
        "mine": request.GET.get("mine") == "1"
        or volunteers.of_request(request),
    }


def _query(filters: dict, **changes) -> str:
    values = {**filters, **changes}
    return urlencode(
        {
            key: ("1" if value is True else value)
            for key, value in values.items()
            if value
        }
    )


def _rows(
    filters: dict, user, with_reason: bool = True, with_level: bool = True
):
    """Return the card rows of the filters.

    The open ones are rows of an opinion in the text review whose page
    still waits on its finding: a page a person dismissed on the review
    page keeps its rows until the next build, and they are no work.
    """
    if filters["status"] == "open":
        # The one rule of an open card (``opinion_cards.open_rows``),
        # which the claim and its hold check read too.
        rows = opinion_cards.open_rows()
    else:
        rows = OpinionCard.objects.filter(
            status__in=STATUS_FILTERS[filters["status"]],
            opinion__status=OpinionReviewStatus.READY_FOR_TEXT_REVIEW,
        )
    if filters["scan"]:
        rows = rows.filter(opinion__scan_id=filters["scan"])
    if filters["volume"]:
        rows = rows.filter(opinion__scan__volume=filters["volume"])
    if filters["mine"]:
        rows = rows.filter(assigned_to=user)
    if with_reason and filters["reason"]:
        rows = rows.filter(reason=filters["reason"])
    if with_level and filters["level"]:
        rows = rows.filter(LEVEL_FILTERS[filters["level"]])
    return rows


def _level_counts(filters: dict, user) -> dict:
    """Return the blocking and the minor cards of the filters, the whole
    corpus's kept for :data:`COUNTS_TTL` like the tabs."""
    scoped = filters["scan"] or filters["volume"] or filters["mine"]
    key = "card-levels:" + _query(filters, level="")
    counts = None if scoped else cache.get(key)
    if counts is None:
        by = {
            (check, minor): n
            for check, minor, n in _rows(filters, user, with_level=False)
            .values_list("check_name", "minor")
            .annotate(n=Count("id"))
            .order_by()
        }
        blocking = {
            key: n
            for key, n in by.items()
            if key[0] in opinion_cards.CARD_CHECKS
        }
        counts = {
            "blocking": sum(blocking.values()),
            # Blocking now, a minor warning after the next build.
            "pending": sum(n for (_, minor), n in blocking.items() if minor),
            "minor": sum(
                n
                for (check, _), n in by.items()
                if check == opinion_cards.MINOR_CHECK
            ),
        }
        if not scoped:
            cache.set(key, counts, COUNTS_TTL)
    return counts


def _tabs(filters: dict, user) -> list[dict]:
    """Return one tab per reason with a card, hardest first."""
    scoped = filters["scan"] or filters["volume"] or filters["mine"]
    key = "card-reasons:" + _query(filters, reason="")
    counts = None if scoped else cache.get(key)
    if counts is None:
        counts = dict(
            _rows(filters, user, with_reason=False)
            .values_list("reason")
            .annotate(n=Count("id"))
            .order_by()
        )
        if not scoped:
            cache.set(key, counts, COUNTS_TTL)
    address = reverse("opinion_card_review")
    tabs = [
        {
            "slug": "",
            "label": "All",
            "count": sum(counts.values()),
            "url": f"{address}?{_query(filters, reason='')}",
            "current": not filters["reason"],
        }
    ]
    for slug, label in blocking_types.TYPES:
        if counts.get(slug):
            tabs.append(
                {
                    "slug": slug,
                    "label": label,
                    "count": counts[slug],
                    "url": f"{address}?{_query(filters, reason=slug)}",
                    "current": slug == filters["reason"],
                }
            )
    return tabs


@login_required
def opinion_card_review(request: HttpRequest) -> HttpResponse:
    """Walk the card rows of a reason, one opinion after the next.

    The rows of the filters (reason, status, scan, volume, the cards
    assigned to the viewer), grouped by opinion: a section per opinion,
    whose cards the script asks :func:`opinion_card_rows` for and whose
    crops come from the opinion's redacted PDF. An answer goes on the
    row (:func:`answer_opinion_card`); nothing on this page writes the
    text.
    """
    filters = _filters(request)
    rows = _rows(filters, request.user)
    opinions_qs = (
        Opinion.objects.filter(pk__in=rows.values("opinion_id"))
        .select_related("scan", "scan__reporter")
        .defer(*Scan.large_fields("scan__"))
    )
    # The order never crosses a table (``views._filtered_opinions``): a
    # scan in page order, the corpus newest first.
    if filters["scan"]:
        opinions_qs = opinions_qs.order_by(
            "first_printed_page", "index_in_page", "pk"
        )
    else:
        opinions_qs = opinions_qs.order_by("-pk")
    page_obj = Paginator(opinions_qs, OPINIONS_PER_PAGE).get_page(
        request.GET.get("page")
    )
    counts = dict(
        rows.filter(opinion_id__in=[o.pk for o in page_obj])
        .values_list("opinion_id")
        .annotate(n=Count("id"))
        .order_by()
    )
    card_query = _query(
        {key: filters[key] for key in ("reason", "level", "status", "mine")}
    )
    for opinion in page_obj:
        kwargs = {"pk": opinion.scan_id, "opinion_pk": opinion.pk}
        opinion.card_count = counts.get(opinion.pk, 0)
        opinion.cards_url = (
            reverse("opinion_card_rows", kwargs=kwargs) + f"?{card_query}"
        )
        opinion.pdf_url_endpoint = reverse("opinion_pdf_url", kwargs=kwargs)
        opinion.review_url = reverse(
            "opinion_review", kwargs={"pk": opinion.pk}
        )
    return render(
        request,
        "scanning/opinion_card_review.html",
        {
            "page_obj": page_obj,
            "filters": filters,
            "tabs": _tabs(filters, request.user),
            "reason_label": blocking_types.LABELS.get(filters["reason"], ""),
            "levels": _level_counts(filters, request.user),
            "level_choices": [
                ("", "All"),
                ("blocking", "Blocking: holds the approval"),
                (
                    "minor",
                    "Minor: letters agree, only spacing or marks differ",
                ),
            ],
            "card_total": rows.count(),
            "status_choices": [
                ("open", "Open"),
                ("answered", "Answered, not applied"),
                ("flagged", "Set aside (new scan, redaction fix)"),
                ("applied", "Applied"),
            ],
            "query": _query(filters),
            **_claim_context(request, filters),
        },
    )


#: The line after a claim, and the line when nothing was left to claim.
CLAIMED_MESSAGE = "You claimed {n} card(s). They are below."
NOTHING_TO_CLAIM_MESSAGE = (
    "There are no cards to claim right now. Try again later."
)
#: The refusal of a first claim before the practice cards are done.
PRACTICE_FIRST_MESSAGE = (
    "Start with the practice cards: finish them once, then claim your "
    "first batch."
)
#: The refusal of a claim while the person still holds open cards.
STILL_HOLDING_MESSAGE = (
    "Finish the cards you have first: a new batch can be claimed once "
    "your list is empty."
)


def _claim_context(request: HttpRequest, filters: dict) -> dict:
    """Return the claim button's state and the viewer's answered total.

    Read on the viewer's own list alone (``mine``), the page a
    volunteer always sees: the button shows when they hold no open card,
    and the total counts every card they ever answered.
    """
    if not filters.get("mine"):
        return {}
    user = request.user
    can_claim = not opinion_cards.holds_open(user)
    return {
        "can_claim": can_claim,
        "must_practice": can_claim and not volunteers.has_practiced(user),
        "answered_total": OpinionCard.objects.filter(answered_by=user).count(),
        "claim_count": opinion_cards.CLAIM_COUNT,
    }


@login_required
@require_POST
def claim_opinion_cards(request: HttpRequest) -> HttpResponse:
    """Give the viewer a batch of cards, when their list is empty.

    ``opinion_cards.claim`` picks them: short three-way word cards, the
    blocking ones first, of opinions with no picture. A person who has
    not finished the practice cards once is sent to them, and one who
    still holds an open card is refused, so a batch is never stacked.
    """
    user = request.user
    if not volunteers.has_practiced(user):
        messages.warning(request, PRACTICE_FIRST_MESSAGE)
        return redirect(reverse("volunteer_practice"))
    # One claim of a person at a time: a double click or a second tab
    # waits on the lock of their user row, then finds the first batch
    # held and is refused, so a batch is never stacked.
    with transaction.atomic():
        get_user_model().objects.select_for_update().filter(pk=user.pk).first()
        n = (
            None
            if opinion_cards.holds_open(user)
            else opinion_cards.claim(user)
        )
    if n is None:
        messages.warning(request, STILL_HOLDING_MESSAGE)
    else:
        logger.info(
            "claim_opinion_cards: user=%s claimed %s card(s)", user.pk, n
        )
        if n:
            messages.success(request, CLAIMED_MESSAGE.format(n=n))
        else:
            messages.info(request, NOTHING_TO_CLAIM_MESSAGE)
    return redirect(reverse("opinion_card_review") + "?mine=1")


def _card_of(row: OpinionCard, scan_pk: int) -> dict:
    """Return one row as the script's card, with its answer and its URL."""
    stale = row.status == OpinionCard.Status.STALE
    flagged = row.status == OpinionCard.Status.FLAGGED
    return {
        **row.card,
        "row_id": row.pk,
        "reason": row.reason,
        "reason_label": blocking_types.LABELS.get(row.reason, row.reason),
        # A stale answer waits for a new one: the card is open again,
        # and its note says why.
        "minor_warning": row.check_name == opinion_cards.MINOR_CHECK,
        "stored": (
            row.get_flag_display()
            if flagged
            else None
            if stale or row.answer is None
            else row.answer
        ),
        "flagged": flagged,
        "drop": row.action == OpinionCard.Action.DROP,
        "applied": row.status == OpinionCard.Status.APPLIED,
        "note": row.note if stale else "",
        "answer_url": reverse(
            "answer_opinion_card",
            kwargs={"pk": scan_pk, "card_pk": row.pk},
        ),
    }


@login_required
def opinion_card_rows(
    request: HttpRequest, pk: int, opinion_pk: int
) -> JsonResponse:
    """Return the card rows of one opinion under the page's filters.

    The answer of ``opinion_blocking_cards`` in shape, so the script
    reads both alike: ``{status, opinion, cards}``.
    """
    opinion = get_object_or_404(Opinion, pk=opinion_pk, scan_id=pk)
    filters = _filters(request)
    rows = list(_rows(filters, request.user).filter(opinion=opinion))
    if filters["status"] == "open":
        # The viewer's own answers stay on the page with their Undo,
        # until the apply writes them: the open filter alone would drop
        # a card the moment it is answered.
        rows += list(
            _rows({**filters, "status": "answered"}, request.user).filter(
                opinion=opinion, answered_by=request.user
            )
        )
        rows += list(
            _rows({**filters, "status": "flagged"}, request.user).filter(
                opinion=opinion, answered_by=request.user
            )
        )
        rows.sort(key=lambda row: row.pk)
    return JsonResponse(
        {
            "status": "ok",
            "level": "blocking",
            "blocking_open": 0,
            "opinion": {
                "pk": opinion.pk,
                "label": str(opinion),
                "status": opinion.status,
                "page_count": opinion.page_count,
                "glue_revision": opinion.glue_revision,
                "edit_revision": opinion.ensemble_edit_revision,
            },
            "cards": [_card_of(row, pk) for row in rows],
        }
    )


@login_required
@require_POST
def answer_opinion_card(
    request: HttpRequest, pk: int, card_pk: int
) -> JsonResponse:
    """Store the answer of one card row, or take it back.

    The body is ``{"answer": "..."}``, the words for the card's place,
    or ``{"answer": null}`` to open the card again. The text does not
    change: ``apply_card_answers`` writes the answers later.
    """
    row = get_object_or_404(OpinionCard, pk=card_pk, opinion__scan_id=pk)
    if (
        volunteers.of_request(request)
        and row.assigned_to_id != request.user.pk
    ):
        return JsonResponse(
            {"status": "error", "message": volunteers.CARD_MESSAGE},
            status=403,
        )
    try:
        body = json.loads(request.body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        body = None
    if not isinstance(body, dict) or not (
        "answer" in body or body.get("drop") is True or "flag" in body
    ):
        return JsonResponse(
            {"status": "error", "message": ANSWER_BAD_REQUEST_MESSAGE},
            status=400,
        )
    if "flag" in body:
        return _set_aside(request, row, body["flag"])
    value = "" if body.get("drop") is True else body["answer"]
    if value is not None and not isinstance(value, str):
        return JsonResponse(
            {"status": "error", "message": ANSWER_BAD_REQUEST_MESSAGE},
            status=400,
        )
    if row.opinion.status != OpinionReviewStatus.READY_FOR_TEXT_REVIEW:
        return JsonResponse(
            {"status": "error", "message": CARD_GONE_MESSAGE}, status=409
        )
    action = (
        OpinionCard.Action.DROP
        if body.get("drop") is True
        else OpinionCard.Action.TEXT
    )
    try:
        opinion_cards.answer(row, request.user, value, action)
    except (opinion_cards.CardClosed, OpinionCard.DoesNotExist):
        return JsonResponse(
            {"status": "error", "message": CARD_GONE_MESSAGE}, status=409
        )
    return JsonResponse(
        {
            "status": "ok",
            "message": ANSWER_CLEARED_MESSAGE
            if value is None
            else ANSWER_SAVED_MESSAGE,
        }
    )


RESCAN_FILED_MESSAGE = (
    "Set aside: a rescan of this page is in the repair queue."
)
REDACTION_FLAGGED_MESSAGE = (
    "Set aside for staff: the redaction over this place needs a fix in "
    "review 2."
)
RESCAN_NO_PAGE_MESSAGE = (
    "This page was inserted in review 1, so it has no original page to "
    "scan again. Set it aside as a redaction fix, or open the review page."
)


def _set_aside(request: HttpRequest, row: OpinionCard, flag) -> JsonResponse:
    """Set a card aside, filing a rescan in the repair queue for a
    ``rescan`` flag: the review-1 button's request, for the original
    page the card sits on (``request_page_repair``'s row and key)."""
    if flag not in OpinionCard.Flag.values:
        return JsonResponse(
            {"status": "error", "message": ANSWER_BAD_REQUEST_MESSAGE},
            status=400,
        )
    if row.status == OpinionCard.Status.APPLIED:
        return JsonResponse(
            {"status": "error", "message": CARD_GONE_MESSAGE}, status=409
        )
    try:
        # One transaction: a card the locked set-aside finds closed takes
        # the rescan it filed back with it, so no repair work outlives a
        # refused flag.
        with transaction.atomic():
            message = _file_and_set_aside(request, row, flag)
    except (opinion_cards.CardClosed, OpinionCard.DoesNotExist):
        return JsonResponse(
            {"status": "error", "message": CARD_GONE_MESSAGE}, status=409
        )
    if isinstance(message, JsonResponse):
        return message
    return JsonResponse({"status": "ok", "message": message})


def _file_and_set_aside(request: HttpRequest, row: OpinionCard, flag: str):
    """File the rescan of a ``rescan`` flag and set the card aside.

    :returns: The success message, or the refusal of a page that has no
        original page to scan again.
    :raises opinion_cards.CardClosed: for a card applied meanwhile.
    """
    from scanning.models import PageRepairRequest
    from scanning.views_process import (
        _pdf_page_of,
        _printed_number_of,
        _rebuild_bad_page_card,
    )

    repair = None
    message = REDACTION_FLAGGED_MESSAGE
    if flag == OpinionCard.Flag.RESCAN:
        # The label reads ``ocr_results`` and the bad-page card rule reads
        # ``page_scores``: the two caches ``request_page_repair`` keeps.
        scan = Scan.without_caches("ocr_results", "page_scores").get(
            pk=row.opinion.scan_id
        )
        edit_id, source_page = (row.card.get("address") or [None, None])[:2]
        pdf_page = None if edit_id else _pdf_page_of(scan, source_page)
        if pdf_page is None:
            return JsonResponse(
                {"status": "error", "message": RESCAN_NO_PAGE_MESSAGE},
                status=409,
            )
        repair, created = PageRepairRequest.objects.get_or_create(
            scan=scan,
            action=PageRepairRequest.Action.REPLACE,
            dismissed_at=None,
            pdf_page=pdf_page,
            defaults={
                "requested_by": request.user,
                "logical_page": _printed_number_of(scan, pdf_page),
                "note": (
                    "From a text-review card "
                    f"({blocking_types.LABELS.get(row.reason, row.reason)}) "
                    f"of {row.opinion}."
                ),
                "source_fingerprint": scan.source_fingerprint,
            },
        )
        if created:
            _rebuild_bad_page_card(scan, pdf_page)
        message = RESCAN_FILED_MESSAGE
    opinion_cards.set_aside(
        row,
        request.user,
        flag,
        repair=repair,
        note=OpinionCard.Flag(flag).label,
    )
    return message


#: The answers of one page of the staff list.
ANSWERS_PER_PAGE = 100

#: The statuses the staff list of answers names.
ANSWER_STATUSES = {
    "answered": (OpinionCard.Status.ANSWERED,),
    "applied": (OpinionCard.Status.APPLIED,),
    "stale": (OpinionCard.Status.STALE,),
    "flagged": (OpinionCard.Status.FLAGGED,),
    "all": (
        OpinionCard.Status.ANSWERED,
        OpinionCard.Status.APPLIED,
        OpinionCard.Status.STALE,
        OpinionCard.Status.FLAGGED,
    ),
}


def _shown(row: OpinionCard) -> dict:
    """Return what the card showed and what the answer makes, in context.

    A word card: the words around it, the word shown and the answer. A
    block card: the block shown and the answer, whole. A row the apply
    kept as shown says so.
    """
    card = row.card
    if card.get("kind") == "word":
        old = "" if card.get("gap") else card.get("token", "")
        before, after = card.get("before", ""), card.get("after", "")
    else:
        old, before, after = card.get("text", ""), "", ""
    keep = row.answer is not None and opinion_cards.is_keep(row)
    if row.status == OpinionCard.Status.FLAGGED:
        new = f"set aside: {row.get_flag_display()}"
    elif row.action == OpinionCard.Action.DROP:
        new = "take the block out (not text)"
    else:
        new = row.answer
    return {
        "before": before,
        "old": old,
        "new": new,
        "after": after,
        "keep": keep,
        "gap": bool(card.get("gap")),
    }


@login_required
def opinion_card_answers(request: HttpRequest) -> HttpResponse:
    """List the stored answers of the card rows, for staff.

    Who answered what, when, against which readings, and what became of
    it: waiting for the apply, applied (with its edit), or sent back for
    a new answer. A staff member can send an answer that is not applied
    back (the answer endpoint with no answer).
    """
    if not request.user.is_staff:
        raise PermissionDenied
    status = request.GET.get("status", "all")
    if status not in ANSWER_STATUSES:
        status = "all"
    rows = (
        OpinionCard.objects.filter(status__in=ANSWER_STATUSES[status])
        .filter(Q(answer__isnull=False) | Q(status=OpinionCard.Status.FLAGGED))
        .select_related(
            "opinion", "opinion__scan", "answered_by", "edit", "repair"
        )
        .defer(*Scan.large_fields("opinion__scan__"))
        .order_by("-answered_at", "-pk")
    )
    reason = request.GET.get("reason", "")
    if reason in blocking_types.LABELS:
        rows = rows.filter(reason=reason)
    else:
        reason = ""
    username = request.GET.get("user", "")
    if username:
        rows = rows.filter(answered_by__username=username)
    scan = _whole_number(request.GET.get("scan"))
    if scan is not None:
        rows = rows.filter(opinion__scan_id=scan)
    page_obj = Paginator(rows, ANSWERS_PER_PAGE).get_page(
        request.GET.get("page")
    )
    for row in page_obj:
        row.shown = _shown(row)
        row.reason_label = blocking_types.LABELS.get(row.reason, row.reason)
        # A word card's readings hold ``word``, a block card's ``text``,
        # and a single block is the one engine's text: one key here.
        row.readings = [
            {
                "engine": reading.get("engine", ""),
                "words": reading.get("word", reading.get("text", "")),
            }
            for reading in row.card.get("readings")
            or [
                {
                    "engine": row.card.get("engine"),
                    "text": row.card.get("text"),
                }
            ]
        ]
        row.review_url = (
            reverse("opinion_review", kwargs={"pk": row.opinion_id})
            + f"#op-page-{row.page_in_opinion}"
        )
        row.send_back_url = reverse(
            "send_back_opinion_card",
            kwargs={"pk": row.opinion.scan_id, "card_pk": row.pk},
        )
    volunteers = (
        get_user_model()
        .objects.filter(answered_cards__isnull=False)
        .distinct()
        .order_by("username")
        .values_list("username", flat=True)
    )
    query = urlencode(
        {
            key: value
            for key, value in (
                ("status", status),
                ("reason", reason),
                ("user", username),
                ("scan", "" if scan is None else scan),
            )
            if value != ""
        }
    )
    return render(
        request,
        "scanning/opinion_card_answers.html",
        {
            "page_obj": page_obj,
            "status": status,
            "reason": reason,
            "username": username,
            "scan": "" if scan is None else scan,
            "reasons": blocking_types.TYPES,
            "volunteers": volunteers,
            "statuses": [
                ("all", "All"),
                ("answered", "Waiting for the apply"),
                ("applied", "Applied"),
                ("stale", "Sent back / needs a new answer"),
                ("flagged", "Set aside (new scan, redaction fix)"),
            ],
            "query": query,
        },
    )


SENT_BACK_MESSAGE = "Sent back. The card is open again for a new answer."


@login_required
@require_POST
def send_back_opinion_card(
    request: HttpRequest, pk: int, card_pk: int
) -> JsonResponse:
    """Send one answer back, for staff (``opinion_cards.send_back``)."""
    if not request.user.is_staff:
        raise PermissionDenied
    row = get_object_or_404(OpinionCard, pk=card_pk, opinion__scan_id=pk)
    try:
        opinion_cards.send_back(row, request.user)
    except (opinion_cards.CardClosed, OpinionCard.DoesNotExist):
        return JsonResponse(
            {"status": "error", "message": CARD_GONE_MESSAGE}, status=409
        )
    return JsonResponse({"status": "ok", "message": SENT_BACK_MESSAGE})


@login_required
def volunteer_faq(request: HttpRequest) -> HttpResponse:
    """The questions and answers of the card review, for the volunteers.

    A page of the template alone; a volunteer may reach it
    (``volunteers.URL_NAMES``), and so may everyone else.
    """
    return render(request, "scanning/volunteer_faq.html", {})


@login_required
def volunteer_practice(request: HttpRequest) -> HttpResponse:
    """The practice cards of the FAQ: real cards with their answers
    (``volunteer_practice.EXAMPLES``), checked in the browser."""
    from scanning import volunteer_practice

    return render(
        request,
        "scanning/volunteer_practice.html",
        {
            "cards": volunteer_practice.cards(),
            "practiced": volunteers.has_practiced(request.user),
        },
    )


@login_required
@require_POST
def volunteer_practice_done(request: HttpRequest) -> JsonResponse:
    """Record that the viewer finished every practice card.

    The practice page's script sends it once the last card is answered,
    right or wrong (each card says why); the first claim waits on it.
    """
    volunteers.mark_practiced(request.user)
    logger.info("volunteer_practice_done: user=%s", request.user.pk)
    return JsonResponse({"status": "ok", "cards_url": volunteers.home_url()})
