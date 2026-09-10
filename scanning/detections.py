"""The detections of review 2: their address, and the curator's decisions.

Issue #240, PR A. Two families of rows share the ``Detection`` table:

- **Model rows are disposable.** Every import deletes the scan's model
  rows and writes the merged run again (``services._import_detections``).
- **Human rows are withdrawn, never deleted.** A hand-drawn box stays
  until a curator takes it back with ``withdrawn_at``.

A curator's decision about a model row (approve it, deactivate it) is a
``DetectionDecision``. It cannot point at the row, which the next import
deletes, so it names its target by **address**: the source page, the
label, and a copy of the model's box. :func:`resolve` lands every
standing decision on the new rows after an import, by that address and
an IoU of at least :data:`IOU_THRESHOLD` against the copy. The row's
``confidence`` and ``active`` are the derived reads every consumer
keeps using; only this module and the import write them.

**The address is the source page** the apply's page map names: the
original as uploaded (``source_edit`` null, ``source_page`` its 1-based
page) or the one-page shard of a page edit (``source_edit`` the edit,
``source_page`` the 1-based page of the shard). A row's ``page_index``
is its position in the space the compute measured, and the address is
what survives a new apply run.
"""

from __future__ import annotations

import logging

from django.db import transaction
from django.utils import timezone

from scanning.models import ApplyRun, Detection, DetectionDecision, Scan

logger = logging.getLogger(__name__)

#: The overlap a new model box needs with a decision's copy of the old
#: one to be the same box (settled in #241). Same fingerprint and same
#: model give the same box, so the match is exact in practice; the
#: threshold covers the column snap, which moves an x edge.
IOU_THRESHOLD = 0.5


class UnaddressableDetection(ValueError):
    """A decision was asked about a row no address can be written for.

    Two routes lead here: a row imported before #240, whose position
    lies outside the standing run's map, and a map without that page.
    A decision with no address could never land, so it is refused
    where it is asked, not written and logged after every import.
    """


def iou(a: list[float], b: list[float]) -> float:
    """Return the intersection over union of two ``[x0, y0, x1, y1]`` boxes.

    :param a: One box.
    :param b: The other box.
    :returns: A value in ``[0, 1]``; 0 when either box is empty.
    """
    ix0, iy0 = max(a[0], b[0]), max(a[1], b[1])
    ix1, iy1 = min(a[2], b[2]), min(a[3], b[3])
    inter = max(0.0, ix1 - ix0) * max(0.0, iy1 - iy0)
    if inter <= 0:
        return 0.0
    area_a = max(0.0, a[2] - a[0]) * max(0.0, a[3] - a[1])
    area_b = max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])
    union = area_a + area_b - inter
    return inter / union if union > 0 else 0.0


# ---------------------------------------------------------------------------
# The address
# ---------------------------------------------------------------------------


def source_of_entry(entry: dict) -> tuple[int | None, int | None]:
    """Return ``(edit_id, source_page)`` for one merged-document detection.

    The apply glue stamps a ``source`` on every detection of a
    corrected volume (``apply._glue_detections``): ``{"kind":
    "original", "pdf_page": p}`` or ``{"kind": "edit", "edit_id": id,
    "page": k}`` with ``k`` 0-based inside the edit's shard. The volume
    merge (``yolo.merge_detect_results``), which an identity run
    aliases, carries ``pdf_page`` alone, and that *is* the original's
    page. A document with neither names no address.

    :param entry: One detection dict of a merged or glued document.
    :returns: The edit pk (None for the original) and the 1-based page
        of that document; ``(None, None)`` when the entry names none.
    """
    source = entry.get("source")
    if isinstance(source, dict):
        if source.get("kind") == "edit":
            page = source.get("page")
            return source.get("edit_id"), (
                page + 1 if page is not None else None
            )
        page = source.get("pdf_page")
        return None, page
    page = entry.get("pdf_page")
    return None, page


