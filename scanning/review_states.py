"""The edges of review 2: when a volume is ready for it, and who says so.

Issue #263, and the mirror of what #154 did for review 1. Review 2 had
no status of its own: a volume sat in ``PAGE_COMPLETENESS_REVIEW_DONE``
before its redactions were measured, while a curator judged them, and
after the curator agreed. One value said three things, so nothing
downstream could tell them apart -- least of all a report (#260) or the
step 3 that #206 brings back.

**The status is the state, and the state is derived.** The issue lists
conditions, not events, so the rule lives in one function
(:func:`redaction_review_ready`) and both writers call it. A rule in
one place cannot disagree with itself, which is the same reason
``views_process._review_flags`` renders one bar from one read (#151).

The three conditions, and what answers each one:

- **Review 1 is approved.** The caller holds the scan's status, so the
  rule does not read it: the apply is claimed from that status, and
  the pass filters on it.
- **The page complete volume exists.** :func:`final_volume_ready` is
  the hook, and one function is the whole surface of issue #224 here:
  the standing ``ApplyRun`` has every glue written
  (``ApplyRun.is_complete``), for this original.
- **The redactions are computed from the detection run, against the
  standing apply run.** That is the run's own ``applied_at`` stamp
  plus the ``apply_run`` it names (``yolo.redactions_current``, #269),
  **not** ``Scan.redaction_rects``: a volume with no headnote to hide
  gets an empty rect list from a computation that fully succeeded, and
  it still needs a curator to judge its pairing.

Two callers, deliberately:

- ``services._park_after_redactions`` parks a successful apply straight
  in ``READY_FOR_REDACTION_REVIEW``. The viewer reloads the page when
  the scan parks (``viewer_progress.js``), so a park in the old status
  would land the curator on a step 2 whose approve button appears a
  tick later, from nothing they did.
- :func:`promote_ready_scans` runs on the collect tick and catches what
  the apply could not see: a volume whose corrected build finishes
  *after* its geometry (#224), and every volume already parked in
  ``PAGE_COMPLETENESS_REVIEW_DONE`` when this ships.

A legacy volume is out of both. Its step 2 lives in ``PENDING_REVIEW``,
because the #154 and #263 states describe a flow it never went through
(``services._park_after_redactions`` already makes that split).

One state lives *before* this edge: :func:`preview_only` (#388), the
read-only step 2 of a volume whose redactions nobody has measured. It
is here because it is the same question asked from the other side --
"the real review cannot open yet, and nothing is being built for it" --
and two rules that disagreed would show a preview over the review.
"""

from __future__ import annotations

import logging

from scanning.models import (
    JobEngine,
    JobProvider,
    JobStage,
    JobStatus,
    Scan,
    Status,
)

logger = logging.getLogger(__name__)


_UNSET = object()


def final_run(scan: Scan, run=_UNSET):
    """Return the standing apply run when the corrected volume is built.

    The hook for issue #224, and its whole surface in this module. The
    corrected volume is the standing ``ApplyRun`` of the scan
    (``apply.current_run``), and it exists when **every** glue is
    written (``ApplyRun.is_complete``): the final ``bitonal.pdf``, the
    OCR volume with its printed pages, and the detections in the final
    page space. Review 2 judges the redactions of the corrected volume,
    so no output of that volume may still be missing when the review
    opens. Every reader of the final space asks this one function
    (#269): the redaction compute before it queues and when it runs,
    the step-2 view, the PDF route and the crop route.

    The run must describe this original: a run built before a
    re-upload carries the old fingerprint, and a blank on either side
    is a legacy value that matches anything (the rule of
    ``page_edits.is_stale``).

    :param scan: The scan to judge.
    :param run: The standing run, when the caller already read it
        (``views_process._review_flags`` does); ``None`` for a scan
        with no run. Read here otherwise.
    :returns: The run, or ``None`` when no corrected volume exists.
    :rtype: ApplyRun | None
    """
    from scanning import apply

    if run is _UNSET:
        run = apply.current_run(scan)
    if run is None or not run.is_complete:
        return None
    mine, theirs = run.source_fingerprint, scan.source_fingerprint
    if mine and theirs and mine != theirs:
        return None
    return run


def final_volume_ready(scan: Scan) -> bool:
    """Return whether the page complete volume of this scan is built.

    :func:`final_run` as a yes or no, for the callers that do not need
    the run itself.

    :param scan: The scan to judge.
    :returns: Whether the corrected volume exists.
    :rtype: bool
    """
    return final_run(scan) is not None


def redaction_review_ready(
    scan: Scan, rows: list | None = None, run=_UNSET
) -> bool:
    """Return whether this scan's redaction review may begin.

    The rule of issue #263, minus the scan's status: every caller holds
    that already, and reading it here would answer a different question
    for the apply (which is claimed *out* of the status) than for the
    pass (which filters *on* it).

    "The redactions are computed" means computed against the standing
    run (``yolo.redactions_current``, #269): a run that supersedes the
    one the rows were measured on makes the geometry stale, and the
    review must wait for the compute that follows it.

    :param scan: The scan to judge.
    :param rows: The live detection rows, when the caller has them --
        the apply reads them anyway, and this saves the query. Read
        here otherwise.
    :param run: The standing apply run, when the caller has it. Read
        here otherwise.
    :returns: Whether the two derivable conditions hold.
    :rtype: bool
    """
    from scanning import yolo

    if rows is None:
        rows = yolo.live_detect_jobs(scan)
    if not rows:
        # No detection run: a legacy volume, or a scan the sweep has
        # not reached. Neither is in this flow.
        return False
    if any(row.status != JobStatus.CONSUMED for row in rows):
        # The run is not merged, so nothing measured its geometry.
        return False
    run = final_run(scan, run)
    return yolo.redactions_current(rows, run)


