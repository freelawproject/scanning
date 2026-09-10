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
from django.urls import reverse
from django.utils.cache import get_conditional_response
from django.utils.http import http_date
from django.views.decorators.http import require_POST

from scanning.models import (
    Detection,
    DetectionDecision,
    Issue,
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
    rows, and the recompute is off until #211, so the endpoint that
    changed a row is what keeps the cards true. A few queries over
    label-filtered rows.

    :param scan: The scan.
    :return: None.
    """
    from scanning import findings

    findings.rebuild(scan)


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

    :param request: The HTTP request.
    :param pk: Scan primary key.
    :return: JSON response with a list of detection dicts.
    """
    scan = get_object_or_404(Scan, pk=pk)
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
            # The standing curator decision on a model row (#240):
            # "approve" here is why the confidence reads 1.0.
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
    from scanning import boundaries

    scan = get_object_or_404(Scan, pk=pk)
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
        addition; 404 when the row is not the scan's.
    """
    from scanning import boundaries

    scan = get_object_or_404(Scan, pk=pk)
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
    :return: ``restored`` says whether a dismissal stood.
    """
    from scanning import boundaries

    scan = get_object_or_404(Scan, pk=pk)
    data = _parse_json_body(request)
    if isinstance(data, JsonResponse):
        return data
    row = _boundary_or_404(scan, data)
    if isinstance(row, JsonResponse):
        return row
    restored = boundaries.restore(scan, row, request.user)
    _rebuild_findings(scan)
    return JsonResponse(
        {"status": "ok", "boundary_id": row.pk, "restored": restored}
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
        }
    )