def measured_run(scan: Scan) -> ApplyRun | None:
    """Return the apply run the scan's rows are measured against, if any.

    The rule of #269: the rows are in the final space of the standing
    run when the detect ledger names it (``yolo.redactions_current``);
    otherwise they are in the original's space. One place, so every
    endpoint that turns a viewer's ``page_index`` into an address asks
    the same question the compute answered.

    :param scan: The scan.
    :returns: The run, or None for the original's space.
    """
    from scanning import review_states, yolo

    run = review_states.final_run(scan)
    if run is None:
        return None
    rows = yolo.live_detect_jobs(scan)
    if rows and yolo.redactions_current(rows, run):
        return run
    return None


def source_for_index(
    scan: Scan, page_index: int, run: ApplyRun | None
) -> tuple[int | None, int | None]:
    """Return the address of ``page_index`` in the space ``run`` names.

    :param scan: The scan.
    :param page_index: A 0-based page of the rows' space.
    :param run: :func:`measured_run`, or None for the original's space.
    :returns: ``(edit_id, source_page)``; ``(None, None)`` when the map
        has no such page.
    """
    if run is None:
        return None, page_index + 1
    pages = (run.page_map or {}).get("pages") or []
    if not 0 <= page_index < len(pages):
        return None, None
    return source_of_entry({"source": pages[page_index]["source"]})


def is_stale(decision: DetectionDecision, scan: Scan) -> bool:
    """Return whether a decision was made against another original.

    Blank on either side is a legacy value and matches anything, the
    rule of ``page_edits.is_stale``.

    :param decision: The decision.
    :param scan: Its scan.
    :returns: Whether the fingerprints disagree.
    """
    if not decision.source_fingerprint or not scan.source_fingerprint:
        return False
    return decision.source_fingerprint != scan.source_fingerprint


# ---------------------------------------------------------------------------
# Decisions
# ---------------------------------------------------------------------------


def standing_decisions(scan: Scan):
    """Return the scan's decisions that are not withdrawn.

    :param scan: The scan.
    :returns: A queryset.
    """
    return DetectionDecision.objects.filter(
        scan=scan, withdrawn_at__isnull=True
    )


def _apply_effect(rows, decision: DetectionDecision) -> int:
    """Write a decision's derived read onto ``rows`` and point them at it.

    :param rows: A queryset of model rows.
    :param decision: The decision.
    :returns: How many rows were written.
    """
    fields = {"decision": decision}
    if decision.kind == DetectionDecision.Kind.APPROVE:
        fields["confidence"] = 1.0
    else:
        fields["active"] = False
    return rows.update(**fields)


def _revert_effect(rows, decision: DetectionDecision) -> int:
    """Give ``rows`` back their own values after ``decision`` is withdrawn.

    :param rows: A queryset of model rows the decision was resolved on.
    :param decision: The withdrawn decision.
    :returns: How many rows were written.
    """
    fields = {"decision": None}
    if decision.kind == DetectionDecision.Kind.APPROVE:
        if decision.target_confidence is not None:
            fields["confidence"] = decision.target_confidence
    else:
        fields["active"] = True
    return rows.update(**fields)


def withdraw(decisions, user) -> int:
    """Take back ``decisions`` and give their rows back their own values.

    Nothing is deleted: the row keeps standing, stamped, so the audit
    shows what was decided and when it was undone (the #232 rule).

    :param decisions: A queryset of decisions.
    :param user: Who took them back. May be None.
    :returns: How many decisions were stamped.
    """
    now = timezone.now()
    count = 0
    with transaction.atomic():
        for decision in decisions.filter(withdrawn_at__isnull=True):
            _revert_effect(
                Detection.objects.filter(decision=decision), decision
            )
            count += DetectionDecision.objects.filter(
                pk=decision.pk, withdrawn_at__isnull=True
            ).update(withdrawn_at=now, withdrawn_by=user, date_modified=now)
    return count