#: The statuses a detection preview is offered in (#388). Review 1,
#: open or approved: those are the volumes whose geometry nobody has
#: measured yet, and the only ones whose step 2 has nothing of its own
#: to show. Spelled out rather than derived from "not review 2": the
#: legacy ``PENDING_REVIEW`` step 2 has its own rows, and an errored or
#: an unconverted volume is nobody's to preview.
PREVIEW_STATUSES = (
    Status.READY_FOR_PAGE_COMPLETENESS_REVIEW,
    Status.PAGE_COMPLETENESS_REVIEW_DONE,
)


def preview_only(scan: Scan, rows: list | None = None, run=_UNSET) -> bool:
    """Return whether step 2 shows this scan as a read-only preview.

    Issue #388. A volume from a new partner, or in a reporter's own
    format, is judged by its detections long before anybody knows
    whether its page numbers can be read: the page-number gate of #342
    can hold review 1 for days, and until it lifts nothing shows what
    blackletter found. The preview opens step 2 over the two documents
    that exist by then -- the bitonal copy and the merged document of
    the volume detection run -- and computes nothing.

    It is **not** review 2, and every caller treats it as its own
    state: the boxes are the model's own, in the page space of the
    volume as uploaded, and no row of the database is behind them.
    That is why every write of step 2 refuses under it
    (``views_api._refuse_preview``) and why the page says so.

    The conditions, and what answers each one:

    - **The volume is in review 1** (:data:`PREVIEW_STATUSES`). Both
      of those are parked human states and neither is busy
      (``models.REVIEW_STATUSES``), so a scan the daemon holds is out
      by its status alone.
    - **A detection run is merged.** The preview draws that document,
      so a run still in flight has nothing to draw
      (``yolo.live_detect_jobs``, every row ``CONSUMED``).
    - **Review 2 is not ready** (:func:`redaction_review_ready`), and
      **no apply is in progress** (a standing run that is not
      complete). Both say the same thing from two sides: the moment the
      corrected volume exists, or is being built, the real review owns
      step 2 and the preview is out of the way.

    The approved status is in the set on purpose. Between the approval
    and the apply's first run there is a window with no standing run,
    which is exactly the volume whose disclaimer says to wait for the
    corrected volume; it is short, so in practice this is a review-1
    view.

    :param scan: The scan to judge.
    :param rows: The live detection rows, when the caller has them.
        Read here otherwise.
    :param run: The standing apply run, when the caller has it. Read
        here otherwise.
    :returns: Whether the preview may be shown.
    :rtype: bool
    """
    from scanning import apply, yolo

    if scan.status not in PREVIEW_STATUSES:
        return False
    if rows is None:
        rows = yolo.live_detect_jobs(scan)
    if not rows or any(row.status != JobStatus.CONSUMED for row in rows):
        return False
    if run is _UNSET:
        run = apply.current_run(scan)
    if run is not None and not run.is_complete:
        # The corrected volume is being built. What it builds is the
        # page space the real review reads, so nothing is previewed
        # over the pages it is about to leave behind.
        return False
    return not redaction_review_ready(scan, rows, run)


def promote_ready_scans() -> int:
    """Take every qualifying approved scan to the redaction review.

    The collect tick's last pass, and the safety net of the two
    writers: the apply parks a scan it just finished, and this catches
    the scans it could not. Two of them exist. A volume whose corrected
    build lands after its geometry (#224) meets the last condition with
    no apply running, and every volume already parked in
    ``PAGE_COMPLETENESS_REVIEW_DONE`` when this ships was measured
    before the status existed.

    Cheap by the same shape ``yolo.queue_ready_runs`` uses: the
    candidates are the approved scans that carry a ``CONSUMED``
    detection row, which is a small set, and each one costs one row
    read plus at most one write.

    The write is a compare-and-swap over the approved status. Losing it
    is not an error and is not marked: a scan somebody moved between
    the read and the write is seen again on the next tick.

    :returns: How many scans were promoted.
    :rtype: int
    """
    from scanning import yolo

    scan_ids = (
        Scan.objects.filter(
            status=Status.PAGE_COMPLETENESS_REVIEW_DONE,
            jobs__stage=JobStage.DETECT,
            jobs__engine=JobEngine.BLACKLETTER,
            jobs__provider=JobProvider.RUNPOD,
            jobs__status=JobStatus.CONSUMED,
            # The volume run only: a page edit apply's one-page shards
            # (#224) share the stage, and the rule would refuse them
            # anyway, at the cost of a read per tick.
            jobs__apply_run__isnull=True,
        )
        .values_list("pk", flat=True)
        .distinct()
    )
    promoted = 0
    for scan in Scan.objects.filter(pk__in=list(scan_ids)):
        rows = yolo.live_detect_jobs(scan)
        if not redaction_review_ready(scan, rows):
            continue
        moved = Scan.objects.filter(
            pk=scan.pk, status=Status.PAGE_COMPLETENESS_REVIEW_DONE
        ).update(
            status=Status.READY_FOR_REDACTION_REVIEW,
            progress_message=(
                "The redactions are ready: check them in the detection review."
            ),
        )
        if not moved:
            continue
        logger.info(
            "review_states: scan %s is ready for the redaction review",
            scan.pk,
        )
        promoted += 1
    return promoted