@login_required
def serve_redactions(request: HttpRequest, pk: int) -> JsonResponse:
    """Return the boxes to paint, grouped by page, in PDF points.

    The redaction rects and the margin strips in one list (#240, PR B),
    read off the ``Redaction`` rows the compute wrote and the curator
    edited. Nothing is computed here: a volume the compute has not
    reached answers an empty list.

    :param request: The HTTP request.
    :param pk: Scan primary key.
    :return: ``[{page_index, rects: [{id, x0, y0, x1, y1, fill,
        rect_type, origin}]}]``.
    """
    from scanning import redactions

    scan = get_object_or_404(Scan, pk=pk)
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
    :return: ``{status, id}``; 400 on a bad body, 409 when the page has
        no address.
    """
    from scanning import redactions

    scan = get_object_or_404(Scan, pk=pk)
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
    return JsonResponse({"status": "ok", "id": row.pk})


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
    :return: ``{status, id}``.
    """
    from scanning import redactions

    scan = get_object_or_404(Scan, pk=pk)
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
    return JsonResponse({"status": "ok", "id": holder.pk})


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
    :return: ``{status}``.
    """
    from scanning import redactions

    scan = get_object_or_404(Scan, pk=pk)
    row = _redaction_of(scan, redaction_id)
    if row is None or row.bbox is None:
        return _redaction_error("Redaction not found", 404)
    redactions.dismiss(scan, row, request.user)
    _rebuild_findings(scan)
    return JsonResponse({"status": "ok"})


@login_required
@require_POST
def restore_redaction(
    request: HttpRequest, pk: int, redaction_id: int
) -> JsonResponse:
    """Give a dismissed computed box back: the undo of a dismiss.

    :param request: The HTTP request.
    :param pk: Scan primary key.
    :param redaction_id: The computed row.
    :return: ``{status, restored}``.
    """
    from scanning import redactions

    scan = get_object_or_404(Scan, pk=pk)
    row = _redaction_of(scan, redaction_id)
    if row is None:
        return _redaction_error("Redaction not found", 404)
    restored = redactions.restore(scan, row, request.user)
    _rebuild_findings(scan)
    return JsonResponse({"status": "ok", "restored": restored})


#: Whether a curator may ask for the redaction computation from review
#: 2. Off for now (#196): the computation renders every page of the
#: volume and takes the scan out of review for a minute or more, and
#: the one run the daemon starts after a detection run is the only one
#: wanted until the stage has been watched on a few volumes (#211).
#: Turning it back on is this flag plus the "Re-pair Opinions" button
#: in ``_process_actions.html``; the queueing code below is kept.
REPAIR_ON_REQUEST_ENABLED = False

REPAIR_DISABLED_MESSAGE = (
    "Re-pairing on request is off for now. The redactions are computed "
    "once, when the detection run finishes."
)

#: The gate of step 3 in the view (#263/#269): a volume of the new
#: pipeline reaches the file generation through the review-2 approval.
GENERATE_REQUIRES_REDACTION_REVIEW_MESSAGE = (
    "The redaction review of this volume is not approved yet. Approve "
    "it in step 2 before the files are generated."
)


@login_required
@require_POST
def pair_opinions_api(request: HttpRequest, pk: int) -> JsonResponse:
    """Ask the daemon to pair the opinions again, with the geometry.

    A curator presses this after they add or delete a detection, and
    what they want is every consequence of that edit: the pairing, the
    redaction rects and the margin strips, which are all measured from
    the same detections. One queued action computes all three (#196),
    so none of them can be left describing the boxes of an hour ago.

    It runs on the daemon rather than here, because the measurement
    renders every page of the volume: 83 seconds for 1364 pages. The
    viewer reloads, sees the scan busy, and its progress poll reloads
    again when the daemon parks it.

    :param request: The HTTP request.
    :param pk: Scan primary key.
    :return: JSON response saying the work is queued, or 409 while
        re-pairing on request is off (``REPAIR_ON_REQUEST_ENABLED``).
    """
    scan = get_object_or_404(Scan, pk=pk)
    if not REPAIR_ON_REQUEST_ENABLED:
        return JsonResponse({"error": REPAIR_DISABLED_MESSAGE}, status=409)
    if not Detection.objects.filter(scan=scan, active=True).exists():
        return JsonResponse({"error": "No detections found"}, status=400)

    from scanning.services import queue_redaction_compute

    queued, message = queue_redaction_compute(scan)
    if not queued:
        return JsonResponse({"error": message}, status=409)
    return JsonResponse({"status": "queued", "message": message}, status=202)


@login_required
@require_POST
def compute_redactions_api(request: HttpRequest, pk: int) -> JsonResponse:
    """Ask the daemon to compute this scan's redaction geometry.

    The same queued action as :func:`pair_opinions_api`, and for the
    same reason: the measurement renders every page of the volume, so
    it cannot run inside a request (#196).

    :param request: The HTTP request.
    :param pk: Scan primary key.
    :return: JSON response saying the work is queued, or 409 while
        re-pairing on request is off (``REPAIR_ON_REQUEST_ENABLED``).
    """
    scan = get_object_or_404(Scan, pk=pk)
    if not REPAIR_ON_REQUEST_ENABLED:
        return JsonResponse({"error": REPAIR_DISABLED_MESSAGE}, status=409)
    if not Detection.objects.filter(scan=scan, active=True).exists():
        return JsonResponse({"error": "No detections found"}, status=400)

    from scanning.services import queue_redaction_compute

    queued, message = queue_redaction_compute(scan)
    if not queued:
        return JsonResponse({"error": message}, status=409)
    return JsonResponse({"status": "queued", "message": message}, status=202)


@login_required
@require_POST
def generate_files(request: HttpRequest, pk: int) -> HttpResponse:
    """Refuse to generate opinion files while the pipeline is paused.

    File generation is post-review-1 processing, which issue #173
    stops until the new OCR stack reaches that stage. The generation
    code (``services.run_generate_files``) is kept, but nothing queues
    it; this view fails with the unified pipeline-paused message.

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

    Generate Files already pushed every output file to
    ``processing/<pk>/...`` on S3, so this view is a pure status flip:
    it validates that file generation has run, then sets
    ``status=APPROVED``. Phase 2 will wire Approve into the
    LLM-extraction handoff.

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
    from scanning.models import REVIEW2_CHECKS

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
    :return: ``{status, dismissal_id}``; 404 when the row is gone, 409
        when the finding takes no dismissal.
    """
    from scanning import findings

    scan = get_object_or_404(Scan, pk=pk)
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
    return JsonResponse({"status": "ok", "dismissal_id": dismissal.pk})


@login_required
@require_POST
def restore_finding(request: HttpRequest, pk: int) -> JsonResponse:
    """Take back the dismissal of a finding: the card comes back.

    :param request: The HTTP request (JSON body with ``issue_id``).
    :param pk: Scan primary key.
    :return: ``{status, restored}``.
    """
    from scanning import findings

    scan = get_object_or_404(Scan, pk=pk)
    data = _parse_json_body(request)
    if isinstance(data, JsonResponse):
        return data
    row = _finding_or_404(scan, data)
    if isinstance(row, JsonResponse):
        return row
    restored = findings.restore(scan, row, request.user)
    return JsonResponse({"status": "ok", "restored": restored})


@login_required
@require_POST
def withdraw_stale_edit(request: HttpRequest, pk: int) -> JsonResponse:
    """Withdraw the curator row a stale finding names, and rebuild.

    The one way out of a ``stale_*`` finding: the decision the compute
    could not land or place is taken back, as its own endpoint would
    take it back, and the findings are written again.

    :param request: The HTTP request (JSON body with ``issue_id``).
    :param pk: Scan primary key.
    :return: ``{status, withdrawn}``; 409 when the finding names no row.
    """
    from scanning import findings

    scan = get_object_or_404(Scan, pk=pk)
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
    return JsonResponse({"status": "ok", "withdrawn": withdrawn})


@login_required
def review_findings(request: HttpRequest, pk: int) -> JsonResponse:
    """Render the step-2 findings section as an HTML fragment.

    The page and this fragment render one template from one context
    (``findings.viewer_groups``), the ``process_actions`` shape (#151):
    a card that disagreed with itself after a refresh would offer a
    button the endpoint refuses. The viewer swaps the section after
    every write.

    :param request: The HTTP request.
    :param pk: Scan primary key.
    :return: ``{html, open, stale}``.
    """
    from django.template.loader import render_to_string

    from scanning import findings

    scan = get_object_or_404(Scan, pk=pk)
    context = findings.viewer_groups(scan)
    html = render_to_string(
        "scanning/_review_findings.html", context, request=request
    )
    return JsonResponse(
        {
            "html": html,
            "open": context["review2_open"],
            "stale": context["review2_stale"],
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
    :return: JSON response with ``deleted`` count, or 404 if not found.
    """
    from scanning import detections

    scan = get_object_or_404(Scan, pk=pk)
    data = _parse_json_body(request)
    if isinstance(data, JsonResponse):
        return data
    row = Detection.objects.filter(pk=data["detection_id"], scan=scan).first()
    if row is None:
        return JsonResponse(
            {"status": "error", "message": "Detection not found"}, status=404
        )
    if row.model_name == Detection.ModelName.MANUAL:
        detections.withdraw_manual(row, request.user)
    else:
        try:
            detections.decide(
                scan, row, DetectionDecision.Kind.DEACTIVATE, request.user
            )
        except detections.UnaddressableDetection:
            return _unaddressable()
    _rebuild_findings(scan)
    return JsonResponse({"status": "ok", "deleted": 1})


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
    :return: JSON response with ``updated`` count and ``detection_id``,
        or 404 if not found.
    """
    from scanning import detections

    scan = get_object_or_404(Scan, pk=pk)
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
        {"status": "ok", "updated": 1, "detection_id": holder.pk}
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
    the same label is that box, and the model box gets an ``approve``
    decision (#240). Otherwise a hand-drawn row is written, addressed
    by its source page. The rows are the only store; nothing here reads
    or writes a file.

    :param request: The HTTP request (JSON body with page_index,
        label_id, bbox, img_width, and img_height).
    :param pk: Scan primary key.
    :return: JSON response with ``added=True`` and the new row's
        ``detection_id`` if new, ``added=False`` and the approved row's
        id if an existing detection was approved.
    """
    from blackletter.models import Label

    from scanning import detections

    scan = get_object_or_404(Scan, pk=pk)
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
        if near.model_name != Detection.ModelName.MANUAL:
            # No run passed: ``decide`` resolves one only for a row with
            # no address, so the common case costs no ledger read.
            try:
                detections.decide(
                    scan, near, DetectionDecision.Kind.APPROVE, request.user
                )
            except detections.UnaddressableDetection:
                return _unaddressable()
        _rebuild_findings(scan)
        return JsonResponse(
            {"status": "ok", "added": False, "detection_id": near.pk}
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
        {"status": "ok", "added": True, "detection_id": row.pk}
    )


@login_required
@require_POST
def approve_detection(request: HttpRequest, pk: int) -> JsonResponse:
    """Approve a detection: its confidence reads 1.0 from now on.

    A model row gets an ``approve`` decision (#240), which the next
    import lands on the same box again. A hand-drawn row is the
    curator's already and needs none.

    :param request: The HTTP request (JSON body with ``detection_id``
        (int, DB pk)).
    :param pk: Scan primary key.
    :return: JSON response with ``updated`` count, or 404 if not found.
    """
    from scanning import detections

    scan = get_object_or_404(Scan, pk=pk)
    data = _parse_json_body(request)
    if isinstance(data, JsonResponse):
        return data
    row = Detection.objects.filter(pk=data["detection_id"], scan=scan).first()
    if row is None:
        return JsonResponse(
            {"status": "error", "message": "Detection not found"}, status=404
        )
    if row.model_name != Detection.ModelName.MANUAL:
        try:
            detections.decide(
                scan, row, DetectionDecision.Kind.APPROVE, request.user
            )
        except detections.UnaddressableDetection:
            return _unaddressable()
    _rebuild_findings(scan)
    return JsonResponse({"status": "ok", "updated": 1})


@login_required
@require_POST
def bake_redactions(request: HttpRequest, pk: int) -> JsonResponse:
    """Bake pending redaction rectangles into the scan PDF (no-op stub).

    :param request: The HTTP request.
    :param pk: Scan primary key.
    :return: JSON response with the bake result.
    """
    scan = get_object_or_404(Scan, pk=pk)
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
