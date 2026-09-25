"""JSON API endpoints and file-serving views for the process page."""

import json
import logging
import os
import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import fitz
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Q
from django.http import (
    FileResponse,
    Http404,
    HttpRequest,
    HttpResponse,
    JsonResponse,
    StreamingHttpResponse,
)
from django.shortcuts import get_object_or_404, redirect
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils.cache import get_conditional_response
from django.utils.http import http_date
from django.views.decorators.http import require_POST

from scanning.models import (
    BUSY_STATUSES,
    REVIEW2_CHECKS,
    Detection,
    DetectionDecision,
    Issue,
    Opinion,
    OpinionBoundary,
    OpinionScan,
    Redaction,
    Scan,
    Stage,
    Status,
)
from scanning.utils import (
    PIPELINE_PAUSED_MESSAGE,
    find_processing_pdf,
    local_original_pdf,
)

logger = logging.getLogger(__name__)


def _rebuild_findings(scan: Scan) -> None:
    """Write the review-2 findings again after a curator's write (#240 PR D).

    Every finding is derived from the detection, boundary and redaction
    rows, so the endpoint that changed a row is what keeps the cards
    true. A few queries over label-filtered rows.

    :param scan: The scan.
    :return: None.
    """
    from scanning import findings

    findings.rebuild(scan)


#: The 409 of every write of review 2 before that review is open
#: (#388). One sentence, because the viewer shows it as it comes, and
#: one for every volume it refuses: a preview and a volume with no
#: detection run are the same state to a curator, which is "the review
#: is not open yet".
REVIEW_NOT_OPEN_MESSAGE = (
    "The redaction review of this volume is not open yet, so nothing "
    "here can be changed. Approve the page completeness review and wait "
    "for the corrected volume to be built."
)


def _refuse_closed_review(scan: Scan) -> JsonResponse | None:
    """Refuse a write of review 2 while the volume is still in review 1.

    The first thing every write of the redaction review does, the twin
    of ``views_process._refuse_locked_edits``, and the gate the preview
    of #388 needed: a curator reading a preview must change nothing.

    **The rule is the status, not the preview.** A row written in
    review 1 is built into nothing and survives nothing: it addresses
    the volume as uploaded, which the apply has not rebuilt yet, and
    the first compute under the new run imports the model rows again
    (``services._import_detections``) and measures the human ones
    against a page space nobody approved. That is true of a volume with
    a merged detection run, of one whose run is still in flight, and of
    one that has no run at all -- so the gate reads
    ``review_states.PREVIEW_STATUSES`` and not
    ``review_states.preview_only``, which would leave the other two
    open. ``add_redaction`` accepted such a box until this.

    Out of it, deliberately: the legacy ``PENDING_REVIEW`` step 2,
    whose rows the old pipeline wrote, and the two #263 statuses, which
    are review 2 itself. Their own rules (the compare-and-swap of
    ``approve_redaction_review``, ``REDACTION_COMPUTE_STATUSES``) stand
    where they already did. So is every read: ``export_pdf`` builds the
    corrected volume from the page edits and hands it over, which is
    review 1's own work and not a write of review 2.

    The gate is here and not in the template alone, for the reason the
    review-1 gates are (#151): a template hides a button, and only a
    view refuses a direct POST.

    :param scan: The scan the write is about.
    :returns: A 409 answer naming the reason, or None when the write
        may proceed.
    :rtype: JsonResponse | None
    """
    from scanning import review_states

    if scan.status not in review_states.PREVIEW_STATUSES:
        return None
    return JsonResponse(
        {"status": "error", "message": REVIEW_NOT_OPEN_MESSAGE}, status=409
    )


# The success lines of the review-2 writes (#322). Every write answers
# one of these as ``message``, and the viewer shows it as a success
# toast: a curator who moves a box had no sign that the server kept it,
# because the box stays where the mouse left it either way. The text
# lives here, in the view that knows what it wrote, never in the
# viewer scripts.
SAVED_REDACTION_MESSAGE = (
    "The box was saved. The redactions are not measured again from it yet."
)
#: A move or a resize, of a redaction box and of a detection box.
MOVED_BOX_MESSAGE = "The box was moved."
#: The same move, when the curator's box replaced a computed one.
MOVED_OVER_COMPUTED_MESSAGE = (
    "The box was moved. Your box replaces the computed one."
)
#: The same move, when the curator's box replaced a model row.
MOVED_OVER_MODEL_MESSAGE = (
    "The box was moved. Your box replaces the model box."
)
DISMISSED_REDACTION_MESSAGE = "The box was dismissed. Nothing was deleted."
WITHDRAWN_REDACTION_MESSAGE = "The box was withdrawn. Nothing was deleted."
RESTORED_REDACTION_MESSAGE = "The box came back."
STANDING_REDACTION_MESSAGE = "The box was standing already."
ADDED_DETECTION_MESSAGE = "The detection was added."
#: A caption or a key icon changes the pairing, which only the
#: measurement can do (#305).
ADDED_ANCHOR_DETECTION_MESSAGE = (
    'The detection was added. Press "Recompute redactions" to pair the '
    "opinions again."
)
STANDING_DETECTION_MESSAGE = "The box is there already."
#: The approval of a row the curator drew: the view writes nothing,
#: because the box is theirs and reads 1.0 from birth.
OWN_DETECTION_MESSAGE = (
    "This box is your own, so it needs no approval: it reads 1.0 already."
)
#: An approval is a move by zero (#414): the box is the curator's own
#: from here on, and a new import keeps it.
APPROVED_DETECTION_MESSAGE = (
    "The detection was approved: the box is your own now and reads 1.0. "
    "A new import keeps it. The card stays until the opinions are paired "
    "again."
)
APPROVED_BRACKET_MESSAGE = (
    "The bracket was approved: the box is your own now and reads 1.0. "
    "A new import keeps it, and the next compute redacts it."
)
DISMISSED_DETECTION_MESSAGE = (
    "The detection was dismissed. Nothing was deleted."
)
WITHDRAWN_DETECTION_MESSAGE = (
    "The detection was withdrawn. Nothing was deleted."
)
ADDED_BOUNDARY_MESSAGE = "The opinion boundary was added."
MOVED_BOUNDARY_MESSAGE = "The opinion boundary was moved."
DISMISSED_BOUNDARY_MESSAGE = (
    "The opinion boundary was dismissed. Nothing was deleted."
)
WITHDRAWN_BOUNDARY_MESSAGE = (
    "The opinion boundary was withdrawn. Nothing was deleted."
)
RESTORED_BOUNDARY_MESSAGE = "The opinion boundary came back."
STANDING_BOUNDARY_MESSAGE = "The opinion boundary was standing already."
DISMISSED_FINDING_MESSAGE = (
    "The finding was dismissed. Press Undo on the card to take it back."
)
RESTORED_FINDING_MESSAGE = "The dismissal was taken back."
STANDING_FINDING_MESSAGE = "The finding was standing already."
WITHDRAWN_DECISION_MESSAGE = "The decision was withdrawn."
STANDING_DECISION_MESSAGE = "The decision was withdrawn already."
REBUILT_FINDINGS_MESSAGE = "The findings were written again from the rows."

#: The answers of the "Re run OCR ensemble" button of review 3 (#365).
#: The text lives here, the rule of the review-2 writes above.
ENSEMBLE_RERUN_MESSAGE = (
    "The text was written again from {engines}: {groups} block(s) read, "
    "{dropped} left out, {low} word(s) with no majority."
)
ENSEMBLE_NOT_GLUED_MESSAGE = (
    "The OCR documents of this opinion are not written at the live "
    "revision, so there is nothing to read yet. The daemon writes them "
    "after the redaction review."
)
#: An approved opinion is never written over: a person read its text
#: and said it is right (#365).
ENSEMBLE_APPROVED_MESSAGE = (
    "The text review of this opinion is done, so its text is not "
    "written again. Reopen it first."
)
#: A review-3 finding takes a dismissal while its opinion is ready for
#: the text review alone (#419): before, the text of the live revision
#: is not written; after, a person approved it.
OPINION_FINDING_CLOSED_MESSAGE = (
    "This opinion is not ready for the text review, so its findings "
    "take no dismissal now."
)
#: The two stale checks of review 3 are facts about the row (#336).
OPINION_FINDING_UNDISMISSABLE_MESSAGE = (
    "This finding cannot be dismissed. It says the opinion row no "
    "longer matches the volume, and the way out is to fix the row."
)
#: A fault that passes. The detail goes to the log and not to the
#: answer: it carries the words of a library, and a message of ours is
#: what a curator can act on.
ENSEMBLE_BUCKET_MESSAGE = (
    "The file store did not answer, so nothing was written. Press the "
    "button again."
)
#: The OCR glue wrote again while the text was written, so the write
#: kept nothing (#365).
ENSEMBLE_MOVED_MESSAGE = (
    "The OCR documents of this opinion were written again while this "
    "ran, so nothing was kept. Press the button again."
)
#: The line of each ``ensemble.EnsembleError`` code. The answer is
#: built from this table and never from the error: the error names the
#: object it read, and a key of the bucket belongs in the log and on
#: the row, not in a browser.
ENSEMBLE_ERROR_MESSAGES = {
    "unreadable": (
        "One of the OCR documents of this opinion is missing, or it is "
        "not a document this portal can read. They must be written "
        "again before the text can be."
    ),
    "no_engine": (
        "The OCR glue of this opinion wrote no engine document, so "
        "there is nothing to read."
    ),
    "short_document": (
        "An OCR document of this opinion has fewer pages than the "
        "opinion. They must be written again before the text can be."
    ),
}

#: The answers of the human edits of review 3 (#376). Every answer is
#: a Django message too, success or error, and the viewer reloads the
#: page to show it: the text lives here, the rule of every write.
EDIT_TEXT_SAVED_MESSAGE = "The text of the block was saved."
EDIT_SECTION_SAVED_MESSAGE = {
    "footnotes": "The block was put in the footnotes.",
    "text": "The block was put in the body text.",
}
EDIT_MOVE_SAVED_MESSAGE = {
    "up": "The block was moved up.",
    "down": "The block was moved down.",
}
EDIT_WITHDRAWN_MESSAGE = (
    "The edit was undone. The block reads as the engines read it."
)
EDIT_STANDING_MESSAGE = "This edit was undone already."
#: A write the database kept, whose text the build did not write. The
#: daemon builds it (``ensemble.due`` reads the edit revision).
EDIT_NOT_BUILT_MESSAGE = (
    "The edit was saved, but the text was not written again: {reason} "
    'The daemon writes it on its next pass, or press "Read the OCR '
    'documents again".'
)
EDIT_BAD_REQUEST_MESSAGE = "The request was not one this page sends."
EDIT_NOT_FOUND_MESSAGE = "This opinion is not in this volume."
EDIT_CLOSED_MESSAGE = (
    "This opinion is not ready for the text review, so its text takes "
    "no edit now."
)
EDIT_NOT_WRITTEN_MESSAGE = (
    "The text of this opinion is not written at the live revision, so "
    "there is nothing to edit yet."
)
EDIT_STALE_PAGE_MESSAGE = (
    "The text of this opinion changed since this page was loaded. The "
    "page was loaded again: look at the block and try again."
)
EDIT_NOT_BUILT_YET_MESSAGE = (
    "The last edit of this opinion is not in its text yet. Press "
    '"Read the OCR documents again" to write it, then edit again.'
)
EDIT_NO_BLOCK_MESSAGE = "That block is not in the text of this opinion."
EDIT_NO_ADDRESS_MESSAGE = (
    "This page of the opinion has no address in the volume, so an edit "
    "of it would land nowhere."
)
EDIT_UNANIMOUS_MESSAGE = (
    "Every engine read this block alike, so its text takes no edit."
)
EDIT_HUMAN_ALREADY_MESSAGE = (
    "This block holds an edit already. Undo it first, then edit the block."
)
EDIT_TABLE_MESSAGE = (
    "A table takes no text edit here: its rows are the engines' rows."
)
EDIT_EMPTY_MESSAGE = "Type the text of the block. An empty block is not saved."
EDIT_TOO_LONG_MESSAGE = (
    "The text is longer than {limit} characters, so it was not saved."
)
EDIT_UNCHANGED_MESSAGE = "The text did not change, so nothing was saved."
EDIT_SAME_SECTION_MESSAGE = "The block is in that section already."
EDIT_EDGE_MESSAGE = (
    "The block is at the edge of its section on this page, so it does "
    "not move."
)
EDIT_NO_EDIT_MESSAGE = "That edit is not an edit of this opinion."

