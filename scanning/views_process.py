"""Process viewer and scan processing action views."""

import itertools
import json
import logging
import os
import re
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import fitz
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.http import (
    FileResponse,
    Http404,
    HttpRequest,
    HttpResponse,
    JsonResponse,
)
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.views.decorators.clickjacking import xframe_options_sameorigin
from django.views.decorators.http import require_POST

from scanning import (
    boundaries,
    dots_mocr,
    findings,
    jobs,
    mistral_ocr,
    opinion_pdf,
    page_edits,
    page_numbers,
    repairs,
    s3_sync,
    stats,
    surya,
    yolo,
)
from scanning.models import (
    BUSY_STATUSES,
    PAGE_EDIT_ROTATIONS,
    PAGE_REVIEW_APPROVED_STATUSES,
    PHYSICAL_PAGE_CHECKS,
    REVIEW2_CHECKS,
    REVIEW_STATUSES,
    CheckName,
    Detection,
    ExternalJob,
    Issue,
    JobEngine,
    JobStage,
    JobStatus,
    Opinion,
    OpinionBoundary,
    OpinionScan,
    PageEdit,
    PageRepairRequest,
    QueuedAction,
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

#: Flashed by the review-1 actions of issue #151. They are constants so
#: the tests assert the copy the curator actually reads.
PAGE_REVIEW_APPROVED_MESSAGE = (
    "Thank you. This scan is marked as page complete."
)
PAGE_REVIEW_ALREADY_DONE_MESSAGE = (
    "This scan is already marked as page complete."
)
PAGE_REVIEW_NOT_READY_MESSAGE = (
    "This scan is not ready for the page completeness review."
)
#: Flashed when the approval is refused because a scanner was asked
#: for a page (#266). It names the two ways out: the new scan arrives,
#: or somebody dismisses the request. A refusal with no way out would
#: strand the review.
REPAIRS_WAITING_MESSAGE = (
    "This scan waits for a scanner. Somebody asked for a page that is "
    "missing or bad, so the volume is not page complete yet. Wait for "
    "the new scan, or dismiss the request on the page if it no longer "
    "applies."
)
#: Flashed when the approval is refused because a page carries no page
#: number (#342). The opinions are named by the printed page, so a page
#: with no number would give one a name nobody approved. It names the
#: two ways out, as the message above does.
PAGE_NUMBERS_MISSING_MESSAGE = (
    "This scan has {count} page{plural} with no page number: {pages}. "
    "The opinions of this volume are named by their printed page, so "
    "every page needs one. Type the number on the page, or dismiss its "
    "\u201cNo page number detected\u201d card when the page carries no "
    "printed number."
)
#: How many pages :func:`page_numbers_missing_message` names. A volume
#: can leave hundreds, and a message nobody reads to the end helps
#: nobody; the bar sends the reviewer to the first of them.
MAX_NAMED_PAGES = 8
#: Flashed by the review-2 approval of issue #263, and constants for
#: the same reason as the three above.
REDACTION_REVIEW_APPROVED_MESSAGE = (
    "Thank you. The opinions of this scan are created on the server, and "
    "this page reloads when they are ready."
)
REDACTION_REVIEW_ALREADY_DONE_MESSAGE = (
    "The redactions of this scan are already marked as reviewed."
)
REDACTION_REVIEW_QUEUED_MESSAGE = (
    "The opinions of this scan are being created. Wait for the page to reload."
)
REDACTION_REVIEW_NOT_READY_MESSAGE = (
    "This scan is not ready for the redaction review. The redactions "
    "are computed after the page completeness approval, and this page "
    "shows them when they are there."
)
LEGACY_OCR_RECOMPUTE_MESSAGE = (
    "The old OCR engine that read this scan no longer runs here. Run "
    "OCR again to recompute the page numbers."
)
RECOMPUTE_DONE_MESSAGE = "The page number issues are recomputed."
#: The 409 of ``dismiss_issue`` on a review-2 finding (#240 PR D).
REVIEW2_FINDING_NOT_HERE_MESSAGE = (
    "This is a finding of the redaction review. Dismiss it from step 2."
)
REVALIDATE_UNAVAILABLE_MESSAGE = (
    "This scan cannot be re-run from here. Sharding, the bitonal "
    "conversion and dots.mocr are deterministic, so a re-run adds "
    "nothing. Ask an admin to re-queue the scan if it really must be "
    "processed a second time."
)
PAGE_REVIEW_APPROVAL_REQUIRED_MESSAGE = (
    "Approve the page completeness review first. Then continue to detection."
)
#: Answered to a request over a page a scanner already rescanned after
#: an earlier request (#249). The unique key matches the open row, and
#: the row reads fulfilled, so nothing new is created: say so, and say
#: the way out. Silence here is the fault the date rule removed, one
#: step later.
REPAIR_ALREADY_FULFILLED_MESSAGE = (
    "This page was already requested, and a new scan of it is saved. If "
    "the new scan is bad too, dismiss the old request on the page and ask "
    "again, or upload a better scan with Replace."
)
PENDING_EDITS_SAVED_MESSAGE = (
    "Your page changes are saved, and not built into the volume yet. "
    "Approve this volume when the pages are complete. The corrected "
    "volume is then built from your changes, and each inserted, "
    "replaced or rotated page goes through the conversion and the OCR "
    "on its own."
)
#: The review-1 edits are locked once the review is approved (#224):
#: the apply builds the final volume from the rows as they stand at
#: the approval, so a row written after it would address a source the
#: pipeline has left behind. A late correction reopens the review.
EDITS_LOCKED_MESSAGE = (
    "The page review of this volume is approved, so its pages cannot "
    "be edited. Ask a staff member to reopen the page review first."
)
PAGE_REVIEW_REOPENED_MESSAGE = (
    "The page review is open again. Make the corrections, then approve "
    "the volume once more; the corrected volume is rebuilt from them."
)
PAGE_REVIEW_NOT_REOPENABLE_MESSAGE = (
    "Only an approved page review can be reopened, and this volume's "
    "is not approved."
)
#: The statuses under which a page edit endpoint refuses a write: an
#: approved review (DONE), a scan the daemon holds -- the apply may be
#: building from the rows at that moment -- and every post-review
#: state. A scan before or outside the review keeps its rows editable:
#: nothing reads them until the review runs.
LOCKED_STATUSES = frozenset(
    {
        Status.PAGE_COMPLETENESS_REVIEW_DONE,
        Status.READY_FOR_REDACTION_REVIEW,
        Status.REDACTION_REVIEW_DONE,
        Status.APPROVED,
        Status.EXTRACTED,
        *BUSY_STATUSES,
    }
)

#: What a page file may start with, and the extension that says what
#: it is. The content type is the browser's word, and the stored
#: extension decides how ``views_api.export_pdf`` and the apply (#206)
#: read the file later, so the first bytes decide -- for an image as
#: much as for a PDF. The image formats are the ones MuPDF opens:
#: ``insert_image`` fails on any other, and a file it refuses would be
#: found out at the apply rather than at the upload. WEBP and SVG are
#: absent for that reason.
_PDF_MAGIC = b"%PDF-"
_IMAGE_MAGIC = (
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"\xff\xd8\xff", "jpg"),
    (b"GIF87a", "gif"),
    (b"GIF89a", "gif"),
    (b"II*\x00", "tif"),
    (b"MM\x00*", "tif"),
    (b"BM", "bmp"),
)
_MAGIC_LENGTH = max(len(_PDF_MAGIC), *(len(m) for m, _ in _IMAGE_MAGIC))

#: The refusal of a page file over the cap. ``{mb}`` is the cap in MB,
#: from ``settings.PAGE_UPLOAD_MAX_BYTES``; see
#: :func:`upload_too_large_message`.
UPLOAD_TOO_LARGE_MESSAGE = (
    "This file is larger than {mb} MB. Upload the scanned pages, not a volume."
)
UPLOAD_WRONG_TYPE_MESSAGE = (
    "Upload an image of the page (PNG, JPEG, GIF, TIFF or BMP), or a "
    "PDF of it."
)
UPLOAD_LOST_RACE_MESSAGE = (
    "Somebody else changed this page at the same moment. Reload the "
    "page to see their file."
)
UPLOAD_BAD_PDF_MESSAGE = (
    "This PDF could not be opened. Upload it again, or send an image."
)
REPLACEMENT_IS_ONE_PAGE_MESSAGE = (
    "A replacement stands for one page, and this PDF holds {pages}. "
    "Upload the one page that replaces it."
)
# What a curator sees when they ask for step 2 on a volume with no
# detections and no detection run. Since #250 the daemon starts the
# run by itself once per shard set, so a volume with no run at all is
# a legacy volume with no shard set, an environment with the stage
# off, or a sweep that has not ticked yet.
NO_DETECTIONS_MESSAGE = (
    "This volume has no detections yet. Detection starts by itself "
    "after the upload, and the redactions appear here when it "
    "finishes. If nothing shows after a few minutes, ask a staff "
    "member."
)

#: The step-2 warning when the run's printed pages could not be read
#: (#269). The page renders with positional labels instead.
PRINTED_PAGES_UNAVAILABLE_MESSAGE = (
    "The printed page numbers of the corrected volume did not load. "
    "Reload the page to try again."
)
#: The 409 of ``serve_final_pdf`` and the ``space=final`` routes when
#: the corrected volume is not built or not measured yet (#269).
FINAL_VOLUME_NOT_READY_MESSAGE = (
    "The corrected volume of this scan is not ready yet. Reload the "
    "page in a minute."
)
#: The 404 of ``scan_ocr_text_url`` for a volume this engine has not
#: read yet (#262): no glued run of it. Every message of this endpoint
#: names the engine since #381, because the dropdown offers three and a
#: message that says "the OCR" would leave a reader guessing which one.
NO_READ_TEXT_MESSAGE = (
    "{label} has not read this volume yet, so there is no text to show."
)
#: The 404 of ``scan_ocr_text_url`` in the final space, for an engine
#: that read the original and not the corrected volume (#381).
#: ``ApplyRun.is_complete`` counts neither ``extract_key`` nor
#: ``surya_key``, so review 2 opens on a volume only dots.mocr read
#: there (#245, #368).
NO_READ_FINAL_TEXT_MESSAGE = (
    "{label} has not read the corrected volume of this scan yet, so "
    "there is no text to show over its pages."
)
#: The 404 of ``scan_ocr_text_url`` when the document was written and
#: is not in the bucket any more (#262).
OCR_TEXT_OBJECT_GONE_MESSAGE = (
    "The text {label} read of this volume is not in the bucket. Ask a "
    "staff member to glue the run again."
)
#: The 400 of ``scan_ocr_text_url`` for an engine nobody has (#381).
#: A person never sees it: the dropdown offers the names of
#: ``opinion_ocr.ENGINES`` and no other.
UNKNOWN_OCR_ENGINE_MESSAGE = "Unknown OCR engine {engine!r}. Known: {known}."
#: The 409 of ``serve_final_pdf`` when the run's bitonal copy is the
#: original itself: a 1-bit upload skips the conversion, and the
#: preview route never streams the original (#185).
FINAL_VOLUME_IS_ORIGINAL_MESSAGE = (
    "This scan is already black-and-white, so its corrected volume is "
    "the original. Load the original scan to see it."
)


def run_is_glued(summary: dict | None) -> bool:
    """Say whether a scan's live run of one engine is glued (#262).

    Off the summary the process view reads already
    (``jobs.run_summary``), so the text overlay's dropdown costs no
    query. The glue writes the document and flips every row to
    ``CONSUMED`` in one pass, so "every row consumed" is the same test
    :func:`jobs.glued_volume_key` makes against the rows.

    One function for the three engines, not three (#381): the test is
    the shared one, and the summary is the shared shape.

    :param summary: The run summary, or None when the stage never ran.
    :returns: Whether a glued volume document exists for the live run.
    :rtype: bool
    """
    if not summary:
        return False
    return summary["statuses"].get(JobStatus.CONSUMED) == summary["total"]


def engine_label(name: str) -> str:
    """Return what a person calls one OCR engine (#381).

    ``JobEngine`` carries the name already, as the label of its own
    choice ("dots.mocr", "Mistral OCR", "Surya"). The dropdown and
    every refusal of :func:`scan_ocr_text_url` read it here, so the
    words are not copied into a second table. ``ShardRead`` keeps its
    own two: one of them is a message word ("OCR run started"), not
    the engine's name.

    :param name: A ``JobEngine`` value, which is a key of
        ``opinion_ocr.ENGINES``.
    :returns: The label of that choice.
    :rtype: str
    """
    return JobEngine(name).label


def ocr_run_summaries(scan) -> dict[str, dict | None]:
    """Return one run summary per OCR engine, in the table's order.

    One walk of ``opinion_ocr.ENGINES`` (#381), because every engine
    has the same ``run_summary(scan)``. The process view reads the
    three by name for the action bar and hands the whole dict to
    :func:`ocr_text_engines`, so the summaries are read once.

    :param scan: The scan to describe.
    :returns: ``{engine name: summary or None}``.
    :rtype: dict[str, dict | None]
    """
    from scanning import opinion_ocr

    return {
        name: spec.module.run_summary(scan)
        for name, spec in opinion_ocr.ENGINES.items()
    }


def ocr_text_engines(
    summaries: dict[str, dict | None], final_run, final_space: bool
) -> list[dict]:
    """Describe every OCR engine for the text overlay's dropdown (#381).

    One entry per engine of ``opinion_ocr.ENGINES``, in that table's
    order, so dots.mocr is first and is the default. ``available`` is
    the same question :func:`scan_ocr_text_url` asks, in the space the
    viewer draws: the ``ApplyRun`` field in the final space, a glued
    volume run in the original one.

    The dropdown offers an engine that did not read, disabled and
    labelled: a viewer must not hide the state of a read, and a
    chooser must not offer a selection the endpoint refuses.

    ``selected`` marks the first engine that did read, because an HTML
    select opens on its first option whether that option is disabled or
    not. dots.mocr is first in the table, so it is the default wherever
    it read.

    :param summaries: ``{engine name: run summary or None}``, the
        summaries the process view reads already.
    :param final_run: The ``ApplyRun`` of the corrected volume, or
        None.
    :param final_space: Whether the viewer draws the corrected volume.
    :returns: ``[{"name", "label", "available", "selected"}]``.
    :rtype: list[dict]
    """
    from scanning import opinion_ocr

    entries = []
    for name, spec in opinion_ocr.ENGINES.items():
        if final_space:
            available = bool(final_run and spec.document_key(final_run))
        else:
            available = run_is_glued(summaries.get(name))
        entries.append(
            {
                "name": name,
                "label": engine_label(name),
                "available": available,
                "selected": False,
            }
        )
    for entry in entries:
        if entry["available"]:
            entry["selected"] = True
            break
    return entries


def detection_message(summary: dict | None) -> str:
    """Say where a volume's detection stands, for a curator (#250).

    One text for the "Next: Detect" title and the flash the view sends
    when it cannot walk to step 2, so the bar and the view agree.
    ``summary`` is ``yolo.run_summary(scan)``: ``None`` when the stage
    has never run, else the counts of the live run.

    :param summary: The run summary, or ``None``.
    :returns: The message.
    :rtype: str
    """
    if not summary:
        return NO_DETECTIONS_MESSAGE
    if summary["failed"]:
        code = summary["error_code"] or "no error code"
        return (
            f"Detection failed on {summary['failed']} of "
            f"{summary['total']} part(s) ({code}). Ask a staff member "
            "to look into it."
        )
    if summary["open"]:
        return (
            f"Detection is running: {summary['done']} of "
            f"{summary['total']} part(s) done. The redactions appear "
            "here when it finishes."
        )
    return (
        "Detection finished. The redactions are computed within a "
        "minute of the page completeness approval."
    )


