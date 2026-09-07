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
  the hook, and it says yes for every scan until issue #224 lands. The
  build of the corrected volume is that issue's, and one function is
  its whole surface here.
- **The redactions are computed from the detection run.** That is the
  run's own ``applied_at`` stamp (``yolo.apply_state``), **not**
  ``Scan.redaction_rects``: a volume with no headnote to hide gets an
  empty rect list from a computation that fully succeeded, and it
  still needs a curator to judge its pairing.

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


def final_volume_ready(scan: Scan) -> bool:
    """Return whether the page complete volume of this scan is built.

    The hook for issue #224, and its whole surface in this module. That
    issue builds the corrected volume from the approved page edits as
    an ``ApplyRun``; until it lands, no scan has one and no scan can
    wait for one, so every scan passes this condition.

    **What #224 changes here:** ask the model for a glued run of this
    scan's ``source_fingerprint``, and return that. Nothing else in
    this module moves, because every reader of the rule goes through
    :func:`redaction_review_ready`.

    :param scan: The scan to judge.
    :returns: Whether the corrected volume exists.
    :rtype: bool
    """
    return True


def redaction_review_ready(scan: Scan, rows: list | None = None) -> bool:
    """Return whether this scan's redaction review may begin.

    The rule of issue #263, minus the scan's status: every caller holds
    that already, and reading it here would answer a different question
    for the apply (which is claimed *out* of the status) than for the
    pass (which filters *on* it).

    :param scan: The scan to judge.
    :param rows: The live detection rows, when the caller has them --
        the apply reads them anyway, and this saves the query. Read
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
    if not yolo.apply_state(rows).get("applied_at"):
        return False
    return final_volume_ready(scan)


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
