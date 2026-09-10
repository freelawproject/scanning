"""The findings of review 2: one rebuild from the rows, and the dismissals.

A finding of review 2 is an ``Issue`` row whose ``check_name`` is in
``models.REVIEW2_CHECKS`` (issue #240, PR D). Seven checks, in three
groups:

- **about detections**: a key icon or a caption no opinion boundary
  names (``unmatched_key_icon``, ``unmatched_caption``);
- **about redactions and pages**: a confident headnote box no black
  redaction covers (``uncovered_headnote``), a run of pages no opinion
  covers (``uncovered_pages``);
- **about the curator's own rows**: a decision the last compute could
  not land or place (``stale_detection_edit``, ``stale_redaction_edit``,
  ``stale_boundary_edit``).

Four rules run through the module:

- **Every finding is derived from the rows, and nothing else.**
  :func:`rebuild` reads ``Detection``, ``OpinionBoundary``,
  ``Redaction`` and the decision rows, deletes the scan's review-2
  rows and writes them again. No S3 read and no page render, so it
  runs after every curator write as well as at the end of the compute:
  the recompute button is off until #211, and a card that only the
  compute rewrote would stand whatever the curator did.
- **Stale is read off the rows, not threaded from the compute.** The
  resolution of each module sets a FK on the row it landed on
  (``Detection.decision``, ``Redaction.decision``,
  ``OpinionBoundary.decision``) and the relocation writes ``apply_run``
  on the row it placed, so a standing decision no row points at, and
  a human row whose ``apply_run`` is not the measured run, say it
  themselves.
- **A dismissal is a row that names its target by address**
  (``models.ReviewDismissal``): the check, the source page, the label
  and a copy of the box. The rebuild resolves it onto the new finding
  (:func:`resolve`) and writes the finding **with** the FK, so the
  viewer shows it muted with an undo. A stale finding has no
  dismissal: it is a fact about a row a person wrote, and the way out
  is to withdraw that row (:func:`withdraw_stale`).
- **No measured finding without a computed boundary.** A volume the
  compute has not reached has no pairing, so every key icon would read
  as unmatched, and that is a fact about the queue and not a finding.
  The stale checks are written regardless.
"""

from __future__ import annotations

import logging
from typing import Any

from django.db import transaction
from django.utils import timezone

from scanning import boundaries, detections, redactions
from scanning.models import (
    REVIEW2_CHECKS,
    STALE_REVIEW2_CHECKS,
    ApplyRun,
    CheckName,
    Detection,
    DetectionDecision,
    Issue,
    OpinionBoundary,
    Redaction,
    ReviewDismissal,
    Scan,
)
from scanning.utils import compute_coverage_gaps

logger = logging.getLogger(__name__)

#: A headnote box the model is at least this sure of must be covered by
#: a redaction. The value the step-2 view used before the finding was a
#: row.
HEADNOTE_CONFIDENCE = 0.8

#: The order the groups of the step-2 section are shown in, with their
#: headings. Redactions first: "everything that has to be redacted is
#: redacted" is the first goal of the review.
TARGET_GROUPS = (
    (Issue.Target.REDACTION, "Redactions"),
    (Issue.Target.BOUNDARY, "Opinion boundaries"),
    (Issue.Target.DETECTION, "Detections"),
    (Issue.Target.PAGES, "Pages"),
)

#: The value of ``metadata["model"]`` on a stale finding, by row class.
STALE_MODELS = {
    DetectionDecision: "detection_decision",
    Detection: "detection",
    Redaction: "redaction",
    OpinionBoundary: "opinion_boundary",
}

_RESOLVE = object()


class UndismissableFinding(ValueError):
    """A dismissal was asked for a finding that takes none.

    A stale finding is a fact about a curator's row, and a review-1
    issue has a dismissal of its own (a ``DISMISS_ISSUE`` page edit).
    """


class NotAStaleFinding(ValueError):
    """A withdrawal was asked through a finding that names no row."""