@login_required
def scan_process_view(request: HttpRequest, pk: int) -> HttpResponse:
    """Unified scan processing page with 3-step workflow.

    :param request: The HTTP request.
    :param pk: Scan primary key.
    :return: Rendered processing page.
    """
    scan = get_object_or_404(Scan.objects.select_related("reporter"), pk=pk)
    is_processing = scan.status in BUSY_STATUSES
    # Breadcrumb for the web-pod observability trail (issue #115): this view
    # does an S3 pull plus a render over potentially large detection sets, so a
    # hang/OOM here should leave a marker in the pod logs and Sentry.
    logger.info(
        "scan_process_view: rendering scan=%s status=%s", scan.pk, scan.status
    )

    # No eager S3 pull here: this page renders entirely from the DB
    # (page_map, ocr_results, the boundaries, detections, the redactions),
    # so it never reads the processing files off disk. Pulling them here
    # blocked the response on I/O it doesn't need -- worst right after a
    # fresh upload, when the only object in the prefix is the multi-GB
    # original and the download took minutes. The PDF and crop assets are
    # streamed by serve_scan_pdf / serve_original_crop, which lazily pull
    # from S3 on demand.

    try:
        step = int(request.GET.get("step", 0))
    except ValueError:
        step = 0
    if step < 1 or step > 3:
        if is_processing:
            step = 1
        elif scan.stage == Stage.APPROVED:
            step = 3
        elif scan.status in (
            Status.PAGE_COMPLETENESS_REVIEW_DONE,
            Status.READY_FOR_REDACTION_REVIEW,
            Status.REDACTION_REVIEW_DONE,
        ):
            # Review 1 is done (#154), so land on the detection review
            # when detections exist. The detection stage (#195) writes
            # no scan status, so its output is the only signal.
            #
            # The two #263 states land there as well, review 2 done
            # included: step 3 is paused (#173/#206), and step 2 is
            # where that state is shown and where its "Next: Generate"
            # link waits. Send nobody to a step whose only button
            # refuses.
            if Detection.objects.filter(scan=scan).exists():
                step = 2
            else:
                step = 1
        elif (
            scan.stage == Stage.PROCESS
            or OpinionBoundary.objects.computed().filter(scan=scan).exists()
        ):
            # Stay on step 1 if there are unresolved issues
            has_issues = scan.issues.exclude(
                check_name__in=REVIEW2_CHECKS
            ).exists()
            has_missing = bool(scan.missing_pages)
            if has_issues or has_missing:
                step = 1
            else:
                step = 2
        else:
            step = 1

    # The review-1 rows only: a review-2 finding (#240 PR D) names a
    # page by its position in the space the redaction rows are drawn
    # in, which is not step 1's, and the step-2 section reads those
    # rows itself (``findings.viewer_groups``, below).
    issues = list(scan.issues.exclude(check_name__in=REVIEW2_CHECKS))

    # No external stage writes a scan status by design (#190, #195,
    # #191), so their rows are the only place their progress lives.
    # One walk of ``opinion_ocr.ENGINES`` for the OCR engines (#381):
    # the action bar reads the three by name and the text overlay's
    # dropdown reads them all, off the same summaries.
    ocr_runs = ocr_run_summaries(scan)
    dots_run = ocr_runs["dots_mocr"]
    mistral_run = ocr_runs["mistral_ocr"]
    surya_run = ocr_runs["surya"]
    yolo_run = yolo.run_summary(scan)

    # The pages a reviewer asked a scanner to scan again, or the gaps
    # they asked a scanner to fill (#249). The waiting ones raise the
    # sidebar badge and the section; every open one reaches the viewer,
    # so a fulfilled request shows as fulfilled on its page.
    repair_requests = repairs.viewer_payload(scan)
    waiting_repairs = [r for r in repair_requests if not r["fulfilled"]]
    pages_needing_repair = {
        r["pdf_page"] for r in waiting_repairs if r["pdf_page"] is not None
    }

    # One read of the review flags for the bar and the page (#151), and
    # with them the space the page is drawn in (#269). Step 2 shows the
    # corrected volume of the standing apply run once the boxes are
    # measured against it (``final_space``): the final PDF, the printed
    # pages the run stored, and no review-1 issue or edit, since those
    # address the original. Step 1 always draws the original's space,
    # where every ``PageEdit`` address lives.
    # The findings of review 2 are rows since #240 PR D
    # (``findings.rebuild`` writes them after the compute and after
    # every curator write), read here for the step-2 section, and their
    # counts are handed to the flags so the bar and the section agree.
    review_findings = findings.viewer_groups(scan) if step >= 2 else {}
    flags = _review_flags(
        scan,
        repairs_waiting=bool(waiting_repairs),
        review2=(
            (review_findings["review2_open"], review_findings["review2_stale"])
            if review_findings
            else None
        ),
    )
    # The preview shows the model's boxes and nothing else (#388). The
    # findings, the boundaries and the redactions of a volume in it are
    # either absent or left over from a superseded run (a reopen keeps
    # the rows until the next import), and both answers are wrong on a
    # page that judges today's detections. The read above is paid on
    # this path alone, and the page renders no findings section.
    preview = step >= 2 and flags["preview_available"]
    if preview:
        review_findings = {}
    final_space = step >= 2 and flags["final_space"]
    printed_warning = None
    if final_space:
        page_map, ocr_by_page, printed_warning = _final_space_pages(
            scan, flags["final_run"]
        )
        ocr_results = [ocr_by_page[page] for page in sorted(ocr_by_page)]
        missing_pages = scan.missing_pages
        replaced_pages = {}
        deleted_pages: list[int] = []
        moves: dict[int, int] = {}
        duplicate_indices: set[int] = set()
        flagged_indices: set[int] = set()
        idx_to_logical = {}
        for entry in page_map:
            idx_to_logical[entry["pdf_index"]] = entry["logical_number"]
        for i in issues:
            i.nav_pdf_index = None
    else:
        # Each uploaded image is shown at the gap its row names, and every
        # remaining placeholder is stamped with the physical page it
        # follows, so an upload can send that address back (#214).
        page_map = page_edits.project_inserts(scan, scan.page_map)
        missing_pages = scan.missing_pages

        # The pages a curator replaced (#232). The viewer draws a note on
        # each one, with a link that opens the file the curator uploaded.
        replaced_pages = page_edits.replacements_by_page(scan)

        # Map pdf_index → logical page number for navigation
        idx_to_logical = {}
        logical_to_indices: dict[int, list[int]] = {}
        for entry in page_map:
            if entry.get("type") == "pdf_page":
                idx_to_logical[entry["pdf_index"]] = entry["logical_number"]
                logical_to_indices.setdefault(
                    entry["logical_number"], []
                ).append(entry["pdf_index"])

        # PDF page indices the page_map flags as duplicates (a detected page
        # number that already appeared on an earlier page). The PDF viewer marks
        # these with a DUPLICATE badge; the sidebar page list mirrors the same set
        # so the two views stay consistent. Unlike a consecutive-only check this
        # also catches duplicates whose copies are far apart (e.g. the same
        # printed "page 1" appearing on several pages).
        duplicate_indices = {
            entry["pdf_index"]
            for entry in page_map
            if entry.get("type") == "pdf_page" and entry.get("duplicate")
        }

        # The viewer highlights flagged pages by pdf_index (a page's physical
        # position), which is unique. An issue's ``page_number`` means a physical
        # PDF page for some checks and a logical/printed page number for others;
        # logical numbers can repeat when unnumbered front matter borrows numbers
        # from the real pages (issue #90), so they must be resolved through the
        # page_map rather than matched directly. The set is shared with the
        # dismissal, which keeps its address in the same two spaces (#214).
        flagged_indices: set[int] = set()
        for i in issues:
            # Resolve each issue to PDF page indices (unique physical positions),
            # used both for the red-border highlight and for click-to-navigate.
            # ``nav_pdf_index`` is the first resolved index, or None when the issue
            # has no page (or points at a missing page absent from the page_map).
            i.nav_pdf_index = None
            if i.page_number is None:
                continue
            if i.check_name in PHYSICAL_PAGE_CHECKS:
                indices = [i.page_number - 1]
            else:
                indices = logical_to_indices.get(i.page_number, [])
            flagged_indices.update(indices)
            if indices:
                i.nav_pdf_index = indices[0]

        # The card of a range missing at the end names the placeholder
        # ("ask a scanner for them at the placeholder at the end of the
        # volume", #256), so the card must reach it. Its own address is a
        # printed number the volume does not show, which resolves to no
        # page above, and the placeholder carries the range as its label,
        # so neither of ``goToPage``'s lookups finds it. The physical
        # address does: the page the gap follows, with the placeholder
        # drawn right below it -- the route a repair request already takes
        # (``PageRepairRequest.nav_pdf_index``).
        #
        # After the loop, and outside ``flagged_indices`` on purpose: the
        # last page of the volume is not itself at fault, so it keeps no
        # red border. The projected entry keeps both keys after a curator
        # uploads into the gap, because ``_inserted_entry`` copies it.
        trailing = next((e for e in page_map if e.get("missing_range")), None)
        if trailing:
            for i in issues:
                if (
                    i.check_name == CheckName.LARGE_GAP
                    and i.page_number == trailing["missing_range"][0]
                ):
                    i.nav_pdf_index = max(
                        trailing.get("anchor_pdf_page", 0) - 1, 0
                    )

        ocr_results = scan.ocr_results
        ocr_by_page = {}
        for r in ocr_results:
            ocr_by_page[r["pdf_page"]] = r

        # The sidebar lists the pages in the order the corrected volume
        # holds them (#261): a page a curator moved sits at its new
        # place, with a badge, and the ORDER divider it answered is
        # gone. The entries are the cache's own, still addressed by
        # ``pdf_page``.
        moves = page_edits.moves_by_page(scan)
        ocr_results = page_edits.order_by_moves(ocr_results, moves)

        # Annotate sequence issues for the sidebar page list. Duplicates are taken
        # from ``duplicate_indices`` (the same page_map data the viewer uses);
        # ``seq_issue`` only covers ordering anomalies (backward / gap).
        #
        # A span scanned out of its order (#261, #395): the card for the
        # printed number that steps back gets a button that puts the
        # span in the order of its printed numbers, when the sequence
        # names one. ``page_edits.sorted_window`` is the one rule: a
        # transposed pair, a page pulled early or late, two blocks the
        # wrong way round and a span in reverse are all a window whose
        # numbers are a shuffle of one consecutive run; a step whose
        # window never closes is a misread and gets no button. The card
        # names a printed number and nothing else, so a number two
        # cards share, two windows share, or the volume prints twice
        # gets no button: the wrong page would move.
        runs: list[list[tuple[int, int]]] = [[]]
        backward_steps: list[tuple[int, int, int]] = []
        prev_num = None
        for r in ocr_results:
            r["seq_issue"] = ""
            r["is_duplicate"] = (r["pdf_page"] - 1) in duplicate_indices
            r["is_replaced"] = r["pdf_page"] in replaced_pages
            r["is_moved"] = r["pdf_page"] in moves
            r["needs_repair"] = r["pdf_page"] in pages_needing_repair
            if r.get("type") == page_numbers.SUFFIXED:
                # The book adds this page between two numbered ones, so
                # it breaks no sequence: the page before it and the page
                # after it stay neighbours (#319).
                continue
            if not r.get("detected") or r.get("type") == "range":
                prev_num = None
                runs.append([])
                continue
            try:
                num = int(r["detected"])
            except (ValueError, TypeError):
                prev_num = None
                runs.append([])
                continue
            runs[-1].append((r["pdf_page"], num))
            if prev_num is not None:
                diff = num - prev_num
                if diff < 0:
                    r["seq_issue"] = "backward"
                    backward_steps.append(
                        (len(runs) - 1, len(runs[-1]) - 1, num)
                    )
                elif diff > 2:
                    r["seq_issue"] = "gap"
            prev_num = num
        printed = Counter(num for run in runs for _, num in run)
        current_order = [r["pdf_page"] for r in ocr_results]
        offers: dict[int, list[dict]] = {}
        answered: set[tuple[int, int]] = set()
        for run_index, index, num in backward_steps:
            if (run_index, index) in answered:
                continue
            answered.add((run_index, index))
            run = runs[run_index]
            window = page_edits.sorted_window(run, index)
            if window is None:
                continue
            numbers = dict(run)
            if any(printed[numbers[p]] != 1 for p in window["pdf_pages"]):
                continue
            # The rows come from the whole corrected order with the
            # window sorted in place, so they fold into the moves that
            # stand (an earlier correction, a swap of #379), and the
            # endpoint replaces the standing set with them.
            window["rows"] = page_edits.rows_for_order(
                page_edits.sorted_order(current_order, window)
            )
            # Every step inside the window is answered by it: a span in
            # reverse has one step per page and one window, so the
            # window is computed once and each card carries it.
            covered = set(window["pdf_pages"])
            for other_run, other_index, other_num in backward_steps:
                if other_run == run_index and run[other_index][0] in covered:
                    answered.add((other_run, other_index))
                    offers.setdefault(other_num, []).append(window)
        if scan.status not in LOCKED_STATUSES:
            backward = [
                i for i in issues if i.check_name == CheckName.BACKWARD_PAGE
            ]
            cards = Counter(i.page_number for i in backward)
            for i in backward:
                found = offers.get(i.page_number, [])
                if len(found) == 1 and cards[i.page_number] == 1:
                    i.move = {
                        **found[0],
                        "label": page_edits.move_label(found[0]),
                        "title": page_edits.move_title(found[0]),
                        "moves_json": json.dumps(found[0]["rows"]),
                    }
        deleted_pages = sorted(page_edits.deleted_pages(scan))

    has_detections = Detection.objects.filter(scan=scan).exists()

    # The boundaries are rows since #240 PR C, read in the legacy dict
    # shape plus their ids. The printed numbers come from the OCR rows
    # the view holds already (``ocr_by_page``, keyed by the 1-based
    # page), which are in the space the rows are drawn in (the final
    # space when the redactions are measured against the standing run,
    # #269) and carry a range as ``detected`` plus ``type``. Not from
    # the page map: blackletter's map puts the physical page in
    # ``logical_number`` for a range page and the range in
    # ``range_label``, so a lookup built from it lost the range's end.
    from scanning.services import printed_page_span

    page_spans = {
        page - 1: span
        for page, row in ocr_by_page.items()
        if (span := printed_page_span(row.get("detected"), row.get("type")))
    }
    # No boundary reaches the preview (#388), for the reason the
    # findings do not: the pairing is the compute's own output, so a
    # row here is a superseded run's, and its card carries a dismiss
    # the endpoint refuses.
    opinions = [] if preview else boundaries.viewer_payload(scan, page_spans)
    opinion_count = sum(1 for op in opinions if not op["dismissed"])

    # Build a set of page indices that contain IMAGE detections
    image_page_indices = set(
        Detection.objects.filter(
            scan=scan, label="IMAGE", active=True
        ).values_list("page_index", flat=True)
    )

    # Attach image_pages to each opinion. Each entry carries the logical
    # number to display (``num``) and the pdf_index to navigate to (``idx``);
    # logical numbers can repeat (#90), so navigation must use the index.
    for op in opinions:
        cp = op.get("caption_page", 0)
        ep = op.get("page_end", op.get("key_page", cp))
        op["image_pages"] = [
            {"num": idx_to_logical.get(idx, idx + 1), "idx": idx}
            for idx in range(cp, ep + 1)
            if idx in image_page_indices
        ]

    opinion_scans = []
    if step == 3:
        for s in OpinionScan.objects.filter(scan=scan).order_by(
            "opinion_order"
        ):
            s.redacted_filename = (
                os.path.basename(s.redacted_pdf.name)
                if s.redacted_pdf and s.redacted_pdf.name
                else ""
            )
            s.unredacted_filename = (
                os.path.basename(s.original_pdf.name)
                if s.original_pdf and s.original_pdf.name
                else ""
            )
            opinion_scans.append(s)

    # The printed-pages warning is a request-time condition, not a
    # finding, so it stays a warning line.
    detect_warnings = []

    # The text overlay's dropdown (#262, #381), off the summaries the
    # page already read and the run the flags already found.
    ocr_engines = ocr_text_engines(
        ocr_runs, flags["final_run"], bool(final_space)
    )

    if printed_warning:
        detect_warnings.insert(0, printed_warning)

    return render(
        request,
        "scanning/scan_process.html",
        {
            "scan": scan,
            "step": step,
            "issues": issues,
            "page_map_json": json.dumps(page_map),
            "missing_pages": missing_pages,
            "flagged_indices_json": json.dumps(sorted(flagged_indices)),
            "ocr_results": ocr_results,
            "ocr_by_page_json": json.dumps(ocr_by_page),
            "has_detections": has_detections,
            "is_processing": is_processing,
            "dots_run": dots_run,
            "yolo_run": yolo_run,
            "mistral_run": mistral_run,
            "surya_run": surya_run,
            "detect_message": detection_message(yolo_run),
            **flags,
            "final_space": final_space,
            # The step-scoped answer (#388): the rule is true of the
            # volume, the banner and the locks are true of step 2
            # alone. Step 1 of the same volume is an open page review.
            "preview_only": preview,
            # The text overlay's dropdown (#262, #381). One entry per
            # engine, so the control says which reads exist and which
            # do not; the template draws the pair when one is
            # available, and a legacy PaddleOCR volume gets neither.
            "ocr_text_engines": ocr_engines,
            # The pair is drawn when one engine read this volume. Off
            # the one list, so the control and its options never
            # disagree.
            "ocr_text_available": any(
                engine["available"] for engine in ocr_engines
            ),
            "opinions": opinions,
            "opinion_count": opinion_count,
            "opinions_json": json.dumps(opinions),
            "opinion_scans": opinion_scans,
            "detect_warnings": detect_warnings,
            **review_findings,
            "deleted_pages_json": json.dumps(deleted_pages),
            "moved_pages_json": json.dumps(
                {str(page): anchor for page, anchor in moves.items()}
            ),
            # The rule of the step-1 bar (#151): the viewer must not
            # offer a control the endpoint refuses. Step 2 runs while
            # a new-pipeline volume is in DONE, which locks every page
            # edit (#224), and a legacy PENDING_REVIEW volume is not
            # locked and keeps its page-number control. The final space
            # is locked whatever the status (#269): a page number there
            # is a page of the corrected volume, not an address
            # ``assign_page`` takes. The preview is locked too (#388),
            # and only as a step-2 render: the same volume's step 1 is
            # an open page review, whose edits this flag must not take
            # away.
            "page_edits_locked": (
                final_space or preview or scan.status in LOCKED_STATUSES
            ),
            "repair_requests": repair_requests,
            "waiting_repairs": waiting_repairs,
            "replaced_pages_json": json.dumps(
                {
                    str(page): {
                        "edit_id": edit.pk,
                        "url": reverse(
                            "page_edit_file",
                            kwargs={"pk": scan.pk, "edit_id": edit.pk},
                        ),
                        "kind": page_edits.uploaded_kind(edit),
                    }
                    for page, edit in replaced_pages.items()
                }
            ),
        },
    )


@login_required
def progress_api(request: HttpRequest, pk: int) -> JsonResponse:
    """Return current processing progress for a scan.

    :param request: The HTTP request.
    :param pk: Scan primary key.
    :return: JSON response with status, progress, and log fields.
    """
    scan = get_object_or_404(Scan, pk=pk)
    data = {
        "status": scan.status,
        "current": scan.progress_current,
        "total": scan.progress_total,
        "message": scan.progress_message,
        "log": scan.progress_log,
    }
    # Include ocr_results when available so the frontend can render
    # the pages sidebar live without a full page reload.
    if scan.ocr_results:
        data["ocr_results"] = scan.ocr_results
    # Neither GPU stage moves a scan status, so a viewer polling this
    # would otherwise see nothing happen for a whole run (#190, #195).
    dots_run = dots_mocr.run_summary(scan)
    if dots_run:
        data["dots_run"] = dots_run
    yolo_run = yolo.run_summary(scan)
    if yolo_run:
        data["yolo_run"] = yolo_run
    mistral_run = mistral_ocr.run_summary(scan)
    if mistral_run:
        data["mistral_run"] = mistral_run
    surya_run = surya.run_summary(scan)
    if surya_run:
        data["surya_run"] = surya_run
    return JsonResponse(data)


# Suffix for every "not ready" message the viewer shows (issue #185):
# uploaders must know the tab is not doing the work.
CLOSE_TAB_NOTE = " You can close this tab. The work continues on the server."

# Stage-specific wait messages, keyed by scan status. Each names what
# runs right now, so the viewer explains the wait instead of a generic
# "still processing" (issue #185).
PREVIEW_WAIT_MESSAGES = {
    Status.UPLOADED: (
        "Your upload is complete and safe. The scan waits for the "
        "processing queue." + CLOSE_TAB_NOTE
    ),
    Status.QUEUED: (
        "Your upload is complete and safe. The scan waits in the "
        "processing queue." + CLOSE_TAB_NOTE
    ),
    Status.PROCESSING: (
        "We are preparing the scan. We cut the PDF into parts, so "
        "servers can work on them in parallel." + CLOSE_TAB_NOTE
    ),
    Status.AWAITING: (
        "We are converting the scan to a small black-and-white preview, "
        "so it loads fast. This takes some minutes, and the preview "
        "appears here automatically." + CLOSE_TAB_NOTE
    ),
}


@login_required
def serve_scan_pdf(request: HttpRequest, pk: int) -> HttpResponse:
    """Serve the small processed PDF (``bitonal.pdf``).

    The viewer only ever gets the small, browser-viewable preview. The
    multi-GB original is never streamed here: it blows past the gunicorn
    worker timeout (the connection dies mid-stream, so the browser sees a
    truncated body and reports ERR_CONTENT_LENGTH_MISMATCH) and pdf.js
    can't hold a file that large in memory anyway. The viewer reads the
    original straight from S3 instead, via ``scan_original_url``
    (issue #185); server-side crops keep their own path
    (``serve_original_crop``).

    Resolution:

    1. Serve the processed PDF if it is already local.
    2. Otherwise pull *only* the preview PDF(s) from S3 and look again.
       This covers prod, where the daemon and web run in separate
       containers with separate ephemeral ``/tmp/`` volumes. The targeted
       pull skips the original and images/, so opening a scan never drags
       gigabytes across the network.
    3. No preview exists. Distinguish transient states (a preview is
       being produced -- the viewer should poll) from terminal ones (a
       preview will never appear -- the viewer should stop and offer the
       original instead). 202 means "poll again"; 409 means "give up".
       Both carry a stage-specific message and ``original_available``,
       which tells the viewer it can offer the "load the original"
       button.

    AWAITING is transient: a bitonal is being made right now (#176), so
    polling gets one. AWAITING_VALIDATION is terminal here: a scan parks
    there either converted (so a preview exists and step 3 is never
    reached) or deliberately unconverted (skipped, failed, or a
    pre-#176 post-cutover upload), and then no poll will ever find one.
    The two #154 review states are also 409s, but with their own
    message: they guarantee a stored preview, so reaching step 3 under
    them means the S3 pull just failed, and a reload (not a poll)
    retries it. The same failed-pull case under AWAITING_VALIDATION
    stays folded into its generic 409.

    A served preview carries ``X-Scan-Preview: bitonal`` so the viewer
    knows it shows the lower-quality conversion and can offer the
    original (issue #185).

    :param request: The HTTP request.
    :param pk: Scan primary key.
    :return: File response streaming the preview PDF, or a 202/409 JSON
        response when there is none.
    """
    scan = get_object_or_404(Scan, pk=pk)
    # Breadcrumb (issue #115): serving may pull from S3; mark the start
    # so a stall here is attributable in the trail.
    logger.info("serve_scan_pdf: resolving pdf for scan=%s", scan.pk)

    def _processed_local() -> FileResponse | None:
        """Return ``bitonal.pdf`` (or a legacy OCR pdf) from ``output_dir``."""
        output = Path(scan.output_dir)
        if not output.is_dir():
            return None
        base_pdf = find_processing_pdf(scan.output_dir)
        if base_pdf:
            response = FileResponse(
                base_pdf.open("rb"), content_type="application/pdf"
            )
            # "bitonal" names the preview class, not the exact file: a
            # legacy OCR PDF (same geometry, pre-#145 scans) reports the
            # same value, and the banner text stays true for it.
            response["X-Scan-Preview"] = "bitonal"
            return response
        return None

    # 1. Prefer the small processed PDF if it is already on disk.
    response = _processed_local()
    if response is not None:
        return response

    # 2. Not local: pull only the preview PDF(s) from S3 and re-check.
    try:
        s3_sync.download_preview_pdf(scan)
    except Exception:
        logger.exception("Lazy S3 preview pull failed for scan %s", scan.pk)

    response = _processed_local()
    if response is not None:
        return response

    # 3. No preview anywhere. Answer with a stage-specific message and
    #    whether the original is there to offer instead.
    original_available = bool(scan.original_pdf and scan.original_pdf.name)

    wait_message = PREVIEW_WAIT_MESSAGES.get(scan.status)
    if wait_message:
        return JsonResponse(
            {
                "status": "not_ready",
                "scan_status": scan.status,
                "message": wait_message,
                "original_available": original_available,
            },
            status=202,
        )

    if scan.status in REVIEW_STATUSES:
        # Every review state guarantees a stored preview (#154/#263):
        # #149 sets the first one only after the bitonal merge, and the
        # two review-2 states come after it. Landing here means the S3
        # pull above just failed, and a reload retries it.
        message = (
            "The preview did not load. Reload the page to try again, "
            "or load the original scan instead."
        )
    elif scan.status == Status.AWAITING_VALIDATION:
        message = (
            "This scan has no small preview. You can load the original "
            "scan instead."
        )
    elif scan.status in (Status.ERROR, Status.ERROR_MAX_RETRIES):
        message = (
            "This scan hit an error during processing, so there's no "
            "preview to show."
        )
    else:
        message = "No preview is available for this scan."
    return JsonResponse(
        {
            "status": "unavailable",
            "scan_status": scan.status,
            "message": message,
            "original_available": original_available,
        },
        status=409,
    )


