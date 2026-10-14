"""The findings of review 2: one rebuild from the rows, and the dismissals.

A finding of review 2 is an ``Issue`` row whose ``check_name`` is in
``models.REVIEW2_CHECKS`` (issue #240, PR D). Twelve checks, in three
groups:

- **about detections and opinions**: a key icon or a caption no opinion
  boundary names (``unmatched_key_icon``, ``unmatched_caption``), a
  headnote bracket the reader saw and the model did not
  (``missing_headnote_bracket``, ``brackets.missing``, #328), and a key
  icon with no caption box after it, so the opinion it closes runs on
  (``missing_caption``, the one error card: an opinion starts at a
  caption box, and a miss is shown, never covered), and a block of
  text the reader saw that no box covers, which the opinion text would
  drop (``undetected_text``, ``undetected.uncovered``);
- **about redactions and pages**: a confident headnote box no black
  redaction covers (``uncovered_headnote``), a headnote bracket box
  under blackletter's redaction gate (``low_confidence_headnote_bracket``,
  #410), an editor's note the reader saw and no redaction covers
  (``uncovered_editors_note``, ``editorial.uncovered``, #450), a run of
  pages no opinion covers (``uncovered_pages``);
- **about the curator's own rows**: a decision the last compute could
  not land or place (``stale_detection_edit``, ``stale_redaction_edit``,
  ``stale_boundary_edit``).

Four rules run through the module:

- **Every finding is derived from the rows, and nothing else.**
  :func:`rebuild` reads ``Detection``, ``OpinionBoundary``,
  ``Redaction`` and the decision rows, deletes the scan's review-2
  rows and writes them again. No S3 read and no page render, so it
  runs after every curator write as well as at the end of the compute,
  and the "Recompute" button of the findings panel calls it in the
  request, on any pod (``views_api.rebuild_findings``, #305). A card
  that only the compute rewrote would stand whatever the curator did.
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

from blackletter.bl_warm import rows_are_bl_warm
from blackletter.models import Label
from blackletter.scanner import HEAD_MATTER_LABELS, label_confidence
from django.db import transaction
from django.db.models import Count, Q
from django.utils import timezone

from scanning import (
    boundaries,
    brackets,
    detections,
    editorial,
    redactions,
    undetected,
)
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

#: The head-matter labels of blackletter's pairing, by name, the form a
#: ``Detection`` row carries.
HEAD_MATTER_NAMES = frozenset(label.name for label in HEAD_MATTER_LABELS)

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
        read = read_ids(scan)
        found.extend(_unmatched_findings(scan, rows, read))
        found.extend(_uncovered_pages_findings(scan, rows, run))
        found.extend(_uncovered_headnote_findings(scan, read))
        found.extend(_low_confidence_bracket_findings(scan))
        found.extend(brackets.missing(scan, rows, run))
        found.extend(editorial.uncovered(scan, run))
        found.extend(undetected.uncovered(scan, run))
    resolve(scan, found)
    with transaction.atomic():
        # One row lock on the scan serializes the rebuilds. Under READ
        # COMMITTED a second DELETE that starts before the first
        # transaction commits does not see its new rows, and both sets
        # survive: two tabs, a double click, or a curator write during
        # a compute doubled every card.
        list(
            Scan.objects.select_for_update()
            .filter(pk=scan.pk)
            .values_list("pk", flat=True)
        )
        Issue.objects.filter(scan=scan, check_name__in=REVIEW2_CHECKS).delete()
        Issue.objects.bulk_create(
            [
                Issue(
                    scan=scan,
                    severity=f.get("severity", Issue.Severity.WARNING),
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
    reads off the card, so the Approve and Delete buttons work on every
    card ``_review_findings.html`` shows them on (the unmatched cards
    and ``low_confidence_headnote_bracket``), plus the source address
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


def read_ids(scan: Scan) -> set[int]:
    """Return the ids of the live rows the compute read.

    ``services.detection_entries`` is the one list the compute pairs
    and measures, and blackletter's junk pass has run over it: a key
    icon or a headnote on the blot along a page edge, an image box
    that is a text box's twin and a key that is the show-through of
    the leaf's other side are not in it. A finding about a box takes
    its candidates from this set, never from a raw query over the live
    rows, or a box the compute never saw is a card about an opinion it
    was never part of. The rows themselves stay live: only the list
    changes.

    :param scan: The scan.
    :returns: The ``Detection`` pks of the entries.
    :rtype: set[int]
    """
    from scanning.services import detection_entries

    return {
        entry["id"]
        for entry in detection_entries(scan.pk, page_numbers={})
        if entry.get("id") is not None
    }


def _unmatched_findings(
    scan: Scan, rows: list[OpinionBoundary], read: set[int] | None = None
):
    """Yield the key icons and captions no live boundary names.

    A key no boundary ends is one of three things. Inside a computed
    opinion it is the key after which the model boxed no caption:
    ``blackletter.scanner._pair_on_keys_before`` let the opinion that closed last run
    on to the next key rather than invent a start, and the miss is the
    ``missing_caption`` error, placed where the caption is likely to be
    (``_caption_guess``). Before the first computed opinion it is the
    first key of the volume with no caption before it, the same error,
    on the key. Any other (a second box over one icon, a key past the
    last caption) is the ``unmatched_key_icon`` warning.
    """
    paired_captions = set()
    paired_keys = set()
    spans: list[OpinionBoundary] = []
    for row in rows:
        if row.is_dismissed:
            continue
        if row.start_detection_id:
            paired_captions.add(row.start_detection_id)
        if row.end_detection_id:
            paired_keys.add(row.end_detection_id)
        if row.origin == OpinionBoundary.Origin.COMPUTED:
            spans.append(row)
    # The keys the compute read (:func:`read_ids`): the blot along a
    # page edge is left out of that list, and it makes no card either.
    if read is None:
        read = read_ids(scan)
    # Of those, the ones the pairing read as keys (``blackletter.scanner.key_icons``,
    # the gate and the size filter over the volume at once): a sliver
    # along a page edge closed no opinion, so it is no card either.
    candidates = list(
        Detection.objects.live()
        .filter(scan=scan, label="KEY_ICON", pk__in=read)
        .order_by("page_index", "y0")
    )
    read_keys = boundaries.key_icon_rows(
        candidates, _model_rows_are_bl_warm(scan)
    )
    keys = [det for det in read_keys if det.pk not in paired_keys]
    paired_rows = {
        row.pk: row for row in Detection.objects.filter(pk__in=paired_keys)
    }
    by_page: dict[int, list[Detection]] = {}
    if keys:
        pages = (
            {k.page_index for k in keys}
            | {k.page_index + 1 for k in keys}
            | {k.page_index - 1 for k in keys}
        )
        # The column and synopsis boxes place the guess; the head matter
        # and the caption boxes say whether a caption is drawn already.
        for det in Detection.objects.live().filter(
            scan=scan,
            page_index__in=pages,
            label__in=[
                Label.TEXT_COLUMN.name,
                Label.CASE_CAPTION.name,
                *HEAD_MATTER_NAMES,
            ],
        ):
            by_page.setdefault(det.page_index, []).append(det)
        # The keys the pairing read, paired or not: the next one down
        # a column ends the guess, because a caption never runs past
        # the key that closes its own opinion. Never a box the size
        # filter rejected: a sliver between the key and a drawn
        # caption would end the place before the caption.
        for det in read_keys:
            by_page.setdefault(det.page_index, []).append(det)
    for det in keys:
        mid_pt, _ = boundaries.to_points(
            det.img_width / 2, 0, det.img_width, det.img_height
        )
        pos = _reading_position(det, mid_pt)
        if _over_a_paired_key(det, paired_rows):
            # A second box over one icon: the warning, as for a key
            # past the last caption, never the error of a missed
            # caption, which it is not.
            yield _detection_finding(
                CheckName.UNMATCHED_KEY_ICON,
                Issue.Target.DETECTION,
                det,
                f"A key icon (confidence {det.confidence:.2f}) is not matched "
                "to any opinion.",
            )
            continue
        starts = [
            (r.start_page_index, r.start_x >= mid_pt, r.start_y) for r in spans
        ]
        # A span ends at its key's own reading position, the key's
        # top, not the bottom the boundary stores: a box over the end
        # key compares as inside against the bottom.
        ends = [
            _reading_position(paired_rows[r.end_detection_id], mid_pt)
            if r.end_detection_id in paired_rows
            else (r.end_page_index, r.end_x >= mid_pt, r.end_y)
            for r in spans
        ]
        holder = next(
            (
                span
                for span, start, end in zip(spans, starts, ends)
                if start < pos < end
            ),
            None,
        )
        if holder is not None:
            if _caption_drawn_after(det, by_page, mid_pt):
                continue
            yield _missing_caption_finding(det, by_page, holder)
        elif starts and pos < min(starts):
            yield _first_key_finding(det)
        else:
            yield _detection_finding(
                CheckName.UNMATCHED_KEY_ICON,
                Issue.Target.DETECTION,
                det,
                f"A key icon (confidence {det.confidence:.2f}) is not matched "
                "to any opinion.",
            )
    paired_key_positions = sorted(
        (d.page_index, d.x0, d.y0) for d in paired_rows.values()
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


def _over_a_paired_key(det: Detection, paired_rows: dict) -> bool:
    """Return whether a key box overlaps a key a boundary ends at.

    Two boxes over one icon are one key, and the pairing skipped the
    second; it is the unmatched-key warning here, never the error of a
    caption nobody missed.
    """
    return any(
        row.page_index == det.page_index
        and min(det.x1, row.x1) > max(det.x0, row.x0)
        and min(det.y1, row.y1) > max(det.y0, row.y0)
        for row in paired_rows.values()
    )


def _model_rows_are_bl_warm(scan: Scan) -> bool:
    """Return whether bl-warm alone drew the scan's live model rows.

    The family picks blackletter's confidence gates, so every reader of
    a gate asks this and never copies the number: the bracket card and
    the key icons of the pairing's rule (``blackletter.scanner.key_icons``).

    :param scan: The scan.
    :returns: ``rows_are_bl_warm`` over the live model rows' ``found_by``.
    """
    return rows_are_bl_warm(
        [
            {"found_by": found_by}
            for found_by in Detection.objects.live()
            .filter(scan=scan)
            .exclude(model_name=Detection.ModelName.MANUAL)
            .exclude(found_by=[])
            .values_list("found_by", flat=True)
        ]
    )


def _caption_drawn_after(
    key: Detection, by_page: dict[int, list[Detection]], mid_pt: float
) -> bool:
    """Return whether a live caption box sits in the caption's place.

    The place is the pairing's (``blackletter.scanner._pair_on_keys_before``): after
    the key in reading order, before the first box of the head matter
    after it or the next key read, and inside a column box of its page
    (a faint box below the column is a footnote the model called a
    caption, and the pairing takes it for nothing). A box there is the
    one the next compute starts the opinion on, so the card is answered
    the moment a curator draws it, like a bracket box answers
    ``brackets.missing``; a caption past the synopsis is a box over the
    body, and the card stays.

    :param key: The key icon with no caption boxed after it.
    :param by_page: The live rows of the key's pages, the set
        ``_unmatched_findings`` reads: the columns, the captions, the
        head matter and the keys read.
    :param mid_pt: The page's middle, in points.
    :returns: Whether a caption is drawn in the place.
    """
    pos = _reading_position(key, mid_pt)
    after = [
        (_reading_position(d, mid_pt), d)
        for page in (key.page_index, key.page_index + 1)
        for d in by_page.get(page, ())
        if d.pk != key.pk
    ]
    after = [(p, d) for p, d in after if p > pos]
    limit = min(
        (
            p
            for p, d in after
            if d.label in HEAD_MATTER_NAMES or d.label == Label.KEY_ICON.name
        ),
        default=None,
    )
    columns = {}
    for page in (key.page_index, key.page_index + 1):
        columns[page] = [
            d
            for d in by_page.get(page, ())
            if d.label == Label.TEXT_COLUMN.name
        ]
    return any(
        d.label == Label.CASE_CAPTION.name
        and (limit is None or p < limit)
        and _in_a_column(d, columns.get(d.page_index))
        for p, d in after
    )


def _in_a_column(det: Detection, columns: list[Detection] | None) -> bool:
    """Return whether a box's centre lies in one of its page's column boxes.

    The pairing's own test (``blackletter.scanner.in_a_column``) over
    rows: a page with no column box vouches for every box.
    """
    if not columns:
        return True
    cx = (det.x0 + det.x1) / 2
    cy = (det.y0 + det.y1) / 2
    return any(c.x0 <= cx <= c.x1 and c.y0 <= cy <= c.y1 for c in columns)


def _reading_position(
    det: Detection, mid_pt: float
) -> tuple[int, bool, float]:
    """Return where a box sits in reading order, in a boundary's terms.

    The page, the column (the right one when the box's centre is past
    the page's middle, in points) and the top, in points: the order the
    start and end points of an ``OpinionBoundary`` sort by.
    """
    x, y = boundaries.to_points(
        (det.x0 + det.x1) / 2, det.y0, det.img_width, det.img_height
    )
    return det.page_index, x >= mid_pt, y


#: The space a caption guess leaves under the key icon, as a share of
#: the key's height: the next title starts a line or two under it.
_CAPTION_GAP = 0.5

#: A column with less than this many key heights left under the key
#: holds no caption: the guess moves to the next column.
_CAPTION_MIN_KEY_HEIGHTS = 2.0

#: How far a guess with no column box to end it runs, in key heights.
_CAPTION_FALLBACK_KEY_HEIGHTS = 4.0


def _caption_guess(
    key: Detection, by_page: dict[int, list[Detection]]
) -> tuple[int, list[float], Detection]:
    """Guess the place of a caption the model did not box, after ``key``.

    The caption starts in the first column after the key with room
    under it, some space down (``_CAPTION_GAP``): the key's own column
    when its text column box runs on at least ``_CAPTION_MIN_KEY_HEIGHTS``
    key heights below the key, else the facing column, else a column of
    the next page. The first synopsis (``BACKGROUND``) box or the next
    key icon of that column under the start ends it (the orders of a
    S.Ct. volume are a column of captions, each closed by a key), else
    the column's foot. A caption
    that runs on into the next column is boxed in its first column
    alone. A key's page with no column box of its own (the washed-out
    page 792 of scan 3579, whose keys were real and whose print the
    model could not read) borrows the column boxes of the page before
    or after it, because the pages of a volume share their geometry,
    and the guess stays on the key's page, keyed by the key's own
    address; a guess that moved to the next page named a spot three
    cards shared, and the caption drawn there answered none of them.
    With no column box anywhere, a few key heights under the key.

    :param key: The key icon the caption follows.
    :param by_page: The live ``TEXT_COLUMN`` and ``BACKGROUND`` rows and
        the read ``KEY_ICON`` rows of the key's page, the one before and
        the next, by page index.
    :returns: The page index, the box in that page's pixels, and the
        row whose page address the dismissal is keyed by.
    """
    key_height = key.y1 - key.y0
    gap = _CAPTION_GAP * key_height
    left = (key.x0 + key.x1) / 2 < key.img_width / 2
    columns: list[tuple[int, bool, float | None]] = [
        (key.page_index, left, key.y1 + gap)
    ]
    if left:
        columns.append((key.page_index, False, None))
    columns.append((key.page_index + 1, True, None))
    columns.append((key.page_index + 1, False, None))
    for page_index, is_left, top in columns:
        dets = [
            d
            for d in by_page.get(page_index, ())
            if ((d.x0 + d.x1) / 2 < d.img_width / 2) == is_left
        ]
        boxes = [d for d in dets if d.label == Label.TEXT_COLUMN.name]
        anchor = boxes[0] if boxes else key
        if not boxes and page_index == key.page_index:
            boxes = _neighbour_columns(key.page_index, is_left, by_page)
        if not boxes:
            continue
        start = top if top is not None else min(d.y0 for d in boxes)
        foot = max(d.y1 for d in boxes)
        if foot - start < _CAPTION_MIN_KEY_HEIGHTS * key_height:
            continue
        ends = [
            d.y0
            for d in dets
            if d.label in (Label.BACKGROUND.name, Label.KEY_ICON.name)
            and d.y0 >= start
        ]
        bottom = max(min(ends) if ends else foot, start + key_height)
        box = [
            min(d.x0 for d in boxes),
            start,
            max(d.x1 for d in boxes),
            bottom,
        ]
        return page_index, box, anchor
    top = key.y1 + gap
    bottom = min(
        key.img_height, top + _CAPTION_FALLBACK_KEY_HEIGHTS * key_height
    )
    return key.page_index, [key.x0, top, key.x1, bottom], key


def _neighbour_columns(
    page_index: int, is_left: bool, by_page: dict[int, list[Detection]]
) -> list[Detection]:
    """Return the column boxes of the page before, else after, on one side."""
    for neighbour in (page_index - 1, page_index + 1):
        boxes = [
            d
            for d in by_page.get(neighbour, ())
            if d.label == Label.TEXT_COLUMN.name
            and ((d.x0 + d.x1) / 2 < d.img_width / 2) == is_left
        ]
        if boxes:
            return boxes
    return []


def _missing_caption_finding(
    key: Detection, by_page: dict[int, list[Detection]], span: OpinionBoundary
) -> dict:
    """Build the error of a caption the model did not box after ``key``.

    The card is placed where the caption is likely to be
    (``_caption_guess``), on that page, and carries no
    ``detection_id``: there is no box to approve or delete, and the
    dismissal is keyed by the page address plus the guessed box. It
    names the computed opinion that runs on past the key (``runs_on``,
    the pk of ``span``, the one row whose start and end enclose the
    key in reading order), so the sidebar lists it under that opinion
    alone and never under every opinion of the key's page.
    """
    page_index, box, row = _caption_guess(key, by_page)
    return {
        "check_name": CheckName.MISSING_CAPTION,
        "target": Issue.Target.BOUNDARY,
        "severity": Issue.Severity.ERROR,
        "page_number": page_index + 1,
        "message": (
            "No caption box was found after the key icon that closes the "
            f"opinion before (page {key.page_index + 1}), so no opinion "
            "starts there and that opinion runs on to the next key. The "
            "box is where the caption is likely to be. Draw the caption "
            "box; the next compute splits the opinion on it."
        ),
        "metadata": {
            "page_index": page_index,
            "label": Label.CASE_CAPTION.name,
            "label_id": Label.CASE_CAPTION.value,
            "bbox": box,
            "img_width": row.img_width,
            "img_height": row.img_height,
            "source_edit": row.source_edit_id,
            "source_page": row.source_page,
            "key_page_index": key.page_index,
            # The key the caption follows, for the crop of the card
            # review (``card_review.caption_card``).
            "key_bbox": [key.x0, key.y0, key.x1, key.y1],
            "runs_on": span.pk,
        },
    }


def _first_key_finding(key: Detection) -> dict:
    """Build the error of the volume's first key icon with no caption before it."""
    finding = _detection_finding(
        CheckName.MISSING_CAPTION,
        Issue.Target.BOUNDARY,
        key,
        "No caption box was found before this key icon, the first of the "
        "volume, so the pages before it belong to no opinion. Draw the "
        "caption box of the first opinion; the next compute pairs it on "
        "the box.",
    )
    finding["severity"] = Issue.Severity.ERROR
    del finding["metadata"]["detection_id"]
    return finding