class UnaddressableFinding(ValueError):
    """A dismissal was asked for a finding whose target has no address.

    A pre-#240 detection row keeps ``source_page`` blank until the next
    import, and a run of pages may end outside the apply run's map. A
    dismissal keyed by no page could never land, so it is refused, the
    rule of ``detections.UnaddressableDetection``.
    """


# ---------------------------------------------------------------------------
# The rebuild
# ---------------------------------------------------------------------------


def rebuild(scan: Scan, run: ApplyRun | None | object = _RESOLVE) -> int:
    """Write the scan's review-2 findings again, from the rows.

    Deletes the scan's ``REVIEW2_CHECKS`` rows and writes the current
    findings, in one transaction. Called by the compute after its last
    row write (which passes the run it measured in, since its own stamp
    is not written yet), and by every review-2 endpoint after its
    write (which passes nothing).

    :param scan: The scan.
    :param run: The apply run the rows are measured against, or None
        for the original's space; ``detections.measured_run`` when
        omitted.
    :returns: How many findings stand open (not dismissed).
    """
    if run is _RESOLVE:
        run = detections.measured_run(scan)
    found = list(_stale_findings(scan, run))
    if OpinionBoundary.objects.computed().filter(scan=scan).exists():
        rows = boundaries.standing(scan)
        found.extend(_unmatched_findings(scan, rows))
        found.extend(_uncovered_pages_findings(scan, rows, run))
        found.extend(_uncovered_headnote_findings(scan))
    resolve(scan, found)
    with transaction.atomic():
        Issue.objects.filter(scan=scan, check_name__in=REVIEW2_CHECKS).delete()
        Issue.objects.bulk_create(
            [
                Issue(
                    scan=scan,
                    severity=Issue.Severity.WARNING,
                    check_name=f["check_name"],
                    target=f["target"],
                    page_number=f.get("page_number"),
                    message=f["message"],
                    metadata=f["metadata"],
                    dismissal_id=f.get("dismissal_id"),
                )
                for f in found
            ]
        )
    open_count = sum(1 for f in found if not f.get("dismissal_id"))
    logger.info(
        "scan %s: %d review-2 finding(s) written, %d open, %d stale",
        scan.pk,
        len(found),
        open_count,
        sum(1 for f in found if f["check_name"] in STALE_REVIEW2_CHECKS),
    )
    return open_count


def _fingerprint_stale(row, scan: Scan) -> bool:
    """Return whether ``row`` was made against another original.

    Blank on either side is a legacy value and matches anything, the
    rule of ``page_edits.is_stale``.
    """
    return bool(
        row.source_fingerprint
        and scan.source_fingerprint
        and row.source_fingerprint != scan.source_fingerprint
    )


def _unplaced(row, scan: Scan, run: ApplyRun | None, source_page) -> bool:
    """Return whether a human row is not in the measured space.

    The relocation leaves an unplaced row's ``apply_run`` as it was and
    writes ``run`` on every row it placed (``detections.relocate_rows``),
    so the column says it. With no run the rows are in the original's
    space and only the fingerprint can make one stale.
    """
    if _fingerprint_stale(row, scan):
        return True
    if run is None:
        return False
    return source_page is None or row.apply_run_id != run.pk


def _stale_finding(row, kind: str, source_page, page_index) -> dict:
    """Build the dict of one stale finding.

    :param row: The human row that did not land or place.
    :param kind: What the row is, for the message.
    :param source_page: The row's source page, or None.
    :param page_index: The row's position, or None.
    """
    model = STALE_MODELS[type(row)]
    target = {
        "detection_decision": Issue.Target.DETECTION,
        "detection": Issue.Target.DETECTION,
        "redaction": Issue.Target.REDACTION,
        "opinion_boundary": Issue.Target.BOUNDARY,
    }[model]
    check = {
        Issue.Target.DETECTION: CheckName.STALE_DETECTION_EDIT,
        Issue.Target.REDACTION: CheckName.STALE_REDACTION_EDIT,
        Issue.Target.BOUNDARY: CheckName.STALE_BOUNDARY_EDIT,
    }[target]
    where = f" on source page {source_page}" if source_page else ""
    return {
        "check_name": check,
        "target": target,
        "page_number": page_index + 1 if page_index is not None else None,
        "message": (
            f"Your {kind}{where} was made against another version of "
            "this volume, or names a page it no longer has, so it is not "
            "applied. The volume shows the model's answer there. "
            "Withdraw it, and make it again on the page as it is now."
        ),
        "metadata": {
            "model": model,
            "pk": row.pk,
            "kind": kind,
            "source_page": source_page,
            "page_index": page_index,
        },
    }