#: The two labels the opinion pairing reads: a box of one of them
#: changes the boundaries, and only the measurement pairs them again.
PAIRING_LABELS = ("CASE_CAPTION", "KEY_ICON")


def _parse_json_body(request: HttpRequest) -> dict | JsonResponse:
    """Parse a JSON request body or return an error response.

    :param request: The HTTP request.
    :returns: The parsed dict on success, or a ``JsonResponse`` with a
        400 error on malformed JSON.
    :rtype: dict | JsonResponse
    """
    try:
        return json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON"}, status=400)


@login_required
def serve_detections(request: HttpRequest, pk: int) -> JsonResponse:
    """Return active detections for a scan as JSON.

    A volume shown as a preview (#388) answers the merged detection
    document instead of the rows (``yolo.preview_entries``), in the
    same shape: it has no rows, and the ones a reopened volume left
    behind are measured in a page space the preview does not show. A
    document that cannot be read answers an empty list, because the
    page around it still renders.

    :param request: The HTTP request.
    :param pk: Scan primary key.
    :return: JSON response with a list of detection dicts.
    """
    from scanning import review_states, yolo

    scan = get_object_or_404(Scan, pk=pk)
    if review_states.preview_only(scan):
        try:
            return JsonResponse(yolo.preview_entries(scan), safe=False)
        except Exception:
            logger.exception(
                "serve_detections: the merged detections of scan %s did "
                "not load",
                scan.pk,
            )
            return JsonResponse([], safe=False)
    dets = (
        Detection.objects.live()
        .filter(scan=scan)
        .select_related("decision")
        .order_by("page_index", "y0")
    )
    data = [
        {
            "id": d.pk,
            "page_index": d.page_index,
            "label": d.label,
            "label_id": d.label_id,
            "confidence": d.confidence,
            "bbox": [d.x0, d.y0, d.x1, d.y1],
            "img_width": d.img_width,
            "img_height": d.img_height,
            "model_count": d.model_count,
            # The viewer draws a hand-added detection dashed, and shows
            # it whatever its label. It sets this flag itself when the
            # reviewer draws the box, so until it came from here a
            # hand-added box lost both on the next page load (PR #167).
            "manual": d.model_name == Detection.ModelName.MANUAL,
            # The standing curator decision on a model row (#240). An
            # "approve" is a row written before #414; since then an
            # approval is a hand-drawn row (``manual``) over a dismiss.
            "decision": d.decision.kind if d.decision_id else None,
        }
        for d in dets
    ]
    return JsonResponse(data, safe=False)


@login_required
def serve_opinions(request: HttpRequest, pk: int) -> JsonResponse:
    """Return the opinion boundaries of a scan as JSON.

    The rows, in the dict shape ``blackletter.api.pair`` produced plus
    their ids (#240 PR C, ``boundaries.viewer_payload``). A dismissed
    computed boundary is in the list with ``dismissed`` true, so the
    sidebar can offer its undo; the overlays skip it.

    :param request: The HTTP request.
    :param pk: Scan primary key.
    :return: JSON response with a list of opinion dicts.
    """
    from scanning import boundaries, review_states

    scan = get_object_or_404(Scan, pk=pk)
    if review_states.preview_only(scan):
        # The pairing is the compute's own output (#388): a row here
        # belongs to a run the preview does not show.
        return JsonResponse([], safe=False)
    return JsonResponse(boundaries.viewer_payload(scan), safe=False)


#: The 409 of a boundary anchor on a page no address can be written for
#: (``boundaries.UnaddressableBoundary``).
BOUNDARY_UNADDRESSABLE_MESSAGE = (
    "This page cannot be addressed in the current volume, so the "
    "boundary cannot be kept. Reload the page; if it stays, ask a staff "
    "member."
)


def _boundary_or_404(scan: Scan, data: dict):
    """Return the boundary ``data`` names, or the 404 response.

    :param scan: The scan.
    :param data: The parsed body, with ``boundary_id``.
    :returns: The row, or a ``JsonResponse``.
    """
    row = OpinionBoundary.objects.filter(
        pk=data.get("boundary_id"), scan=scan
    ).first()
    if row is None:
        return JsonResponse(
            {"status": "error", "message": "Opinion boundary not found"},
            status=404,
        )
    return row


@login_required
@require_POST
def dismiss_boundary(request: HttpRequest, pk: int) -> JsonResponse:
    """Take an opinion boundary out of the volume.

    A computed row gets a ``dismiss`` row that copies its anchors (#240
    PR C): the row is rebuilt at the next compute, and the dismissal is
    what carries the curator's choice onto the new row. A boundary the
    curator added is withdrawn, and gives back the computed one it
    replaced, if any. Nothing is deleted.

    :param request: The HTTP request (JSON body with ``boundary_id``).
    :param pk: Scan primary key.
    :return: ``dismissal_id`` for a computed row, null for a withdrawn
        addition, plus the ``message`` the viewer shows (#322); 404 when
        the row is not the scan's.
    """
    from scanning import boundaries

    scan = get_object_or_404(Scan, pk=pk)
    if (refusal := _refuse_closed_review(scan)) is not None:
        return refusal
    data = _parse_json_body(request)
    if isinstance(data, JsonResponse):
        return data
    row = _boundary_or_404(scan, data)
    if isinstance(row, JsonResponse):
        return row
    try:
        dismissal = boundaries.dismiss(scan, row, request.user)
    except boundaries.UnaddressableBoundary:
        return JsonResponse(
            {"status": "error", "message": BOUNDARY_UNADDRESSABLE_MESSAGE},
            status=409,
        )
    _rebuild_findings(scan)
    return JsonResponse(
        {
            "status": "ok",
            "boundary_id": row.pk,
            "dismissal_id": dismissal.pk if dismissal else None,
            "withdrawn": dismissal is None,
            "message": (
                WITHDRAWN_BOUNDARY_MESSAGE
                if dismissal is None
                else DISMISSED_BOUNDARY_MESSAGE
            ),
        }
    )


@login_required
@require_POST
def restore_boundary(request: HttpRequest, pk: int) -> JsonResponse:
    """Give a dismissed computed boundary back.

    Withdraws the standing dismissal; the boundary is drawn again with
    no compute. A withdrawn addition is not restored: the curator adds
    again.

    :param request: The HTTP request (JSON body with ``boundary_id``).
    :param pk: Scan primary key.
    :return: ``restored`` says whether a dismissal stood, plus the
        ``message`` the viewer shows (#322).
    """
    from scanning import boundaries

    scan = get_object_or_404(Scan, pk=pk)
    if (refusal := _refuse_closed_review(scan)) is not None:
        return refusal
    data = _parse_json_body(request)
    if isinstance(data, JsonResponse):
        return data
    row = _boundary_or_404(scan, data)
    if isinstance(row, JsonResponse):
        return row
    restored = boundaries.restore(scan, row, request.user)
    _rebuild_findings(scan)
    return JsonResponse(
        {
            "status": "ok",
            "boundary_id": row.pk,
            "restored": restored,
            "message": (
                RESTORED_BOUNDARY_MESSAGE
                if restored
                else STANDING_BOUNDARY_MESSAGE
            ),
        }
    )


def _anchor_spec(scan: Scan, spec) -> "Detection | tuple | JsonResponse":
    """Turn one anchor of the ``add`` body into what ``boundaries.add`` takes.

    :param scan: The scan.
    :param spec: ``{"detection_id": n}`` or ``{"page_index", "x", "y"}``
        with the point in PDF points.
    :returns: The detection row, the point, or a 400/404 response.
    """
    if not isinstance(spec, dict):
        return JsonResponse(
            {"status": "error", "message": "An anchor must be an object"},
            status=400,
        )
    if spec.get("detection_id") is not None:
        row = Detection.objects.filter(
            pk=spec["detection_id"], scan=scan
        ).first()
        if row is None:
            return JsonResponse(
                {"status": "error", "message": "Detection not found"},
                status=404,
            )
        return row
    try:
        return (int(spec["page_index"]), float(spec["x"]), float(spec["y"]))
    except (KeyError, TypeError, ValueError):
        return JsonResponse(
            {
                "status": "error",
                "message": "An anchor needs a detection_id, or a "
                "page_index with x and y",
            },
            status=400,
        )


@login_required
@require_POST
def add_boundary(request: HttpRequest, pk: int) -> JsonResponse:
    """Write an opinion boundary the curator drew.

    Each anchor is a detection the curator picked (the caption for the
    start, the key icon for the end) or a point in PDF points.
    ``replaces`` names the boundary the new one stands in place of (a
    moved anchor): a computed one is dismissed and the addition written
    in one transaction, and dismissing the addition gives it back; a
    curator's own addition is withdrawn and its dismissal carried, so a
    second move still leaves one boundary.

    :param request: The HTTP request (JSON body with ``start``, ``end``
        and optionally ``replaces``).
    :param pk: Scan primary key.
    :return: The new ``boundary_id`` and the ``dismissal_id`` it
        replaces; 409 when a page has no address; 400 when the end page
        is before the start.
    """
    from scanning import boundaries, detections

    scan = get_object_or_404(Scan, pk=pk)
    if (refusal := _refuse_closed_review(scan)) is not None:
        return refusal
    data = _parse_json_body(request)
    if isinstance(data, JsonResponse):
        return data
    start = _anchor_spec(scan, data.get("start"))
    if isinstance(start, JsonResponse):
        return start
    end = _anchor_spec(scan, data.get("end"))
    if isinstance(end, JsonResponse):
        return end
    replaces = None
    if data.get("replaces") is not None:
        replaces = (
            OpinionBoundary.objects.filter(pk=data["replaces"], scan=scan)
            .filter(
                Q(origin=OpinionBoundary.Origin.COMPUTED)
                | Q(
                    origin=OpinionBoundary.Origin.HUMAN,
                    kind=OpinionBoundary.Kind.ADD,
                    withdrawn_at__isnull=True,
                )
            )
            .first()
        )
        if replaces is None:
            return JsonResponse(
                {
                    "status": "error",
                    "message": "The boundary to replace was not found",
                },
                status=404,
            )
    try:
        row = boundaries.add(
            scan,
            start,
            end,
            request.user,
            detections.measured_run(scan),
            replaces=replaces,
        )
    except boundaries.UnaddressableBoundary:
        return JsonResponse(
            {"status": "error", "message": BOUNDARY_UNADDRESSABLE_MESSAGE},
            status=409,
        )
    except boundaries.MisorderedBoundary:
        return JsonResponse(
            {
                "status": "error",
                "message": "The end of an opinion cannot be on a page "
                "before its start.",
            },
            status=400,
        )
    _rebuild_findings(scan)
    return JsonResponse(
        {
            "status": "ok",
            "boundary_id": row.pk,
            "dismissal_id": row.replaces_id,
            "message": (
                MOVED_BOUNDARY_MESSAGE
                if row.replaces_id
                else ADDED_BOUNDARY_MESSAGE
            ),
        }
    )


