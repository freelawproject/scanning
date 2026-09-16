"""The redactions of review 2: the boxes to paint, and the curator's decisions.

Issue #240, PR B. One ``Redaction`` table, in PDF points, replaced the
two blobs ``Scan.redaction_rects`` (pixels) and ``Scan.margin_rects``
(points). Two families of rows share it, the rule the detections
follow (PR A, ``detections.py``):

- **Computed rows are disposable.** Each compute deletes the scan's
  computed rows and writes what blackletter measured
  (:func:`write_computed`).
- **Human rows are withdrawn, never deleted.** An ``add`` is a box the
  curator drew; a ``dismiss`` takes a computed box out. A move of a
  computed box is a dismiss plus an add that names it in ``replaces``.

A dismiss cannot point at the computed row, which the next compute
deletes, so it names its target by **address**: the source page, the
``rect_type``, and a copy of the box. :func:`resolve` lands every
standing dismiss on the new rows after a compute, by that address and
an IoU of at least :data:`detections.IOU_THRESHOLD` against the copy,
and sets the computed row's ``decision``, which hides it.

Nothing in this module reads a file or renders a page: the compute
hands it what blackletter returned and the document it measured with.
"""

from __future__ import annotations

import logging

from django.db import transaction
from django.utils import timezone

from scanning import detections
from scanning.detections import IOU_THRESHOLD, iou
from scanning.models import ApplyRun, Redaction, Scan

logger = logging.getLogger(__name__)

#: "Resolve the run yourself": the default of :func:`add`, distinct from
#: None, which names the original's space on purpose.
_RESOLVE = object()


class UnaddressableRedaction(ValueError):
    """A box was asked for on a page no address can be written for.

    A map without that page. A row with no address could never follow
    the page space, so it is refused where it is asked.
    """


def _round(value: float) -> float:
    """Round a coordinate to a tenth of a point, as blackletter does.

    :param value: The coordinate.
    :returns: The rounded value.
    """
    return round(float(value), 1)


# ---------------------------------------------------------------------------
# The compute writes
# ---------------------------------------------------------------------------


def write_computed(
    scan: Scan,
    run: ApplyRun | None,
    detect_run: int | None,
    rects_px: list[dict],
    margins_pt: list[dict],
    pages: list,
) -> int:
    """Replace the scan's computed rows with what blackletter measured.

    The redaction rects come in pixels of the 200 dpi render, one entry
    per page (``{"page_index", "rects": [{x0, y0, x1, y1, fill,
    type}]}``); each is converted with the ``scale_x``/``scale_y`` of the
    blackletter ``Page`` of that index, the arithmetic
    ``blackletter.api.build_redactions`` did at step 3. The margin
    strips come in points already (``{"page_index", "rects": [{x0, y0,
    x1, y1}]}``) and become white ``margin`` rows. Every row is
    addressed through ``run``'s map (the identity when ``run`` is None).

    One transaction: a reader between the delete and the writes would
    see no box at all.

    :param scan: The scan.
    :param run: The apply run whose space the geometry was measured in,
        or None for the original's.
    :param detect_run: The detection run the geometry came from.
    :param rects_px: blackletter's redaction rects, in pixels.
    :param margins_pt: blackletter's margin strips, in points.
    :param pages: The blackletter ``Page`` objects the geometry was
        measured with, for the page scales.
    :returns: How many rows were written.
    """
    scales = {page.index: (page.scale_x, page.scale_y) for page in pages}
    rows = []

    def address(page_index: int) -> tuple[int | None, int]:
        edit_id, page = detections.source_for_index(scan, page_index, run)
        if not page:
            raise UnaddressableRedaction(
                f"scan {scan.pk}: page_index {page_index} has no address in "
                f"{run.label if run else 'the original'}"
            )
        return edit_id, page

    common = {
        "scan": scan,
        "origin": Redaction.Origin.COMPUTED,
        "source_fingerprint": scan.source_fingerprint or "",
        "apply_run": run,
        "detect_run": detect_run,
    }
    for entry in rects_px:
        page_index = entry["page_index"]
        if page_index not in scales:
            # Pixels stored as points would be a small box in the wrong
            # place, with no error: refuse, as a missing address is.
            raise UnaddressableRedaction(
                f"scan {scan.pk}: page_index {page_index} has rects but no "
                "page scale in the document they were measured from"
            )
        sx, sy = scales[page_index]
        edit_id, page = address(page_index)
        for r in entry.get("rects") or []:
            x0, y0 = _round(r["x0"] * sx), _round(r["y0"] * sy)
            x1, y1 = _round(r["x1"] * sx), _round(r["y1"] * sy)
            if x0 >= x1 or y0 >= y1:
                continue
            rows.append(
                Redaction(
                    rect_type=r.get("type") or "",
                    fill=r.get("fill") or Redaction.Fill.BLACK,
                    x0=x0,
                    y0=y0,
                    x1=x1,
                    y1=y1,
                    source_edit_id=edit_id,
                    source_page=page,
                    page_index=page_index,
                    **common,
                )
            )
    for entry in margins_pt:
        page_index = entry["page_index"]
        edit_id, page = address(page_index)
        for r in entry.get("rects") or []:
            x0, y0, x1, y1 = (_round(r[k]) for k in ("x0", "y0", "x1", "y1"))
            if x0 >= x1 or y0 >= y1:
                continue
            rows.append(
                Redaction(
                    rect_type=Redaction.MARGIN_TYPE,
                    fill=Redaction.Fill.WHITE,
                    x0=x0,
                    y0=y0,
                    x1=x1,
                    y1=y1,
                    source_edit_id=edit_id,
                    source_page=page,
                    page_index=page_index,
                    **common,
                )
            )
    with transaction.atomic():
        Redaction.objects.computed().filter(scan=scan).delete()
        Redaction.objects.bulk_create(rows, batch_size=1000)
    return len(rows)