def _stale_findings(scan: Scan, run: ApplyRun | None):
    """Yield one finding per curator row the compute could not use."""
    # Detection decisions: standing, and no model row points at them.
    landed = set(
        Detection.objects.filter(scan=scan, decision__isnull=False)
        .values_list("decision_id", flat=True)
        .distinct()
    )
    for decision in DetectionDecision.objects.filter(
        scan=scan, withdrawn_at__isnull=True
    ):
        if decision.pk not in landed or _fingerprint_stale(decision, scan):
            yield _stale_finding(
                decision,
                f"{decision.get_kind_display().lower()} of a "
                f"{decision.label.lower().replace('_', ' ')}",
                decision.source_page,
                None,
            )
    # Hand-drawn detections: not withdrawn, and not in the measured space.
    for row in Detection.objects.filter(
        scan=scan,
        model_name=Detection.ModelName.MANUAL,
        withdrawn_at__isnull=True,
    ):
        if _unplaced(row, scan, run, row.source_page):
            yield _stale_finding(
                row,
                f"hand-drawn {row.label.lower().replace('_', ' ')} box",
                row.source_page,
                row.page_index,
            )
    # Redactions: a dismiss no computed row points at, an add unplaced.
    landed = set(
        Redaction.objects.filter(scan=scan, decision__isnull=False)
        .values_list("decision_id", flat=True)
        .distinct()
    )
    for row in Redaction.objects.human().filter(scan=scan):
        if row.kind == Redaction.Kind.DISMISS:
            stale = row.pk not in landed or _fingerprint_stale(row, scan)
            kind = "dismissal of a computed redaction"
        else:
            stale = _unplaced(row, scan, run, row.source_page)
            kind = f"drawn {row.fill} box"
        if stale:
            yield _stale_finding(row, kind, row.source_page, row.page_index)
    # Boundaries: the same two rules over the start anchor.
    landed = set(
        OpinionBoundary.objects.filter(scan=scan, decision__isnull=False)
        .values_list("decision_id", flat=True)
        .distinct()
    )
    for row in OpinionBoundary.objects.human().filter(
        scan=scan, withdrawn_at__isnull=True
    ):
        if row.kind == OpinionBoundary.Kind.DISMISS:
            stale = row.pk not in landed or _fingerprint_stale(row, scan)
            kind = "dismissal of a computed opinion boundary"
        else:
            stale = _unplaced(row, scan, run, row.start_source_page)
            kind = "opinion boundary"
        if stale:
            yield _stale_finding(
                row, kind, row.start_source_page, row.start_page_index
            )


def _detection_finding(
    check: str, target: str, row: Detection, message: str
) -> dict:
    """Build the dict of a finding about one detection box.

    The metadata is what ``_getDetectionData`` in ``viewer_sidebar.js``
    reads off the card, so the Approve and Delete buttons of the card
    work as they did on the unmatched cards, plus the source address
    the dismissal is keyed by.
    """
    return {
        "check_name": check,
        "target": target,
        "page_number": row.page_index + 1,
        "message": message,
        "metadata": {
            "detection_id": row.pk,
            "page_index": row.page_index,
            "label": row.label,
            "label_id": row.label_id,
            "conf": round(row.confidence, 2),
            "bbox": [row.x0, row.y0, row.x1, row.y1],
            "img_width": row.img_width,
            "img_height": row.img_height,
            "source_edit": row.source_edit_id,
            "source_page": row.source_page,
        },
    }