@login_required
def serve_redactions(request: HttpRequest, pk: int) -> JsonResponse:
    """Return the boxes to paint, grouped by page, in PDF points.

    The redaction rects and the margin strips in one list (#240, PR B),
    read off the ``Redaction`` rows the compute wrote and the curator
    edited. Nothing is computed here: a volume the compute has not
    reached answers an empty list, and so does one shown as a preview
    (#388).

    :param request: The HTTP request.
    :param pk: Scan primary key.
    :return: ``[{page_index, rects: [{id, x0, y0, x1, y1, fill,
        rect_type, origin}]}]``.
    """
    from scanning import redactions, review_states

    scan = get_object_or_404(Scan, pk=pk)
    if review_states.preview_only(scan):
        # A preview shows the model's boxes and no measured geometry
        # (#388), including the rows a reopened volume left behind.
        return JsonResponse([], safe=False)
    return JsonResponse(redactions.visible_by_page(scan), safe=False)


#: The 400 of a malformed box body: one fixed sentence per endpoint,
#: never the exception's own text (CodeQL).
BAD_BOX_MESSAGE = (
    "Bad box: x0, y0, x1 and y1 are required, and the box needs a "
    "positive width and height."
)
BAD_NEW_BOX_MESSAGE = (
    "Bad box: page_index, x0, y0, x1 and y1 are required, the box needs "
    "a positive width and height, and fill is black or white."
)

#: The 409 of a box on a page no address can be written for.
REDACTION_UNADDRESSABLE_MESSAGE = (
    "This page cannot be addressed in the current volume, so the box "
    "cannot be kept. Reload the page; if it stays, ask a staff member."
)


def _redaction_error(message: str, status: int) -> JsonResponse:
    """Return a refusal the viewer reads (``status == "error"``).

    :param message: What to show the curator.
    :param status: The HTTP status.
    :returns: The response.
    """
    return JsonResponse({"status": "error", "message": message}, status=status)


def _bbox_of(data: dict) -> list[float]:
    """Read ``x0, y0, x1, y1`` off a JSON body, in points.

    :param data: The body.
    :returns: The four numbers.
    :raises ValueError: If one is missing or not a number, or the box
        is empty.
    """
    try:
        bbox = [float(data[k]) for k in ("x0", "y0", "x1", "y1")]
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("x0, y0, x1 and y1 are required") from exc
    if bbox[0] >= bbox[2] or bbox[1] >= bbox[3]:
        raise ValueError("a box needs a positive width and height")
    return bbox


def _redaction_of(scan: Scan, redaction_id: int) -> Redaction | None:
    """Return the scan's redaction row, or None.

    :param scan: The scan.
    :param redaction_id: The row's pk.
    :returns: The row.
    """
    return Redaction.objects.filter(pk=redaction_id, scan=scan).first()


@login_required
@require_POST
def add_redaction(request: HttpRequest, pk: int) -> JsonResponse:
    """Draw a box: a human ``add`` row, addressed by its source page.

    :param request: JSON body with ``page_index``, ``x0``, ``y0``,
        ``x1``, ``y1`` (points) and ``fill`` (``black`` or ``white``).
    :param pk: Scan primary key.
    :return: ``{status, id, message}``; 400 on a bad body, 409 when the
        page has no address.
    """
    from scanning import redactions

    scan = get_object_or_404(Scan, pk=pk)
    if (refusal := _refuse_closed_review(scan)) is not None:
        return refusal
    data = _parse_json_body(request)
    if isinstance(data, JsonResponse):
        return data
    try:
        page_index = int(data["page_index"])
        bbox = _bbox_of(data)
        fill = str(data.get("fill") or Redaction.Fill.BLACK)
        if fill not in Redaction.Fill.values:
            raise ValueError("fill is black or white")
    except (KeyError, TypeError, ValueError):
        # The detail goes to the log, not to the browser (CodeQL).
        logger.warning(
            "add_redaction: scan %s: malformed body", pk, exc_info=True
        )
        return _redaction_error(BAD_NEW_BOX_MESSAGE, 400)
    try:
        row = redactions.add(scan, page_index, bbox, fill, request.user)
    except redactions.UnaddressableRedaction:
        return _redaction_error(REDACTION_UNADDRESSABLE_MESSAGE, 409)
    _rebuild_findings(scan)
    return JsonResponse(
        {"status": "ok", "id": row.pk, "message": SAVED_REDACTION_MESSAGE}
    )


@login_required
@require_POST
def move_redaction(
    request: HttpRequest, pk: int, redaction_id: int
) -> JsonResponse:
    """Move or resize a box, and answer the row that holds it now.

    A human box is written in place; a computed one is dismissed and a
    human box is drawn where the curator put it (#240), so the viewer
    must address the answered id from then on.

    :param request: JSON body with ``x0``, ``y0``, ``x1``, ``y1``.
    :param pk: Scan primary key.
    :param redaction_id: The row.
    :return: ``{status, id, message}``.
    """
    from scanning import redactions

    scan = get_object_or_404(Scan, pk=pk)
    if (refusal := _refuse_closed_review(scan)) is not None:
        return refusal
    data = _parse_json_body(request)
    if isinstance(data, JsonResponse):
        return data
    row = _redaction_of(scan, redaction_id)
    if row is None or row.bbox is None:
        return _redaction_error("Redaction not found", 404)
    try:
        bbox = _bbox_of(data)
    except ValueError:
        logger.warning(
            "move_redaction: scan %s: malformed body", pk, exc_info=True
        )
        return _redaction_error(BAD_BOX_MESSAGE, 400)
    try:
        holder = redactions.move(scan, row, bbox, request.user)
    except redactions.UnaddressableRedaction:
        return _redaction_error(REDACTION_UNADDRESSABLE_MESSAGE, 409)
    _rebuild_findings(scan)
    return JsonResponse(
        {
            "status": "ok",
            "id": holder.pk,
            "message": (
                MOVED_BOX_MESSAGE
                if holder.pk == row.pk
                else MOVED_OVER_COMPUTED_MESSAGE
            ),
        }
    )


@login_required
@require_POST
def dismiss_redaction(
    request: HttpRequest, pk: int, redaction_id: int
) -> JsonResponse:
    """Take a box out: a dismiss of a computed row, a withdrawal of a
    human one. Nothing is deleted; a second call is a no-op.

    :param request: The HTTP request.
    :param pk: Scan primary key.
    :param redaction_id: The row.
    :return: ``{status, message}``.
    """
    from scanning import redactions

    scan = get_object_or_404(Scan, pk=pk)
    if (refusal := _refuse_closed_review(scan)) is not None:
        return refusal
    row = _redaction_of(scan, redaction_id)
    if row is None or row.bbox is None:
        return _redaction_error("Redaction not found", 404)
    human = row.origin == Redaction.Origin.HUMAN
    redactions.dismiss(scan, row, request.user)
    _rebuild_findings(scan)
    return JsonResponse(
        {
            "status": "ok",
            "message": (
                WITHDRAWN_REDACTION_MESSAGE
                if human
                else DISMISSED_REDACTION_MESSAGE
            ),
        }
    )


@login_required
@require_POST
def restore_redaction(
    request: HttpRequest, pk: int, redaction_id: int
) -> JsonResponse:
    """Give a dismissed computed box back: the undo of a dismiss.

    :param request: The HTTP request.
    :param pk: Scan primary key.
    :param redaction_id: The computed row.
    :return: ``{status, restored, message}``.
    """
    from scanning import redactions

    scan = get_object_or_404(Scan, pk=pk)
    if (refusal := _refuse_closed_review(scan)) is not None:
        return refusal
    row = _redaction_of(scan, redaction_id)
    if row is None:
        return _redaction_error("Redaction not found", 404)
    restored = redactions.restore(scan, row, request.user)
    _rebuild_findings(scan)
    return JsonResponse(
        {
            "status": "ok",
            "restored": restored,
            "message": (
                RESTORED_REDACTION_MESSAGE
                if restored
                else STANDING_REDACTION_MESSAGE
            ),
        }
    )


#: The refusal of the redaction recompute when the volume carries no
#: box to measure (#305).
NO_DETECTIONS_MESSAGE = (
    "This volume has no detections yet, so there is nothing to measure."
)

#: The refusal of the findings rebuild while the volume is busy (#305).
#: The compute writes the findings itself and stamps the run it
#: measured in afterwards, so a rebuild in that window would write the
#: cards against the space before it.
FINDINGS_BUSY_MESSAGE = (
    "This volume is busy. Its findings are written when the work ends."
)

#: The gate of step 3 in the view (#263/#269): a volume of the new
#: pipeline reaches the file generation through the review-2 approval.
GENERATE_REQUIRES_REDACTION_REVIEW_MESSAGE = (
    "The redaction review of this volume is not approved yet. Approve "
    "it in step 2 before the files are generated."
)


@login_required
@require_POST
def compute_redactions_api(request: HttpRequest, pk: int) -> JsonResponse:
    """Ask the daemon to measure this scan's redactions again.

    The "Recompute redactions" button of review 2 (#305). A curator
    presses it after they draw or dismiss a detection, and what they
    want is every consequence of that edit: the pairing, the redaction
    boxes and the margin strips, which are all measured from the same
    detections. One queued action computes all three (#196), so none of
    them can be left describing the boxes of an hour ago.

    **It runs on the daemon, not here.** The measurement renders every
    page of the volume: 83 seconds for 1364 pages. The viewer reloads,
    sees the scan busy, and its progress poll reloads again when the
    daemon parks it. Its twin, :func:`rebuild_findings`, reads rows
    alone and does run here.

    The curator's own rows are kept. A recompute against the standing
    apply run measures again and imports no model row, so it cannot
    throw away the edit the curator pressed this for
    (``run_compute_redactions``).

    There was a second name for this one action, ``pair_opinions_api``
    at ``scans/<pk>/pair-opinions/``, with the same body (#305). One
    button does not need two routes.

    :param request: The HTTP request.
    :param pk: Scan primary key.
    A refusal answers ``{status, message}``, the shape of every other
    refusal of these views. It answered ``{error}`` while no button
    reached it (#196); the curator reads the message now.

    :return: JSON response saying the work is queued, 400 when the
        volume has no detection to measure, or 409 when the status
        takes no compute (``services.REDACTION_COMPUTE_STATUSES``: a
        closed review 2 is one, and the way back is the re-queue).
    """
    scan = get_object_or_404(Scan, pk=pk)
    if not Detection.objects.filter(scan=scan, active=True).exists():
        return JsonResponse(
            {"status": "error", "message": NO_DETECTIONS_MESSAGE}, status=400
        )

    from scanning.services import queue_redaction_compute

    queued, message = queue_redaction_compute(scan)
    if not queued:
        return JsonResponse(
            {"status": "error", "message": message}, status=409
        )
    logger.info(
        "scan %s: %s queued a redaction recompute", scan.pk, request.user
    )
    return JsonResponse({"status": "queued", "message": message}, status=202)