@login_required
def scan_original_url(request: HttpRequest, pk: int) -> JsonResponse:
    """Return a URL the browser can read the original PDF from.

    The viewer calls this when the user asks for the original (issue
    #185). With S3 active, the answer is a presigned GET on the bucket:
    pdf.js reads the (up to 3 GB) file with range requests, straight
    from S3, so the web pod never streams it. Without S3 (dev, tests),
    the answer is our own ``serve_scan_original`` stream, and
    ``embedded_whole`` tells the viewer to load it in one piece --
    local files are small and local.

    The URL is minted per request, so every click gets a fresh
    signature.

    :param request: The HTTP request.
    :param pk: Scan primary key.
    :return: JSON with ``url`` and ``embedded_whole``, or a 404 when
        the scan has no original file.
    """
    scan = get_object_or_404(Scan, pk=pk)
    if not (scan.original_pdf and scan.original_pdf.name):
        return JsonResponse(
            {"error": "This scan has no original PDF."}, status=404
        )

    if request.GET.get("space") == "final":
        # The corrected volume at full quality (#269): the run's final
        # PDF, which aliases the original when no page was edited. Only
        # S3 holds it, and no run exists without S3.
        from scanning import review_states

        run = review_states.final_run(scan)
        if run is None or not s3_sync.s3_active():
            return JsonResponse(
                {"error": FINAL_VOLUME_NOT_READY_MESSAGE}, status=409
            )
        url = s3_sync.presign_get(
            run.final_pdf_key, settings.ORIGINAL_VIEW_PRESIGN_TTL
        )
        return JsonResponse({"url": url, "embedded_whole": False})

    url = s3_sync.presign_original_get(scan)
    if url:
        return JsonResponse({"url": url, "embedded_whole": False})
    return JsonResponse(
        {
            "url": reverse("serve_scan_original", kwargs={"pk": scan.pk}),
            "embedded_whole": True,
        }
    )


@login_required
def scan_ocr_text_url(request: HttpRequest, pk: int) -> JsonResponse:
    """Return a URL the browser can read the OCR document from (#262).

    The twin of :func:`scan_original_url`, for the text overlay of the
    viewer. The browser reads the document straight from the bucket, so
    the web pod mints one presigned GET and reads no byte of it: a
    glued volume of 1300 pages holds every cell and the text of every
    page, and a download plus a parse per press of the button would
    cost the pod that memory on the pod that also takes the uploads.

    The answer is JSON and not a redirect, although #243 and #269 both
    have a redirect route for these documents. A browser judges the
    CORS rules of a redirected request differently from a direct one,
    and the viewer reads this URL with ``fetch``; pdf.js reads the URL
    of :func:`scan_original_url` the same way, and that is the path
    the bucket rule is known to serve.

    Which document depends on the space the viewer draws (#269), and
    there is no fallback between the two: the text of the original over
    the pages of the corrected volume would sit one page out from the
    first deletion onwards.

    Which engine is the ``engine`` parameter (#381), a name of
    ``opinion_ocr.ENGINES``, dots.mocr by default. The answer carries
    that engine's ``fields`` as well, so the browser reads a document
    whose shape it holds no copy of: a fourth engine is one more entry
    of that table and no script change.

    :param request: The HTTP request.
    :param pk: Scan primary key.
    :return: JSON with ``url``, ``space``, ``size``, ``engine``,
        ``label`` and ``fields``; a 400 for an engine nobody has, a 409
        when the corrected volume is not built, a 404 when this engine
        read nothing, when the object is gone, or when S3 is off.
    """
    from scanning import opinion_ocr

    scan = get_object_or_404(Scan, pk=pk)
    name = request.GET.get("engine") or opinion_ocr.DEFAULT_ENGINE
    spec = opinion_ocr.ENGINES.get(name)
    if spec is None:
        return JsonResponse(
            {
                "error": UNKNOWN_OCR_ENGINE_MESSAGE.format(
                    engine=name, known=", ".join(opinion_ocr.ENGINES)
                )
            },
            status=400,
        )
    space = "original"
    if request.GET.get("space") == "final":
        from scanning import review_states

        run = review_states.final_run(scan)
        if run is None:
            return JsonResponse(
                {"error": FINAL_VOLUME_NOT_READY_MESSAGE}, status=409
            )
        # The corrected volume exists and this engine did not read it:
        # that is a fact about the engine, not about the volume, and
        # only dots.mocr is guaranteed (#245, #368).
        key = spec.document_key(run)
        if not key:
            return JsonResponse(
                {
                    "error": NO_READ_FINAL_TEXT_MESSAGE.format(
                        label=engine_label(name)
                    )
                },
                status=404,
            )
        space = "final"
    else:
        key = spec.module.glued_volume_key(scan)
        if not key:
            return JsonResponse(
                {
                    "error": NO_READ_TEXT_MESSAGE.format(
                        label=engine_label(name)
                    )
                },
                status=404,
            )

    if not s3_sync.s3_active():
        return JsonResponse({"error": NO_S3_GLUED_OUTPUT_MESSAGE}, status=404)
    # One head_object. It says the object is really there -- a run
    # glued before a sweep is not -- and its size lets the button say
    # how much it reads before it reads it.
    size = s3_sync.object_size(key)
    if size is None:
        return JsonResponse(
            {
                "error": OCR_TEXT_OBJECT_GONE_MESSAGE.format(
                    label=engine_label(name)
                )
            },
            status=404,
        )
    # No ``content_disposition``: that header makes a browser save a
    # named file, which is what the routes of #243 want and the
    # opposite of what a ``fetch`` wants.
    url = s3_sync.presign_get(key, GLUED_OUTPUT_PRESIGN_TTL)
    return JsonResponse(
        {
            "url": url,
            "space": space,
            "size": size,
            "engine": spec.name,
            "label": engine_label(name),
            "fields": spec.fields,
        }
    )


@login_required
def serve_scan_original(request: HttpRequest, pk: int) -> FileResponse:
    """Stream the original PDF from local disk.

    The no-S3 fallback behind ``scan_original_url``: in dev and tests
    the original never left this machine, so a plain stream serves it.
    With S3 active this view refuses with a 404 instead of streaming:
    it would pull the whole original to the pod and push it through
    gunicorn -- the exact slow, truncating path #185 removed from the
    preview endpoint -- and nothing links here in that mode, since the
    viewer gets a presigned URL from ``scan_original_url``.

    :param request: The HTTP request.
    :param pk: Scan primary key.
    :return: File response streaming the original PDF.
    :raises Http404: When S3 is active, or when no local copy can be
        made available.
    """
    scan = get_object_or_404(Scan, pk=pk)

    if s3_sync.s3_active():
        raise Http404("The original PDF is read from storage, not from here.")

    original = local_original_pdf(scan)
    if not original:
        raise Http404("No original PDF is available for this scan.")
    response = FileResponse(
        open(original, "rb"), content_type="application/pdf"
    )
    response["X-Scan-Preview"] = "original"
    return response


@login_required
def serve_final_pdf(request: HttpRequest, pk: int) -> HttpResponse:
    """Serve the bitonal copy of the corrected volume (#269).

    The step-2 counterpart of :func:`serve_scan_pdf`: the run's
    ``bitonal_key``, pulled to its local mirror on a miss
    (``apply.local_copy``). The template chooses this route when the
    boxes are measured against the standing run (``final_space``), so
    the answer here is only "is there a corrected volume": a 409 with
    ``original_available`` otherwise, which the viewer answers with the
    "load the original" button, as it does for the review-1 route.

    A run over a 1-bit original has the original as its bitonal copy,
    and #185 keeps the multi-GB original out of this stream: that case
    is a 409 too, and the original load resolves through
    ``scan_original_url?space=final`` to the same file.

    :param request: The HTTP request.
    :param pk: Scan primary key.
    :return: File response streaming the copy, or a 409 JSON response.
    """
    from scanning import apply, review_states

    scan = get_object_or_404(Scan, pk=pk)
    original_available = bool(scan.original_pdf and scan.original_pdf.name)

    def refuse(message: str) -> JsonResponse:
        return JsonResponse(
            {
                "status": "unavailable",
                "scan_status": scan.status,
                "message": message,
                "original_available": original_available,
            },
            status=409,
        )

    run = review_states.final_run(scan)
    if run is None:
        return refuse(FINAL_VOLUME_NOT_READY_MESSAGE)
    if run.bitonal_key == s3_sync.s3_original_key(scan):
        return refuse(FINAL_VOLUME_IS_ORIGINAL_MESSAGE)
    logger.info(
        "serve_final_pdf: scan=%s run=%s key=%s",
        scan.pk,
        run.label,
        run.bitonal_key,
    )
    try:
        path = apply.local_copy(scan, run.bitonal_key)
    except apply.ApplyError:
        logger.exception(
            "serve_final_pdf: the corrected volume of scan %s did not load",
            scan.pk,
        )
        return refuse(
            "The corrected volume did not load. Reload the page to try "
            "again, or load the original scan instead."
        )
    response = FileResponse(path.open("rb"), content_type="application/pdf")
    response["X-Scan-Preview"] = "bitonal"
    return response


#: Lifetime of a presigned GET minted by the glued-output routes (#243).
#: One click is one download, so ten minutes is ample.
#: ``ORIGINAL_VIEW_PRESIGN_TTL`` (8h) serves a viewer that scrolls for
#: hours and is the wrong size here.
GLUED_OUTPUT_PRESIGN_TTL = 600

#: Slug -> (stage, engine, glued key function): the glued documents of
#: issue #243. The outputs differ in nothing else, so one more engine is
#: one more entry, not a view. Surya is listed although no pass glues it
#: yet (#364): the index reads the rows, and the volume route answers
#: "not glued yet" for a key with no object, which is the true answer.
GLUED_OUTPUTS: dict[str, tuple[str, str, Callable[[Scan, int], str]]] = {
    "dots-mocr": (
        JobStage.ANALYZE,
        JobEngine.DOTS_MOCR,
        dots_mocr.glued_result_key,
    ),
    "yolo": (JobStage.DETECT, JobEngine.BLACKLETTER, yolo.merged_result_key),
    "mistral": (
        JobStage.EXTRACT,
        JobEngine.MISTRAL_OCR,
        mistral_ocr.glued_result_key,
    ),
    "surya": (JobStage.EXTRACT, JobEngine.SURYA, surya.glued_result_key),
}

#: What a start button says when the committed manifest describes no
#: shard at all. ``ensure_*`` then creates no row, and the "already
#: read" line would address ``created[0]`` and raise. A manifest like
#: that is a fault of the cut, not of the press, so the answer names
#: it rather than claiming a read that never happened.
NO_SHARDS_TO_READ_MESSAGE = (
    "This volume's shard set lists no part to read. Re-cut it with the "
    "admin re-queue before you start a read."
)

NO_S3_GLUED_OUTPUT_MESSAGE = (
    "No glued output exists without S3: the daemon glues into the "
    "bucket, and the workers write their results there."
)


def _json_404(message: str, **fields) -> JsonResponse:
    """Answer a 404 as JSON, the shape every answer of these routes has.

    ``Http404`` renders an HTML page, and a ``curl`` user would get two
    formats from one API. The scan lookup keeps ``get_object_or_404``
    on purpose: a missing scan looks the same on every scan route.

    :param message: What is missing.
    :param fields: More keys for the body (``run``, ``label``).
    :returns: The response.
    """
    return JsonResponse({"error": message, **fields}, status=404)


def _unknown_output(output: str) -> JsonResponse:
    """Answer for a slug :data:`GLUED_OUTPUTS` does not know."""
    return _json_404(
        f"Unknown glued output {output!r}. "
        f"Known: {', '.join(sorted(GLUED_OUTPUTS))}."
    )


def _glued_run_rows(
    scan: Scan, stage: str, engine: str, run: int
) -> list[ExternalJob]:
    """Return one run's rows in shard order; empty for a run nobody made.

    :param scan: The scan.
    :param stage: A :class:`~scanning.models.JobStage` value.
    :param engine: A :class:`~scanning.models.JobEngine` value.
    :param run: The run number.
    :returns: The rows ordered by ``shard_index``.
    """
    return list(
        ExternalJob.objects.filter(
            scan=scan,
            stage=stage,
            engine=engine,
            opinion=None,
            run=run,
            apply_run__isnull=True,
        ).order_by("shard_index")
    )


def _redirect_to_object(
    scan: Scan,
    output: str,
    key: str,
    *,
    filename: str,
    missing_message: str,
    disposition: str = "attachment",
    **fields,
) -> HttpResponse:
    """Send the browser to one object of the bucket, or say why not.

    A redirect to a presigned GET, not a stream (#243): a glued
    document of a long volume holds every cell and the text of every
    page, and #185 already took the large stream out of the preview
    endpoint for the gunicorn timeout. The bytes never cross the web
    pod. A navigation to S3 needs no CORS rule.

    One ``head_object`` first. Without it a run that is not glued yet,
    or a shard that never completed, would send the browser to an S3
    XML error. A non-missing S3 error is left to raise: a throttle or
    an IAM fault must reach Sentry, not read as "not there".

    :param scan: The scan the object belongs to.
    :param output: The slug, for the log line.
    :param key: Object key inside the private bucket.
    :param filename: The name the browser saves the file under.
    :param missing_message: The 404 message when the object is absent.
    :param disposition: ``attachment``, the default, or ``inline`` for
        a frame that shows the object instead of saving it (#334).
    :param fields: What names the object in the 404 body and the log
        line: ``run`` and ``label`` for a glued or an apply output,
        ``opinion`` and ``revision`` for an opinion's PDF (#336). Each
        route means something else by its key, so none is fixed here.
    :returns: A 302 to the presigned URL, or a 404 JSON response.
    """
    if not s3_sync.s3_active():
        return _json_404(NO_S3_GLUED_OUTPUT_MESSAGE, **fields)
    if not s3_sync.object_exists(key):
        return _json_404(missing_message, **fields)
    logger.info(
        "glued output: scan=%s output=%s %s key=%s",
        scan.pk,
        output,
        " ".join(f"{name}={value}" for name, value in fields.items()),
        key,
    )
    url = s3_sync.presign_get(
        key,
        GLUED_OUTPUT_PRESIGN_TTL,
        content_disposition=f'{disposition}; filename="{filename}"',
    )
    return redirect(url)


#: Which page lists one engine's summary carries, keyed by engine. Each
#: engine reports its own faults and no other's, so an empty list of a
#: name the engine does not report would read as "none" where the truth
#: is "not a question here". One table, because the index is the triage
#: tool and a second copy of a name would go stale in silence.
SHARD_PAGE_LISTS: dict[str, tuple[str, ...]] = {
    # The two holes, the pages a retry rung saved, and the pages whose
    # layout JSON was repaired (#242).
    JobEngine.DOTS_MOCR: jobs.PAGE_LIST_NAMES,
    # One list: a batch line either answered or it did not (#245).
    JobEngine.MISTRAL_OCR: ("failed_pages",),
    # The worker's own lists (#320/#364/#368): the pages that raised,
    # the pages that came back with no block twice, the pages surya
    # re-read block by block, and the pages whose parse lost a block.
    # Read off the glue's table, so a fifth list reaches the index with
    # the glue that reports it.
    JobEngine.SURYA: tuple(name for name, _member in surya.PAGE_LISTS),
}


def _shard_entry(scan: Scan, output: str, row: ExternalJob) -> dict:
    """Describe one shard row for the glued-output index.

    ``from_page`` and ``to_page`` are 1-based volume pages, the
    convention of the log lines (``jobs._failure_location``); the
    stored manifest holds fitz indexes. The dots.mocr page lists stay
    shard-local, as the worker reports them: an offset would put two
    conventions in one document. An absent key means "not known" and
    an empty value means "none", twice: ``url`` is absent on a row
    with no result, and the page lists are absent on a row with no
    stored summary -- a row an S3 HEAD completed stores ``output=None``
    until the glue stamps the lists, and a carried row copies none. The
    index is the triage tool for #242, so "no holes" must not be
    inferred from "nothing recorded".

    :param scan: The scan.
    :param output: The slug the row is listed under.
    :param row: The row.
    :returns: One entry of the ``shards`` list.
    """
    manifest = row.input_manifest or {}
    from_page = manifest.get("from_page")
    to_page = manifest.get("to_page")
    entry = {
        "shard_index": row.shard_index,
        "shard_count": row.shard_count,
        "attempt": row.attempt,
        "status": row.status,
        "error_code": row.error_code,
        "from_page": from_page + 1 if isinstance(from_page, int) else None,
        "to_page": to_page + 1 if isinstance(to_page, int) else None,
        "page_count": manifest.get("page_count"),
    }
    summary = (row.provider_meta or {}).get("output")
    if isinstance(summary, dict):
        for name in SHARD_PAGE_LISTS.get(row.engine, ()):
            value = summary.get(name)
            entry[name] = list(value) if isinstance(value, list) else []
    if row.result_key:
        entry["url"] = reverse(
            "serve_glued_shard",
            kwargs={
                "pk": scan.pk,
                "output": output,
                "run": row.run,
                "shard": row.shard_index,
            },
        )
    return entry


@login_required
def glued_output_index(
    request: HttpRequest, pk: int, output: str
) -> JsonResponse:
    """List every run and every shard of one glued output (#243).

    The answer to "how many shards are there, and which one failed":
    the runs newest first, each with its shards, their page ranges,
    their states, and the URL of each file. Every fact is on the rows,
    so the index makes no S3 call and answers in every environment.
    ``glued`` is "every row of the run is CONSUMED", which the glue and
    the merge write; the volume route still checks the object before
    it redirects. A scan with no rows gets an empty list, not an
    error: nothing ran is a fact.

    :param request: The HTTP request.
    :param pk: Scan primary key.
    :param output: A key of :data:`GLUED_OUTPUTS`.
    :return: JSON with ``scan``, ``output``, ``stage``, ``engine``,
        ``live_run`` and ``runs``.
    """
    spec = GLUED_OUTPUTS.get(output)
    if spec is None:
        return _unknown_output(output)
    stage, engine, _key_fn = spec
    scan = get_object_or_404(Scan, pk=pk)
    # The volume runs only: the one-page shards of a page edit apply
    # (#224) share the stage and the engine, and their run numbers, but
    # no glued volume document is written for them.
    rows = ExternalJob.objects.filter(
        scan=scan,
        stage=stage,
        engine=engine,
        opinion=None,
        apply_run__isnull=True,
    ).order_by("-run", "shard_index")
    runs = []
    for run, group in itertools.groupby(rows, key=lambda row: row.run):
        group = list(group)
        runs.append(
            {
                "run": run,
                "glued": all(
                    row.status == JobStatus.CONSUMED for row in group
                ),
                "label": jobs.rows_label(group),
                "volume_url": reverse(
                    "serve_glued_volume",
                    kwargs={"pk": scan.pk, "output": output, "run": run},
                ),
                "shards": [_shard_entry(scan, output, row) for row in group],
            }
        )
    return JsonResponse(
        {
            "scan": scan.pk,
            "output": output,
            "stage": stage,
            "engine": engine,
            "live_run": runs[0]["run"] if runs else None,
            "runs": runs,
        }
    )