def _caption_is_continuation(
    det: Detection, paired_keys_sorted: list[tuple[int, float, float]]
) -> bool:
    """Return whether a caption falls between two paired key icons.

    A caption between two *actual* paired keys is a continuation of the
    opinion in that span, not a missed opinion. The open-ended span past
    the last key is a new opinion whose closing key is not on this scan
    (it likely continues into the next volume), so it stays unmatched.

    :param det: A caption row.
    :param paired_keys_sorted: Sorted ``(page_index, x0, y0)`` of the
        key icons the boundaries name.
    :returns: Whether the caption is inside a span.
    """
    for i, (kp, _kx, ky) in enumerate(paired_keys_sorted):
        if i + 1 >= len(paired_keys_sorted):
            break
        next_kp, _, next_ky = paired_keys_sorted[i + 1]
        after_key = det.page_index > kp or (
            det.page_index == kp and det.y0 > ky
        )
        before_next = det.page_index < next_kp or (
            det.page_index == next_kp and det.y0 < next_ky
        )
        if after_key and before_next:
            return True
    return False


def _unmatched_findings(scan: Scan, rows: list[OpinionBoundary]):
    """Yield the key icons and captions no live boundary names."""
    paired_captions = set()
    paired_keys = set()
    for row in rows:
        if row.is_dismissed:
            continue
        if row.start_detection_id:
            paired_captions.add(row.start_detection_id)
        if row.end_detection_id:
            paired_keys.add(row.end_detection_id)
    for det in (
        Detection.objects.live()
        .filter(scan=scan, label="KEY_ICON")
        .exclude(pk__in=paired_keys)
        .order_by("page_index", "y0")
    ):
        yield _detection_finding(
            CheckName.UNMATCHED_KEY_ICON,
            Issue.Target.DETECTION,
            det,
            f"A key icon (confidence {det.confidence:.2f}) is not matched "
            "to any opinion.",
        )
    paired_key_positions = sorted(
        (d.page_index, d.x0, d.y0)
        for d in Detection.objects.filter(pk__in=paired_keys)
    )
    for det in (
        Detection.objects.live()
        .filter(scan=scan, label="CASE_CAPTION")
        .exclude(pk__in=paired_captions)
        .order_by("page_index", "y0")
    ):
        if _caption_is_continuation(det, paired_key_positions):
            continue
        yield _detection_finding(
            CheckName.UNMATCHED_CAPTION,
            Issue.Target.DETECTION,
            det,
            f"A case caption (confidence {det.confidence:.2f}) is not "
            "matched to any opinion, and it is not inside one.",
        )


def _uncovered_pages_findings(
    scan: Scan, rows: list[OpinionBoundary], run: ApplyRun | None
):
    """Yield one finding per run of pages no live opinion covers.

    The arithmetic is ``utils.compute_coverage_gaps``'s: an index plus
    ``Scan.start_page`` is the printed number, as the warning line said
    before this was a row. The printed pages of the final space live in
    S3, and a rebuild from an endpoint must not read them.
    """
    opinions = [
        {
            "caption_page": r.start_page_index,
            "key_page": r.end_page_index,
            "dismissed": r.is_dismissed,
        }
        for r in rows
    ]
    start_page = scan.start_page or 0
    for start, end, count in compute_coverage_gaps(
        opinions, scan.start_page, scan.end_page
    ):
        first = start - start_page
        last = end - start_page
        first_edit, first_source = detections.source_for_index(
            scan, first, run
        )
        last_edit, last_source = detections.source_for_index(scan, last, run)
        yield {
            "check_name": CheckName.UNCOVERED_PAGES,
            "target": Issue.Target.PAGES,
            "page_number": first + 1,
            "message": (
                f"Pages {start}-{end} ({count} pages) not covered by any "
                "opinion"
                if count > 1
                else f"Page {start} (1 page) not covered by any opinion"
            ),
            "metadata": {
                "first_index": first,
                "last_index": last,
                "count": count,
                "source_edit": first_edit,
                "source_page": first_source,
                "end_source_edit": last_edit,
                "end_source_page": last_source,
            },
        }