@login_required
@require_POST
def rerun_opinion_ensemble(
    request: HttpRequest, pk: int, opinion_pk: int
) -> JsonResponse:
    """Read this opinion's OCR documents again and write its text (#365).

    The "Re run OCR ensemble" button of review 3. The work is a few
    small S3 reads and a geometry over the pages of one opinion, so it
    runs here and not on the daemon: the rule of
    :func:`rebuild_findings`, whose twin ``compute_redactions_api``
    renders every page and therefore queues.

    It **waives the engine gate**. The daemon pass waits for
    ``OPINION_ENSEMBLE_MIN_ENGINES`` engine documents, which a volume
    read by two engines never holds. The button of the review page
    posts here, and the ``rerun_opinion_ensemble`` command runs the
    same work over a whole volume.

    :param request: The HTTP request.
    :param pk: Scan primary key.
    :param opinion_pk: The ``Opinion`` primary key.
    :return: JSON with the message, 404 for an opinion of another scan,
        or 409 when the OCR documents are not written or the row
        refuses to read.
    """
    from scanning import ensemble, opinion_ocr, s3_sync
    from scanning.models import OpinionReviewStatus

    scan = get_object_or_404(Scan, pk=pk)
    opinion = get_object_or_404(Opinion, pk=opinion_pk, scan=scan)
    if opinion.status == OpinionReviewStatus.TEXT_REVIEW_DONE:
        return JsonResponse(
            {"status": "error", "message": ENSEMBLE_APPROVED_MESSAGE},
            status=409,
        )
    if not opinion_ocr.is_written(opinion):
        return JsonResponse(
            {"status": "error", "message": ENSEMBLE_NOT_GLUED_MESSAGE},
            status=409,
        )
    if not s3_sync.s3_active():
        # The reads and the write are the bucket, the rule of
        # ``ensemble.run_tick``, which asks the same question first.
        return JsonResponse(
            {"status": "error", "message": ENSEMBLE_BUCKET_MESSAGE},
            status=409,
        )
    try:
        document = ensemble.rerun(opinion)
    except ensemble.RevisionMoved:
        # The OCR documents were written again while this ran, so the
        # rows went back. Nothing was kept, and the answer says so.
        return JsonResponse(
            {"status": "error", "message": ENSEMBLE_MOVED_MESSAGE},
            status=409,
        )
    except ensemble.TransientFault as exc:
        # A fault that passes: the curator presses the button again,
        # and no attempt was spent. The detail is logged, never sent.
        logger.warning(
            "%s of scan %s: the ensemble did not reach the bucket: %s",
            opinion,
            scan.pk,
            exc,
        )
        return JsonResponse(
            {"status": "error", "message": ENSEMBLE_BUCKET_MESSAGE},
            status=409,
        )
    except ensemble.EnsembleError as exc:
        # The line comes from the table above, by the code of the
        # error: the error itself names the object it read.
        logger.warning(
            "%s of scan %s: the ensemble refused the row: %s",
            opinion,
            scan.pk,
            exc,
        )
        return JsonResponse(
            {
                "status": "error",
                "message": ENSEMBLE_ERROR_MESSAGES.get(
                    exc.code, ENSEMBLE_ERROR_MESSAGES["unreadable"]
                ),
            },
            status=409,
        )
    logger.info(
        "%s of scan %s: %s ran the OCR ensemble again",
        opinion,
        scan.pk,
        request.user,
    )
    counts = document["counts"]
    return JsonResponse(
        {
            "status": "ok",
            "message": ENSEMBLE_RERUN_MESSAGE.format(
                engines=", ".join(document["engines"]),
                groups=counts["groups"],
                dropped=counts["dropped"],
                low=counts["low_confidence"],
            ),
        }
    )


def _opinion_finding_or_refusal(
    pk: int, opinion_pk: int, finding_pk: int
) -> tuple[Opinion, Any] | JsonResponse:
    """Return ``(opinion, finding)``, or the refusal of the request.

    A finding of another opinion, or of an opinion of another scan, is
    a 404. An opinion that is not ready for the text review refuses
    with 409 (#419): the gate lives here, in the view.

    :param pk: Scan primary key.
    :param opinion_pk: The ``Opinion`` primary key.
    :param finding_pk: The ``OpinionFinding`` primary key.
    :returns: The two rows, or the refusal.
    """
    from scanning.models import OpinionFinding, OpinionReviewStatus

    scan = get_object_or_404(Scan, pk=pk)
    opinion = get_object_or_404(Opinion, pk=opinion_pk, scan=scan)
    finding = get_object_or_404(OpinionFinding, pk=finding_pk, opinion=opinion)
    if opinion.status != OpinionReviewStatus.READY_FOR_TEXT_REVIEW:
        return JsonResponse(
            {"status": "error", "message": OPINION_FINDING_CLOSED_MESSAGE},
            status=409,
        )
    return opinion, finding


@login_required
@require_POST
def dismiss_opinion_finding(
    request: HttpRequest, pk: int, opinion_pk: int, finding_pk: int
) -> JsonResponse:
    """Dismiss a finding of review 3 (#419).

    One ``OpinionFindingDismissal`` row at the finding's address, and
    the card's FK set at once, so the card is muted with no rebuild.
    Any logged-in user may press it, the rule of review 1 and review 2.
    A dismissal is the answer to an ERROR card: the approval of the
    text waits on the open ones.

    :param request: The HTTP request.
    :param pk: Scan primary key.
    :param opinion_pk: The ``Opinion`` primary key.
    :param finding_pk: The ``OpinionFinding`` primary key.
    :return: ``{status, message}``; 404 for a finding of another
        opinion, 409 for a closed opinion or a stale finding.
    """
    from scanning import opinion_findings

    rows = _opinion_finding_or_refusal(pk, opinion_pk, finding_pk)
    if isinstance(rows, JsonResponse):
        return rows
    opinion, finding = rows
    try:
        opinion_findings.dismiss(opinion, finding, request.user)
    except opinion_findings.UndismissableOpinionFinding:
        return JsonResponse(
            {
                "status": "error",
                "message": OPINION_FINDING_UNDISMISSABLE_MESSAGE,
            },
            status=409,
        )
    logger.info(
        "%s of scan %s: %s dismissed the %s card of page %s",
        opinion,
        pk,
        request.user,
        finding.check_name,
        finding.page_in_opinion,
    )
    return JsonResponse({"status": "ok", "message": DISMISSED_FINDING_MESSAGE})


@login_required
@require_POST
def restore_opinion_finding(
    request: HttpRequest, pk: int, opinion_pk: int, finding_pk: int
) -> JsonResponse:
    """Take back the dismissal of a review-3 finding (#419).

    :param request: The HTTP request.
    :param pk: Scan primary key.
    :param opinion_pk: The ``Opinion`` primary key.
    :param finding_pk: The ``OpinionFinding`` primary key.
    :return: ``{status, message}``; 404 for a finding of another
        opinion, 409 for a closed opinion.
    """
    from scanning import opinion_findings

    rows = _opinion_finding_or_refusal(pk, opinion_pk, finding_pk)
    if isinstance(rows, JsonResponse):
        return rows
    opinion, finding = rows
    restored = opinion_findings.restore(opinion, finding)
    return JsonResponse(
        {
            "status": "ok",
            "message": (
                RESTORED_FINDING_MESSAGE
                if restored
                else STANDING_FINDING_MESSAGE
            ),
        }
    )


# ---------------------------------------------------------------------------
# The human edits of review 3 (#376)
# ---------------------------------------------------------------------------


def _edit_refusal(
    request: HttpRequest, message: str, status: int = 409
) -> JsonResponse:
    """Refuse one edit: an error message, and the JSON of the refusal.

    Every trip of an edit to the database answers the curator with a
    Django message (#376), and the viewer reloads the page to show it.

    :param request: The HTTP request.
    :param message: The line.
    :param status: The HTTP status.
    :returns: The answer.
    """
    messages.error(request, message)
    return JsonResponse({"status": "error", "message": message}, status=status)