@login_required
def serve_glued_volume(
    request: HttpRequest, pk: int, output: str, run: int
) -> HttpResponse:
    """Send the browser to one run's glued volume document (#243).

    The 404 for an absent object follows the rows: an open run is "not
    glued yet", while a run whose every row is CONSUMED was glued and
    has lost its object (swept, or expired), and saying "not glued yet:
    2 result applied" would contradict itself.

    :param request: The HTTP request.
    :param pk: Scan primary key.
    :param output: A key of :data:`GLUED_OUTPUTS`.
    :param run: The run number.
    :return: A 302 to a presigned GET, or a 404 JSON response when the
        run is not glued yet or no S3 is active.
    """
    spec = GLUED_OUTPUTS.get(output)
    if spec is None:
        return _unknown_output(output)
    stage, engine, key_fn = spec
    scan = get_object_or_404(Scan, pk=pk)
    rows = _glued_run_rows(scan, stage, engine, run)
    if not rows:
        return _json_404(f"Run {run} does not exist for this scan.", run=run)
    label = jobs.rows_label(rows)
    if all(row.status == JobStatus.CONSUMED for row in rows):
        missing = (
            f"Run {run} was glued, but its document is not in the bucket "
            f"({label})."
        )
    else:
        missing = f"Run {run} is not glued yet: {label}."
    return _redirect_to_object(
        scan,
        output,
        key_fn(scan, run),
        filename=f"scan-{scan.pk}-{output}-r{run}.json",
        missing_message=missing,
        run=run,
        label=label,
    )


#: The 404 of ``serve_opinion_pdf`` before the pass has written the file.
OPINION_PDF_NOT_WRITTEN_MESSAGE = (
    "The redacted PDF of this opinion is not written yet. The daemon "
    "writes one per tick after the redaction review is approved."
)

#: The 404 of ``opinion_ensemble_url`` before the pass has written the
#: ensemble document at the live revision (#365).
OPINION_ENSEMBLE_NOT_WRITTEN_MESSAGE = (
    "The text of this opinion is not written yet. The daemon writes it "
    "after the OCR glue, and the button reads the documents again."
)
#: The 404 of the two reader routes when the row says the object was
#: written and the bucket does not hold it.
OPINION_OBJECT_GONE_MESSAGE = (
    "The row says this object was written, but it is not in the "
    "bucket. Ask a staff member to write it again."
)

#: The objects of an opinion that are not one engine's document:
#: ``opinion_ocr``'s manifest and the document of the ensemble (#365).
#: One table, read by ``serve_opinion_ocr`` and by
#: :func:`opinion_file_index`, so a name lives in one place.
EXTRA_OPINION_OBJECTS = ("manifest", "ensemble")


def _opinion_object_key(opinion: Opinion, name: str) -> str:
    """Return the key of one object of an opinion's glue prefix.

    Each module owns the key of its own object: ``opinion_ocr`` the
    engine documents and the manifest, ``ensemble`` the document of the
    ensemble. This function chooses between them and writes no key.

    :param opinion: The row.
    :param name: An engine, ``manifest`` or ``ensemble``.
    :returns: The key.
    :rtype: str
    """
    from scanning import ensemble, opinion_ocr

    if name == "ensemble":
        return ensemble.document_key(opinion)
    return opinion_ocr.engine_key(opinion, name)


@login_required
def opinion_pdf_url(
    request: HttpRequest, pk: int, opinion_pk: int
) -> JsonResponse:
    """Return a URL the browser can read the redacted PDF from (#365).

    The twin of :func:`scan_original_url`, for one opinion. The review
    page draws the pages with pdf.js, which reads the file with range
    requests straight from the bucket.

    **The answer is JSON and not the 302 of** :func:`serve_opinion_pdf`.
    A browser judges the CORS rules of a redirected request differently
    from a direct one, the reason :func:`scan_ocr_text_url` gives, and
    the direct presigned GET is the path the bucket rule is known to
    serve. The 302 route keeps the download and the tab, where a
    navigation needs no CORS rule at all.

    ``opinion_pdf.is_written`` is the one rule for "the PDF exists", a
    read of the row. One ``head_object`` follows it, so a row that is
    stamped and an object that is gone do not send pdf.js to an S3
    error page. The signature lives as long as the original's
    (``ORIGINAL_VIEW_PRESIGN_TTL``), because the reader scrolls for
    hours and every range is one more request.

    :param request: The HTTP request.
    :param pk: Scan primary key.
    :param opinion_pk: The ``Opinion`` primary key; it must be of that scan.
    :return: JSON with ``url`` and ``revision``, or a 404.
    """
    scan = get_object_or_404(Scan, pk=pk)
    opinion = get_object_or_404(Opinion, pk=opinion_pk, scan=scan)
    if not opinion_pdf.is_written(opinion):
        return _json_404(
            OPINION_PDF_NOT_WRITTEN_MESSAGE,
            opinion=opinion.pk,
            revision=opinion.glue_revision,
        )
    return _presigned_opinion_object(
        opinion_pdf.key(opinion),
        opinion.glue_revision,
        opinion.pk,
        # pdf.js holds this URL for the life of the page and asks for
        # another range whenever the reviewer scrolls, so the signature
        # must outlive the reading. ``GLUED_OUTPUT_PRESIGN_TTL`` is ten
        # minutes, the size of one download, and a range after it would
        # be a 403 on a page that shows no reason.
        ttl=settings.ORIGINAL_VIEW_PRESIGN_TTL,
    )


@login_required
def opinion_ensemble_url(
    request: HttpRequest, pk: int, opinion_pk: int
) -> JsonResponse:
    """Return a URL the browser can read the ensemble document from (#365).

    The twin of :func:`scan_ocr_text_url`, for one opinion. The review
    page reads the document with ``fetch`` and draws its boxes and its
    text; the document of one opinion is small, and the pod still reads
    no byte of it.

    ``ensemble.is_written`` is the one rule for "the ensemble exists",
    and it names the live revision of the OCR glue. The staff route
    ``serve_opinion_ocr`` answers the same object with a redirect.

    :param request: The HTTP request.
    :param pk: Scan primary key.
    :param opinion_pk: The ``Opinion`` primary key; it must be of that scan.
    :return: JSON with ``url`` and ``revision``, or a 404.
    """
    from scanning import ensemble

    scan = get_object_or_404(Scan, pk=pk)
    opinion = get_object_or_404(Opinion, pk=opinion_pk, scan=scan)
    if not ensemble.is_written(opinion):
        return _json_404(
            OPINION_ENSEMBLE_NOT_WRITTEN_MESSAGE,
            opinion=opinion.pk,
            revision=opinion.glue_revision,
        )
    return _presigned_opinion_object(
        ensemble.document_key(opinion), opinion.glue_revision, opinion.pk
    )


def _presigned_opinion_object(
    key: str,
    revision: int | None,
    opinion_pk: int,
    *,
    ttl: int = GLUED_OUTPUT_PRESIGN_TTL,
) -> JsonResponse:
    """Answer one object of an opinion as a URL the browser reads.

    The body of the two routes above. No ``content_disposition``: that
    header makes a browser save a named file, which is what the routes
    of #243 want and the opposite of what a reader wants.

    :param key: Object key inside the private bucket.
    :param revision: The glue revision the row is stamped at.
    :param opinion_pk: The row, for the body of a 404.
    :param ttl: How long the signature lives. The default is one read
        of one object; a file pdf.js keeps reading takes the long one.
    :returns: JSON with ``url`` and ``revision``, or a 404.
    :rtype: JsonResponse
    """
    if not s3_sync.s3_active():
        return _json_404(
            NO_S3_GLUED_OUTPUT_MESSAGE,
            opinion=opinion_pk,
            revision=revision,
        )
    # One ``head_object``, the rule of :func:`_redirect_to_object`:
    # without it a row that is stamped and an object that is gone would
    # send pdf.js to an S3 error page.
    if not s3_sync.object_exists(key):
        return _json_404(
            OPINION_OBJECT_GONE_MESSAGE,
            opinion=opinion_pk,
            revision=revision,
        )
    return JsonResponse(
        {
            "url": s3_sync.presign_get(key, ttl),
            "revision": revision,
        }
    )


@login_required
@xframe_options_sameorigin
def serve_opinion_pdf(
    request: HttpRequest, pk: int, opinion_pk: int
) -> HttpResponse:
    """Send the browser to the redacted PDF of one opinion (#336).

    A developer's route redirects (#243/#262): a 302 to a presigned GET
    with the printed range as the download name, which is the one place
    that name lives (#165). ``opinion_pdf.is_written`` is the one rule
    for "the PDF exists"; before it holds, a 404 that says so, without
    an S3 HEAD. The review page of #334 reads the same key through the
    same rule.

    ``?disposition=inline`` asks for the same object to be shown and
    not saved (#334). The review page opens this route in a tab of its
    own, a navigation that needs no CORS rule, and the browser's own
    PDF viewer shows the file. Every other caller gets the download
    name. The page itself draws the pages from ``opinion_pdf_url``,
    whose answer pdf.js reads directly (#365).

    The route answers ``SAMEORIGIN`` where the site answers ``DENY``,
    so a page of the site can frame it: a browser that reads the header
    on the redirect would otherwise refuse the frame. The review page
    frames it no longer (#365 draws the pages instead), and the header
    stays at that narrower value for the next page that does. An
    exemption would let any site frame the route, and although the
    frame ends at the bucket, which is cross-origin and gives its bytes
    to no page, a third party's page would still make this pod sign a
    URL.

    :param request: The HTTP request.
    :param pk: Scan primary key.
    :param opinion_pk: The opinion's primary key; it must be of that scan.
    :return: A 302 to a presigned GET, or a 404 JSON response.
    """
    scan = get_object_or_404(Scan, pk=pk)
    opinion = get_object_or_404(Opinion, pk=opinion_pk, scan=scan)
    label = f"r{opinion.glue_revision}"
    if not opinion_pdf.is_written(opinion):
        return _json_404(
            OPINION_PDF_NOT_WRITTEN_MESSAGE,
            opinion=opinion.pk,
            revision=opinion.glue_revision,
        )
    return _redirect_to_object(
        scan,
        "opinion-pdf",
        opinion_pdf.key(opinion),
        filename=opinion_pdf.download_name(opinion),
        disposition=(
            "inline"
            if request.GET.get("disposition") == "inline"
            else "attachment"
        ),
        missing_message=(
            f"The redacted PDF of opinion {opinion.pk} was written at "
            f"{label}, but it is not in the bucket."
        ),
        opinion=opinion.pk,
        revision=opinion.glue_revision,
    )


@login_required
def serve_opinion_ocr(
    request: HttpRequest, pk: int, opinion_pk: int, engine: str
) -> HttpResponse:
    """Send the browser to one engine's OCR document of one opinion (#350).

    A developer's route, so it redirects (#243/#262). ``manifest``
    names the manifest, and ``ensemble`` the document of the OCR
    ensemble (#365), which has a ledger of its own
    (``ensemble.is_written``): the engine documents of a revision can
    exist while nothing read them yet. A 404 before the glue is
    written (``opinion_ocr.is_written``), for an engine this module
    does not know, and for an opinion of another scan.

    :param request: The HTTP request.
    :param pk: Scan primary key.
    :param opinion_pk: The ``Opinion`` primary key.
    :param engine: A name of ``opinion_ocr.ENGINES``, ``manifest``, or
        ``ensemble``.
    :return: A 302 to a presigned GET, or a 404 JSON response.
    """
    from scanning import ensemble as ensemble_module
    from scanning import opinion_ocr

    scan = get_object_or_404(Scan, pk=pk)
    opinion = get_object_or_404(Opinion, pk=opinion_pk, scan=scan)
    if (
        engine not in EXTRA_OPINION_OBJECTS
        and engine not in opinion_ocr.ENGINES
    ):
        return _json_404(
            f"Unknown engine {engine!r}. Known: "
            f"{', '.join((*EXTRA_OPINION_OBJECTS, *opinion_ocr.ENGINES))}."
        )
    revision = opinion.glue_revision
    if not opinion_ocr.is_written(opinion):
        return _json_404(
            f"The OCR glue of {opinion} is not written at r{revision}.",
            opinion=opinion.pk,
            revision=revision,
            label=opinion.status,
        )
    if engine == "ensemble" and not ensemble_module.is_written(opinion):
        return _json_404(
            OPINION_ENSEMBLE_NOT_WRITTEN_MESSAGE,
            opinion=opinion.pk,
            revision=revision,
            label=opinion.status,
        )
    return _redirect_to_object(
        scan,
        f"opinion-{engine}",
        _opinion_object_key(opinion, engine),
        filename=(
            f"scan-{scan.pk}-opinion-{opinion.first_printed_page}."
            f"{opinion.index_in_page}-r{revision}-{engine}.json"
        ),
        missing_message=(
            f"The OCR glue of {opinion} is stamped at r{revision}, but "
            f"its {engine} document is not in the bucket."
        ),
        opinion=opinion.pk,
        revision=revision,
        label=opinion.status,
    )


@login_required
def opinion_file_index(
    request: HttpRequest, pk: int, opinion_pk: int
) -> JsonResponse:
    """List the glued objects of one opinion (#334).

    The ``files`` index of an opinion, the twin of
    :func:`glued_output_index` for a volume: the review page links it,
    and it answers "which object exists, and where is it". Every fact
    is on the row, so the index makes no S3 call and answers in every
    environment. It writes no copy of a rule: the two ledgers are
    ``opinion_pdf.is_written`` and ``opinion_ocr.is_written``, the same
    two the two routes read. An entry carries its ``url`` only when it
    is written, the rule of :func:`_shard_entry`, where a link that
    cannot work is left out.

    **The OCR ledger is one stamp over every file of the revision, and
    the glue writes one file per engine the run has**
    (``opinion_ocr.write``). The count follows ``opinion_ocr.ENGINES``,
    which #368 made three, plus :data:`EXTRA_OPINION_OBJECTS`, so no
    reader counts. So an engine document is written when the stamp is
    live **and** the run carries that engine's key: a volume nobody
    read with Mistral is glued from dots.mocr alone, and its
    ``mistral_ocr.json`` was never put in the bucket. The manifest is
    written on every glue. A key that lands on the run after the glue
    reads as written until the next re-glue, the one error left here,
    and the rarer one.

    **The ensemble has a ledger of its own** (``ensemble.is_written``,
    #365). It is written after the engine documents of the same
    revision, and a volume two engines read waits for the button, so
    the OCR stamp does not answer for it.

    The keys are of the live revision (``Opinion.glue_prefix``). A
    re-glue raises the revision, so this index never names an object of
    an older prefix, although that object stays in the bucket.

    :param request: The HTTP request.
    :param pk: Scan primary key.
    :param opinion_pk: The ``Opinion`` primary key; it must be of that scan.
    :return: JSON with ``scan``, ``opinion``, ``label``, ``status``,
        ``glue_revision``, ``prefix`` and ``files``.
    """
    from scanning import ensemble, opinion_ocr

    scan = get_object_or_404(Scan, pk=pk)
    opinion = get_object_or_404(Opinion, pk=opinion_pk, scan=scan)
    files = [
        {
            "name": opinion_pdf.REDACTED_NAME,
            "output": "redacted-pdf",
            "written": opinion_pdf.is_written(opinion),
            "key": opinion_pdf.key(opinion),
            "url": reverse(
                "serve_opinion_pdf",
                kwargs={"pk": scan.pk, "opinion_pk": opinion.pk},
            ),
        }
    ]
    ocr_written = opinion_ocr.is_written(opinion)
    run = opinion.apply_run
    for engine in (*opinion_ocr.ENGINES, *EXTRA_OPINION_OBJECTS):
        key = _opinion_object_key(opinion, engine)
        spec = opinion_ocr.ENGINES.get(engine)
        has_read = spec is None or bool(
            run is not None and spec.document_key(run)
        )
        # The ensemble has a ledger of its own: it is written after the
        # engine documents of the same revision, and a two-engine
        # volume waits for the button (#365).
        written = (
            ensemble.is_written(opinion)
            if engine == "ensemble"
            else ocr_written and has_read
        )
        files.append(
            {
                "name": key.rsplit("/", 1)[-1],
                "output": f"opinion-{engine}",
                "written": written,
                "key": key,
                "url": reverse(
                    "serve_opinion_ocr",
                    kwargs={
                        "pk": scan.pk,
                        "opinion_pk": opinion.pk,
                        "engine": engine,
                    },
                ),
            }
        )
    for entry in files:
        if not entry["written"]:
            entry.pop("url")
    return JsonResponse(
        {
            "scan": scan.pk,
            "opinion": opinion.pk,
            "label": str(opinion),
            "status": opinion.status,
            "glue_revision": opinion.glue_revision,
            "prefix": opinion.glue_prefix,
            "files": files,
        }
    )


@login_required
def serve_glued_shard(
    request: HttpRequest, pk: int, output: str, run: int, shard: int
) -> HttpResponse:
    """Send the browser to one shard's result object (#243).

    The worker's own answer, at the row's ``result_key``: for dots.mocr
    it holds ``raw``, the answer as the model wrote it, which the glue
    leaves out of the volume document (#238) and which #242 needs. A
    carried row (#190) names the previous attempt's object, and that
    is the right file.

    :param request: The HTTP request.
    :param pk: Scan primary key.
    :param output: A key of :data:`GLUED_OUTPUTS`.
    :param run: The run number.
    :param shard: The ``shard_index`` inside the run.
    :return: A 302 to a presigned GET, or a 404 JSON response when the
        shard has no result or no S3 is active.
    """
    spec = GLUED_OUTPUTS.get(output)
    if spec is None:
        return _unknown_output(output)
    stage, engine, _key_fn = spec
    scan = get_object_or_404(Scan, pk=pk)
    rows = _glued_run_rows(scan, stage, engine, run)
    if not rows:
        return _json_404(f"Run {run} does not exist for this scan.", run=run)
    label = jobs.rows_label(rows)
    row = next((row for row in rows if row.shard_index == shard), None)
    if row is None:
        return _json_404(
            f"Run {run} has no shard {shard}: it has {len(rows)} shard(s).",
            run=run,
            label=label,
        )
    status = row.get_status_display().lower()
    if not row.result_key:
        return _json_404(
            f"Shard {shard} of run {run} has no result ({status}).",
            run=run,
            label=label,
        )
    return _redirect_to_object(
        scan,
        output,
        row.result_key,
        filename=f"scan-{scan.pk}-{output}-r{run}-s{shard}.json",
        run=run,
        missing_message=(
            f"The result of shard {shard} of run {run} is not in the "
            f"bucket ({status})."
        ),
        label=label,
    )


#: Slug -> (``ApplyRun`` key field, extension) of the apply's outputs
#: (#224), for the routes below (#269). ``page-map`` has no key field:
#: its object is written beside the run's outputs at a fixed name.
APPLY_OUTPUTS: dict[str, tuple[str | None, str]] = {
    "final-pdf": ("final_pdf_key", "pdf"),
    "bitonal": ("bitonal_key", "pdf"),
    "ocr-volume": ("ocr_key", "json"),
    "printed-pages": ("printed_pages_key", "json"),
    "detections-volume": ("detections_key", "json"),
    "extract-volume": ("extract_key", "json"),
    "surya-volume": ("surya_key", "json"),
    "page-map": (None, "json"),
}


def _apply_output_key(scan: Scan, run, output: str) -> str | None:
    """Return the S3 key of one apply output, or None when not written.

    :param scan: The scan.
    :param run: The ``ApplyRun``.
    :param output: A key of :data:`APPLY_OUTPUTS`.
    :returns: The key, or None for a blank field.
    """
    from scanning import apply

    field, _ext = APPLY_OUTPUTS[output]
    if field is None:
        return (
            f"{apply.run_prefix(scan, run)}page_map.json"
            if run.is_built
            else None
        )
    return getattr(run, field) or None


def _apply_run_entry(scan: Scan, run, rows: list, measured: bool) -> dict:
    """Describe one apply run for the outputs index.

    :param scan: The scan.
    :param run: The ``ApplyRun``.
    :param rows: The run's own ``ExternalJob`` rows.
    :param measured: Whether the redaction rows are measured against it.
    :returns: One entry of the ``runs`` list.
    """
    from scanning import apply

    files = {}
    for output in APPLY_OUTPUTS:
        if _apply_output_key(scan, run, output):
            files[output] = reverse(
                "serve_apply_output",
                kwargs={"pk": scan.pk, "number": run.number, "output": output},
            )
    shards = []
    for row in sorted(rows, key=lambda r: (r.stage, r.pk)):
        manifest = row.input_manifest or {}
        entry = {
            "pk": row.pk,
            "stage": row.stage,
            "engine": row.engine,
            "edit_id": manifest.get("edit_id"),
            "attempt": row.attempt,
            "status": row.status,
            "error_code": row.error_code,
            "page_count": manifest.get("page_count"),
        }
        summary = (row.provider_meta or {}).get("output")
        if isinstance(summary, dict):
            for name in SHARD_PAGE_LISTS.get(row.engine, ()):
                value = summary.get(name)
                entry[name] = list(value) if isinstance(value, list) else []
        if row.result_key:
            entry["url"] = reverse(
                "serve_apply_shard",
                kwargs={"pk": scan.pk, "number": run.number, "row_pk": row.pk},
            )
        shards.append(entry)
    return {
        "label": run.label,
        "number": run.number,
        "standing": run.superseded_at is None,
        "built_at": run.built_at.isoformat() if run.built_at else None,
        "complete": run.is_complete,
        "measured": measured,
        "source_fingerprint": run.source_fingerprint,
        # The attempt count and the row states, not ``last_error``: it
        # holds the text of an exception, and a response must not carry
        # one (CodeQL). The admin shows it.
        "attempts": run.attempts,
        **apply.describe_map(run.page_map),
        "files": files,
        "shards": shards,
    }