def _uncovered_headnote_findings(scan: Scan):
    """Yield one finding per confident headnote box no black box covers.

    The detections are in the render's pixels and the redactions in
    points (PR B); the box centre is converted with
    ``boundaries.to_points``, which is within a point of the compute's
    own scale. A curator's ``add`` counts as cover: they drew it to
    cover the headnote.
    """
    boxes: dict[int, list[tuple[float, float, float, float]]] = {}
    for row in Redaction.objects.visible().filter(
        scan=scan, fill=Redaction.Fill.BLACK
    ):
        boxes.setdefault(row.page_index, []).append(
            (row.x0, row.y0, row.x1, row.y1)
        )
    for det in (
        Detection.objects.live()
        .filter(
            scan=scan, label="HEADNOTE", confidence__gte=HEADNOTE_CONFIDENCE
        )
        .order_by("page_index", "y0")
    ):
        cx, cy = boundaries.to_points(
            (det.x0 + det.x1) / 2,
            (det.y0 + det.y1) / 2,
            det.img_width,
            det.img_height,
        )
        covered = any(
            x0 <= cx <= x1 and y0 <= cy <= y1
            for x0, y0, x1, y1 in boxes.get(det.page_index, [])
        )
        if covered:
            continue
        yield _detection_finding(
            CheckName.UNCOVERED_HEADNOTE,
            Issue.Target.REDACTION,
            det,
            f"A headnote box (confidence {det.confidence:.2f}) is not "
            "covered by a redaction.",
        )


# ---------------------------------------------------------------------------
# The address, and the resolution of the dismissals
# ---------------------------------------------------------------------------


def address_of(finding: dict[str, Any]) -> dict[str, Any]:
    """Return the ``ReviewDismissal`` fields that address ``finding``.

    :param finding: A finding dict, or an ``Issue`` row's fields.
    :returns: The columns of the address.
    :raises UndismissableFinding: for a stale finding.
    :raises UnaddressableFinding: when the target names no source page.
    """
    check = finding["check_name"]
    if check in STALE_REVIEW2_CHECKS or check not in REVIEW2_CHECKS:
        raise UndismissableFinding(check)
    meta = finding["metadata"]
    if meta.get("source_page") is None:
        raise UnaddressableFinding(check)
    address = {
        "check_name": check,
        "source_edit_id": meta.get("source_edit"),
        "source_page": meta.get("source_page"),
    }
    if check == CheckName.UNCOVERED_PAGES:
        if meta.get("end_source_page") is None:
            raise UnaddressableFinding(check)
        address["end_source_edit_id"] = meta.get("end_source_edit")
        address["end_source_page"] = meta.get("end_source_page")
    else:
        bbox = meta.get("bbox") or [None] * 4
        address.update(
            label=meta.get("label") or "",
            target_x0=bbox[0],
            target_y0=bbox[1],
            target_x1=bbox[2],
            target_y1=bbox[3],
            img_width=int(meta.get("img_width") or 0),
            img_height=int(meta.get("img_height") or 0),
        )
    return address


def _same_address(dismissal: ReviewDismissal, finding: dict) -> bool:
    """Return whether ``dismissal`` names the page(s) of ``finding``."""
    meta = finding["metadata"]
    if dismissal.check_name != finding["check_name"]:
        return False
    if dismissal.source_edit_id != meta.get("source_edit"):
        return False
    if dismissal.source_page != meta.get("source_page"):
        return False
    if finding["check_name"] == CheckName.UNCOVERED_PAGES:
        return dismissal.end_source_edit_id == meta.get(
            "end_source_edit"
        ) and dismissal.end_source_page == meta.get("end_source_page")
    return dismissal.label == (meta.get("label") or "")


def _overlap(dismissal: ReviewDismissal, finding: dict) -> float:
    """Return how well the dismissal's box matches the finding's.

    1.0 for a run of pages, whose address is exact; the IoU for a box.
    """
    if finding["check_name"] == CheckName.UNCOVERED_PAGES:
        return 1.0
    target = dismissal.target_bbox
    bbox = finding["metadata"].get("bbox")
    if not target or not bbox:
        return 0.0
    return detections.iou(target, bbox)


