"""The counts of the stats page (issue #260).

The portal has no column that stamps a review: the approve button of
review 1 writes ``PAGE_COMPLETENESS_REVIEW_DONE`` and one log line,
and nothing else records the decision. So every count here is a set of
``Scan.status`` values, and one count adds a condition on the
``Detection`` rows.

Three rules run through the module:

- **A row with no file is not an upload.** ``presign_scan_upload``
  creates the row and ``confirm_scan_upload`` attaches the file. A
  browser that dies between the two leaves a row with an empty
  ``original_pdf``, and nothing deletes it. :func:`uploaded_scans` is
  where every other query starts.
- **A volume is the pair (reporter, volume number)**, the unique key
  of the ``Volume`` model. ``Scan.volume_obj`` accepts NULL, so a scan
  outside the queue would drop out of a count that read it.
- **The retired pipeline gets one counter, not a stage.** Its review 1
  and its end are gone, so :data:`LEGACY_STATUSES` holds the four
  statuses no new scan can reach, and :data:`FUNNEL_ROWS` counts the
  new pipeline alone.
"""

from __future__ import annotations

from django.db.models import (
    CharField,
    Count,
    Exists,
    OuterRef,
    QuerySet,
    Value,
)
from django.db.models.functions import Concat

from scanning import repairs
from scanning.models import Detection, Scan, Status

#: The statuses of the retired pipeline (#173). No new scan reaches
#: any of the four, so the status alone tells a legacy row from a live
#: one, and the page needs no query per scan:
#:
#: - ``PENDING_REVIEW`` is review 1 of the legacy rows. A scan of the
#:   new pipeline goes to READY_FOR_PAGE_COMPLETENESS_REVIEW (#154).
#: - ``APPROVED`` comes from ``views_api.approve_scan``, which first
#:   demands ``Stage.APPROVED`` from the file generation of step 3.
#:   Step 3 is absent (#206). **When #206 lands, move this value out
#:   of here**: it becomes a live status, and it then belongs to
#:   "passed the page completeness review" and to "redaction review
#:   complete".
#: - ``EXTRACTED`` has no writer at all. The text stage (#191) is
#:   switched off behind two locks.
#: - ``CANCELLED`` lost its writer with the user cancel (#219).
LEGACY_STATUSES = (
    Status.PENDING_REVIEW,
    Status.APPROVED,
    Status.EXTRACTED,
    Status.CANCELLED,
)

#: The rows of the first table: where the scans are now. The groups do
#: not overlap, and together they hold every ``Status`` value, so the
#: rows add up to the total. ``TestStatusGroups`` pins both
#: properties, or a status added later would drop off the page in
#: silence.
STATUS_GROUPS = (
    ("waiting_to_start", "Uploaded, not started", (Status.UPLOADED,)),
    (
        "in_the_pipeline",
        "In the pipeline",
        (
            Status.QUEUED,
            Status.PROCESSING,
            Status.AWAITING,
            Status.AWAITING_VALIDATION,
        ),
    ),
    (
        "page_review",
        "Ready for page completeness review",
        (Status.READY_FOR_PAGE_COMPLETENESS_REVIEW,),
    ),
    (
        "page_review_done",
        "Page completeness review done",
        (Status.PAGE_COMPLETENESS_REVIEW_DONE,),
    ),
    (
        "failed",
        "Stopped with an error",
        (
            Status.ERROR,
            Status.ERROR_MAX_RETRIES,
            Status.ERROR_INTERRUPTED,
        ),
    ),
    # One row for the retired pipeline. A legacy row that stopped with
    # an error counts as an error, not as a legacy row: the status is
    # the only signal the page reads.
    ("legacy", "Legacy rows (retired pipeline)", LEGACY_STATUSES),
)

