"""The card rows of the text review: walked by reason, answered, kept.

The volunteers' page over ``OpinionCard``: the open cards of a reason
across the corpus (or of a volume), one section per opinion, each
answer stored on its row and nothing else. ``apply_card_answers``
writes the answers into the text later (``opinion_cards.apply``).
"""

from __future__ import annotations

import json

from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db.models import Count, Exists, OuterRef
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.utils.http import urlencode
from django.views.decorators.http import require_POST

from scanning import blocking_types, opinion_cards
from scanning.models import (
    Opinion,
    OpinionCard,
    OpinionFinding,
    OpinionReviewStatus,
    Scan,
)

#: The opinions of one page of the walk.
OPINIONS_PER_PAGE = 25

#: How long the reason counts of the whole corpus are kept, in seconds.
COUNTS_TTL = 120

#: The statuses a filter of the page names.
STATUS_FILTERS = {
    "open": opinion_cards.WAITING,
    "answered": (OpinionCard.Status.ANSWERED,),
    "applied": (OpinionCard.Status.APPLIED,),
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
    return {
        "reason": reason if reason in blocking_types.LABELS else "",
        "status": status if status in STATUS_FILTERS else "open",
        "scan": str(scan) if scan is not None else "",
        "volume": str(volume) if volume is not None else "",
        "mine": request.GET.get("mine") == "1",
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


def _rows(filters: dict, user, with_reason: bool = True):
    """Return the card rows of the filters.

    The open ones are rows of an opinion in the text review whose page
    still waits on its finding: a page a person dismissed on the review
    page keeps its rows until the next build, and they are no work.
    """
    rows = OpinionCard.objects.filter(
        status__in=STATUS_FILTERS[filters["status"]],
        opinion__status=OpinionReviewStatus.READY_FOR_TEXT_REVIEW,
    )
    if filters["status"] == "open":
        rows = rows.filter(
            Exists(
                OpinionFinding.objects.filter(
                    opinion_id=OuterRef("opinion_id"),
                    page_in_opinion=OuterRef("page_in_opinion"),
                    check_name=OuterRef("check_name"),
                    dismissal__isnull=True,
                )
            )
        )
    if filters["scan"]:
        rows = rows.filter(opinion__scan_id=filters["scan"])
    if filters["volume"]:
        rows = rows.filter(opinion__scan__volume=filters["volume"])
    if filters["mine"]:
        rows = rows.filter(assigned_to=user)
    if with_reason and filters["reason"]:
        rows = rows.filter(reason=filters["reason"])
    return rows


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
        {key: filters[key] for key in ("reason", "status", "mine")}
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
            "card_total": rows.count(),
            "status_choices": [
                ("open", "Open"),
                ("answered", "Answered, not applied"),
                ("applied", "Applied"),
            ],
            "query": _query(filters),
        },
    )


def _card_of(row: OpinionCard, scan_pk: int) -> dict:
    """Return one row as the script's card, with its answer and its URL."""
    stale = row.status == OpinionCard.Status.STALE
    return {
        **row.card,
        "row_id": row.pk,
        "reason": row.reason,
        "reason_label": blocking_types.LABELS.get(row.reason, row.reason),
        # A stale answer waits for a new one: the card is open again,
        # and its note says why.
        "stored": None if stale or row.answer is None else row.answer,
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
    rows = _rows(filters, request.user).filter(opinion=opinion)
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
    try:
        body = json.loads(request.body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        body = None
    if not isinstance(body, dict) or "answer" not in body:
        return JsonResponse(
            {"status": "error", "message": ANSWER_BAD_REQUEST_MESSAGE},
            status=400,
        )
    value = body["answer"]
    if value is not None and not isinstance(value, str):
        return JsonResponse(
            {"status": "error", "message": ANSWER_BAD_REQUEST_MESSAGE},
            status=400,
        )
    if row.opinion.status != OpinionReviewStatus.READY_FOR_TEXT_REVIEW:
        return JsonResponse(
            {"status": "error", "message": CARD_GONE_MESSAGE}, status=409
        )
    try:
        opinion_cards.answer(row, request.user, value)
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


#: The answers of one page of the staff list.
ANSWERS_PER_PAGE = 100

#: The statuses the staff list of answers names.
ANSWER_STATUSES = {
    "answered": (OpinionCard.Status.ANSWERED,),
    "applied": (OpinionCard.Status.APPLIED,),
    "stale": (OpinionCard.Status.STALE,),
    "all": (
        OpinionCard.Status.ANSWERED,
        OpinionCard.Status.APPLIED,
        OpinionCard.Status.STALE,
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
    return {
        "before": before,
        "old": old,
        "new": row.answer,
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
        OpinionCard.objects.filter(
            status__in=ANSWER_STATUSES[status], answer__isnull=False
        )
        .select_related("opinion", "opinion__scan", "answered_by", "edit")
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
        row.answer_url = reverse(
            "answer_opinion_card",
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
            ],
            "query": query,
        },
    )