@login_required
def apply_output_index(request: HttpRequest, pk: int) -> JsonResponse:
    """List every apply run of a scan with its outputs and shards (#269).

    The #243 shape for the corrected volume (#224): the runs newest
    first, each with its counts, the URL of each written output, and
    its one-page shard rows. Every fact is on the rows, so no S3 call.
    ``measured`` says whether the redaction rows of the scan are
    measured against that run (``yolo.redactions_current``), which is
    what step 2 shows.

    :param request: The HTTP request.
    :param pk: Scan primary key.
    :return: JSON with ``scan``, ``standing`` and ``runs``.
    """
    from scanning.models import ApplyRun

    scan = get_object_or_404(Scan, pk=pk)
    runs = list(ApplyRun.objects.filter(scan=scan).order_by("-number"))
    rows_by_run: dict[int, list] = {}
    for row in ExternalJob.objects.filter(scan=scan, apply_run__isnull=False):
        rows_by_run.setdefault(row.apply_run_id, []).append(row)
    detect_rows = yolo.live_detect_jobs(scan)
    entries = [
        _apply_run_entry(
            scan,
            run,
            rows_by_run.get(run.pk, []),
            bool(detect_rows) and yolo.redactions_current(detect_rows, run),
        )
        for run in runs
    ]
    standing = next((e["label"] for e in entries if e["standing"]), None)
    return JsonResponse(
        {"scan": scan.pk, "standing": standing, "runs": entries}
    )


@login_required
def serve_apply_output(
    request: HttpRequest, pk: int, number: int, output: str
) -> HttpResponse:
    """Send the browser to one output of an apply run (#269).

    :param request: The HTTP request.
    :param pk: Scan primary key.
    :param number: The run number (the ``n`` of ``a{n}``).
    :param output: A key of :data:`APPLY_OUTPUTS`.
    :return: A 302 to a presigned GET, or a 404 JSON response.
    """
    from scanning.models import ApplyRun

    if output not in APPLY_OUTPUTS:
        return _json_404(
            f"Unknown apply output {output!r}. "
            f"Known: {', '.join(sorted(APPLY_OUTPUTS))}."
        )
    scan = get_object_or_404(Scan, pk=pk)
    run = ApplyRun.objects.filter(scan=scan, number=number).first()
    if run is None:
        return _json_404(
            f"Scan {scan.pk} has no apply run a{number}.", run=number
        )
    key = _apply_output_key(scan, run, output)
    if not key:
        return _json_404(
            f"Apply run {run.label} has not written its {output} yet.",
            run=number,
            label=run.label,
        )
    _field, ext = APPLY_OUTPUTS[output]
    return _redirect_to_object(
        scan,
        f"apply/{output}",
        key,
        filename=f"scan-{scan.pk}-apply-{run.label}-{output}.{ext}",
        run=number,
        missing_message=(
            f"The {output} of apply run {run.label} is not in the bucket."
        ),
        label=run.label,
    )


@login_required
def serve_apply_shard(
    request: HttpRequest, pk: int, number: int, row_pk: int
) -> HttpResponse:
    """Send the browser to one apply row's result object (#269).

    The one-page shard results of a page edit apply (#224) are kept,
    and the #243 shard route cannot reach them: it resolves rows by a
    volume slug and a run number, and filters the apply rows out.

    :param request: The HTTP request.
    :param pk: Scan primary key.
    :param number: The run number.
    :param row_pk: The ``ExternalJob`` pk.
    :return: A 302 to a presigned GET, or a 404 JSON response.
    """
    scan = get_object_or_404(Scan, pk=pk)
    row = ExternalJob.objects.filter(
        scan=scan, pk=row_pk, apply_run__number=number
    ).first()
    if row is None:
        return _json_404(
            f"Apply run a{number} of scan {scan.pk} has no row {row_pk}.",
            run=number,
        )
    if not row.result_key:
        return _json_404(
            f"Row {row_pk} has no result yet ({row.status}).", run=number
        )
    return _redirect_to_object(
        scan,
        "apply/shard",
        row.result_key,
        run=number,
        filename=(
            f"scan-{scan.pk}-apply-a{number}-{row.stage}-{row.engine}-"
            f"e{(row.input_manifest or {}).get('edit_id')}"
            f"{Path(row.result_key).suffix or '.json'}"
        ),
        missing_message=f"Row {row_pk} names a result that is not in the bucket.",
        label=row.status,
    )


@login_required
def serve_original_crop(request: HttpRequest, pk: int) -> HttpResponse:
    """Render a cropped region from the original (non-bitonal) PDF as PNG.

    :param request: The HTTP request (crop coordinates via query params).
    :param pk: Scan primary key.
    :return: PNG image response of the cropped region.
    """
    scan = get_object_or_404(Scan, pk=pk)
    try:
        page = int(request.GET.get("page", 0))
        x0 = float(request.GET.get("x0", 0))
        y0 = float(request.GET.get("y0", 0))
        x1 = float(request.GET.get("x1", 0))
        y1 = float(request.GET.get("y1", 0))
        dpi = min(max(int(request.GET.get("dpi", 150)), 72), 300)
    except ValueError:
        return HttpResponse(status=400)

    # Breadcrumb (issue #115): the pixmap render below is the clearest
    # per-request memory spike in the web pod (5-20 MB at dpi=300), so log it
    # with its scale before allocating.
    logger.info(
        "serve_original_crop: rendering scan=%s page=%s dpi=%s",
        scan.pk,
        page,
        dpi,
    )
    if request.GET.get("space") == "final":
        # Step 2 sends a page of the corrected volume (#269). The run's
        # page map says which page of the original it is; a page a
        # curator added, replaced or rotated has no crop in the original
        # (a rotated page's boxes are in the rotated space), so the
        # viewer keeps the bitonal render for it.
        from scanning import review_states

        run = review_states.final_run(scan)
        if run is None:
            return HttpResponse(status=404)
        entries = run.page_map.get("pages") or []
        if page < 0 or page >= len(entries):
            return HttpResponse(status=404)
        source = entries[page].get("source") or {}
        if source.get("kind") != "original":
            return HttpResponse(status=404)
        page = int(source["pdf_page"]) - 1

    # Prod: the original lives only in S3 (direct-to-S3 upload, and the
    # classic prod path streams straight to S3 too). The process view no
    # longer eagerly lands it locally, and download_preview_pdf excludes
    # the original, so this pulls just the original when it is missing.
    original = local_original_pdf(scan)
    if not original:
        return HttpResponse(status=404)

    with fitz.open(original) as doc:
        if page < 0 or page >= doc.page_count:
            return HttpResponse(status=404)
        clip = fitz.Rect(x0, y0, x1, y1)
        pix = doc[page].get_pixmap(clip=clip, dpi=dpi)
        png_bytes = pix.tobytes("png")
        # Release the pixmap's C-side buffer immediately rather than
        # waiting for GC; per-request crops at dpi=300 are 5-20 MB.
        pix = None
    resp = HttpResponse(png_bytes, content_type="image/png")
    resp["Cache-Control"] = "max-age=3600"
    return resp


def _review_flags(
    scan: Scan,
    repairs_waiting: bool | None = None,
    review2: tuple[int, int] | None = None,
) -> dict:
    """Return the review flags the step-1 and step-2 button bars read.

    Both :func:`scan_process_view` and the :func:`process_actions`
    fragment render those bars, so the flags come from one place (#151).
    A bar that disagreed with itself would offer an approve button the
    view refuses, or hide the one it accepts. The two review-2 flags
    (#263) ride along for that same reason, and their approve button is
    the gate of step 3.

    ``page_review_done`` says "review 1 is approved", which stays true
    for the whole of review 2: a curator who walks back to step 1 from
    there -- through the step tabs, the repair queue link, or the
    recompute button -- must find the bar they left, with its mark and
    its "Next: Detect" button. ``start_detect`` accepts all three
    statuses, so a narrower flag would hide a button the view honours.

    ``legacy_review`` is the status, not
    :func:`services.has_legacy_ocr`: the two ask different questions.
    ``has_legacy_ocr`` asks who read the page numbers, and it turns
    false the moment a backfill run gives an old volume an ``ANALYZE``
    row; ``legacy_review`` asks which review flow the volume is in, and
    ``PENDING_REVIEW`` is where a legacy step 2 lives (the park of
    ``run_compute_redactions`` and the step chooser both say so).

    ``repairs_waiting`` is the gate of the review-1 approval (#266): a
    volume whose pages a scanner must still scan is not page complete,
    so the bar shows a note in place of the approve button and
    ``approve_page_completeness`` refuses the POST. The caller may pass
    the answer it already holds -- ``scan_process_view`` reads the
    requests for the sidebar anyway -- and the flag is queried only for
    a caller that does not (the ``process_actions`` fragment).

    ``preview_available`` is the detection preview of #388, and
    ``preview_approved`` says which disclaimer it gets: a volume whose
    page review is still open must approve it, and an approved one must
    wait for the corrected volume. The rule is
    ``review_states.preview_only``, and a render turns it into
    ``preview_only``, which is that rule on a step-2 page.

    ``pages_without_number`` is the second gate of that approval
    (#342), and it is read in READY alone, which is the condition the
    view reads: a volume past review 1 pays no query for it, whichever
    step asks for the bar. The bar shows a note for each gate that
    refuses, and the button when neither does.

    :param scan: The scan the bars are rendered for.
    :param repairs_waiting: Whether a scanner still has to act on this
        scan. ``None`` asks :func:`repairs.has_waiting`.
    :param review2: The open and the stale review-2 findings, when the
        caller holds them (``findings.viewer_groups``). ``None`` asks
        :func:`findings.open_count`, past the review-1 approval.
    :returns: ``page_review_ready``, ``page_review_done``,
        ``redaction_review_ready``, ``redaction_review_done``,
        ``preview_available``, ``preview_approved``, ``legacy_review``,
        ``has_legacy_ocr``, ``repairs_waiting``,
        ``pages_without_number``, ``legacy_pipeline``,
        ``review3_opinions`` and the two pending-edit flags, for the
        template context.
    :rtype: dict
    """
    from scanning import apply, review_states, services

    done = scan.status == Status.PAGE_COMPLETENESS_REVIEW_DONE
    approved = scan.status in PAGE_REVIEW_APPROVED_STATUSES
    # One read of the standing apply run for the readers below.
    run = apply.current_run(scan)
    # The corrected volume (#269): the run when it is complete for this
    # original, and whether the boxes are measured against it. The
    # viewer follows the rows, not the run alone: a complete run over
    # rows measured on the original would put the final PDF under boxes
    # of another space, the one thing step 2 must never show.
    final = review_states.final_run(scan, run)
    detect_rows = yolo.live_detect_jobs(scan) if final is not None else None
    final_space = final is not None and yolo.redactions_current(
        detect_rows, final
    )
    # The read-only step 2 of a volume with no measured geometry
    # (#388). The run is the one read above, and the rows are read by
    # the rule itself, past its status check: a volume no preview can
    # reach pays no query for one.
    preview_available = review_states.preview_only(scan, detect_rows, run)
    final_volume = None
    if final is not None:
        final_volume = {
            "label": final.label,
            "measured": final_space,
            **apply.describe_map(final.page_map),
        }
    if repairs_waiting is None:
        repairs_waiting = repairs.has_waiting(scan)
    ready = scan.status == Status.READY_FOR_PAGE_COMPLETENESS_REVIEW
    # Only where the button is offered (#342). The rule overlays the
    # page numbers, which is three queries, and no other status reads
    # the answer.
    pages_without_number = (
        page_numbers.pages_without_number(scan) if ready else []
    )
    if review2 is None:
        review2 = findings.open_count(scan) if approved else (0, 0)
    review2_open, review2_stale = review2
    return {
        "page_review_ready": ready,
        "page_review_done": approved,
        "redaction_review_ready": (
            scan.status == Status.READY_FOR_REDACTION_REVIEW
        ),
        "redaction_review_done": (scan.status == Status.REDACTION_REVIEW_DONE),
        # The detection preview (#388). ``preview_available`` is the
        # rule, true of the volume whichever step is rendered, because
        # step 1 links the preview and the step-2 tab marks it. The
        # page turns it into ``preview_only``, the step-2 render, which
        # is what the banner and the locks read: step 1 of the same
        # volume is an open page review. ``preview_approved`` picks the
        # disclaimer: a volume whose page review is still open must be
        # approved, an approved one must wait for the corrected volume.
        "preview_available": preview_available,
        "preview_approved": preview_available and done,
        # The last word of the server on a volume parked in review 2
        # (#336). A failed opinion creation or a failed recompute parks
        # the scan here with the reason in ``progress_message``, and the
        # poll reloads the page at once, so without this line the
        # approve button seems to do nothing.
        "redaction_review_note": (
            scan.progress_message
            if scan.status == Status.READY_FOR_REDACTION_REVIEW
            else ""
        ),
        "legacy_review": scan.status == Status.PENDING_REVIEW,
        # Which pipeline the volume belongs to (#334). Not
        # ``legacy_review``, which is PENDING_REVIEW alone: a legacy
        # volume also holds APPROVED and EXTRACTED, and those keep the
        # step 3 that lists the files the legacy pipeline generated.
        # ``stats.LEGACY_STATUSES`` is the project's one definition of
        # a status no new scan can reach.
        "legacy_pipeline": scan.status in stats.LEGACY_STATUSES,
        # The reopen is a compare-and-swap on DONE (#224), so the
        # button shows only there: a volume in review 2 keeps its
        # badge and loses the button.
        "page_review_reopenable": done,
        "has_legacy_ocr": services.has_legacy_ocr(scan),
        # The apply writes no scan status while its rows run (#224), so
        # the run row is the only place its progress lives. Read for
        # every status past the review-1 approval, because the step-2
        # note says where the corrected volume stands (#269).
        "apply_run": apply.run_state(scan, run) if approved else None,
        "final_run": final,
        "final_space": final_space,
        "final_volume": final_volume,
        "repairs_waiting": repairs_waiting,
        # The pages review 1 left with no number (#342). The bar names
        # the count and sends the reviewer to the first of them.
        "pages_without_number": pages_without_number,
        # "Next: Generate" (#240 PR C): one read for both renders of
        # the bar, or a volume whose only boundary is a curator's
        # showed the link on a full load and hid it after the fragment
        # refresh.
        "has_opinions": boundaries.has_live(scan),
        # The open findings of review 2 (#240 PR D), for the badge and
        # the confirm of the approve button. Read past the review-1
        # approval only: before it there is no compute and no finding.
        "review2_open": review2_open,
        "review2_stale": review2_stale,
        # The opinions of review 3 (#334). The step-3 tab links the
        # opinions page with this scan as its filter, and only when the
        # rows exist: a tab that opened an empty list would send a
        # curator to a page with no work on it. A legacy volume has no
        # ``Opinion`` row and keeps its own step 3. Not
        # ``opinion_count``, which the step-2 sidebar already uses for
        # the boundaries of the volume.
        "review3_opinions": Opinion.objects.filter(scan=scan).count(),
        **page_edits.pending_edit_flags(scan, run),
    }


def _final_space_pages(scan: Scan, run) -> tuple[list, dict, str | None]:
    """Return the step-2 page map and labels of the corrected volume.

    From the run's printed-page map (``apply.viewer_pages``, #269). When
    that read fails the page still renders: the map comes from the
    stored ``page_map`` with no labels (``apply.positional_pages``),
    and one warning says so.

    :param scan: The scan.
    :param run: The standing, complete apply run.
    :returns: ``(page_map, ocr_by_page, warning)``; ``warning`` is
        ``None`` when the printed pages loaded.
    :rtype: tuple[list, dict, str | None]
    """
    from scanning import apply

    try:
        printed = apply.load_printed_pages(scan, run)
    except Exception:
        logger.exception(
            "scan_process_view: the printed pages of scan %s (%s) did not load",
            scan.pk,
            run.label,
        )
        page_map, ocr_by_page = apply.positional_pages(run)
        return page_map, ocr_by_page, PRINTED_PAGES_UNAVAILABLE_MESSAGE
    page_map, ocr_by_page = apply.viewer_pages(printed)
    return page_map, ocr_by_page, None


def _refuse_locked_edits(scan: Scan) -> JsonResponse | None:
    """Refuse a page edit on a volume whose review is not open.

    The first thing every page edit endpoint does (#224), the dismissal
    of an issue excepted: it is built into nothing. Once the page
    review is approved the apply builds the final volume from the rows
    as they stand, so a row written after that addresses a source the
    pipeline has left behind: it would be applied by no run, or by the
    wrong one. A late correction reopens the review first
    (:func:`reopen_page_review`), which supersedes the run in flight.

    :param scan: The scan the edit is about.
    :returns: A 409 answer naming the reason, or None when the edit
        may proceed.
    :rtype: JsonResponse | None
    """
    if scan.status not in LOCKED_STATUSES:
        return None
    return JsonResponse({"error": EDITS_LOCKED_MESSAGE}, status=409)


def _block_if_pending_changes(
    request: HttpRequest, scan: Scan
) -> HttpResponse | None:
    """Redirect back to step 1 when the scan has unapplied page changes.

    The detect action ignores the structural page edits -- a delete, an
    insert, a replacement, a rotation -- so running it would silently
    strand the curator's work. They must be applied first (#224), and
    the apply runs after the review-1 approval.

    Only ``start_detect`` calls this, which is step 2, and it checks
    the approval before it calls this, so the message here never asks
    an approved reviewer to approve. The recompute of review 1 does
    not call this: it warns and continues (#151), and the approve
    button of review 1 does not either (#232) -- the approval is what
    the apply waits for.

    :param request: The HTTP request.
    :type request: HttpRequest
    :param scan: The scan to check for pending changes.
    :type scan: Scan
    :returns: A redirect response if there are pending changes, else
        ``None``.
    :rtype: HttpResponse | None
    """
    if page_edits.has_pending_changes(scan):
        messages.warning(
            request,
            "Your page changes are not built into the volume yet, so "
            "this step would ignore them. The corrected volume is "
            "built after the approval; wait for that to finish.",
        )
        return redirect(
            reverse("scan_process", kwargs={"pk": scan.pk}) + "?step=1"
        )
    return None


@login_required
def process_actions(request: HttpRequest, pk: int) -> JsonResponse:
    """Render the step action bar as an HTML fragment.

    Lets the step-1/step-2 viewers refresh the action buttons in place
    after a page is marked for (or restored from) deletion, so the
    correct buttons appear without a full page reload.

    :param request: The HTTP request (optional ``step`` query param).
    :type request: HttpRequest
    :param pk: Scan primary key.
    :type pk: int
    :returns: JSON with the rendered ``html`` and the current
        ``has_pending_changes`` flag.
    :rtype: JsonResponse
    """
    scan = get_object_or_404(Scan, pk=pk)
    try:
        step = int(request.GET.get("step", 1))
    except ValueError:
        step = 1
    if step < 1 or step > 3:
        step = 1

    yolo_run = yolo.run_summary(scan)
    flags = _review_flags(scan)
    context = {
        "scan": scan,
        "step": step,
        "is_processing": scan.status in BUSY_STATUSES,
        "issues": scan.issues.exclude(check_name__in=REVIEW2_CHECKS),
        "missing_pages": scan.missing_pages,
        "has_detections": Detection.objects.filter(scan=scan).exists(),
        "dots_run": dots_mocr.run_summary(scan),
        "yolo_run": yolo_run,
        "mistral_run": mistral_ocr.run_summary(scan),
        "surya_run": surya.run_summary(scan),
        "detect_message": detection_message(yolo_run),
        **flags,
        # Step-scoped, as the page renders it (#388): the bar of step 1
        # belongs to the page review, whichever state step 2 is in.
        "preview_only": step >= 2 and flags["preview_available"],
    }
    html = render_to_string(
        "scanning/_process_actions.html", context, request=request
    )
    return JsonResponse(
        {
            "html": html,
            "has_pending_changes": context["has_pending_changes"],
        }
    )