def resolve(scan: Scan) -> tuple[int, list[Redaction]]:
    """Land every standing dismiss on the computed rows just written.

    For each dismiss that is not withdrawn and not of another original:
    the computed rows at its address with its ``rect_type`` and no
    decision yet, the one with the best IoU against the copied box, at
    least the threshold, each row taken once. An unresolved dismiss is
    logged and left standing; PR D raises it as an issue.

    :param scan: The scan whose computed rows were just written.
    :returns: How many dismissals landed, and the ones that did not.
    """
    scan.refresh_from_db(fields=["source_fingerprint"])
    dismissals = list(
        Redaction.objects.human()
        .filter(scan=scan, kind=Redaction.Kind.DISMISS)
        .order_by("pk")
    )
    if not dismissals:
        return 0, []
    rows = list(
        Redaction.objects.computed().filter(
            scan=scan,
            decision__isnull=True,
            source_page__in={d.source_page for d in dismissals},
            rect_type__in={d.rect_type for d in dismissals},
        )
    )
    by_address: dict[tuple, list[Redaction]] = {}
    for row in rows:
        by_address.setdefault(
            (row.source_edit_id, row.source_page, row.rect_type), []
        ).append(row)
    landed = 0
    stale: list[Redaction] = []
    taken: set[int] = set()
    for dismiss in dismissals:
        if is_stale(dismiss, scan) or dismiss.target_bbox is None:
            stale.append(dismiss)
            continue
        candidates = by_address.get(
            (dismiss.source_edit_id, dismiss.source_page, dismiss.rect_type),
            [],
        )
        best, best_iou = None, 0.0
        for row in candidates:
            if row.pk in taken:
                continue
            score = iou(row.bbox, dismiss.target_bbox)
            if score > best_iou:
                best, best_iou = row, score
        if best is None or best_iou < IOU_THRESHOLD:
            stale.append(dismiss)
            continue
        taken.add(best.pk)
        Redaction.objects.filter(pk=best.pk).update(decision=dismiss)
        landed += 1
    if stale:
        logger.warning(
            "scan %s: %d redaction dismissal(s) found no box to land on: %s",
            scan.pk,
            len(stale),
            ", ".join(
                f"#{d.pk} {d.rect_type} src p.{d.source_page}"
                for d in stale[:20]
            ),
        )
    return landed, stale


def is_stale(row: Redaction, scan: Scan) -> bool:
    """Return whether a row was written against another original.

    Blank on either side matches anything, the rule of
    ``page_edits.is_stale``.

    :param row: The row.
    :param scan: Its scan.
    :returns: Whether the fingerprints disagree.
    """
    if not row.source_fingerprint or not scan.source_fingerprint:
        return False
    return row.source_fingerprint != scan.source_fingerprint


# ---------------------------------------------------------------------------
# The readers
# ---------------------------------------------------------------------------