def _edit_context(
    request: HttpRequest, pk: int, opinion_pk: int
) -> tuple[Opinion, dict, dict] | JsonResponse:
    """Return ``(opinion, body, document)``, or the refusal of the edit.

    The gates of every edit but the Undo, in order: the opinion of this
    scan, the status (``READY_FOR_TEXT_REVIEW`` alone, the gate of
    #419), a written ensemble, the bucket, the body, and the revisions
    the page was drawn at. The page sends the ``glue_revision`` and the
    ``edit_revision`` of the document it drew; a document that is not
    the stamped one, or an edit the stamped one does not hold yet, is a
    page the curator judged over another text.

    :param request: The HTTP request.
    :param pk: Scan primary key.
    :param opinion_pk: The ``Opinion`` primary key.
    :returns: The three, or the refusal.
    """
    from scanning import ensemble, s3_sync
    from scanning.models import OpinionReviewStatus

    opinion = (
        Opinion.objects.filter(pk=opinion_pk, scan_id=pk)
        .select_related("scan", "apply_run")
        .first()
    )
    if opinion is None:
        return _edit_refusal(request, EDIT_NOT_FOUND_MESSAGE, 404)
    if opinion.status != OpinionReviewStatus.READY_FOR_TEXT_REVIEW:
        return _edit_refusal(request, EDIT_CLOSED_MESSAGE)
    if not ensemble.is_written(opinion):
        return _edit_refusal(request, EDIT_NOT_WRITTEN_MESSAGE)
    if not s3_sync.s3_active():
        return _edit_refusal(request, ENSEMBLE_BUCKET_MESSAGE)
    try:
        body = json.loads(request.body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return _edit_refusal(request, EDIT_BAD_REQUEST_MESSAGE, 400)
    if not isinstance(body, dict):
        return _edit_refusal(request, EDIT_BAD_REQUEST_MESSAGE, 400)
    if opinion.edit_revision != opinion.ensemble_edit_revision:
        # An edit the text does not hold yet: its build failed. A new
        # edit judged over the text before it would land on a block the
        # curator cannot see as it will read.
        return _edit_refusal(request, EDIT_NOT_BUILT_YET_MESSAGE)
    if (
        body.get("glue_revision") != opinion.glue_revision
        or body.get("edit_revision") != opinion.ensemble_edit_revision
    ):
        return _edit_refusal(request, EDIT_STALE_PAGE_MESSAGE)
    try:
        document = ensemble.read_document(opinion)
    except ensemble.TransientFault:
        return _edit_refusal(request, ENSEMBLE_BUCKET_MESSAGE)
    except ensemble.EnsembleError:
        return _edit_refusal(request, EDIT_NOT_WRITTEN_MESSAGE)
    return opinion, body, document


def _edit_page(document: dict, body: dict) -> dict | None:
    """Return the page of the document the body names, or None."""
    wanted = body.get("page_in_opinion")
    if not isinstance(wanted, int) or isinstance(wanted, bool):
        return None
    return next(
        (
            page
            for page in document.get("pages") or []
            if page.get("page_in_opinion") == wanted
        ),
        None,
    )


def _edit_group(page: dict, body: dict) -> dict | None:
    """Return the group of the page the body names, or None."""
    wanted = body.get("group_id")
    if not isinstance(wanted, int) or isinstance(wanted, bool):
        return None
    return next(
        (group for group in page.get("groups") or [] if group["id"] == wanted),
        None,
    )


def _edit_target(
    request: HttpRequest, document: dict, body: dict, with_group: bool = True
) -> tuple[dict, dict | None, tuple] | JsonResponse:
    """Return ``(page, group, address)`` of an edit, or the refusal.

    :param request: The HTTP request.
    :param document: The stamped ensemble document.
    :param body: The request body.
    :param with_group: Whether the body must name a group.
    :returns: The three, or the refusal.
    """
    from scanning import ensemble

    page = _edit_page(document, body)
    if page is None:
        return _edit_refusal(request, EDIT_NO_BLOCK_MESSAGE)
    group = _edit_group(page, body) if with_group else None
    if with_group and group is None:
        return _edit_refusal(request, EDIT_NO_BLOCK_MESSAGE)
    address = ensemble._address(page)
    if address == (None, None):
        return _edit_refusal(request, EDIT_NO_ADDRESS_MESSAGE)
    return page, group, address


def _build_after_edit(request: HttpRequest, opinion: Opinion, saved: str):
    """Write the text again after an edit, and answer the curator.

    The build runs here, the rule of the "Read the OCR documents again"
    button: a few small reads and a geometry over one opinion. A build
    another write overtook is tried once more. A build that fails keeps
    the edit, which the database holds, and the daemon builds it
    (``ensemble.due``), so the answer is a warning and not an error.

    :param request: The HTTP request.
    :param opinion: The opinion.
    :param saved: The line of the success.
    :returns: The answer.
    """
    from scanning import ensemble

    reason = ""
    for _attempt in range(2):
        fresh = Opinion.objects.select_related("scan", "apply_run").get(
            pk=opinion.pk
        )
        try:
            ensemble.rerun(fresh)
        except ensemble.RevisionMoved:
            reason = ENSEMBLE_MOVED_MESSAGE
            continue
        except ensemble.TransientFault as exc:
            logger.warning(
                "%s of scan %s: the text after an edit did not reach the "
                "bucket: %s",
                opinion,
                opinion.scan_id,
                exc,
            )
            reason = ENSEMBLE_BUCKET_MESSAGE
            break
        except ensemble.EnsembleError as exc:
            logger.warning(
                "%s of scan %s: the text after an edit was refused: %s",
                opinion,
                opinion.scan_id,
                exc,
            )
            reason = ENSEMBLE_ERROR_MESSAGES.get(
                exc.code, ENSEMBLE_ERROR_MESSAGES["unreadable"]
            )
            break
        else:
            messages.success(request, saved)
            return JsonResponse({"status": "ok", "message": saved})
    warning = EDIT_NOT_BUILT_MESSAGE.format(reason=reason)
    messages.warning(request, warning)
    return JsonResponse({"status": "ok", "message": warning})


@login_required
@require_POST
def edit_opinion_text(
    request: HttpRequest, pk: int, opinion_pk: int
) -> JsonResponse:
    """Write the text of one block of an opinion (#376).

    Only a block the engines did not read alike takes a text edit:
    ``ensemble.disagreement_level`` is the rule, here and in the
    viewer, which only hides the button. The server copies the box,
    the section and the text the curator saw from the stamped document;
    the page sends the group id, the new text and the two revisions.

    :param request: The HTTP request.
    :param pk: Scan primary key.
    :param opinion_pk: The ``Opinion`` primary key.
    :return: ``{status, message}``; 404 for an opinion of another scan,
        400 for a body the page does not send, 409 for a refusal.
    """
    from scanning import ensemble, markup, opinion_edits
    from scanning.models import OpinionEdit

    context = _edit_context(request, pk, opinion_pk)
    if isinstance(context, JsonResponse):
        return context
    opinion, body, document = context
    target = _edit_target(request, document, body)
    if isinstance(target, JsonResponse):
        return target
    page, group, address = target
    if group.get("human"):
        return _edit_refusal(request, EDIT_HUMAN_ALREADY_MESSAGE)
    if group.get("level") is None:
        return _edit_refusal(request, EDIT_UNANIMOUS_MESSAGE)
    if group.get("kind") == markup.TABLE:
        return _edit_refusal(request, EDIT_TABLE_MESSAGE)
    text = body.get("text")
    if not isinstance(text, str):
        return _edit_refusal(request, EDIT_BAD_REQUEST_MESSAGE, 400)
    text = opinion_edits.fold(text)
    if not text:
        return _edit_refusal(request, EDIT_EMPTY_MESSAGE)
    if len(text) > opinion_edits.MAX_TEXT_CHARS:
        return _edit_refusal(
            request,
            EDIT_TOO_LONG_MESSAGE.format(limit=opinion_edits.MAX_TEXT_CHARS),
        )
    if text == group["text"]:
        return _edit_refusal(request, EDIT_UNCHANGED_MESSAGE)
    opinion_edits.supersede(
        opinion,
        request.user,
        kind=OpinionEdit.Kind.TEXT,
        source_edit_id=address[0],
        source_page=address[1],
        page_in_opinion=page["page_in_opinion"],
        box_pt=group["box_pt"],
        section=group.get("section") or ensemble.BODY,
        base_text=group["text"],
        text=text,
        glue_revision=opinion.glue_revision,
    )
    logger.info(
        "%s of scan %s: %s wrote the text of block %s of page %s",
        opinion,
        pk,
        request.user,
        group["id"],
        page["page_in_opinion"],
    )
    return _build_after_edit(request, opinion, EDIT_TEXT_SAVED_MESSAGE)


@login_required
@require_POST
def edit_opinion_section(
    request: HttpRequest, pk: int, opinion_pk: int
) -> JsonResponse:
    """Put one block of an opinion in the body or in the footnotes (#376).

    Every block takes it, a unanimous one too: the edit moves the
    block and not its words.

    :param request: The HTTP request.
    :param pk: Scan primary key.
    :param opinion_pk: The ``Opinion`` primary key.
    :return: ``{status, message}``; 404, 400 or 409 on a refusal.
    """
    from scanning import ensemble, opinion_edits
    from scanning.models import OpinionEdit

    context = _edit_context(request, pk, opinion_pk)
    if isinstance(context, JsonResponse):
        return context
    opinion, body, document = context
    target = _edit_target(request, document, body)
    if isinstance(target, JsonResponse):
        return target
    page, group, address = target
    section = body.get("section")
    if section not in (ensemble.BODY, ensemble.FOOTNOTES):
        return _edit_refusal(request, EDIT_BAD_REQUEST_MESSAGE, 400)
    if (group.get("section") or ensemble.BODY) == section:
        return _edit_refusal(request, EDIT_SAME_SECTION_MESSAGE)
    opinion_edits.supersede(
        opinion,
        request.user,
        kind=OpinionEdit.Kind.SECTION,
        source_edit_id=address[0],
        source_page=address[1],
        page_in_opinion=page["page_in_opinion"],
        box_pt=group["box_pt"],
        section=section,
        glue_revision=opinion.glue_revision,
    )
    logger.info(
        "%s of scan %s: %s put block %s of page %s in the %s",
        opinion,
        pk,
        request.user,
        group["id"],
        page["page_in_opinion"],
        section,
    )
    return _build_after_edit(
        request, opinion, EDIT_SECTION_SAVED_MESSAGE[section]
    )


@login_required
@require_POST
def move_opinion_block(
    request: HttpRequest, pk: int, opinion_pk: int
) -> JsonResponse:
    """Move one block of an opinion one place up or down (#376).

    The move is one ``ORDER`` edit of the block's section on its page:
    the boxes of the section in the order the page shows, with the
    block and its neighbour swapped. It supersedes the standing order
    of that section, so the list is always the whole order the curator
    saw.

    :param request: The HTTP request.
    :param pk: Scan primary key.
    :param opinion_pk: The ``Opinion`` primary key.
    :return: ``{status, message}``; 404, 400 or 409 on a refusal.
    """
    from scanning import ensemble, opinion_edits
    from scanning.models import OpinionEdit

    context = _edit_context(request, pk, opinion_pk)
    if isinstance(context, JsonResponse):
        return context
    opinion, body, document = context
    target = _edit_target(request, document, body)
    if isinstance(target, JsonResponse):
        return target
    page, group, address = target
    direction = body.get("direction")
    if direction not in EDIT_MOVE_SAVED_MESSAGE:
        return _edit_refusal(request, EDIT_BAD_REQUEST_MESSAGE, 400)
    section = group.get("section") or ensemble.BODY
    same = [
        other
        for other in page.get("groups") or []
        if (other.get("section") or ensemble.BODY) == section
    ]
    order = opinion_edits.swapped_order(
        same, group["id"], -1 if direction == "up" else 1
    )
    if order is None:
        return _edit_refusal(request, EDIT_EDGE_MESSAGE)
    opinion_edits.supersede(
        opinion,
        request.user,
        kind=OpinionEdit.Kind.ORDER,
        source_edit_id=address[0],
        source_page=address[1],
        page_in_opinion=page["page_in_opinion"],
        section=section,
        order=order,
        glue_revision=opinion.glue_revision,
    )
    logger.info(
        "%s of scan %s: %s moved block %s of page %s %s",
        opinion,
        pk,
        request.user,
        group["id"],
        page["page_in_opinion"],
        direction,
    )
    return _build_after_edit(
        request, opinion, EDIT_MOVE_SAVED_MESSAGE[direction]
    )


@login_required
@require_POST
def withdraw_opinion_edit(
    request: HttpRequest, pk: int, opinion_pk: int
) -> JsonResponse:
    """Undo one human edit of an opinion's text (#376).

    The row is withdrawn and never deleted, and the block goes back to
    the engines' text. It takes no revision from the page: the edit is
    named by its id, and an Undo of an edit that stands is the same
    Undo whatever the text around it.

    :param request: The HTTP request.
    :param pk: Scan primary key.
    :param opinion_pk: The ``Opinion`` primary key.
    :return: ``{status, message}``; 404, 400 or 409 on a refusal.
    """
    from scanning import opinion_edits, s3_sync
    from scanning.models import OpinionEdit, OpinionReviewStatus

    opinion = (
        Opinion.objects.filter(pk=opinion_pk, scan_id=pk)
        .select_related("scan", "apply_run")
        .first()
    )
    if opinion is None:
        return _edit_refusal(request, EDIT_NOT_FOUND_MESSAGE, 404)
    if opinion.status != OpinionReviewStatus.READY_FOR_TEXT_REVIEW:
        return _edit_refusal(request, EDIT_CLOSED_MESSAGE)
    if not s3_sync.s3_active():
        return _edit_refusal(request, ENSEMBLE_BUCKET_MESSAGE)
    try:
        body = json.loads(request.body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return _edit_refusal(request, EDIT_BAD_REQUEST_MESSAGE, 400)
    edit_id = body.get("edit_id") if isinstance(body, dict) else None
    if not isinstance(edit_id, int) or isinstance(edit_id, bool):
        return _edit_refusal(request, EDIT_BAD_REQUEST_MESSAGE, 400)
    edit = OpinionEdit.objects.filter(pk=edit_id, opinion=opinion).first()
    if edit is None:
        return _edit_refusal(request, EDIT_NO_EDIT_MESSAGE, 404)
    if not opinion_edits.withdraw(opinion, edit, request.user):
        return _edit_refusal(request, EDIT_STANDING_MESSAGE)
    logger.info(
        "%s of scan %s: %s undid the %s edit %s of page %s",
        opinion,
        pk,
        request.user,
        edit.kind,
        edit.pk,
        edit.page_in_opinion,
    )
    return _build_after_edit(request, opinion, EDIT_WITHDRAWN_MESSAGE)


@login_required
@require_POST
def generate_files(request: HttpRequest, pk: int) -> HttpResponse:
    """Refuse to generate opinion files while the pipeline is paused.

    File generation is post-review-1 processing, which issue #173
    stops until the new OCR stack reaches that stage. The old
    volume-level generation code is deleted (#360), and #206 rebuilds
    the step over the corrected volume and the ``Opinion`` rows, where
    ``opinion_pdf`` already writes one redacted PDF per opinion
    (#336). This view fails with the unified pipeline-paused message.

    The review-2 approval is the gate of step 3 (#263), and it is
    checked here first (#269), before the paused flash: a template gate
    alone cannot refuse a direct POST, the rule ``start_detect`` follows
    for review 1. A legacy volume (``PENDING_REVIEW``) never holds the
    approval and keeps its way in. The order holds the day #206
    connects the generation.

    :param request: The HTTP request.
    :param pk: Scan primary key.
    :return: Redirect to the scan processing page.
    """
    scan = get_object_or_404(Scan, pk=pk)
    if scan.status not in (
        Status.REDACTION_REVIEW_DONE,
        Status.PENDING_REVIEW,
    ):
        messages.warning(request, GENERATE_REQUIRES_REDACTION_REVIEW_MESSAGE)
        return redirect(
            f"{reverse('scan_process', kwargs={'pk': scan.pk})}?step=2"
        )
    messages.warning(request, PIPELINE_PAUSED_MESSAGE)
    return redirect("scan_process", pk=scan.pk)


@login_required
@require_POST
def approve_scan(request: HttpRequest, pk: int) -> HttpResponse:
    """Mark a scan as approved.

    A pure status flip: it asks for ``Stage.APPROVED`` from step 3,
    then writes ``status=APPROVED``. No scan reaches that stage while
    the step is paused (#173), and the S3 copy the approval once made
    went with the old generation code (#360). What an approval means
    over the ``Opinion`` rows is #206's question.

    :param request: The HTTP request.
    :param pk: Scan primary key.
    :return: Redirect to the process page.
    """
    from scanning.services import refresh_volume_queue_status_for_scan

    scan = get_object_or_404(Scan, pk=pk)

    if scan.stage != Stage.APPROVED:
        messages.error(
            request, "Before approving you need to generate the files."
        )
        return redirect("scan_process", pk=scan.pk)

    scan.status = Status.APPROVED
    scan.save(update_fields=["status"])
    refresh_volume_queue_status_for_scan(scan)
    messages.success(request, "Scan approved.")
    return redirect("scan_process", pk=scan.pk)


def _resolve_opinion_pdf(
    field: Any, opinion: OpinionScan
) -> tuple[int, datetime, Any] | None:
    """Locate an OpinionScan PDF and return its size, mtime, and opener.

    The FileField name is either a MEDIA_ROOT-relative path (DEV,
    resolves via the storage backend) or a scan.output_dir-relative
    path (prod, file lives under /tmp/scanning/{pk}/...). Try the
    storage backend first; fall back to resolving against the scan's
    output_dir, with a lazy S3 pull as a last resort.

    :param field: The FileField from the OpinionScan instance.
    :param opinion: The OpinionScan instance.
    :returns: ``(size_bytes, mtime, opener)`` where ``opener`` is a
        zero-arg callable returning an open binary file handle, or
        ``None`` if the file cannot be located.
    :rtype: tuple[int, datetime, Any] | None
    """
    if field.storage.exists(field.name):
        size = field.storage.size(field.name)
        mtime = field.storage.get_modified_time(field.name)
        return size, mtime, lambda: field.open("rb")

    if opinion.scan:
        candidate = Path(opinion.scan.output_dir) / field.name
        if not candidate.is_file():
            try:
                from scanning import s3_sync

                # Just this file: see s3_sync.download_processing_file for
                # why pulling the whole prefix here hangs the process.
                s3_sync.download_processing_file(opinion.scan, field.name)
            except Exception:
                logger.exception(
                    "Lazy S3 pull failed for opinion %s", opinion.pk
                )
        if candidate.is_file():
            stat = candidate.stat()
            mtime = datetime.fromtimestamp(stat.st_mtime, tz=UTC)
            return stat.st_size, mtime, lambda: candidate.open("rb")

    return None


@login_required
def serve_opinionscan_pdf(
    request: HttpRequest, pk: int, variant: str
) -> FileResponse | HttpResponse:
    """Serve a PDF for an OpinionScan by variant (redacted/original).

    The file path comes from the model field, not from user input. The
    response carries ``ETag`` / ``Last-Modified`` / ``Cache-Control:
    no-cache`` so the browser always revalidates but skips re-downloading
    the file when its cached copy is still fresh (returns 304). The
    ``ETag`` is derived from the file's mtime and size, so it
    invalidates automatically when a redaction edit rewrites the file.

    :param request: The HTTP request.
    :param pk: OpinionScan primary key.
    :param variant: One of 'redacted' or 'original'.
    :return: File response streaming the PDF, or a 304 Not Modified
        response when the client's cached copy is still fresh.
    :rtype: FileResponse | HttpResponse
    """
    opinion = get_object_or_404(OpinionScan, pk=pk)
    field_map = {
        "redacted": opinion.redacted_pdf,
        "original": opinion.original_pdf,
    }
    field = field_map.get(variant)
    if not field or not field.name:
        raise Http404

    resolved = _resolve_opinion_pdf(field, opinion)
    if resolved is None:
        raise Http404
    size, mtime, opener = resolved

    last_modified = int(mtime.timestamp())
    etag = f'"{last_modified}-{size}"'
    conditional = get_conditional_response(
        request, etag=etag, last_modified=last_modified
    )
    if conditional is not None:
        response: FileResponse | HttpResponse = conditional
    else:
        response = FileResponse(opener(), content_type="application/pdf")
    response["Cache-Control"] = "private, no-cache"
    response["ETag"] = etag
    response["Last-Modified"] = http_date(last_modified)
    return response


def _apply_rect_to_pdf(
    pdf_path: str,
    page_index: int,
    x0: float,
    y0: float,
    x1: float,
    y1: float,
    fill: str,
) -> None:
    """Apply a redaction rectangle directly to a PDF file on disk.

    :param pdf_path: Filesystem path to the PDF to modify.
    :param page_index: Zero-based page index.
    :param x0: Left coordinate of the rectangle.
    :param y0: Top coordinate of the rectangle.
    :param x1: Right coordinate of the rectangle.
    :param y1: Bottom coordinate of the rectangle.
    :param fill: Fill color, either ``"black"`` or ``"white"``.
    :return: None.
    """
    with fitz.open(pdf_path) as doc:
        if page_index < 0 or page_index >= doc.page_count:
            raise ValueError(
                f"Page index {page_index} out of range (0-{doc.page_count - 1})"
            )
        page = doc.load_page(page_index)
        rect = fitz.Rect(x0, y0, x1, y1)
        color = (0, 0, 0) if fill == "black" else (1, 1, 1)
        page.add_redact_annot(rect, fill=color)
        page.apply_redactions()
        # Save to temp file then move -- fitz can't save to the same path it opened
        fd, tmp_path = tempfile.mkstemp(
            suffix=".pdf", dir=os.path.dirname(pdf_path)
        )
        os.close(fd)
        doc.save(tmp_path, garbage=3, deflate=True)
    shutil.move(tmp_path, pdf_path)


@login_required
@require_POST
def apply_rect_to_opinion(
    request: HttpRequest, pk: int, opinion_pk: int
) -> JsonResponse:
    """Apply a redaction rectangle to an opinion's redacted PDF.

    :param request: The HTTP request (JSON body with page_index,
        x0, y0, x1, y1, and fill).
    :param pk: Scan primary key.
    :param opinion_pk: OpinionScan primary key.
    :return: JSON response confirming the operation.
    """
    scan = get_object_or_404(Scan, pk=pk)
    if (refusal := _refuse_closed_review(scan)) is not None:
        return refusal
    opinion = get_object_or_404(OpinionScan, pk=opinion_pk, scan=scan)
    data = _parse_json_body(request)
    if isinstance(data, JsonResponse):
        return data
    page_index = data["page_index"]
    x0, y0, x1, y1 = data["x0"], data["y0"], data["x1"], data["y1"]
    fill = data.get("fill", "black")

    # Always apply to the redacted PDF
    redacted_path = os.path.join(
        scan.output_dir,
        "redacted",
        os.path.basename(opinion.redacted_pdf.name),
    )
    if os.path.isfile(redacted_path):
        _apply_rect_to_pdf(redacted_path, page_index, x0, y0, x1, y1, fill)

    return JsonResponse({"status": "ok"})


@login_required
def serve_redacted_pdf(
    request: HttpRequest, pk: int
) -> FileResponse | HttpResponse:
    """Serve the redacted PDF for a scan.

    Falls back to the processing PDF if the redacted version hasn't been
    generated yet, so the viewer has something to display.

    :param request: The HTTP request.
    :param pk: Scan primary key.
    :return: File response streaming the PDF.
    """
    scan = get_object_or_404(Scan, pk=pk)

    # 1. Try the explicit redacted PDF path
    if scan.redacted_pdf_path and os.path.isfile(scan.redacted_pdf_path):
        return FileResponse(
            Path(scan.redacted_pdf_path).open("rb"),
            content_type="application/pdf",
        )

    # 2. Fall back to the processing PDF (for preview before generation)
    output = Path(scan.output_dir)
    if output.is_dir():
        base_pdf = find_processing_pdf(output)
        if base_pdf:
            return FileResponse(
                base_pdf.open("rb"), content_type="application/pdf"
            )

    # 3. Lazy S3 pull + retry: handles the case where the daemon just
    # finished Generate Files and this container's /tmp/ is stale. Pull only
    # the file being served -- the whole prefix runs to gigabytes on a full
    # volume, and every sync view shares one executor under ASGI.
    try:
        from scanning import s3_sync

        if scan.redacted_pdf_path:
            s3_sync.download_processing_file(
                scan, os.path.basename(scan.redacted_pdf_path)
            )
        else:
            s3_sync.download_preview_pdf(scan)
    except Exception:
        logger.exception("Lazy S3 pull failed for scan %s", scan.pk)
    if scan.redacted_pdf_path and os.path.isfile(scan.redacted_pdf_path):
        return FileResponse(
            Path(scan.redacted_pdf_path).open("rb"),
            content_type="application/pdf",
        )
    if output.is_dir():
        base_pdf = find_processing_pdf(output)
        if base_pdf:
            return FileResponse(
                base_pdf.open("rb"), content_type="application/pdf"
            )

    return HttpResponse("No PDF available", status=404)


@login_required
def serve_ocr_results(request: HttpRequest, pk: int) -> JsonResponse:
    """Return OCR page-number results for a scan as JSON.

    :param request: The HTTP request.
    :param pk: Scan primary key.
    :return: JSON response with a list of OCR result dicts.
    """
    scan = get_object_or_404(Scan, pk=pk)
    if scan.ocr_results:
        return JsonResponse(scan.ocr_results, safe=False)
    return JsonResponse([], safe=False)


#: The 409 of a dismissal asked for a finding that takes none (#240 PR D).
FINDING_UNDISMISSABLE_MESSAGE = (
    "This finding cannot be dismissed. It says that one of your own "
    "decisions is not applied: withdraw that decision instead, or make "
    "it again on the page as it is now."
)

#: The 409 of a dismissal whose target has no source page (#240 PR D):
#: a detection row from before the address existed, or a run of pages
#: that ends outside the apply run's map.
FINDING_UNADDRESSABLE_MESSAGE = (
    "This finding cannot be addressed in the current volume, so the "
    "dismissal cannot be kept. It goes away when the rows are imported "
    "again; if it stays, ask a staff member."
)

#: The 404 of a finding the rebuild has replaced under the viewer.
FINDING_GONE_MESSAGE = (
    "This finding is not there any more; the list was rebuilt. Reload "
    "the page."
)


def _finding_or_404(scan: Scan, data: dict):
    """Return the review-2 finding ``data`` names, or the 404 response.

    :param scan: The scan.
    :param data: The parsed body, with ``issue_id``.
    :returns: The row, or a ``JsonResponse``.
    """
    row = Issue.objects.filter(
        pk=data.get("issue_id"), scan=scan, check_name__in=REVIEW2_CHECKS
    ).first()
    if row is None:
        return JsonResponse(
            {"status": "error", "message": FINDING_GONE_MESSAGE}, status=404
        )
    return row


@login_required
@require_POST
def dismiss_finding(request: HttpRequest, pk: int) -> JsonResponse:
    """Dismiss a finding of review 2 (#240 PR D).

    One ``ReviewDismissal`` row at the finding's address, and the
    finding's FK set at once, so the card is muted with no rebuild. Any
    logged-in user may press it (the #151 rule). A stale finding is
    refused: the way out of one is to withdraw the decision it names.

    :param request: The HTTP request (JSON body with ``issue_id``).
    :param pk: Scan primary key.
    :return: ``{status, dismissal_id, message}``; 404 when the row is
        gone, 409 when the finding takes no dismissal.
    """
    from scanning import findings

    scan = get_object_or_404(Scan, pk=pk)
    if (refusal := _refuse_closed_review(scan)) is not None:
        return refusal
    data = _parse_json_body(request)
    if isinstance(data, JsonResponse):
        return data
    row = _finding_or_404(scan, data)
    if isinstance(row, JsonResponse):
        return row
    try:
        dismissal = findings.dismiss(scan, row, request.user)
    except findings.UndismissableFinding:
        return JsonResponse(
            {"status": "error", "message": FINDING_UNDISMISSABLE_MESSAGE},
            status=409,
        )
    except findings.UnaddressableFinding:
        return JsonResponse(
            {"status": "error", "message": FINDING_UNADDRESSABLE_MESSAGE},
            status=409,
        )
    return JsonResponse(
        {
            "status": "ok",
            "dismissal_id": dismissal.pk,
            "message": DISMISSED_FINDING_MESSAGE,
        }
    )


@login_required
@require_POST
def restore_finding(request: HttpRequest, pk: int) -> JsonResponse:
    """Take back the dismissal of a finding: the card comes back.

    :param request: The HTTP request (JSON body with ``issue_id``).
    :param pk: Scan primary key.
    :return: ``{status, restored, message}``.
    """
    from scanning import findings

    scan = get_object_or_404(Scan, pk=pk)
    if (refusal := _refuse_closed_review(scan)) is not None:
        return refusal
    data = _parse_json_body(request)
    if isinstance(data, JsonResponse):
        return data
    row = _finding_or_404(scan, data)
    if isinstance(row, JsonResponse):
        return row
    restored = findings.restore(scan, row, request.user)
    return JsonResponse(
        {
            "status": "ok",
            "restored": restored,
            "message": (
                RESTORED_FINDING_MESSAGE
                if restored
                else STANDING_FINDING_MESSAGE
            ),
        }
    )


@login_required
@require_POST
def withdraw_stale_edit(request: HttpRequest, pk: int) -> JsonResponse:
    """Withdraw the curator row a stale finding names, and rebuild.

    The one way out of a ``stale_*`` finding: the decision the compute
    could not land or place is taken back, as its own endpoint would
    take it back, and the findings are written again.

    :param request: The HTTP request (JSON body with ``issue_id``).
    :param pk: Scan primary key.
    :return: ``{status, withdrawn, message}``; 409 when the finding
        names no row.
    """
    from scanning import findings

    scan = get_object_or_404(Scan, pk=pk)
    if (refusal := _refuse_closed_review(scan)) is not None:
        return refusal
    data = _parse_json_body(request)
    if isinstance(data, JsonResponse):
        return data
    row = _finding_or_404(scan, data)
    if isinstance(row, JsonResponse):
        return row
    try:
        withdrawn = findings.withdraw_stale(scan, row, request.user)
    except findings.NotAStaleFinding:
        return JsonResponse(
            {
                "status": "error",
                "message": "This finding names no decision to withdraw.",
            },
            status=409,
        )
    return JsonResponse(
        {
            "status": "ok",
            "withdrawn": withdrawn,
            "message": (
                WITHDRAWN_DECISION_MESSAGE
                if withdrawn
                else STANDING_DECISION_MESSAGE
            ),
        }
    )


def _findings_payload(scan: Scan, request: HttpRequest) -> dict:
    """Render the step-2 findings section and its two counts.

    One context for one template, whichever view answers
    (``findings.viewer_groups``): the section the page renders and the
    section a refresh swaps in must agree, or a card would offer a
    button the endpoint refuses.

    :param scan: The scan.
    :param request: The HTTP request, for the template context.
    :return: ``{html, open, stale}``.
    """
    from scanning import findings

    context = findings.viewer_groups(scan)
    return {
        "html": render_to_string(
            "scanning/_review_findings.html", context, request=request
        ),
        "open": context["review2_open"],
        "stale": context["review2_stale"],
    }


@login_required
def review_findings(request: HttpRequest, pk: int) -> JsonResponse:
    """Render the step-2 findings section as an HTML fragment.

    The viewer swaps the section after every write.

    :param request: The HTTP request.
    :param pk: Scan primary key.
    :return: ``{html, open, stale}``.
    """
    scan = get_object_or_404(Scan, pk=pk)
    return JsonResponse(_findings_payload(scan, request))


@login_required
@require_POST
def rebuild_findings(request: HttpRequest, pk: int) -> JsonResponse:
    """Write the review-2 findings again, here, and answer the section.

    The "Recompute" button of the findings panel (#305), and the twin
    of review 1's ``recalculate``. ``findings.rebuild`` derives every
    finding from the ``Detection``, ``OpinionBoundary``, ``Redaction``
    and decision rows, plus ``ApplyRun.page_map``, which is a column.
    No S3 read and no page render, so it runs in the request on a web
    pod that never pulled the volume's files (the #153 rule).

    It changes no box. A finding that only a measurement can answer --
    a caption the curator drew that no opinion boundary names yet --
    needs :func:`compute_redactions_api` instead.

    Every logged-in user may press it: review 2 is a curator's step,
    not a staff one (#151). No review gate either: the rebuild is
    derived from the rows and is idempotent, so a volume whose rows
    cannot change gets the findings it already had.

    **A busy volume is refused.** The compute writes the findings
    itself, against the run it measured in, and stamps that run on the
    ledger afterwards; a rebuild in that window reads the stamp of the
    space before it and writes the cards against the wrong one. The
    sidebar shows the progress panel there and offers no button, and a
    gate lives in the view, not only in the template.

    :param request: The HTTP request.
    :param pk: Scan primary key.
    :return: ``{status, html, open, stale, message}``; 409 while the
        volume is busy.
    """
    from scanning import findings

    scan = get_object_or_404(Scan, pk=pk)
    if (refusal := _refuse_closed_review(scan)) is not None:
        return refusal
    if scan.status in BUSY_STATUSES:
        return JsonResponse(
            {"status": "error", "message": FINDINGS_BUSY_MESSAGE}, status=409
        )
    findings.rebuild(scan)
    logger.info(
        "scan %s: %s rebuilt the review-2 findings", scan.pk, request.user
    )
    return JsonResponse(
        {
            "status": "ok",
            "message": REBUILT_FINDINGS_MESSAGE,
            **_findings_payload(scan, request),
        }
    )


@login_required
@require_POST
def delete_detection(request: HttpRequest, pk: int) -> JsonResponse:
    """Take a detection out of the volume.

    A model row gets a ``deactivate`` decision (#240): the row is
    deleted and written again at the next import, and the decision is
    what carries the curator's choice onto the new row. A hand-drawn
    row is withdrawn, and gives back the model box it replaced, if any.
    Nothing is deleted.

    :param request: The HTTP request (JSON body with ``detection_id``
        (int, DB pk)).
    :param pk: Scan primary key.
    :return: JSON response with ``deleted`` count and the ``message``
        the viewer shows (#322), or 404 if not found.
    """
    from scanning import detections

    scan = get_object_or_404(Scan, pk=pk)
    if (refusal := _refuse_closed_review(scan)) is not None:
        return refusal
    data = _parse_json_body(request)
    if isinstance(data, JsonResponse):
        return data
    row = Detection.objects.filter(pk=data["detection_id"], scan=scan).first()
    if row is None:
        return JsonResponse(
            {"status": "error", "message": "Detection not found"}, status=404
        )
    manual = row.model_name == Detection.ModelName.MANUAL
    if manual:
        detections.withdraw_manual(row, request.user)
    else:
        try:
            detections.decide(
                scan, row, DetectionDecision.Kind.DEACTIVATE, request.user
            )
        except detections.UnaddressableDetection:
            return _unaddressable()
    _rebuild_findings(scan)
    return JsonResponse(
        {
            "status": "ok",
            "deleted": 1,
            "message": (
                WITHDRAWN_DETECTION_MESSAGE
                if manual
                else DISMISSED_DETECTION_MESSAGE
            ),
        }
    )


@login_required
@require_POST
def update_detection(request: HttpRequest, pk: int) -> JsonResponse:
    """Move or resize a detection box.

    A hand-drawn row is the curator's own and is written in place. A
    model row is not written (#240): it is deactivated by a decision
    and a hand-drawn row is created where the curator put the box, so
    the move survives the next import. The response names the row that
    now holds the box, and the viewer must address that one from then
    on.

    :param request: The HTTP request (JSON body with ``detection_id``
        (int, DB pk) and ``new_bbox`` (list[float], ``[x0,y0,x1,y1]``)).
    :param pk: Scan primary key.
    :return: JSON response with ``updated`` count, ``detection_id`` and
        the ``message`` the viewer shows (#322), or 404 if not found.
    """
    from scanning import detections

    scan = get_object_or_404(Scan, pk=pk)
    if (refusal := _refuse_closed_review(scan)) is not None:
        return refusal
    data = _parse_json_body(request)
    if isinstance(data, JsonResponse):
        return data
    new_bbox = data["new_bbox"]
    row = Detection.objects.filter(pk=data["detection_id"], scan=scan).first()
    if row is None:
        return JsonResponse(
            {"status": "error", "message": "Detection not found"}, status=404
        )
    if row.model_name == Detection.ModelName.MANUAL:
        Detection.objects.filter(pk=row.pk).update(
            x0=new_bbox[0], y0=new_bbox[1], x1=new_bbox[2], y1=new_bbox[3]
        )
        holder = row
    else:
        try:
            holder = detections.move_model_row(
                scan, row, new_bbox, request.user
            )
        except detections.UnaddressableDetection:
            return _unaddressable()
    _rebuild_findings(scan)
    return JsonResponse(
        {
            "status": "ok",
            "updated": 1,
            "detection_id": holder.pk,
            "message": (
                MOVED_BOX_MESSAGE
                if holder.pk == row.pk
                else MOVED_OVER_MODEL_MESSAGE
            ),
        }
    )


#: The 409 of a decision about a box no address can be written for
#: (``detections.UnaddressableDetection``).
DETECTION_UNADDRESSABLE_MESSAGE = (
    "This box cannot be addressed in the current volume, so the change "
    "cannot be kept. Reload the page; if it stays, ask a staff member."
)


def _unaddressable() -> JsonResponse:
    """Return the 409 for a refused decision.

    :returns: The response.
    """
    return JsonResponse(
        {"status": "error", "message": DETECTION_UNADDRESSABLE_MESSAGE},
        status=409,
    )


#: How far, in image pixels, a drawn box may sit from a model box and
#: still mean "that one": the add endpoint then approves the model box
#: rather than draw a second one over it.
BOOST_TOLERANCE_PX = 15


@login_required
@require_POST
def add_single_detection(request: HttpRequest, pk: int) -> JsonResponse:
    """Add a detection by hand, or approve the model box it lands on.

    A box drawn within ``BOOST_TOLERANCE_PX`` of a live model box with
    the same label is that box, and the model box is approved: a move
    by zero (#414, ``detections.approve_model_row``), so the model row
    gets a dismiss and a hand-drawn row at the same box holds it now.
    Otherwise a hand-drawn row is written, addressed by its source
    page. The rows are the only store; nothing here reads or writes a
    file.

    :param request: The HTTP request (JSON body with page_index,
        label_id, bbox, img_width, and img_height).
    :param pk: Scan primary key.
    :return: JSON response with ``added=True`` and the new row's
        ``detection_id`` if new; ``added=False``, the ``detection_id``
        of the hand-drawn row that holds the box now and the
        ``replaced_id`` of the row the drawing landed on if an existing
        detection was approved. Both carry the ``message`` the viewer
        shows (#322).
    """
    from blackletter.models import Label

    from scanning import detections

    scan = get_object_or_404(Scan, pk=pk)
    if (refusal := _refuse_closed_review(scan)) is not None:
        return refusal
    det = _parse_json_body(request)
    if isinstance(det, JsonResponse):
        return det
    try:
        page_index = int(det["page_index"])
        label_id = int(det["label_id"])
        bbox = [float(v) for v in det["bbox"]]
        if len(bbox) != 4:
            raise ValueError("bbox needs four numbers")
        label_name = Label(label_id).name
    except (KeyError, TypeError, ValueError):
        # The detail goes to the log, not to the browser (CodeQL).
        logger.warning(
            "add_single_detection: scan %s: malformed body", pk, exc_info=True
        )
        return JsonResponse(
            {
                "error": "Bad detection: page_index, label_id and a bbox "
                "of four numbers are required"
            },
            status=400,
        )

    # Any live row, hand-drawn ones included: a second click on the
    # curator's own box must be a no-op, not a second box over it.
    near = (
        Detection.objects.live()
        .filter(
            scan=scan,
            page_index=page_index,
            label_id=label_id,
            x0__gte=bbox[0] - BOOST_TOLERANCE_PX,
            x0__lte=bbox[0] + BOOST_TOLERANCE_PX,
            y0__gte=bbox[1] - BOOST_TOLERANCE_PX,
            y0__lte=bbox[1] + BOOST_TOLERANCE_PX,
        )
        .order_by("pk")
        .first()
    )
    if near is not None:
        message = STANDING_DETECTION_MESSAGE
        holder = near
        if near.model_name != Detection.ModelName.MANUAL:
            # A box drawn over a model box approves it (#414): the
            # model row gets a dismiss and the curator a hand-drawn row
            # at the same box, and ``detection_id`` names that row.
            message = _approval_message(near, manual=False)
            try:
                holder = detections.approve_model_row(scan, near, request.user)
            except detections.UnaddressableDetection:
                return _unaddressable()
        _rebuild_findings(scan)
        return JsonResponse(
            {
                "status": "ok",
                "added": False,
                "detection_id": holder.pk,
                "replaced_id": near.pk,
                "message": message,
            }
        )
    run = detections.measured_run(scan)
    try:
        row = detections.add_manual(
            scan,
            page_index,
            label_name,
            label_id,
            bbox,
            int(det.get("img_width") or 0),
            int(det.get("img_height") or 0),
            run=run,
        )
    except detections.UnaddressableDetection:
        return _unaddressable()
    _rebuild_findings(scan)
    return JsonResponse(
        {
            "status": "ok",
            "added": True,
            "detection_id": row.pk,
            "message": (
                ADDED_ANCHOR_DETECTION_MESSAGE
                if label_name in PAIRING_LABELS
                else ADDED_DETECTION_MESSAGE
            ),
        }
    )


def _approval_message(row: Detection, manual: bool) -> str:
    """Return what an approval of ``row`` changed, for the viewer (#322).

    A bracket box is approved from its ``low_confidence_headnote_bracket``
    card (#410): the hand-drawn row reads 1.0, over the redaction gate,
    so the card goes now and the next compute redacts it. Every other
    approval waits for the next pairing.
    """
    from blackletter.models import Label

    if manual:
        return OWN_DETECTION_MESSAGE
    if row.label == Label.HEADNOTE_BRACKET.name:
        return APPROVED_BRACKET_MESSAGE
    return APPROVED_DETECTION_MESSAGE


@login_required
@require_POST
def approve_detection(request: HttpRequest, pk: int) -> JsonResponse:
    """Approve a detection: the box becomes the curator's own.

    An approval is a move by zero (#414, ``detections.approve_model_row``):
    the model row gets a dismiss, and a hand-drawn row at the same box
    reads 1.0 and survives every import. The response names that row,
    and the viewer addresses it from then on, as after a move. A
    hand-drawn row is the curator's already and needs none, and the
    message says so (#322): this view writes nothing for one.

    :param request: The HTTP request (JSON body with ``detection_id``
        (int, DB pk)).
    :param pk: Scan primary key.
    :return: JSON response with ``updated`` count, ``detection_id`` of
        the row that holds the box now, ``replaced_id`` of the row the
        request named and the ``message`` the viewer shows (#322), or
        404 if not found.
    """
    from scanning import detections

    scan = get_object_or_404(Scan, pk=pk)
    if (refusal := _refuse_closed_review(scan)) is not None:
        return refusal
    data = _parse_json_body(request)
    if isinstance(data, JsonResponse):
        return data
    row = Detection.objects.filter(pk=data["detection_id"], scan=scan).first()
    if row is None:
        return JsonResponse(
            {"status": "error", "message": "Detection not found"}, status=404
        )
    manual = row.model_name == Detection.ModelName.MANUAL
    holder = row
    if not manual:
        try:
            holder = detections.approve_model_row(scan, row, request.user)
        except detections.UnaddressableDetection:
            return _unaddressable()
    _rebuild_findings(scan)
    return JsonResponse(
        {
            "status": "ok",
            "updated": 1,
            "detection_id": holder.pk,
            "replaced_id": row.pk,
            "message": _approval_message(row, manual),
        }
    )


@login_required
@require_POST
def bake_redactions(request: HttpRequest, pk: int) -> JsonResponse:
    """Bake pending redaction rectangles into the scan PDF (no-op stub).

    :param request: The HTTP request.
    :param pk: Scan primary key.
    :return: JSON response with the bake result.
    """
    scan = get_object_or_404(Scan, pk=pk)
    if (refusal := _refuse_closed_review(scan)) is not None:
        return refusal
    if not Path(scan.output_dir).is_dir():
        return JsonResponse(
            {"status": "error", "message": "No output dir"}, status=400
        )
    return JsonResponse(
        {"status": "ok", "message": "No redactions to bake", "count": 0}
    )


#: The 409 of ``export_pdf``: the standing page edits cannot be built
#: into a volume. The fault itself is logged, never sent.
EXPORT_NOT_BUILDABLE_MESSAGE = (
    "The corrected PDF cannot be built from the page edits as they "
    "stand. Check the step-1 page changes, or ask a staff member."
)


def _unlink_quietly(path: str) -> None:
    """Remove a temp file, and swallow a file that is already gone.

    :param path: The file.
    :return: None.
    """
    try:
        os.unlink(path)
    except OSError:
        pass


@login_required
def export_pdf(
    request: HttpRequest, pk: int
) -> StreamingHttpResponse | HttpResponse:
    """Export the corrected PDF, as the apply builds it.

    Reads the curator's decisions off the ``PageEdit`` rows (#214), in
    the physical space of the original, and runs the walk the apply
    runs (``apply.build_final_pdf``, #224): a page marked for deletion
    is dropped, an uploaded page stands in for a replaced one, a
    rotated page is turned, and each inserted file is placed in the gap
    its row names. The export and the final PDF are one walk, so what
    a curator downloads is what the apply builds.

    :param request: The HTTP request.
    :param pk: Scan primary key.
    :return: PDF file download response, a 404 when the original PDF
        cannot be made available locally, or a 409 naming the fault when
        the rows cannot be built into a volume.
    """
    from scanning import apply

    scan = get_object_or_404(Scan, pk=pk)
    # Resolve the source PDF before the temp file exists, so a missing
    # original is a clean 404 rather than a leaked temp file.
    original = local_original_pdf(scan)
    if not original:
        return HttpResponse(status=404)
    tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
    tmp.close()
    tmp_path = tmp.name
    try:
        with fitz.open(original) as pdf_doc:
            # The plan is built over ``Scan.page_count``, so a row that
            # disagrees with the file would address a page the walk
            # cannot reach and raise inside ``build_final_pdf``. The
            # old walk clamped every index; this reads the file, which
            # is what ``apply._build`` checks too.
            scan.page_count = pdf_doc.page_count
        # Each uploaded file is read once, for the plan and the walk.
        files, counts = apply.preload_edit_files(scan)
        plan = apply.plan_run(scan, counts)
        with fitz.open(original) as source:
            with apply.build_final_pdf(
                source, plan, read_file=lambda edit: files[edit.pk]
            ) as pdf_doc:
                pdf_doc.save(tmp_path)
    except apply.ApplyError as exc:
        # The rows do not build into a volume: a shard with fewer pages
        # than the map asks for, a file that is gone. The apply counts
        # the same fault on its run. The detail goes to the log and not
        # to the browser (CodeQL: an exception's text is not for an
        # external user); the answer says what to do.
        _unlink_quietly(tmp_path)
        logger.warning(
            "export_pdf: scan %s: the page edits do not build: %s", pk, exc
        )
        return HttpResponse(
            EXPORT_NOT_BUILDABLE_MESSAGE, status=409, content_type="text/plain"
        )
    except Exception:
        _unlink_quietly(tmp_path)
        raise

    filename = f"{scan.reporter.short_name}_{scan.volume}_corrected.pdf"

    def _stream_and_cleanup(path: str, chunk_size: int = 64 * 1024):
        try:
            with open(path, "rb") as fh:
                while True:
                    chunk = fh.read(chunk_size)
                    if not chunk:
                        break
                    yield chunk
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass

    response = StreamingHttpResponse(
        _stream_and_cleanup(tmp_path), content_type="application/pdf"
    )
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response