@login_required
@require_POST
def start_validate(request: HttpRequest, pk: int) -> HttpResponse:
    """Refuse to re-run the pipeline. Say why, per scan.

    This button used to re-queue the full pipeline from scratch,
    invalidating the stored GPU results first so nothing was reused.
    The stages it re-ran -- bitonal, YOLO detect, PaddleOCR validation
    -- were disconnected by issue #173.

    A **new-pipeline scan is refused for good** (#151), not until the
    replacements land: sharding, the bitonal conversion and dots.mocr
    are deterministic, so a second run returns what the first one
    already stored, at the price of another doctor conversion and
    another park out of the review flow. Nothing here is a recompute of
    the page numbers either -- that is the recompute button, over the
    stored readings. The escape hatch for a volume that genuinely must
    be processed again is the admin re-queue, which is deliberately not
    a curator's button.

    A legacy row keeps the paused message: its stages are gone rather
    than pointless, and the bar still offers it one (#173).

    :param request: The HTTP request.
    :param pk: Scan primary key.
    :return: Redirect to the scan processing page.
    """
    from scanning import services

    scan = get_object_or_404(Scan, pk=pk)
    if services.has_legacy_ocr(scan):
        messages.warning(request, PIPELINE_PAUSED_MESSAGE)
    else:
        messages.warning(request, REVALIDATE_UNAVAILABLE_MESSAGE)
    return redirect("scan_process", pk=scan.pk)


@login_required
@require_POST
def start_detect(request: HttpRequest, pk: int) -> HttpResponse:
    """Skip to review 2 when detections exist; otherwise explain.

    The only thing left of this action is its shortcut: a scan that has
    detections goes straight to step 2. It starts nothing itself -- the
    daemon starts the detection run once per shard set (#250) -- so a
    volume with no detections is told where its run stands
    (:func:`detection_message`).

    Approval is the gate (#151), in the view and not only in the bar:
    a scan still in READY_FOR_PAGE_COMPLETENESS_REVIEW is sent back to
    step 1, so a direct POST cannot walk past the review the template
    hides the button for. READY is the one status where the approval
    is pending; a legacy scan never holds it, so the old rows keep
    their shortcut.

    :param request: The HTTP request.
    :param pk: Scan primary key.
    :return: Redirect to the scan processing page (step 2).
    """
    scan = get_object_or_404(Scan, pk=pk)
    # The approval first, then the pending edits: a scan already
    # approved with an open edit must hear about the edit, not be
    # asked to approve again (#232).
    if scan.status == Status.READY_FOR_PAGE_COMPLETENESS_REVIEW:
        messages.warning(request, PAGE_REVIEW_APPROVAL_REQUIRED_MESSAGE)
        return redirect(
            reverse("scan_process", kwargs={"pk": scan.pk}) + "?step=1"
        )
    guard = _block_if_pending_changes(request, scan)
    if guard:
        return guard
    if Detection.objects.filter(scan=scan).exists():
        # Detections exist: the #196 apply imported them, or the old
        # full pipeline left them behind.
        return redirect(
            reverse("scan_process", kwargs={"pk": scan.pk}) + "?step=2"
        )

    messages.info(request, detection_message(yolo.run_summary(scan)))
    return redirect("scan_process", pk=scan.pk)


@dataclass(frozen=True)
class ShardRead:
    """What one engine's start button says, asks and calls.

    The three buttons (#190, #191, #364) are one view: each writes one
    ``ExternalJob`` row per original shard, behind the same four gates,
    and each answers the same five messages. Only the words and the
    three functions differ, so they are an entry here rather than a
    copy of the view.

    :ivar name: The view's name, for its log line.
    :ivar label: What a message calls this read ("Mistral OCR").
    :ivar off_label: What the "not switched on" line calls it. The
        dots.mocr button says "OCR" everywhere else, but naming the
        engine is what makes its two switches findable.
    :ivar cost: What a press spends, in the staff refusal.
    :ivar switches: The environment names an operator must set.
    :ivar dispatch: What the daemon does next, in the success line.
        Mistral renders the pages itself before it sends them.
    :ivar is_enabled: Whether this stage may be dispatched at all.
    :ivar run_summary: The live run of this engine, or ``None``.
    :ivar create: The row creator. **This is what costs money**, which
        is why the AST test of ``TestKnownEnqueuePaths`` pins the
        modules that name one.
    """

    name: str
    label: str
    off_label: str
    cost: str
    switches: str
    dispatch: str
    is_enabled: Callable[[], bool]
    run_summary: Callable[[Scan], dict | None]
    create: Callable[[Scan, dict], list[ExternalJob]]


def _shard_reads() -> dict[str, ShardRead]:
    """Return the three reads over a volume's original shards.

    Rebuilt on each call, and deliberately not cached, for the reason
    ``jobs._runpod_engines`` is: the entries read functions off the
    stage modules at build time, so a test that patches
    ``mistral_ocr.enabled`` reaches this table too.

    :returns: The table, keyed by engine.
    :rtype: dict[str, ShardRead]
    """
    return {
        JobEngine.DOTS_MOCR: ShardRead(
            name="start_dots_mocr",
            label="OCR",
            off_label="dots.mocr",
            cost="GPU time",
            switches="DOTS_MOCR_ENABLED and RUNPOD_DOTSMOCR_ENDPOINT_ID",
            dispatch="sends them to RunPod",
            is_enabled=dots_mocr.enabled,
            run_summary=dots_mocr.run_summary,
            create=dots_mocr.ensure_analyze_jobs,
        ),
        JobEngine.MISTRAL_OCR: ShardRead(
            name="start_mistral_ocr",
            label="Mistral OCR",
            off_label="Mistral OCR",
            cost="money",
            switches="MISTRAL_API_KEY",
            # The daemon renders every page of the shard before it
            # uploads it, which is minutes rather than a POST (#191).
            dispatch="renders and sends them",
            is_enabled=mistral_ocr.enabled,
            run_summary=mistral_ocr.run_summary,
            create=mistral_ocr.ensure_extract_jobs,
        ),
        JobEngine.SURYA: ShardRead(
            name="start_surya_ocr",
            label="Surya OCR",
            off_label="Surya OCR",
            cost="money",
            switches="RUNPOD_SURYA_ENDPOINT_ID",
            dispatch="sends them to RunPod",
            is_enabled=surya.enabled,
            run_summary=surya.run_summary,
            create=surya.ensure_extract_jobs,
        ),
    }


def _start_shard_read(
    request: HttpRequest, pk: int, spec: ShardRead
) -> HttpResponse:
    """Create one engine's rows over a scan's original shards.

    The body of the three start buttons. Four gates, in this order:

    1. **Staff only.** Every press can start real paid work.
    2. **The stage must be switched on.** An environment that must not
       spend leaves the engine's key or endpoint id unset.
    3. **An open run is not restarted.** It means the daemon is still
       working on the last press. A *finished* run is reused rather
       than refused, which is what keeps the creator from paying twice
       for shards already read.
    4. **The shard set must be committed.**
       ``sharding.committed_manifest`` verifies the stored set against
       the original with one ``head_object``, and a stale or missing
       set is refused, because re-cutting is the pipeline's job. So a
       web pod never pulls the original.

    **This request calls no provider.** It writes one ``ExternalJob``
    row per shard and returns; the daemon's next ``submit_external_jobs``
    tick sends them, and ``collect_external_jobs`` polls, harvests and
    retries them. That keeps a request thread off a slow HTTP call, and
    it is what makes a run survive a redeployed web pod.

    The answer says what happened and never more: a dispatch that is
    coming, a run that was reused, or a shard set with nothing in it.

    :param request: The HTTP request.
    :param pk: Scan primary key.
    :param spec: Which read to start.
    :return: Redirect to the scan processing page.
    """
    from scanning import sharding

    scan = get_object_or_404(Scan, pk=pk)
    back = redirect("scan_process", pk=scan.pk)

    if not request.user.is_staff:
        messages.error(
            request,
            f"Only staff can start {spec.label}: each run costs {spec.cost}.",
        )
        return back

    if not spec.is_enabled():
        messages.warning(
            request,
            f"{spec.off_label} is not switched on in this environment. "
            f"Set {spec.switches} first.",
        )
        return back

    summary = spec.run_summary(scan)
    if summary and summary["open"]:
        messages.info(
            request,
            f"{spec.label} run {summary['run']} is already going: "
            f"{summary['done']} of {summary['total']} part(s) done.",
        )
        return back

    manifest, reason = sharding.committed_manifest(scan)
    if manifest is None:
        messages.warning(request, reason)
        return back

    created = spec.create(scan, manifest)
    queued = sum(1 for job in created if job.status == JobStatus.PENDING)
    logger.info(
        "%s: scan=%s user=%s run=%s shards=%d queued=%d",
        spec.name,
        scan.pk,
        request.user.pk,
        created[0].run if created else "?",
        len(created),
        queued,
    )
    if queued:
        messages.success(
            request,
            f"Queued {spec.label} for {queued} part(s) of this volume. "
            f"The daemon {spec.dispatch} within a few seconds.",
        )
    elif created:
        # The creator reused a run that is already done, so nothing was
        # queued and nothing will be sent. Saying otherwise would have
        # staff waiting on a dispatch that is not coming.
        messages.info(
            request,
            f"This volume was already read: run {created[0].run} covers "
            f"all {len(created)} part(s). Nothing new was queued.",
        )
    else:
        messages.warning(request, NO_SHARDS_TO_READ_MESSAGE)
    return back


@login_required
@require_POST
def start_dots_mocr(request: HttpRequest, pk: int) -> HttpResponse:
    """Start the dots.mocr stage over a scan's original shards (#190).

    Since #207 the pipeline creates these rows for every new upload, so
    this button is the manual way in: a fresh run over an edited
    volume, or a backfill for a scan uploaded while the stage was
    button-only.

    :param request: The HTTP request.
    :param pk: Scan primary key.
    :return: See :func:`_start_shard_read`.
    """
    return _start_shard_read(request, pk, _shard_reads()[JobEngine.DOTS_MOCR])


@login_required
@require_POST
def start_mistral_ocr(request: HttpRequest, pk: int) -> HttpResponse:
    """Start the Mistral OCR read over a scan's shards (#191).

    The only way into this stage until a daemon trigger lands. The read
    is over the original shards, so the button waits on no review state
    and on no redacted volume: the set exists from the moment the
    pipeline cut it.

    :param request: The HTTP request.
    :param pk: Scan primary key.
    :return: See :func:`_start_shard_read`.
    """
    return _start_shard_read(
        request, pk, _shard_reads()[JobEngine.MISTRAL_OCR]
    )


@login_required
@require_POST
def start_surya_ocr(request: HttpRequest, pk: int) -> HttpResponse:
    """Start the Surya OCR read over a scan's shards (#364).

    The only way into this stage: no tick and no pipeline arm creates a
    Surya row. The read is over the original shards, so the button
    waits on no review state, and the pages are unredacted, which is
    what a reader of the headnote brackets needs (#303).

    :param request: The HTTP request.
    :param pk: Scan primary key.
    :return: See :func:`_start_shard_read`.
    """
    return _start_shard_read(request, pk, _shard_reads()[JobEngine.SURYA])


@login_required
@require_POST
def recalculate(request: HttpRequest, pk: int) -> HttpResponse:
    """Rebuild the page number Issues from the stored readings.

    The "Recompute page number issues" button of review 1 (#151). It
    runs on stored data only, so it works on a web pod that never
    pulled the scan's files from S3 (#153).

    Two cases it answers rather than obeys. A scan the retired
    PaddleOCR stage read gets the legacy message and no recompute: the
    readings cannot change, so a rebuild would only look like work
    (#173). A scan carrying pending inserts or deletes gets the recompute
    plus the warning that says what is and is not done with those
    rows: nothing applies them until #206. Blocking here would leave
    the curator with no way forward at all, which is the fault that
    took the approve button away until #232.

    :param request: The HTTP request.
    :param pk: Scan primary key.
    :return: Redirect to the scan processing page.
    """
    scan = get_object_or_404(Scan, pk=pk)
    if not scan.ocr_results:
        return redirect("scan_process", pk=pk)
    from scanning import services

    if services.has_legacy_ocr(scan):
        messages.warning(request, LEGACY_OCR_RECOMPUTE_MESSAGE)
        return redirect("scan_process", pk=pk)
    if page_edits.has_pending_changes(scan):
        messages.warning(request, PENDING_EDITS_SAVED_MESSAGE)

    # Breadcrumb (issue #115): recalculation runs synchronously on the request
    # thread over the scan's OCR results.
    logger.info("recalculate: recomputing issues for scan=%s", scan.pk)
    services.recalculate_issues(scan)
    messages.success(request, RECOMPUTE_DONE_MESSAGE)
    return redirect("scan_process", pk=pk)


def page_numbers_missing_message(pages: list[int]) -> str:
    """Name the pages that have no page number, for the refusal (#342).

    At most ``MAX_NAMED_PAGES`` of them, then a count: the message is
    read in one line at the top of the page, and a volume can leave
    hundreds. The cards name every one of them, and the note in the
    bar sends the reviewer to the first.

    :param pages: What ``page_numbers.pages_without_number`` returned.
    :returns: The message the view flashes.
    :rtype: str
    """
    named = ", ".join(str(page) for page in pages[:MAX_NAMED_PAGES])
    if len(pages) > MAX_NAMED_PAGES:
        named += f" and {len(pages) - MAX_NAMED_PAGES} more"
    return PAGE_NUMBERS_MISSING_MESSAGE.format(
        count=len(pages),
        plural="" if len(pages) == 1 else "s",
        pages=named,
    )


@login_required
@require_POST
def approve_page_completeness(request: HttpRequest, pk: int) -> HttpResponse:
    """Record that a person reviewed the scan for page completeness.

    The approve button of review 1 (#151), and the only writer of
    ``PAGE_COMPLETENESS_REVIEW_DONE`` (#154). Every logged-in user may
    press it: review 1 is the scanners' own step, not a staff one.

    The write is one compare-and-swap on READY, never a full instance
    save. The collect tick can write READY over the same row at the
    same moment (``services.run_compute_issues``), and a scan that is
    cancelled, errored, or still waiting on its inputs must not be
    approved by a stale page a curator left open.

    **A waiting repair request refuses the approval** (#266). A page a
    scanner must still scan is a page the volume does not have, so the
    volume is not page complete, and the step-1 bar shows a note in
    place of the button. The gate is here as well as in the bar,
    because a template gate alone cannot refuse a direct POST -- the
    rule ``start_detect`` follows for the review it gates. Open
    *issues* still do not block: a suspicion is the curator's to
    judge, and a missing page is not (#151).

    **A page with no page number refuses it too** (#342), which is the
    one open card that is not a suspicion. The opinions are named by
    their printed page (#335), so a page nobody numbered would name an
    opinion by its position in the volume, a number no person approved
    and one that moves when the first page of the volume moves. The
    rule is ``page_numbers.pages_without_number``, and its answers are
    a number, a deletion or a dismissal of that page's card.

    The two rules are read together and each flashes its own message.
    A reviewer who answers one must see the other without a second
    press of the button. Both are read in READY alone, which is the
    condition ``_review_flags`` reads for the bar: in every other
    status the compare-and-swap below owns the answer, and a volume
    already approved has locked pages, so a gate would name work
    nobody can do.

    The gate is a read, then the compare-and-swap. A request made
    between the two does not block that approval, and the plan accepts
    it: both acts are decisions of a person, seconds apart, and the way
    back from a wrong approval is the admin re-queue whichever wins.

    :param request: The HTTP request.
    :param pk: Scan primary key.
    :return: Redirect to step 1 of the scan processing page.
    """
    scan = get_object_or_404(Scan, pk=pk)
    # The two gates speak for the status that offers the button, and
    # for no other. A volume already approved has its pages locked, so
    # "type the number" would name work nobody can do; the
    # compare-and-swap below says what is true of such a row instead.
    # It is the condition ``_review_flags`` reads, so the bar and the
    # view cannot disagree.
    if scan.status == Status.READY_FOR_PAGE_COMPLETENESS_REVIEW:
        refusals = []
        if repairs.has_waiting(scan):
            refusals.append(REPAIRS_WAITING_MESSAGE)
        missing = page_numbers.pages_without_number(scan)
        if missing:
            refusals.append(page_numbers_missing_message(missing))
        if refusals:
            # One message for each rule, repairs first: the two
            # refusals are different work for different people, and a
            # reviewer who answers one must see the other without a
            # second press.
            for refusal in refusals:
                messages.warning(request, refusal)
            return redirect(
                reverse("scan_process", kwargs={"pk": scan.pk}) + "?step=1"
            )
    approved = Scan.objects.filter(
        pk=scan.pk, status=Status.READY_FOR_PAGE_COMPLETENESS_REVIEW
    ).update(status=Status.PAGE_COMPLETENESS_REVIEW_DONE)
    if approved:
        # Breadcrumb (issue #115): this is a human decision, and the
        # only record of who made it.
        logger.info(
            "approve_page_completeness: scan=%s approved by user=%s",
            scan.pk,
            request.user.pk,
        )
        messages.success(request, PAGE_REVIEW_APPROVED_MESSAGE)
    else:
        # The write lost, so the fetch above is stale: a concurrent
        # approval moved the row between the read and the write.
        # Re-read it so the message describes the row as it is.
        scan.refresh_from_db()
        if scan.status == Status.PAGE_COMPLETENESS_REVIEW_DONE:
            messages.info(request, PAGE_REVIEW_ALREADY_DONE_MESSAGE)
        else:
            messages.warning(request, PAGE_REVIEW_NOT_READY_MESSAGE)
    return redirect(
        reverse("scan_process", kwargs={"pk": scan.pk}) + "?step=1"
    )


@login_required
@require_POST
def reopen_page_review(request: HttpRequest, pk: int) -> HttpResponse:
    """Open the page review again, after an approval.

    The way back for a late correction (#224). The approval locks the
    page edit endpoints, because the apply builds the final volume from
    the rows as they stand at the approval. A curator who then finds a
    page review 1 missed asks a staff member to press this. It
    supersedes the apply run in flight -- its open job rows are
    cancelled, its outputs stay in S3 -- and moves the scan back to
    READY with one compare-and-swap. The next approval writes DONE
    again, and the trigger builds ``a{n+1}`` from every standing row,
    reusing every paid result the edits did not change.

    Staff only: the reopen throws away a paid build, and the curators'
    own step is the approval, not its reversal.

    :param request: The HTTP request.
    :param pk: Scan primary key.
    :return: Redirect to step 1 of the scan processing page.
    """
    scan = get_object_or_404(Scan, pk=pk)
    if not request.user.is_staff:
        messages.warning(request, "Only a staff member can reopen a review.")
        return redirect(
            reverse("scan_process", kwargs={"pk": scan.pk}) + "?step=1"
        )
    from scanning import apply

    reopened = Scan.objects.filter(
        pk=scan.pk, status=Status.PAGE_COMPLETENESS_REVIEW_DONE
    ).update(status=Status.READY_FOR_PAGE_COMPLETENESS_REVIEW)
    if reopened:
        # After the status write, not before: an apply worker that
        # claims the scan between the two would build a run this
        # reopen then supersedes, and the status is what stops it.
        apply.supersede_runs(
            scan, f"Page review reopened by user {request.user.pk}"
        )
        logger.info(
            "reopen_page_review: scan=%s reopened by user=%s",
            scan.pk,
            request.user.pk,
        )
        messages.success(request, PAGE_REVIEW_REOPENED_MESSAGE)
    else:
        messages.warning(request, PAGE_REVIEW_NOT_REOPENABLE_MESSAGE)
    return redirect(
        reverse("scan_process", kwargs={"pk": scan.pk}) + "?step=1"
    )