def _address_of_row(
    scan: Scan, row: Detection, run: ApplyRun | None
) -> tuple[int | None, int | None]:
    """Return the address of ``row``: its own columns, else by position.

    A row imported since #240 carries its address. A row imported
    before it, or a hand-drawn row of that time, is placed by its
    ``page_index`` in the space the rows are in now.

    :param scan: The scan.
    :param row: The row.
    :param run: :func:`measured_run`.
    :returns: ``(edit_id, source_page)``.
    """
    if row.source_page is not None:
        return row.source_edit_id, row.source_page
    return source_for_index(scan, row.page_index, run)


def decide(
    scan: Scan, row: Detection, kind: str, user, run: ApplyRun | None = None
) -> DetectionDecision:
    """Record a curator's decision about model row ``row``, and apply it.

    One decision stands per target: an earlier standing decision on the
    same row is withdrawn first, so an approval followed by a deletion
    leaves one deactivation standing. The same decision twice is a
    no-op that returns the standing row.

    :param scan: The scan.
    :param row: A model row (never a hand-drawn one; see
        :func:`withdraw_manual`).
    :param kind: A ``DetectionDecision.Kind`` value.
    :param user: The curator. May be None.
    :param run: :func:`measured_run`, when the caller holds it;
        resolved here otherwise. Only read for a row with no address.
    :returns: The standing decision.
    """
    if run is None and row.source_page is None:
        run = measured_run(scan)
    with transaction.atomic():
        # Lock the row before reading its decision: two clicks at once
        # (a double click, two reviewers on one volume) would otherwise
        # both see no standing decision and both write one.
        row = (
            Detection.objects.select_for_update(of=("self",))
            .select_related("decision")
            .get(pk=row.pk)
        )
        current = (
            row.decision
            if row.decision_id and row.decision.withdrawn_at is None
            else None
        )
        if current is not None and current.kind == kind:
            return current
        if current is not None:
            withdraw(DetectionDecision.objects.filter(pk=current.pk), user)
            row.refresh_from_db(fields=["confidence", "active", "decision"])
        edit_id, page = _address_of_row(scan, row, run)
        if not page:
            logger.warning(
                "scan %s: detection %s (page_index %s) has no address in the "
                "standing map; the %s is refused",
                scan.pk,
                row.pk,
                row.page_index,
                kind,
            )
            raise UnaddressableDetection(
                f"detection {row.pk} of scan {scan.pk} has no address"
            )
        decision = DetectionDecision.objects.create(
            scan=scan,
            kind=kind,
            source_edit_id=edit_id,
            source_page=page,
            source_fingerprint=scan.source_fingerprint or "",
            label=row.label,
            label_id=row.label_id,
            target_x0=row.x0,
            target_y0=row.y0,
            target_x1=row.x1,
            target_y1=row.y1,
            img_width=row.img_width,
            img_height=row.img_height,
            target_confidence=row.confidence,
            author=user,
        )
        _apply_effect(Detection.objects.filter(pk=row.pk), decision)
    return decision


def withdraw_manual(row: Detection, user) -> bool:
    """Take back a hand-drawn row, and the deactivation it replaced.

    A moved model box is a deactivation plus a hand-drawn row that
    names it in ``replaces``. Taking the hand-drawn row back gives the
    model box back too, or the page would show neither.

    :param row: A hand-drawn row.
    :param user: The curator. May be None.
    :returns: Whether the row stood before this call.
    """
    now = timezone.now()
    with transaction.atomic():
        count = Detection.objects.filter(
            pk=row.pk, withdrawn_at__isnull=True
        ).update(active=False, withdrawn_at=now, withdrawn_by=user)
        if count and row.replaces_id:
            withdraw(
                DetectionDecision.objects.filter(pk=row.replaces_id), user
            )
    return bool(count)