def _uncovered_pages_findings(
    scan: Scan, rows: list[OpinionBoundary], run: ApplyRun | None
):
    """Yield one finding per run of pages no live opinion covers.

    The arithmetic is ``utils.compute_coverage_gaps``'s: an index plus
    ``Scan.start_page`` is the printed number, as the warning line said
    before this was a row. The printed pages of the final space live in
    S3, and a rebuild from an endpoint must not read them. The address
    is the first page alone: a volume short of its last pages has a gap
    that ends past the map, and the end page adds nothing, since two
    runs cannot start on one page.
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
            },
        }


def _uncovered_headnote_findings(scan: Scan, read: set[int] | None = None):
    """Yield one finding per confident headnote box no headnote box covers.

    The detections are in the render's pixels and the redactions in
    points (PR B); the box centre is converted with
    ``boundaries.to_points``, which is within a point of the compute's
    own scale. Cover is a computed ``headnote`` rect, the rule the
    step-2 view applied before this was a row, or any black box a
    curator drew: they drew it to cover the headnote. A computed box of
    another type (a key number) is not cover, whatever it overlaps.
    """
    boxes: dict[int, list[tuple[float, float, float, float]]] = {}
    for row in (
        Redaction.objects.visible()
        .filter(scan=scan, fill=Redaction.Fill.BLACK)
        .filter(Q(rect_type="headnote") | Q(origin=Redaction.Origin.HUMAN))
    ):
        boxes.setdefault(row.page_index, []).append(
            (row.x0, row.y0, row.x1, row.y1)
        )
    # The headnote boxes the compute read (:func:`read_ids`): a box on
    # the blot along the page edge had no redaction because it was
    # never in the document, and it is no card.
    if read is None:
        read = read_ids(scan)
    for det in (
        Detection.objects.live()
        .filter(
            scan=scan,
            label="HEADNOTE",
            confidence__gte=HEADNOTE_CONFIDENCE,
            pk__in=read,
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


def _low_confidence_bracket_findings(scan: Scan):
    """Yield one finding per bracket box too weak for blackletter to redact.

    YOLO keeps a box down to 0.20, and step 2 draws every live row, but
    blackletter redacts a detection only at or above
    ``label_confidence`` (0.30 for bl-warm, 0.50 for the others). A
    bracket box between the two is drawn, answers
    ``brackets.missing``, and leaves the bracket in the deliverable
    (#410).

    The gate is blackletter's own function, and the model family is the
    one the compute reads: ``rows_are_bl_warm`` over the ``found_by`` of
    the live model rows, one flag for the volume, the list
    ``services.detection_entries`` hands it. A hand-drawn row is left
    out: it carries no model confidence and no provenance. The family
    is read only when a box is under the higher gate, so a volume with
    no weak bracket pays one query.

    The card offers the two detection buttons: an approval writes 1.0
    on the row, so the card goes and the next compute redacts the box;
    a deletion takes out a wrong box, and ``brackets.missing`` then
    judges the reading under it.
    """
    label = Label.HEADNOTE_BRACKET
    gates = {
        bl_warm: label_confidence(label, bl_warm) for bl_warm in (True, False)
    }
    model_rows = (
        Detection.objects.live()
        .filter(scan=scan)
        .exclude(model_name=Detection.ModelName.MANUAL)
    )
    weak = list(
        model_rows.filter(
            label=label.name, confidence__lt=max(gates.values())
        ).order_by("page_index", "y0")
    )
    if not weak:
        return
    gate = gates[_model_rows_are_bl_warm(scan)]
    for det in weak:
        if det.confidence >= gate:
            continue
        yield _detection_finding(
            CheckName.LOW_CONFIDENCE_HEADNOTE_BRACKET,
            Issue.Target.REDACTION,
            det,
            f"A headnote bracket box (confidence {det.confidence:.2f}) is "
            f"under the redaction gate ({gate:.2f}), so it is not redacted. "
            "Approve it to redact it at the next compute, or delete it if "
            "it is not a bracket.",
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
    if check != CheckName.UNCOVERED_PAGES:
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
        return True
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
    counts = Issue.objects.filter(
        scan=scan, check_name__in=REVIEW2_CHECKS, dismissal__isnull=True
    ).aggregate(
        open=Count("pk"),
        stale=Count("pk", filter=Q(check_name__in=STALE_REVIEW2_CHECKS)),
    )
    return counts["open"] or 0, counts["stale"] or 0


def viewer_groups(scan: Scan) -> dict:
    """Return the step-2 findings section's context.

    The rows of the scan, annotated for the template: ``nav_pdf_index``
    (the position in the drawn space, ``page_number - 1``),
    ``is_stale``, and grouped by ``target`` in ``TARGET_GROUPS`` order.
    The page and the fragment render one template from this one
    context, so it carries no printed page label: the fragment cannot
    read the printed pages of the final space without S3, and a label
    that differed between the two renders was the fault this rule
    exists to prevent. The viewer labels each card from the page map it
    holds (``labelFindingCards`` in ``viewer_sidebar.js``).

    :param scan: The scan.
    :returns: ``finding_groups``, ``findings_computed`` (a computed
        boundary exists, so the measured checks were run),
        ``review2_open``, ``review2_stale``, ``review2_total``.
    """
    rows = list(
        Issue.objects.filter(
            scan=scan, check_name__in=REVIEW2_CHECKS
        ).order_by("page_number", "pk")
    )
    by_target: dict[str, list[Issue]] = {}
    for row in rows:
        row.nav_pdf_index = row.page_number - 1 if row.page_number else None
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