#: The rows of the second table: the review funnel of issue #260. The
#: rows are cumulative and they overlap by design, because a scan that
#: passed review 1 also waits for review 2.
#:
#: Each entry is ``(key, label, statuses, needs_detections, note)``.
#: An empty ``statuses`` tuple means the stage that would write the
#: count is absent, so the row reads zero and needs no query. The note
#: says why.
#:
#: The funnel holds no legacy row, and it needs no filter for one: a
#: legacy scan reaches neither of the two #154 statuses.
FUNNEL_ROWS = (
    (
        "page_review_ready",
        "Ready for page completeness review",
        (Status.READY_FOR_PAGE_COMPLETENESS_REVIEW,),
        False,
        "",
    ),
    (
        "page_review_passed",
        "Passed the page completeness review",
        (Status.PAGE_COMPLETENESS_REVIEW_DONE,),
        False,
        "",
    ),
    (
        "redaction_review_ready",
        "Ready for redaction review",
        (Status.PAGE_COMPLETENESS_REVIEW_DONE,),
        True,
        "The detection run is merged into rows a curator can edit.",
    ),
    (
        "redaction_review_done",
        "Redaction review complete",
        (),
        False,
        "Nothing writes this count yet: step 3 is absent (#206).",
    ),
    (
        "text_review_ready",
        "Ready for text review",
        (),
        False,
        "Nothing writes this count yet: the text stage is off (#191).",
    ),
)


def uploaded_scans() -> QuerySet:
    """Return the scans that hold a file.

    Every count of the page starts here. A row with an empty
    ``original_pdf`` is an upload the browser never confirmed, and
    nothing deletes it, so it must not raise a number.

    :returns: The ``Scan`` rows with an original.
    :rtype: QuerySet
    """
    return Scan.objects.exclude(original_pdf="")


def _volume_key() -> Concat:
    """Return the expression that identifies a volume.

    The pair (reporter, volume number) is the unique key of the
    ``Volume`` model, and a ``Scan`` carries both columns itself. The
    expression goes **inside** ``Count(..., distinct=True)``, never
    into an ``annotate`` before a ``values`` call: an annotation there
    would join the ``GROUP BY`` and break the group.

    :returns: A text expression, one value per volume.
    :rtype: Concat
    """
    return Concat(
        "reporter_id", Value(":"), "volume", output_field=CharField()
    )


def _totals(queryset: QuerySet) -> dict:
    """Return how many scans and how many volumes the rows hold.

    :param queryset: Any set of ``Scan`` rows.
    :returns: ``{"scans": int, "volumes": int}``.
    :rtype: dict
    """
    return queryset.aggregate(
        scans=Count("pk"),
        volumes=Count(_volume_key(), distinct=True),
    )


def _row(key: str, label: str, queryset: QuerySet, note: str = "") -> dict:
    """Return one table row: the label, the counts, and the note.

    :param key: The name of the row, for a test and a template.
    :param label: What the page prints.
    :param queryset: The scans the row counts.
    :param note: One sentence under the label, or an empty string.
    :returns: The row.
    :rtype: dict
    """
    return {"key": key, "label": label, "note": note, **_totals(queryset)}


def status_groups() -> list[dict]:
    """Return the first table: where the scans are now.

    One aggregate per group. The scan counts add up to the scan total,
    because :data:`STATUS_GROUPS` covers every status once.

    :returns: One row per group, in the order of ``STATUS_GROUPS``.
    :rtype: list[dict]
    """
    return [
        _row(key, label, uploaded_scans().filter(status__in=statuses))
        for key, label, statuses in STATUS_GROUPS
    ]


def funnel() -> list[dict]:
    """Return the second table: the review funnel.

    A row with no status reads zero and costs no query: the stage that
    would write it is absent, and the note says so.

    :returns: One row per entry of :data:`FUNNEL_ROWS`.
    :rtype: list[dict]
    """
    rows = []
    for key, label, statuses, needs_detections, note in FUNNEL_ROWS:
        if not statuses:
            rows.append(
                {
                    "key": key,
                    "label": label,
                    "note": note,
                    "scans": 0,
                    "volumes": 0,
                }
            )
            continue
        queryset = uploaded_scans().filter(status__in=statuses)
        if needs_detections:
            # The same rule as the "Next: Detect" button, which walks
            # to step 2 when a Detection row exists
            # (``views_process.start_detect``) -- and with no filter on
            # ``active`` either, or the number and the button would
            # disagree.
            queryset = queryset.filter(
                Exists(Detection.objects.filter(scan=OuterRef("pk")))
            )
        rows.append(_row(key, label, queryset, note))
    return rows


def collect() -> dict:
    """Return the whole context of the stats page.

    :returns: The two totals, the two tables, and the repair pair.
    :rtype: dict
    """
    requests, scans = repairs.waiting_totals()
    return {
        "totals": _totals(uploaded_scans()),
        "status_groups": status_groups(),
        "funnel": funnel(),
        "repairs": {"requests": requests, "scans": scans},
    }