def add_manual(
    scan: Scan,
    page_index: int,
    label: str,
    label_id: int,
    bbox: list[float],
    img_width: int,
    img_height: int,
    run: ApplyRun | None = None,
    replaces: DetectionDecision | None = None,
) -> Detection:
    """Write a hand-drawn detection, addressed by its source page.

    No ``found_by``, on purpose (#196): the confidence gates are per
    model family, and a second family in the volume sends it back to
    the legacy gates.

    :param scan: The scan.
    :param page_index: The 0-based page in the rows' space.
    :param label: The label name.
    :param label_id: The label id.
    :param bbox: ``[x0, y0, x1, y1]`` in image pixels.
    :param img_width: The render width those pixels count in.
    :param img_height: The render height.
    :param run: :func:`measured_run`, when the caller holds it.
    :param replaces: The deactivation this box is drawn in place of.
    :returns: The new row.
    """
    if run is None:
        run = measured_run(scan)
    edit_id, page = source_for_index(scan, page_index, run)
    if not page:
        # The same refusal as ``decide``: a row with no address could
        # never follow the page space, and would be logged after every
        # import instead.
        logger.warning(
            "scan %s: page_index %s has no address in the standing map; "
            "the hand-drawn box is refused",
            scan.pk,
            page_index,
        )
        raise UnaddressableDetection(
            f"page_index {page_index} of scan {scan.pk} has no address"
        )
    return Detection.objects.create(
        scan=scan,
        page_index=page_index,
        label=label,
        label_id=label_id,
        confidence=1.0,
        x0=bbox[0],
        y0=bbox[1],
        x1=bbox[2],
        y1=bbox[3],
        img_width=img_width,
        img_height=img_height,
        model_name=Detection.ModelName.MANUAL,
        model_count=1,
        found_by=[],
        active=True,
        source_edit_id=edit_id,
        source_page=page,
        source_fingerprint=scan.source_fingerprint or "",
        apply_run=run,
        replaces=replaces,
    )


def move_model_row(
    scan: Scan, row: Detection, bbox: list[float], user
) -> Detection:
    """Move a model box: deactivate it, and draw the box where the curator put it.

    A write on the model row would be lost at the next import. The two
    human rows survive it: the deactivation carries to the re-imported
    box by address, and the hand-drawn row stands on its own (#240,
    plan section 3.0).

    :param scan: The scan.
    :param row: The model row.
    :param bbox: The new ``[x0, y0, x1, y1]``.
    :param user: The curator.
    :returns: The hand-drawn row that now holds the box.
    """
    run = measured_run(scan) if row.source_page is None else None
    with transaction.atomic():
        decision = decide(
            scan, row, DetectionDecision.Kind.DEACTIVATE, user, run=run
        )
        return add_manual(
            scan,
            row.page_index,
            row.label,
            row.label_id,
            bbox,
            row.img_width,
            row.img_height,
            run=row.apply_run,
            replaces=decision,
        )


# ---------------------------------------------------------------------------
# The resolution after an import
# ---------------------------------------------------------------------------