def visible_by_page(scan: Scan) -> list[dict]:
    """Return the boxes to paint, grouped by page, in the viewer's shape.

    ``[{"page_index": i, "rects": [{"id", "x0", "y0", "x1", "y1", "fill",
    "rect_type", "origin"}]}]``, points, pages in order.

    :param scan: The scan.
    :returns: The list.
    """
    pages: dict[int, list[dict]] = {}
    for row in (
        Redaction.objects.visible()
        .filter(scan=scan)
        .order_by("page_index", "y0", "x0")
    ):
        pages.setdefault(row.page_index, []).append(
            {
                "id": row.pk,
                "x0": row.x0,
                "y0": row.y0,
                "x1": row.x1,
                "y1": row.y1,
                "fill": row.fill,
                "rect_type": row.rect_type,
                "origin": row.origin,
            }
        )
    return [
        {"page_index": index, "rects": rects}
        for index, rects in sorted(pages.items())
    ]


# ---------------------------------------------------------------------------
# The curator's decisions
# ---------------------------------------------------------------------------


def add(
    scan: Scan,
    page_index: int,
    bbox: list[float],
    fill: str,
    user,
    run: ApplyRun | None | object = _RESOLVE,
    replaces: Redaction | None = None,
    rect_type: str = Redaction.MANUAL_TYPE,
) -> Redaction:
    """Write a box the curator drew, addressed by its source page.

    :param scan: The scan.
    :param page_index: The 0-based page in the rows' space.
    :param bbox: ``[x0, y0, x1, y1]`` in points.
    :param fill: ``black`` or ``white``.
    :param user: The curator. May be None.
    :param run: The space ``page_index`` is in: an apply run, None for
        the original's, or left out to resolve it
        (:func:`detections.measured_run`).
    :param replaces: The dismiss this box is drawn in place of.
    :param rect_type: The type to store; the computed box's on a move.
    :returns: The new row.
    :raises UnaddressableRedaction: If the map has no such page.
    """
    if run is _RESOLVE:
        run = detections.measured_run(scan)
    edit_id, page = detections.source_for_index(scan, page_index, run)
    if not page:
        logger.warning(
            "scan %s: page_index %s has no address in the standing map; "
            "the drawn box is refused",
            scan.pk,
            page_index,
        )
        raise UnaddressableRedaction(
            f"page_index {page_index} of scan {scan.pk} has no address"
        )
    x0, y0, x1, y1 = (_round(v) for v in bbox)
    if x0 >= x1 or y0 >= y1:
        raise ValueError("a box needs a positive width and height")
    return Redaction.objects.create(
        scan=scan,
        origin=Redaction.Origin.HUMAN,
        kind=Redaction.Kind.ADD,
        rect_type=rect_type,
        fill=fill,
        x0=x0,
        y0=y0,
        x1=x1,
        y1=y1,
        replaces=replaces,
        source_edit_id=edit_id,
        source_page=page,
        source_fingerprint=scan.source_fingerprint or "",
        page_index=page_index,
        apply_run=run,
        author=user,
    )


def dismiss(scan: Scan, row: Redaction, user) -> Redaction | None:
    """Take a box out of the volume.

    A computed row gets a ``dismiss`` with a copy of its box, and its
    ``decision`` set; a second call is a no-op that returns the standing
    dismiss. A human ``add`` is withdrawn (:func:`withdraw`), and None
    is returned; a moved box deleted this way stays deleted, the
    computed box it replaced does not come back (that is
    :func:`undo_move`).

    :param scan: The scan.
    :param row: The row.
    :param user: The curator. May be None.
    :returns: The standing dismiss of a computed row, else None.
    """
    if row.origin == Redaction.Origin.HUMAN:
        withdraw(Redaction.objects.filter(pk=row.pk), user)
        return None
    with transaction.atomic():
        row = (
            Redaction.objects.select_for_update(of=("self",))
            .select_related("decision")
            .get(pk=row.pk)
        )
        if row.decision_id and row.decision.withdrawn_at is None:
            return row.decision
        decision = Redaction.objects.create(
            scan=scan,
            origin=Redaction.Origin.HUMAN,
            kind=Redaction.Kind.DISMISS,
            rect_type=row.rect_type,
            fill=row.fill,
            target_x0=row.x0,
            target_y0=row.y0,
            target_x1=row.x1,
            target_y1=row.y1,
            source_edit_id=row.source_edit_id,
            source_page=row.source_page,
            source_fingerprint=scan.source_fingerprint or "",
            page_index=row.page_index,
            apply_run=row.apply_run,
            author=user,
        )
        Redaction.objects.filter(pk=row.pk).update(decision=decision)
    return decision