@login_required
@require_POST
def approve_redaction_review(request: HttpRequest, pk: int) -> HttpResponse:
    """Record that a person reviewed the redactions of this scan.

    The approve button of review 2 (#263), and the only place a person
    closes it. Every logged-in user may press it, which is the rule of
    the review-1 approve button (#151): both are the same kind of human
    decision, and the log line below is the only record of who made
    this one.

    The write is one compare-and-swap on ``READY_FOR_REDACTION_REVIEW``
    (``opinions.queue_create_opinions``, #336): the scan goes to
    ``QUEUED`` with ``CREATE_OPINIONS``, the daemon writes the
    ``Opinion`` rows, and its worker parks the scan in
    ``REDACTION_REVIEW_DONE``. A failure parks it back here with the
    reason, so the next press is the retry. Never a full instance save:
    the collect tick and the redaction compute both write the status
    over the same row (``review_states``), and a scan that was
    re-queued, errored, or whose geometry is being measured again must
    not be approved from a stale page a curator left open.

    Open detections or unpaired opinions do not block it. The curator
    is the judge of the geometry, exactly as they are the judge of a
    page-completeness suspicion (#151).

    :param request: The HTTP request.
    :param pk: Scan primary key.
    :return: Redirect to step 2 of the scan processing page.
    """
    from scanning import opinions

    scan = get_object_or_404(Scan, pk=pk)
    if opinions.queue_create_opinions(scan):
        logger.info(
            "approve_redaction_review: scan=%s approved by user=%s",
            scan.pk,
            request.user.pk,
        )
        messages.success(request, REDACTION_REVIEW_APPROVED_MESSAGE)
    else:
        # The write lost, so the fetch above is stale. Re-read the row
        # so the message describes it as it is.
        scan.refresh_from_db()
        if scan.status == Status.REDACTION_REVIEW_DONE:
            messages.info(request, REDACTION_REVIEW_ALREADY_DONE_MESSAGE)
        elif (
            scan.status in (Status.QUEUED, Status.PROCESSING)
            and scan.queued_action == QueuedAction.CREATE_OPINIONS
        ):
            messages.info(request, REDACTION_REVIEW_QUEUED_MESSAGE)
        else:
            messages.warning(request, REDACTION_REVIEW_NOT_READY_MESSAGE)
    return redirect(
        reverse("scan_process", kwargs={"pk": scan.pk}) + "?step=2"
    )


@login_required
@require_POST
def reprocess(request: HttpRequest, pk: int) -> HttpResponse:
    """Refuse to apply pending page edits while the pipeline is paused.

    Applying inserts/deletions re-ran OCR on the edited pages through
    the retired PaddleOCR path (issue #173), so this fails with the
    unified message until the dots.mocr replacement lands. The
    ``PageEdit`` rows stay recorded and will be applicable again then
    (#206).

    :param request: The HTTP request.
    :param pk: Scan primary key.
    :return: Redirect to the scan processing page.
    """
    scan = get_object_or_404(Scan, pk=pk)
    messages.warning(request, PIPELINE_PAUSED_MESSAGE)
    return redirect("scan_process", pk=scan.pk)


#: What the curator may type, in the one wording the server and both
#: viewers use. ``shared.js`` carries the copy the browser shows
#: before the request leaves the page (#319).
PAGE_NUMBER_ERROR = (
    "Page number must be a positive whole number, a number with one "
    "trailing letter like 2094a, or a range like 678-686."
)


def _page_number_value(raw) -> str | None:
    """Return a curator's page number entry, normalized, or None.

    Accepts the three shapes a book prints: a positive whole number; a
    number with one trailing letter (``2094a``, issue #319) on the page
    the book adds between two numbered pages; and a range like
    ``678-686`` for the one PDF page that carries several book pages,
    the shape ``CheckName.PAGE_RANGE`` exists for. A blank entry is the
    curator clearing the number, which is a decision, so it returns the
    empty string rather than None.

    A reporter prints the range with an en dash (``913–925``), and a
    curator types what the page shows, so an en dash and an em dash
    read as the hyphen (issue #233). The stored value carries one
    hyphen, which is the shape every reader of a range parses
    (``services._page_number_lookup``,
    ``blackletter.validate.RANGE_RE``).

    The case of a trailing letter is kept: the book prints one of the
    two glyphs and no reader compares them. The reader asks for two
    digits before the letter (``page_numbers.MIN_SUFFIXED_DIGITS``) and
    this does not: that guard is against a token of a running head, and
    here a person has the page in front of them.

    :param raw: The ``page_number`` field of the request body.
    :returns: The value for ``PageEdit.value``, or None when the entry
        is not a page number at all.
    :rtype: str | None
    """
    if raw is None:
        return ""
    text = str(raw).strip().replace("–", "-").replace("—", "-")
    if not text:
        return ""
    if page_numbers.number_type(text) == page_numbers.SUFFIXED:
        if int(text[:-1]) < 1:
            return None
        return f"{int(text[:-1])}{text[-1]}"
    parts = [part.strip() for part in text.split("-")]
    if len(parts) > 2 or not all(p.isdigit() and int(p) >= 1 for p in parts):
        return None
    if len(parts) == 2 and int(parts[0]) >= int(parts[1]):
        return None
    return "-".join(str(int(p)) for p in parts)


@login_required
@require_POST
def assign_page(request: HttpRequest, pk: int) -> JsonResponse:
    """Record the page number a curator read off the page itself.

    One ``PageEdit`` row per page, since #214: the decision used to be
    an edit of one entry inside the ``Scan.ocr_results`` list, written
    back whole, so two curators on two pages of one volume lost one of
    the two numbers with no error and no trace.

    A blank ``page_number`` clears the number, marking the page as
    having none (front matter the model mis-tagged, say). It is the
    same kind of row with a blank value, so the row's existence still
    separates "a person cleared this" from "the model read nothing".
    Any other value is a printed number, or a printed range like
    ``678-686`` when one PDF page carries several book pages, which is
    what ``page_range`` is raised for.

    The blob is then rebuilt from the run plus the rows, so the viewer
    shows the number and its duplicate flags at once.

    :param request: The HTTP request (JSON body with ``pdf_page`` and
        ``page_number``; ``page_number`` may be null/empty to clear).
    :param pk: Scan primary key.
    :return: JSON response with the stored value and duplicate flag.
    """
    scan = get_object_or_404(Scan, pk=pk)
    locked = _refuse_locked_edits(scan)
    if locked is not None:
        return locked
    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON"}, status=400)
    pdf_page = data.get("pdf_page")
    if pdf_page is None:
        return JsonResponse({"error": "pdf_page is required."}, status=400)
    if "page_number" not in data:
        return JsonResponse(
            {"error": 'page_number is required (send null or "" to clear).'},
            status=400,
        )
    page_value = _page_number_value(data["page_number"])
    if page_value is None:
        return JsonResponse(
            {"error": PAGE_NUMBER_ERROR},
            status=400,
        )

    ocr_results = scan.ocr_results
    entry = next((r for r in ocr_results if r["pdf_page"] == pdf_page), None)
    if entry is None:
        return JsonResponse({"error": "Unknown PDF page."}, status=404)

    page_edits.supersede(
        scan,
        PageEdit.Kind.SET_NUMBER,
        {"pdf_page": pdf_page},
        {
            "value": page_value,
            # The reading this number overrules. It is rebuilt from
            # the run on every recompute, so this row is the only
            # record that a person disagreed with the model.
            "previous_value": str(entry.get("detected") or ""),
            "source_fingerprint": scan.source_fingerprint,
        },
        request.user,
    )

    # Clear the page's no-page-number flag; the rebuild does not touch Issue
    # rows, so a full Recheck re-derives the issue list (and any new flag).
    scan.issues.filter(
        check_name=CheckName.NO_PAGE_NUMBER, page_number=pdf_page
    ).delete()

    # Rebuild page_map so the viewer/sidebar duplicate flags reflect the edit
    # immediately. It overlays the rows, so it also writes the new number
    # into the cached ocr_results.
    from scanning import services

    services.rebuild_page_map(scan)

    duplicate = any(
        e.get("type") == "pdf_page"
        and e.get("pdf_index") == pdf_page - 1
        and e.get("duplicate")
        for e in scan.page_map
    )
    return JsonResponse(
        {
            "status": "ok",
            "detected": page_value or None,
            # The shape, so the viewer draws the right tag without
            # deriving it from the string a second time (#319).
            "type": page_numbers.number_type(page_value),
            "duplicate": duplicate,
        }
    )


def _uploaded_page_file(upload) -> str | None:
    """Return ``"pdf"`` or ``"image"`` for an accepted upload, else None.

    A curator scans a page as often to a PDF as to an image (#232), so
    both endpoints that take a page take both. Three rules:

    - **The bytes decide, not the content type.** A browser names the
      type, and a person can name it wrong. A file that says PDF and
      does not start with ``%PDF-`` is refused, and a file that starts
      with it is a PDF whatever the browser said. An image is judged
      the same way, against ``_IMAGE_MAGIC``: a file sent as
      ``image/svg+xml`` used to pass on its content type alone and
      fail in ``insert_image`` at the export.
    - **The stored name carries the right extension.** A browser may
      send a PDF with no extension at all, or a JPEG named ``.png``.
      ``models.page_edit_image_path`` keeps the extension of the name,
      and ``views_api.export_pdf`` reads it to decide between
      ``insert_pdf`` and ``insert_image``, so a wrong extension puts a
      page through the wrong call.
    - **The upload is rewound.** Both readers here read from the file
      the storage backend then saves.

    :param upload: The ``UploadedFile``, or None.
    :returns: The kind, or None when the file is neither.
    :rtype: str | None
    """
    if upload is None:
        return None
    head = upload.read(_MAGIC_LENGTH)
    upload.seek(0)
    if head.startswith(_PDF_MAGIC):
        kind, ext = "pdf", "pdf"
    else:
        ext = next(
            (ext for magic, ext in _IMAGE_MAGIC if head.startswith(magic)),
            None,
        )
        if ext is None:
            return None
        kind = "image"
    base = (upload.name or "page").rsplit("/", 1)[-1].rsplit(".", 1)[0]
    upload.name = f"{base[:64] or 'page'}.{ext}"
    return kind


def upload_too_large_message() -> str:
    """Return the refusal of a page file over the cap, with the cap in MB.

    :returns: :data:`UPLOAD_TOO_LARGE_MESSAGE` with the current
        ``settings.PAGE_UPLOAD_MAX_BYTES``.
    :rtype: str
    """
    return UPLOAD_TOO_LARGE_MESSAGE.format(
        mb=settings.PAGE_UPLOAD_MAX_BYTES // (1024 * 1024)
    )


def _pdf_page_count(upload) -> int | None:
    """Return how many pages an uploaded PDF holds, or None.

    Django writes an upload over ``FILE_UPLOAD_MAX_MEMORY_SIZE`` to a
    temporary file, and fitz opens that file by its path. So a file at
    the cap is not read into the web pod's memory a second time: the
    cap is 512 MiB by default, and one worker that held one file
    would take more than the pod asks for.

    :param upload: The ``UploadedFile``, rewound by
        :func:`_uploaded_page_file`.
    :returns: The page count, or None when the file will not open.
    :rtype: int | None
    """
    temporary_file_path = getattr(upload, "temporary_file_path", None)
    try:
        if temporary_file_path is not None:
            with fitz.open(temporary_file_path(), filetype="pdf") as doc:
                return doc.page_count
        data = upload.read()
        upload.seek(0)
        with fitz.open(stream=data, filetype="pdf") as doc:
            return doc.page_count
    except Exception:
        return None


def _accept_page_upload(upload, one_page: bool) -> tuple[str | None, str]:
    """Judge one uploaded page file for either endpoint.

    :param upload: The ``UploadedFile``, or None.
    :param one_page: Whether the file must hold exactly one page. True
        for a replacement: one page stands for one page, and a volume
        uploaded by mistake would land whole on that address. An
        insert takes every page, because a missing leaf is often two
        and ``export_pdf`` already places them all.
    :returns: The kind and an empty message, or None and the message
        the curator reads.
    :rtype: tuple[str | None, str]
    """
    if upload is None:
        return None, "Missing file"
    if upload.size and upload.size > settings.PAGE_UPLOAD_MAX_BYTES:
        return None, upload_too_large_message()
    kind = _uploaded_page_file(upload)
    if kind is None:
        return None, UPLOAD_WRONG_TYPE_MESSAGE
    if kind == "pdf":
        pages = _pdf_page_count(upload)
        if pages is None or pages < 1:
            return None, UPLOAD_BAD_PDF_MESSAGE
        if one_page and pages != 1:
            return None, REPLACEMENT_IS_ONE_PAGE_MESSAGE.format(pages=pages)
    return kind, ""


def _save_page_file_row(edit: PageEdit, upload, before=None) -> bool:
    """Store an upload's file, then its row; drop the file if the row loses.

    Two curators who act on one page at the same moment both find no
    row to withdraw, and the second insert loses to the partial unique
    key. ``PageEdit.objects.create`` would have uploaded the file inside
    the row's own save, so the losing request left an object in the
    bucket that no row names. Here the file goes to the storage first
    and the row after it, and a refused row takes its file back.

    :param edit: The unsaved row. Its ``image`` is empty.
    :param upload: The ``UploadedFile`` to store under it.
    :param before: Run inside the row's transaction, before the save:
        the withdrawal of the rows this one supersedes.
    :returns: Whether the row was saved. False when the unique key
        refused it, which is the race above.
    :rtype: bool
    """
    edit.image.save(upload.name, upload, save=False)
    try:
        with transaction.atomic():
            if before is not None:
                before()
            edit.save()
    except IntegrityError:
        edit.image.delete(save=False)
        return False
    return True


def _pdf_page_of(scan: Scan, raw) -> int | None:
    """Return a 1-based page of this volume, or None.

    An address the volume does not have is refused rather than stored:
    a row naming a page that is not there is the drift this model
    exists to prevent.

    :param scan: The scan the page belongs to.
    :param raw: The page number from the request.
    :returns: The page, or None when it is not one of this volume's.
    :rtype: int | None
    """
    try:
        page = int(raw)
    except (TypeError, ValueError):
        return None
    if page < 1:
        return None
    if scan.page_count and page > scan.page_count:
        return None
    return page


#: What a printed page number may be made of. Not digits alone: a
#: volume prints roman numerals in its front matter ("xiv"), letter
#: suffixes on inserted leaves ("1075a"), and section numbers ("A-3").
#: Everything outside this refuses -- which is every character markup
#: is made of, plus every control character. The label is a person's
#: typing that every viewer of the scan then sees, so it is narrowed
#: here *and* escaped where the viewer draws it (``escapeHtml`` in
#: ``shared.js``): narrowing alone would be one regex away from an
#: injection, and escaping alone would keep junk in the column.
_PAGE_LABEL_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z .\u2013/-]*$")


def _page_label(raw: str) -> str | None:
    """Return the printed page number a curator filed a page under.

    :param raw: The ``page_number`` form field.
    :returns: The label, or None when it is not a page number at all.
        An empty entry is allowed and returns the empty string: an
        inserted page need not carry a printed number.
    :rtype: str | None
    """
    label = (raw or "").strip()
    if not label:
        return ""
    if len(label) > 32 or not _PAGE_LABEL_RE.match(label):
        return None
    return label


def _anchor_of(scan: Scan, raw, label: str) -> int | None:
    """Return the original page an uploaded image follows, or None.

    The viewer sends the anchor it rendered. An older viewer sends only
    the printed number, so the anchor is resolved from the stored page
    map instead -- the same walk the viewer's stamp comes from.

    :param scan: The scan the insert belongs to.
    :param raw: The ``anchor_pdf_page`` field, when the viewer sent one.
    :param label: The printed page number the placeholder showed.
    :returns: The anchor, or None when the position cannot be resolved.
    :rtype: int | None
    """
    if raw not in (None, ""):
        try:
            anchor = int(raw)
        except (TypeError, ValueError):
            return None
        if 0 <= anchor <= (scan.page_count or anchor):
            return anchor
        return None
    if not label:
        return None
    for entry in page_edits.project_inserts(scan, scan.page_map):
        if entry.get("type") == "missing" and str(
            entry.get("logical_number")
        ) == str(label):
            return entry.get("anchor_pdf_page")
    return None


@login_required
@require_POST
def delete_page(request: HttpRequest, pk: int) -> HttpResponse:
    """Mark a page of the original for deletion.

    A duplicate page, or a blank one: two of the issue types review 1
    exists to find. One ``PageEdit`` row, addressed by the physical
    page (#214). The apply (#206) decides what a delete does to the
    volume; until then the row is a saved decision and nothing else.

    Several pages at once, when the body carries ``pdf_pages``: the
    front-matter card offers the unnumbered run before the first
    printed number as one decision, and one request is one confirm.
    Every page is checked before any row is written, so a request that
    names a page the volume does not have writes nothing.

    :param request: The HTTP request (JSON body with ``pdf_page``, or
        ``pdf_pages`` for several).
    :param pk: Scan primary key.
    :return: JSON response confirming the deletion record, with the
        pages it marked.
    """
    scan = get_object_or_404(Scan, pk=pk)
    locked = _refuse_locked_edits(scan)
    if locked is not None:
        return locked
    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON"}, status=400)
    raw = data.get("pdf_pages")
    if raw is None:
        raw = [data.get("pdf_page")]
    if not isinstance(raw, list) or not raw:
        return JsonResponse({"error": "Unknown PDF page."}, status=404)
    pages = [_pdf_page_of(scan, value) for value in raw]
    if any(page is None for page in pages):
        return JsonResponse({"error": "Unknown PDF page."}, status=404)
    # A standing deletion is left as it is: a second click has nothing
    # to refresh. An applied one is superseded, so the new decision is
    # a row of its own (#224).
    for pdf_page in sorted(set(pages)):
        page_edits.supersede(
            scan,
            PageEdit.Kind.DELETE_PAGE,
            {"pdf_page": pdf_page},
            {"source_fingerprint": scan.source_fingerprint},
            request.user,
            refresh_open=False,
        )
    return JsonResponse({"status": "ok", "pdf_pages": sorted(set(pages))})


#: The refusal of a move whose anchor is its own page.
MOVE_ONTO_ITSELF_MESSAGE = "A page cannot follow itself."


@login_required
@require_POST
def move_page(request: HttpRequest, pk: int) -> HttpResponse:
    """Move pages of the original to after other ones (#261, #395).

    A span scanned out of its order, the case the ``backward_page``
    card finds: one ``PageEdit`` row per page that moves, addressed by
    the page and the original page it lands after (0 for before page
    1), the insert's vocabulary, plus an ``ordinal`` that orders the
    pages landing on one anchor. The apply (#224) writes each page at
    its new place and pays no read for it; until then the rows are a
    saved decision, and the page map is rebuilt so the viewer and the
    sidebar draw the corrected order at once.

    The body is one move (``pdf_page``, ``anchor_pdf_page``), which is
    added to the moves that stand, or a list of them under ``moves``,
    the reorder the card offers, which **replaces** the standing set:
    the card derives its rows from the whole corrected order
    (``page_edits.rows_for_order``), so a page in no row of the list
    goes back to its slot, and a row equal to the standing one is left
    alone, applied or not. Every address is checked before any row is
    written, and the rows go in one transaction: a reorder half written
    is an order nobody chose. A second move of the same page refreshes
    the open row, as a page number does; an applied row is superseded.

    :param request: The HTTP request (JSON body).
    :param pk: Scan primary key.
    :return: JSON response confirming the move records.
    """
    scan = get_object_or_404(Scan, pk=pk)
    locked = _refuse_locked_edits(scan)
    if locked is not None:
        return locked
    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON"}, status=400)
    requested = data.get("moves")
    replace = requested is not None
    if requested is None:
        requested = [data]
    if not isinstance(requested, list) or not requested:
        return JsonResponse({"error": "Invalid JSON"}, status=400)
    moves = []
    for item in requested:
        if not isinstance(item, dict):
            return JsonResponse({"error": "Invalid JSON"}, status=400)
        pdf_page = _pdf_page_of(scan, item.get("pdf_page"))
        anchor = item.get("anchor_pdf_page")
        if anchor == 0 or anchor == "0":
            anchor = 0
        else:
            anchor = _pdf_page_of(scan, anchor)
        if pdf_page is None or anchor is None:
            return JsonResponse({"error": "Unknown PDF page."}, status=404)
        if anchor == pdf_page:
            return JsonResponse(
                {"error": MOVE_ONTO_ITSELF_MESSAGE}, status=409
            )
        # The column's own range (a small positive integer), so a
        # value it cannot hold is refused here and not by the database.
        try:
            ordinal = int(item.get("ordinal", 0))
            PageEdit._meta.get_field("ordinal").run_validators(ordinal)
        except (TypeError, ValueError, ValidationError):
            return JsonResponse({"error": "Invalid JSON"}, status=400)
        moves.append(
            {
                "pdf_page": pdf_page,
                "anchor_pdf_page": anchor,
                "ordinal": ordinal,
            }
        )
    if len({m["pdf_page"] for m in moves}) != len(moves):
        return JsonResponse({"error": "A page is named twice."}, status=409)
    from scanning import services

    standing = {
        edit.pdf_page: edit
        for edit in page_edits.current_edits(scan, PageEdit.Kind.MOVE_PAGE)
    }
    with transaction.atomic():
        if replace:
            gone = [
                edit.pk
                for pdf_page, edit in standing.items()
                if pdf_page not in {m["pdf_page"] for m in moves}
            ]
            if gone:
                page_edits.withdraw(
                    PageEdit.objects.filter(pk__in=gone), request.user
                )
        for move in moves:
            edit = standing.get(move["pdf_page"])
            if (
                edit is not None
                and edit.anchor_pdf_page == move["anchor_pdf_page"]
                and edit.ordinal == move["ordinal"]
            ):
                continue
            page_edits.supersede(
                scan,
                PageEdit.Kind.MOVE_PAGE,
                {"pdf_page": move["pdf_page"]},
                {
                    "anchor_pdf_page": move["anchor_pdf_page"],
                    "ordinal": move["ordinal"],
                    "source_fingerprint": scan.source_fingerprint,
                },
                request.user,
            )
    services.rebuild_page_map(scan)
    answer = {"status": "ok", "moves": moves}
    if len(moves) == 1:
        answer.update(moves[0])
    return JsonResponse(answer)