def resolve(scan: Scan) -> tuple[int, list[DetectionDecision]]:
    """Land every standing decision on the model rows just imported.

    For each decision: the model rows at its address with its label
    and no decision yet, the one with the best IoU against the copied
    box, and that at least :data:`IOU_THRESHOLD`. A decision made
    against another original (:func:`is_stale`) resolves to nothing.
    An unresolved decision is logged here and left standing; #240 PR D
    raises it as an issue the curator sees.

    :param scan: The scan whose model rows were just written.
    :returns: How many decisions landed, and the ones that did not.
    """
    scan.refresh_from_db(fields=["source_fingerprint"])
    landed = 0
    stale: list[DetectionDecision] = []
    decisions = list(standing_decisions(scan).order_by("pk"))
    if not decisions:
        # The common case, and a volume holds tens of thousands of
        # model rows: read none of them.
        return 0, []
    rows = list(
        Detection.objects.model_rows().filter(
            scan=scan,
            decision__isnull=True,
            source_page__in={d.source_page for d in decisions},
            label_id__in={d.label_id for d in decisions},
        )
    )
    by_address: dict[tuple, list[Detection]] = {}
    for row in rows:
        by_address.setdefault(
            (row.source_edit_id, row.source_page, row.label_id), []
        ).append(row)
    taken: set[int] = set()
    for decision in decisions:
        if is_stale(decision, scan):
            stale.append(decision)
            continue
        candidates = by_address.get(
            (decision.source_edit_id, decision.source_page, decision.label_id),
            [],
        )
        best, best_iou = None, 0.0
        for row in candidates:
            if row.pk in taken:
                continue
            score = iou([row.x0, row.y0, row.x1, row.y1], decision.target_bbox)
            if score > best_iou:
                best, best_iou = row, score
        if best is None or best_iou < IOU_THRESHOLD:
            stale.append(decision)
            continue
        taken.add(best.pk)
        _apply_effect(Detection.objects.filter(pk=best.pk), decision)
        landed += 1
    if stale:
        logger.warning(
            "scan %s: %d detection decision(s) found no box to land on: %s",
            scan.pk,
            len(stale),
            ", ".join(
                f"#{d.pk} {d.kind} {d.label} src p.{d.source_page}"
                for d in stale[:20]
            ),
        )
    return landed, stale


# ---------------------------------------------------------------------------
# The hand-drawn rows follow the new page space
# ---------------------------------------------------------------------------


def relocate_manual_rows(
    scan: Scan, run: ApplyRun
) -> tuple[int, list[Detection]]:
    """Put every standing hand-drawn row at its page in ``run``'s space.

    The import writes the model rows in the new run's space and keeps
    the hand-drawn rows as they are, so after a reopen that deletes a
    page a box drawn under ``a1`` would paint one page out under
    ``a2``. The row's address says where it belongs: an original page
    goes through ``originals_to_final`` (a replaced page has new
    content, so a box on it does not carry), an edit page through the
    ``(edit_id, page)`` slots of the map. A row the map does not hold,
    a row of another original, or a row with no address (imported
    before #240) is left as it is and logged; #240 PR D raises it as
    a stale finding.

    :param scan: The scan.
    :param run: The run whose space the model rows were just imported in.
    :returns: How many rows were written, and the rows left unplaced.
    """
    from scanning import apply

    page_map = run.page_map or {}
    originals = apply.originals_to_final(page_map)
    slots = {
        (entry["source"]["edit_id"], entry["source"]["page"]): entry[
            "final_page"
        ]
        for entry in page_map.get("pages") or []
        if entry["source"]["kind"] == "edit"
    }
    moved = 0
    unplaced: list[Detection] = []
    rows = Detection.objects.filter(
        scan=scan,
        model_name=Detection.ModelName.MANUAL,
        withdrawn_at__isnull=True,
    )
    for row in rows:
        stale = (
            row.source_fingerprint
            and scan.source_fingerprint
            and row.source_fingerprint != scan.source_fingerprint
        )
        if row.source_page is None or stale:
            unplaced.append(row)
            continue
        if row.source_edit_id is None:
            final = originals.get(row.source_page)
        else:
            final = slots.get((row.source_edit_id, row.source_page - 1))
        if final is None:
            unplaced.append(row)
            continue
        if row.page_index != final - 1 or row.apply_run_id != run.pk:
            Detection.objects.filter(pk=row.pk).update(
                page_index=final - 1, apply_run=run
            )
            moved += 1
    if unplaced:
        logger.warning(
            "scan %s: %d hand-drawn detection(s) have no page in %s and keep "
            "their old position: %s",
            scan.pk,
            len(unplaced),
            run.label,
            ", ".join(
                f"#{r.pk} {r.label} src p.{r.source_page}"
                for r in unplaced[:20]
            ),
        )
    return moved, unplaced