def move(scan: Scan, row: Redaction, bbox: list[float], user) -> Redaction:
    """Move or resize a box, and return the row that holds it now.

    A human ``add`` is the curator's own and is written in place. A
    computed row is not written: it is dismissed and a human ``add`` is
    drawn where the curator put the box, naming the dismiss in
    ``replaces``, so the move survives the next compute.

    :param scan: The scan.
    :param row: The row.
    :param bbox: The new ``[x0, y0, x1, y1]`` in points.
    :param user: The curator.
    :returns: The row that holds the box.
    """
    x0, y0, x1, y1 = (_round(v) for v in bbox)
    if x0 >= x1 or y0 >= y1:
        raise ValueError("a box needs a positive width and height")
    if row.origin == Redaction.Origin.HUMAN:
        Redaction.objects.filter(pk=row.pk).update(x0=x0, y0=y0, x1=x1, y1=y1)
        row.refresh_from_db()
        return row
    with transaction.atomic():
        decision = dismiss(scan, row, user)
        # A second move of the same computed box, in flight before the
        # first answer reached the viewer, must not draw a second box:
        # the add that replaced it stands, so the new box goes on it.
        # ``dismiss`` holds the computed row's lock for this whole
        # transaction, so the second request sees the first one's add.
        standing = (
            Redaction.objects.human()
            .filter(kind=Redaction.Kind.ADD, replaces=decision)
            .order_by("pk")
            .first()
        )
        if standing is not None:
            Redaction.objects.filter(pk=standing.pk).update(
                x0=x0, y0=y0, x1=x1, y1=y1
            )
            standing.refresh_from_db()
            return standing
        return add(
            scan,
            row.page_index,
            [x0, y0, x1, y1],
            row.fill,
            user,
            run=row.apply_run,
            replaces=decision,
            rect_type=row.rect_type,
        )


def withdraw(rows, user) -> int:
    """Take back the human rows in ``rows``. Nothing is deleted.

    A withdrawn dismiss clears ``decision`` on the computed row it hid,
    so that box paints again. A withdrawn add paints no more, and that
    is all: the dismiss it ``replaces`` stands, so deleting a moved box
    deletes it and does not bring the computed box back. The undo of a
    move, which does bring it back, is :func:`undo_move`.

    :param rows: A queryset of human rows.
    :param user: Who took them back. May be None.
    :returns: How many rows were stamped.
    """
    now = timezone.now()
    count = 0
    with transaction.atomic():
        for row in rows.filter(
            origin=Redaction.Origin.HUMAN, withdrawn_at__isnull=True
        ):
            stamped = Redaction.objects.filter(
                pk=row.pk, withdrawn_at__isnull=True
            ).update(withdrawn_at=now, withdrawn_by=user, date_modified=now)
            if not stamped:
                continue
            count += 1
            if row.kind == Redaction.Kind.DISMISS:
                Redaction.objects.filter(decision=row).update(decision=None)
    return count


def undo_move(scan: Scan, row: Redaction, user) -> int:
    """Take a moved box back to where the compute put it.

    The add is withdrawn, and the dismiss it ``replaces`` with it, so the
    computed box paints again at its first position. The button is
    #287's; this is the one path that cascades, on purpose: a delete of
    the moved box (:func:`dismiss`) must not.

    :param scan: The scan.
    :param row: The add that replaced a computed box.
    :param user: The curator.
    :returns: How many rows were stamped: 2, or 0 when nothing stood.
    """
    if row.kind != Redaction.Kind.ADD or not row.replaces_id:
        return 0
    with transaction.atomic():
        count = withdraw(Redaction.objects.filter(pk=row.pk), user)
        if count:
            count += withdraw(
                Redaction.objects.filter(pk=row.replaces_id), user
            )
    return count


def restore(scan: Scan, row: Redaction, user) -> bool:
    """Give a dismissed computed box back, as it was: the undo of a delete.

    The dismiss is withdrawn, and so is every standing add that replaced
    it (a moved copy): the computed box and its moved copy must not
    paint together.

    :param scan: The scan.
    :param row: The computed row.
    :param user: The curator.
    :returns: Whether a standing dismiss was withdrawn.
    """
    if row.origin != Redaction.Origin.COMPUTED:
        return False
    # The caller's instance may predate the dismiss: read the FK now.
    decision_id = (
        Redaction.objects.filter(pk=row.pk)
        .values_list("decision_id", flat=True)
        .first()
    )
    if not decision_id:
        return False
    with transaction.atomic():
        withdraw(Redaction.objects.filter(replaces_id=decision_id), user)
        return bool(withdraw(Redaction.objects.filter(pk=decision_id), user))


def human_rows(scan: Scan):
    """Return the standing human rows, for the relocation after a compute.

    :param scan: The scan.
    :returns: A queryset.
    """
    return Redaction.objects.human().filter(scan=scan)