@login_required
@require_POST
def undo_move_page(request: HttpRequest, pk: int) -> HttpResponse:
    """Take back a page move, leaving the page where it was scanned.

    The row is stamped, not deleted (#232), like every decision a
    curator takes back.

    :param request: The HTTP request (JSON body with ``pdf_page``).
    :param pk: Scan primary key.
    :return: JSON response confirming the undo.
    """
    scan = get_object_or_404(Scan, pk=pk)
    locked = _refuse_locked_edits(scan)
    if locked is not None:
        return locked
    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON"}, status=400)
    from scanning import services

    page_edits.withdraw(
        page_edits.standing_edits(scan, PageEdit.Kind.MOVE_PAGE).filter(
            pdf_page=data.get("pdf_page")
        ),
        request.user,
    )
    services.rebuild_page_map(scan)
    return JsonResponse({"status": "ok"})


@login_required
@require_POST
def undo_delete_page(request: HttpRequest, pk: int) -> HttpResponse:
    """Take back a page deletion, restoring the page.

    The row is stamped, not deleted (#232): a decision a curator took
    back is history too, and the audit must show that somebody marked
    this page and somebody unmarked it.

    :param request: The HTTP request (JSON body with pdf_page).
    :param pk: Scan primary key.
    :return: JSON response confirming the undo.
    """
    scan = get_object_or_404(Scan, pk=pk)
    locked = _refuse_locked_edits(scan)
    if locked is not None:
        return locked
    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON"}, status=400)
    page_edits.withdraw(
        page_edits.standing_edits(scan, PageEdit.Kind.DELETE_PAGE).filter(
            pdf_page=data.get("pdf_page")
        ),
        request.user,
    )
    return JsonResponse({"status": "ok"})


@login_required
@require_POST
def add_page_insert(request: HttpRequest, pk: int) -> JsonResponse:
    """Upload the image of a page the volume is missing.

    The address is the gap, not the printed number (#214):
    ``anchor_pdf_page`` is the original page the image follows, and 0
    puts it before page 1. The viewer stamps that anchor on every
    ``missing`` placeholder it renders and sends it back here, so the
    physical position is resolved once, by the person who can see it.
    A printed number cannot address anything -- front matter has none,
    and two pages can print the same one -- so it is kept beside the
    address as the label.

    The label is free text on purpose: a printed page number is not
    always a whole number ("xiv", "1075a", "A-3"), so casting it to an
    integer would lose what the curator read off the page. It is
    narrowed to the alphabet a printed number uses (``_page_label``)
    and escaped where the viewer draws it, rather than cast.

    The file may be an image of the page or a PDF of it (#232), and a
    PDF may hold several pages: a missing leaf is often two, and
    ``views_api.export_pdf`` already places every page of one.

    :param request: The HTTP request (form data with ``image``,
        ``anchor_pdf_page`` and the ``page_number`` label).
    :param pk: Scan primary key.
    :return: JSON response with the insert URL and page number.
    """
    scan = get_object_or_404(Scan, pk=pk)
    locked = _refuse_locked_edits(scan)
    if locked is not None:
        return locked
    image_file = request.FILES.get("image")
    kind, refusal = _accept_page_upload(image_file, one_page=False)
    if kind is None:
        return JsonResponse({"error": refusal}, status=400)
    label = _page_label(request.POST.get("page_number"))
    if label is None:
        return JsonResponse(
            {
                "error": (
                    "A page number may hold letters and digits, spaces, "
                    "a dot, a slash and a dash, and no more than 32 of "
                    "them."
                )
            },
            status=400,
        )
    anchor = _anchor_of(scan, request.POST.get("anchor_pdf_page"), label)
    if anchor is None:
        return JsonResponse(
            {
                "error": (
                    "This page could not be placed in the volume. "
                    "Reload the page and try again."
                )
            },
            status=400,
        )

    edit = PageEdit(
        scan=scan,
        kind=PageEdit.Kind.INSERT_PAGE,
        author=request.user,
        anchor_pdf_page=anchor,
        ordinal=page_edits.next_ordinal(scan, anchor),
        logical_page=label,
        source_fingerprint=scan.source_fingerprint,
    )
    # Two inserts into one gap at the same moment compute one ordinal;
    # the second loses to the partial key and leaves no file behind.
    if not _save_page_file_row(edit, image_file):
        return JsonResponse({"error": UPLOAD_LOST_RACE_MESSAGE}, status=409)
    return JsonResponse(
        {
            "status": "ok",
            "page_number": label,
            "edit_id": edit.pk,
            "image_url": edit.image.url,
            "kind": kind,
            "file_url": reverse(
                "page_edit_file", kwargs={"pk": scan.pk, "edit_id": edit.pk}
            ),
        }
    )


@login_required
@require_POST
def remove_page_insert(request: HttpRequest, pk: int) -> JsonResponse:
    """Take back an uploaded page image.

    The portal had no way to undo an insert: a deletion had its undo
    and an insert did not, so a wrong image could only be replaced by
    another one. The row is stamped and the file is kept (#232): the
    audit shows what a person uploaded, and an object nobody reads
    costs less than a row that names a file which is gone.

    :param request: The HTTP request (JSON body with ``edit_id``).
    :param pk: Scan primary key.
    :return: JSON response confirming the removal.
    """
    scan = get_object_or_404(Scan, pk=pk)
    locked = _refuse_locked_edits(scan)
    if locked is not None:
        return locked
    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON"}, status=400)
    edit = (
        page_edits.standing_edits(scan, PageEdit.Kind.INSERT_PAGE)
        .filter(pk=data.get("edit_id"))
        .first()
    )
    if edit is None:
        return JsonResponse({"error": "Unknown page insert."}, status=404)
    page_edits.withdraw(
        page_edits.standing_edits(scan, PageEdit.Kind.INSERT_PAGE).filter(
            pk=edit.pk
        ),
        request.user,
    )
    return JsonResponse({"status": "ok"})


@login_required
@require_POST
def replace_page(request: HttpRequest, pk: int) -> JsonResponse:
    """Upload a page to stand in for one that cannot be read.

    The Replace button of review 1 (#232). A blurry page is a kind of
    missing page (#205), and the portal could not record it at all.
    One kind, not a delete beside an insert: those two would need an
    address in two spaces to say "this page stands where that page
    stood", and a curator taking the replacement back would have to
    take back both.

    The upload may be an image or a PDF of one page. **A second upload
    for one page withdraws the row before it and writes a new one**,
    rather than writing over its ``image`` field: the audit must show
    every file a person uploaded, and an overwritten field leaves its
    object in the bucket with no row that names it.

    Nothing applies the row to the volume yet. The pass that does is
    #206, and it runs each replacement through the stages as a
    one-page shard.

    :param request: The HTTP request (form data with ``pdf_page`` and
        ``image``).
    :param pk: Scan primary key.
    :return: JSON response with the edit id and the file URL.
    """
    scan = get_object_or_404(Scan, pk=pk)
    locked = _refuse_locked_edits(scan)
    if locked is not None:
        return locked
    image_file = request.FILES.get("image")
    kind, refusal = _accept_page_upload(image_file, one_page=True)
    if kind is None:
        return JsonResponse({"error": refusal}, status=400)
    pdf_page = _pdf_page_of(scan, request.POST.get("pdf_page"))
    if pdf_page is None:
        return JsonResponse({"error": "Unknown PDF page."}, status=404)

    def withdraw_earlier():
        """Close the replacement this one supersedes, if any."""
        page_edits.withdraw(
            page_edits.standing_edits(scan, PageEdit.Kind.REPLACE_PAGE).filter(
                pdf_page=pdf_page
            ),
            request.user,
        )

    edit = PageEdit(
        scan=scan,
        kind=PageEdit.Kind.REPLACE_PAGE,
        author=request.user,
        pdf_page=pdf_page,
        source_fingerprint=scan.source_fingerprint,
    )
    # Two replacements of one page at the same moment both withdraw
    # nothing and both insert; the second loses to the partial unique
    # key, and its file is taken back with it.
    if not _save_page_file_row(edit, image_file, before=withdraw_earlier):
        return JsonResponse({"error": UPLOAD_LOST_RACE_MESSAGE}, status=409)
    return JsonResponse(
        {
            "status": "ok",
            "edit_id": edit.pk,
            "image_url": edit.image.url,
            "kind": kind,
            "file_url": reverse(
                "page_edit_file", kwargs={"pk": scan.pk, "edit_id": edit.pk}
            ),
        }
    )


@login_required
@require_POST
def undo_replace_page(request: HttpRequest, pk: int) -> JsonResponse:
    """Take back the replacement of a page.

    The row is stamped and the file is kept (#232), like every other
    undo of review 1. The page then stands as it was scanned, and it
    may be replaced again: the unique key is partial over the rows
    that carry neither stamp.

    An undo of a replacement that no longer stands is a no-op, as it
    is in ``undo_delete_page``: a second tab, or a second click, must
    not show an error for a page that is already back.

    :param request: The HTTP request (JSON body with ``pdf_page``).
    :param pk: Scan primary key.
    :return: JSON response confirming the undo.
    """
    scan = get_object_or_404(Scan, pk=pk)
    locked = _refuse_locked_edits(scan)
    if locked is not None:
        return locked
    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON"}, status=400)
    page_edits.withdraw(
        page_edits.standing_edits(scan, PageEdit.Kind.REPLACE_PAGE).filter(
            pdf_page=data.get("pdf_page")
        ),
        request.user,
    )
    return JsonResponse({"status": "ok"})


@login_required
def page_edit_file(
    request: HttpRequest, pk: int, edit_id: int
) -> HttpResponse:
    """Send the reader to the file a curator uploaded for one page.

    A redirect, and not the URL itself in the page (#232): the default
    storage signs its URLs and the signature expires in an hour, while
    a review page stays open for longer. Signing at the moment of the
    click also keeps the link working with the local storage of a
    development environment.

    A withdrawn or applied row is served too, so a link in the audit
    keeps working after the decision is closed.

    :param request: The HTTP request.
    :param pk: Scan primary key.
    :param edit_id: The ``PageEdit`` whose file is wanted.
    :return: A redirect to the file.
    """
    scan = get_object_or_404(Scan, pk=pk)
    edit = get_object_or_404(PageEdit, pk=edit_id, scan=scan)
    if not edit.image:
        raise Http404("This page edit carries no file.")
    return redirect(edit.image.url)


@login_required
@require_POST
def rotate_page(request: HttpRequest, pk: int) -> JsonResponse:
    """Record that a page is printed the wrong way up.

    The answer to ``CheckName.ORIENTATION``, which the portal could
    raise but never resolve. The value is clockwise degrees, and only a
    quarter turn is a legal one.

    The endpoint lands with the model; the button belongs with #151.
    The apply (#224) re-renders a rotated page as a one-page shard.

    :param request: The HTTP request (JSON body with ``pdf_page`` and
        ``degrees``).
    :param pk: Scan primary key.
    :return: JSON response confirming the rotation.
    """
    scan = get_object_or_404(Scan, pk=pk)
    locked = _refuse_locked_edits(scan)
    if locked is not None:
        return locked
    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON"}, status=400)
    pdf_page = _pdf_page_of(scan, data.get("pdf_page"))
    if pdf_page is None:
        return JsonResponse({"error": "Unknown PDF page."}, status=404)
    degrees = str(data.get("degrees", "")).strip()
    if degrees not in PAGE_EDIT_ROTATIONS:
        return JsonResponse(
            {
                "error": (
                    "A rotation must be one of "
                    f"{', '.join(PAGE_EDIT_ROTATIONS)} degrees."
                )
            },
            status=400,
        )
    page_edits.supersede(
        scan,
        PageEdit.Kind.ROTATE_PAGE,
        {"pdf_page": pdf_page},
        {"value": degrees, "source_fingerprint": scan.source_fingerprint},
        request.user,
    )
    return JsonResponse({"status": "ok"})


@login_required
@require_POST
def dismiss_issue(request: HttpRequest, pk: int) -> JsonResponse:
    """Record that a curator judged one issue not worth acting on.

    One ``PageEdit`` row, since #214, and no longer a ``DELETE`` of the
    ``Issue`` row: every rebuild deletes the derived issues and writes
    them again with new primary keys, so a dismissal used to come back
    on the next press of the recompute button. The rebuild now reads
    these rows as an input, the way it already keeps
    ``suppress_detection``.

    The row keeps the address in the space its check uses -- a physical
    PDF page for the checks in ``PHYSICAL_PAGE_CHECKS``, the printed
    number for the rest -- because those two are different spaces and a
    printed number is not unique.

    The issue row itself is deleted too, so the card goes away at once
    without a page reload. The pending-changes guard is gone with the
    convention it protected: a dismissal is durable now, so it needs no
    apply and cannot be lost by one.

    :param request: The HTTP request (JSON body with issue_id).
    :param pk: Scan primary key.
    :return: JSON response confirming dismissal.
    """
    scan = get_object_or_404(Scan, pk=pk)
    # Not locked with the page edits (#224): a dismissal is built into
    # nothing, and the recompute button stays reachable after the
    # approval, so a curator must be able to answer the cards it raises.
    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON"}, status=400)
    issue = Issue.objects.filter(pk=data.get("issue_id"), scan=scan).first()
    if issue is None:
        return JsonResponse({"error": "Unknown issue."}, status=404)
    if issue.check_name in REVIEW2_CHECKS:
        # A review-2 finding has a dismissal of its own (#240 PR D): a
        # ``ReviewDismissal`` keyed by the target's address, not a page
        # edit keyed by a printed number.
        return JsonResponse(
            {"error": REVIEW2_FINDING_NOT_HERE_MESSAGE}, status=409
        )

    physical = issue.check_name in PHYSICAL_PAGE_CHECKS
    page_edits.supersede(
        scan,
        PageEdit.Kind.DISMISS_ISSUE,
        {
            "pdf_page": issue.page_number if physical else None,
            "logical_page": (
                ""
                if physical or issue.page_number is None
                else str(issue.page_number)
            ),
            "value": issue.check_name,
        },
        {"source_fingerprint": scan.source_fingerprint},
        request.user,
    )
    issue.delete()
    return JsonResponse({"status": "ok"})


def _printed_number_of(scan: Scan, pdf_page: int) -> str:
    """Return the printed number the page shows, as a label.

    Read off the cached ``ocr_results``, which carries the curator's
    own numbers too (#214). A label only: the scanner reads it on the
    Repairs page to find the leaf in the book. It goes through
    ``_page_label`` like every other label, and a reading the narrowing
    refuses is dropped: the narrowing is the first of the two layers,
    and the blob is not a trusted source.

    :param scan: The scan.
    :param pdf_page: The 1-based page.
    :returns: The printed number, or the empty string.
    :rtype: str
    """
    for entry in scan.ocr_results or []:
        if entry.get("pdf_page") == pdf_page:
            return _page_label(str(entry.get("detected") or "")) or ""
    return ""


@login_required
@require_POST
def request_page_repair(request: HttpRequest, pk: int) -> JsonResponse:
    """Record that a page needs a scanner, and what the scanner must do.

    The button of a reviewer who has no book (#249). A REPLACE names
    the page to scan again; an INSERT names the gap a missing leaf
    goes in, by the page it follows, the address an insert uses
    (#214). One open row per address: a second request for the same
    page answers the first row, with ``created`` false, so two
    reviewers who find one page do not stack two requests.

    **A fulfilled row is still an open row**, and the key matches it
    too. The derivation refuses an edit older than the request, but
    SQL cannot index a derived flag, so the key cannot. So when the
    matched row is fulfilled the answer says so
    (``already_fulfilled``, ``REPAIR_ALREADY_FULFILLED_MESSAGE``): the
    reviewer dismisses the answered request and asks again, or uses
    Replace. A toast that said "already requested" here would lose the
    ask, which is the silence this feature exists to remove.

    The note is free text a person typed. It is cut at
    ``repairs.NOTE_MAX_CHARS`` here and escaped where it is drawn: in
    the viewer through ``escapeHtml``, in the templates by the
    auto-escape. Both layers, on purpose.

    :param request: The HTTP request (JSON body with ``action``,
        ``pdf_page`` or ``anchor_pdf_page``, ``logical_page``,
        ``note``).
    :param pk: Scan primary key.
    :return: JSON response with the request, and whether it is new.
    """
    scan = get_object_or_404(Scan, pk=pk)
    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    action = data.get("action")
    if action not in PageRepairRequest.Action.values:
        return JsonResponse({"error": "Unknown action."}, status=400)
    note = str(data.get("note") or "").strip()[: repairs.NOTE_MAX_CHARS]

    address = {}
    if action == PageRepairRequest.Action.REPLACE:
        # The label is a hint for the scanner, not the reviewer's
        # typing, so the server reads it off ``ocr_results`` itself and
        # drops a reading the narrowing refuses. A label sent by the
        # viewer is ignored: refusing it would make the button fail on
        # exactly the page whose reading is junk.
        pdf_page = _pdf_page_of(scan, data.get("pdf_page"))
        if pdf_page is None:
            return JsonResponse({"error": "Unknown PDF page."}, status=404)
        address["pdf_page"] = pdf_page
        label = _printed_number_of(scan, pdf_page)
    else:
        # An older viewer places the gap by the label alone
        # (``_anchor_of``), so here the label is an address and a
        # refused one is an error.
        label = _page_label(str(data.get("logical_page") or ""))
        if label is None:
            return JsonResponse({"error": "Invalid page number."}, status=400)
        anchor = _anchor_of(scan, data.get("anchor_pdf_page"), label)
        if anchor is None:
            return JsonResponse({"error": "Unknown gap."}, status=404)
        address["anchor_pdf_page"] = anchor

    row, created = PageRepairRequest.objects.get_or_create(
        scan=scan,
        action=action,
        dismissed_at=None,
        **address,
        defaults={
            "requested_by": request.user,
            "logical_page": label,
            "note": note,
            "source_fingerprint": scan.source_fingerprint,
        },
    )
    row = repairs.open_requests(scan).get(pk=row.pk)
    answer = {
        "status": "ok",
        "created": created,
        "already_fulfilled": bool(not created and row.fulfilled),
        "request": repairs.as_dict(row, scan),
    }
    if answer["already_fulfilled"]:
        answer["message"] = REPAIR_ALREADY_FULFILLED_MESSAGE
    return JsonResponse(answer)


@login_required
@require_POST
def dismiss_page_repair(request: HttpRequest, pk: int) -> JsonResponse:
    """Close a repair request without deleting it.

    Any logged-in user may dismiss, the rule of every review-1 button.
    The row is stamped with who and when. A second dismissal of the
    same row is a no-op, not an error, like ``undo_delete_page``: a
    second tab must not fail on a request that is already closed.

    :param request: The HTTP request (JSON body with ``request_id``).
    :param pk: Scan primary key.
    :return: JSON response confirming the dismissal.
    """
    scan = get_object_or_404(Scan, pk=pk)
    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON"}, status=400)
    try:
        request_id = int(data.get("request_id"))
    except (TypeError, ValueError):
        return JsonResponse({"error": "Unknown request."}, status=404)
    rows = scan.repair_requests.filter(pk=request_id)
    if not rows.exists():
        return JsonResponse({"error": "Unknown request."}, status=404)
    repairs.dismiss(rows, request.user)
    return JsonResponse({"status": "ok"})