def standing_dismissals(scan: Scan):
    """Return the dismissals that are not withdrawn and not stale."""
    return [
        d
        for d in ReviewDismissal.objects.filter(
            scan=scan, withdrawn_at__isnull=True
        )
        if not _fingerprint_stale(d, scan)
    ]


def resolve(scan: Scan, findings: list[dict]) -> int:
    """Land the standing dismissals on the rebuilt findings.

    For each dismissal, the finding with the same check and address
    whose box overlaps the copied one best, at least
    ``detections.IOU_THRESHOLD``; a run of pages matches by address
    alone. Each finding is taken once. Sets ``dismissal_id`` on the
    dict. A dismissal that lands on nothing is left standing and is
    **not** a finding: it hides nothing, and the next rebuild may land
    it. Reads no dismissal when none stands.

    :param scan: The scan.
    :param findings: The rebuilt dicts, changed in place.
    :returns: How many dismissals landed.
    """
    dismissals = standing_dismissals(scan)
    if not dismissals:
        return 0
    landed = 0
    for dismissal in dismissals:
        best, best_score = None, detections.IOU_THRESHOLD
        for finding in findings:
            if finding.get("dismissal_id"):
                continue
            if finding["check_name"] in STALE_REVIEW2_CHECKS:
                continue
            if not _same_address(dismissal, finding):
                continue
            score = _overlap(dismissal, finding)
            if score >= best_score:
                best, best_score = finding, score
        if best is not None:
            best["dismissal_id"] = dismissal.pk
            landed += 1
    return landed


# ---------------------------------------------------------------------------
# The curator's decisions
# ---------------------------------------------------------------------------


def dismiss(scan: Scan, issue: Issue, user) -> ReviewDismissal:
    """Dismiss a finding: a row at its address, and the FK set at once.

    A second dismissal of a dismissed finding answers the standing row.

    :param scan: The scan.
    :param issue: The finding row.
    :param user: The curator. May be None.
    :returns: The standing dismissal.
    :raises UndismissableFinding: for a stale finding, or a review-1 row.
    :raises UnaddressableFinding: when the target names no source page.
    """
    if issue.dismissal_id is not None:
        standing = ReviewDismissal.objects.filter(
            pk=issue.dismissal_id, withdrawn_at__isnull=True
        ).first()
        if standing is not None:
            return standing
    address = address_of(
        {"check_name": issue.check_name, "metadata": issue.metadata or {}}
    )
    with transaction.atomic():
        row = ReviewDismissal.objects.create(
            scan=scan,
            source_fingerprint=scan.source_fingerprint,
            author=user,
            **address,
        )
        Issue.objects.filter(pk=issue.pk).update(dismissal=row)
    return row


def restore(scan: Scan, issue: Issue, user) -> bool:
    """Withdraw the dismissal of a finding, and clear the FK.

    :param scan: The scan.
    :param issue: The finding row.
    :param user: The curator. May be None.
    :returns: Whether a dismissal stood.
    """
    if issue.dismissal_id is None:
        return False
    return bool(
        withdraw(
            ReviewDismissal.objects.filter(pk=issue.dismissal_id, scan=scan),
            user,
        )
    )


def withdraw(rows, user) -> int:
    """Take back the dismissals in ``rows``. Nothing is deleted.

    Clears the FK on the findings they covered, so the cards come back
    without a rebuild.

    :param rows: A queryset of dismissals.
    :param user: Who took them back. May be None.
    :returns: How many rows were stamped.
    """
    now = timezone.now()
    count = 0
    with transaction.atomic():
        for row in rows.filter(withdrawn_at__isnull=True):
            stamped = ReviewDismissal.objects.filter(
                pk=row.pk, withdrawn_at__isnull=True
            ).update(withdrawn_at=now, withdrawn_by=user, date_modified=now)
            if stamped:
                count += 1
                Issue.objects.filter(dismissal=row).update(dismissal=None)
    return count


def withdraw_stale(scan: Scan, issue: Issue, user) -> bool:
    """Withdraw the curator row a stale finding names, then rebuild.

    :param scan: The scan.
    :param issue: A ``STALE_REVIEW2_CHECKS`` row.
    :param user: The curator. May be None.
    :returns: Whether the row stood before this call.
    :raises NotAStaleFinding: when the finding names no row.
    """
    if issue.check_name not in STALE_REVIEW2_CHECKS:
        raise NotAStaleFinding(issue.check_name)
    meta = issue.metadata or {}
    model, pk = meta.get("model"), meta.get("pk")
    if model == "detection_decision":
        count = detections.withdraw(
            DetectionDecision.objects.filter(pk=pk, scan=scan), user
        )
    elif model == "detection":
        row = Detection.objects.filter(
            pk=pk, scan=scan, model_name=Detection.ModelName.MANUAL
        ).first()
        count = int(detections.withdraw_manual(row, user)) if row else 0
    elif model == "redaction":
        count = redactions.withdraw(
            Redaction.objects.filter(pk=pk, scan=scan), user
        )
    elif model == "opinion_boundary":
        count = boundaries.withdraw(
            OpinionBoundary.objects.filter(pk=pk, scan=scan), user
        )
    else:
        raise NotAStaleFinding(str(model))
    rebuild(scan)
    return bool(count)


# ---------------------------------------------------------------------------
# The readers
# ---------------------------------------------------------------------------


def open_count(scan: Scan) -> tuple[int, int]:
    """Return the open review-2 findings, and the stale ones among them.

    :param scan: The scan.
    :returns: ``(open, stale)``.
    """
    from django.db.models import Count, Q

    counts = Issue.objects.filter(
        scan=scan, check_name__in=REVIEW2_CHECKS, dismissal__isnull=True
    ).aggregate(
        open=Count("pk"),
        stale=Count("pk", filter=Q(check_name__in=STALE_REVIEW2_CHECKS)),
    )
    return counts["open"] or 0, counts["stale"] or 0


def viewer_groups(
    scan: Scan, idx_to_logical: dict[int, Any] | None = None
) -> dict:
    """Return the step-2 findings section's context.

    The rows of the scan, annotated for the template: ``nav_pdf_index``
    (the position in the drawn space, ``page_number - 1``),
    ``logical_page`` (from ``idx_to_logical`` when the caller holds
    it), ``is_stale``, and grouped by ``target`` in ``TARGET_GROUPS``
    order. The page and the fragment render one template from this
    one context.

    :param scan: The scan.
    :param idx_to_logical: ``{page_index: printed label}`` in the drawn
        space, when the caller has it.
    :returns: ``finding_groups``, ``findings_computed`` (a computed
        boundary exists, so the measured checks were run),
        ``review2_open``, ``review2_stale``, ``review2_total``.
    """
    idx_to_logical = idx_to_logical or {}
    rows = list(
        Issue.objects.filter(
            scan=scan, check_name__in=REVIEW2_CHECKS
        ).order_by("page_number", "pk")
    )
    by_target: dict[str, list[Issue]] = {}
    for row in rows:
        row.nav_pdf_index = row.page_number - 1 if row.page_number else None
        row.logical_page = (
            idx_to_logical.get(row.nav_pdf_index, row.page_number)
            if row.page_number
            else None
        )
        row.is_stale = row.check_name in STALE_REVIEW2_CHECKS
        by_target.setdefault(row.target, []).append(row)
    groups = [
        {"key": key, "label": label, "rows": by_target[key]}
        for key, label in TARGET_GROUPS
        if by_target.get(key)
    ]
    open_rows = [r for r in rows if not r.is_dismissed]
    return {
        "finding_groups": groups,
        "findings_computed": OpinionBoundary.objects.computed()
        .filter(scan=scan)
        .exists(),
        "review2_total": len(rows),
        "review2_open": len(open_rows),
        "review2_stale": sum(1 for r in open_rows if r.is_stale),
    }
