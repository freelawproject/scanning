"""Apply the page edits: build the final volume, and glue the paid
results into its page space (issue #224).

Review 1 ends when a curator approves the page completeness
(``PAGE_COMPLETENESS_REVIEW_DONE``, #151). The ``PageEdit`` rows (#214)
plus the original then describe the complete volume, and this module
builds it. **The apply assembles; it does not recompute.** A page nobody
touched keeps its paid conversion, its paid OCR read and its paid
detections. Only a page a curator added or changed enters a queue, as a
one-page shard.

The pieces, and the rules that hold them together:

- **The plan** (:func:`plan_run`) reads the current structural rows and
  computes the offset map: one entry per final page naming its source,
  an original page or a page of an edit's file. The map is stored on
  the ``ApplyRun`` row once, at build time, and every glue reads it.
  Nothing derives it again.
- **The walk** (:func:`build_final_pdf`) turns the plan into the final
  PDF. ``views_api.export_pdf`` runs the same walk, so the export shows
  what the apply builds.
- **The final PDF is a derived artifact, not a new source.** The
  original stays the source of record and ``Scan.source_fingerprint``
  stays the original's. Every edit keeps its address in the original's
  page space; the map is the bridge.
- **The page shards are keyed by edit**, not by run
  (``jobs/apply/pages/e{pk}.pdf``). A row is immutable, so the next run
  finds the same key and the same identity, and ``jobs._reusable_results``
  carries the paid result for free. The apply's glues keep their
  results for the same reason, where the volume bitonal merge deletes
  its own.
- **Every output goes under ``jobs/apply/a{n}/``**, which the generic
  sync never carries and the admin deletion sweeps. The review-1
  artifacts -- the first ``bitonal.pdf``, the ``r{run}-volume.json``,
  the stored page map -- are never written over, so the page review
  still renders if the review is reopened.
- **A run is a row of its own** (``models.ApplyRun``), because a run
  may have no job rows at all, spans three stages, and is asked about
  on every collect tick.
"""

from __future__ import annotations

import logging
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

import fitz
from django.db import transaction
from django.utils import timezone

from scanning import page_edits, s3_sync
from scanning.models import (
    DEAD_JOB_STATUSES,
    IN_FLIGHT_JOB_STATUSES,
    ApplyRun,
    ExternalJob,
    JobEngine,
    JobProvider,
    JobStage,
    JobStatus,
    PageEdit,
    QueuedAction,
    Scan,
    Status,
)
from scanning.utils import local_original_pdf

logger = logging.getLogger(__name__)

#: The shape of ``ApplyRun.page_map`` and of ``page_map.json``.
MAP_SCHEMA_VERSION = 1

#: Where the apply's scratch files go in the system temp dir, so the
#: ``cleanup_processing_tmp`` sweep reclaims what a SIGKILL orphans.
BUILD_TMP_PREFIX = "pageapply-"

#: How many failed attempts a phase gets before the trigger stops
#: queueing the run. The same loud-then-quiet ledger the volume glues
#: use; the way back is the admin action that supersedes the run.
APPLY_MAX_ATTEMPTS = 3

#: The kinds that need a one-page shard of their own: an image or a PDF
#: the curator sent, or a page of the original turned the right way up.
#: A deletion costs no job -- the glue drops the page's slice.
SHARD_KINDS = (
    PageEdit.Kind.INSERT_PAGE,
    PageEdit.Kind.REPLACE_PAGE,
    PageEdit.Kind.ROTATE_PAGE,
)


class ApplyError(Exception):
    """The apply could not build or glue a run."""


# ── keys ───────────────────────────────────────────────────────────
def apply_prefix(scan: Scan) -> str:
    """Return the S3 prefix that holds every apply artifact of a scan.

    Under ``jobs/`` so the generic sync never carries it and the admin
    deletion sweeps it with the job results.

    :param scan: The scan.
    :returns: ``{processing prefix}jobs/apply/``.
    :rtype: str
    """
    return f"{s3_sync.s3_processing_prefix(scan)}{s3_sync.JOB_RESULTS_SUBDIR}apply/"


def run_prefix(scan: Scan, run: ApplyRun) -> str:
    """Return the S3 prefix of one run's outputs.

    :param scan: The scan.
    :param run: The apply run.
    :returns: ``{apply prefix}a{n}/``.
    :rtype: str
    """
    return f"{apply_prefix(scan)}{run.label}/"


def page_shard_key(scan: Scan, edit: PageEdit) -> str:
    """Return the S3 key of the one-page shard built from one edit.

    Named by the edit and not by the run, on purpose: a row is
    immutable, so a later run finds the same shard under the same key
    with the same identity, and the carry of prior results
    (``jobs._reusable_results``) hands it the paid read at no cost.

    :param scan: The scan.
    :param edit: An insert, a replacement or a rotation row.
    :returns: ``{apply prefix}pages/e{pk}.pdf``.
    :rtype: str
    """
    return f"{apply_prefix(scan)}pages/e{edit.pk}.pdf"


# ── the plan ───────────────────────────────────────────────────────
@dataclass
class ApplyPlan:
    """What one apply run builds: the final page order and its sources.

    :ivar source_page_count: Pages in the original.
    :ivar deleted_pages: The 1-based original pages the final PDF drops.
    :ivar edits: The current structural rows the plan read, in primary
        key order. These are the rows a build stamps.
    :ivar pages: One entry per final page, in order. Each is
        ``{"final_page": n, "source": {...}}`` where the source is one
        of two shapes: ``{"kind": "original", "pdf_page": p}`` for a
        page kept as scanned, or ``{"kind": "edit", "edit_id": id,
        "edit_kind": kind, "page": k, ...}`` for page ``k`` of the
        shard built from one edit -- an uploaded image or PDF, or the
        original page ``pdf_page`` turned by ``rotation`` degrees. An
        image entry also names the ``reference_pdf_page`` whose
        MediaBox it takes.
    """

    source_page_count: int
    deleted_pages: list[int] = field(default_factory=list)
    edits: list[PageEdit] = field(default_factory=list)
    pages: list[dict] = field(default_factory=list)

    @property
    def final_page_count(self) -> int:
        """Return how many pages the final PDF has."""
        return len(self.pages)

    @property
    def is_identity(self) -> bool:
        """Return whether the final PDF is the original, page for page.

        True when no structural edit stands. Such a run copies nothing:
        it aliases the original and the review-1 artifacts.
        """
        return not self.edits

    @property
    def shard_edits(self) -> list[PageEdit]:
        """Return the rows that need a one-page shard and a job row."""
        return [edit for edit in self.edits if edit.kind in SHARD_KINDS]

    def edit_by_id(self, edit_id: int) -> PageEdit:
        """Return one of the plan's rows by primary key.

        :param edit_id: The row's pk, as a map entry names it.
        :returns: The row.
        :raises KeyError: If the plan holds no such row.
        """
        for edit in self.edits:
            if edit.pk == edit_id:
                return edit
        raise KeyError(edit_id)

    def to_map(self) -> dict:
        """Return the offset map as the run stores it.

        :returns: ``schema_version``, ``source_page_count``,
            ``final_page_count``, ``deleted_pages`` and ``pages``.
        :rtype: dict
        """
        return {
            "schema_version": MAP_SCHEMA_VERSION,
            "source_page_count": self.source_page_count,
            "final_page_count": self.final_page_count,
            "deleted_pages": list(self.deleted_pages),
            "pages": list(self.pages),
        }


def final_page_of(page_map: dict, pdf_page: int) -> int | None:
    """Return where one original page landed in the final PDF.

    The inverse lookup over a stored map, for the readers that carry a
    row addressed in the original's space -- a ``SET_NUMBER`` row, a
    detection -- into the final space.

    :param page_map: A stored offset map (:meth:`ApplyPlan.to_map`).
    :param pdf_page: A 1-based page of the original.
    :returns: The 1-based final page, or None when the page was
        deleted or replaced.
    :rtype: int | None
    """
    for entry in page_map.get("pages", []):
        source = entry["source"]
        if source["kind"] == "original" and source["pdf_page"] == pdf_page:
            return entry["final_page"]
    return None


def reference_page(edit: PageEdit, page_count: int) -> int:
    """Return the original page whose MediaBox an uploaded image takes.

    The page it replaces, or the page it follows (page 1 for anchor 0,
    the last page for an anchor past the end). One rule for the plan
    and the shard, so the two agree.

    :param edit: An insert or a replacement row.
    :param page_count: Pages in the original.
    :returns: A 1-based page of the original.
    :rtype: int
    """
    page = edit.pdf_page if edit.pdf_page else (edit.anchor_pdf_page or 0)
    return min(max(page, 1), page_count)


def final_slot_of(page_map: dict, pdf_page: int) -> int | None:
    """Return the final page that stands where one original page stood.

    Unlike :func:`final_page_of`, a replaced or a rotated page answers
    too: its slot is taken by the shard built for it. The readers that
    carry a curator's decision about a *position* -- a typed page
    number -- use this one; the readers that carry a paid result about
    the page's *content* use :func:`final_page_of`, since a replaced
    page's content is new.

    :param page_map: A stored offset map (:meth:`ApplyPlan.to_map`).
    :param pdf_page: A 1-based page of the original.
    :returns: The 1-based final page, or None when the page was
        deleted.
    :rtype: int | None
    """
    for entry in page_map.get("pages", []):
        if entry["source"].get("pdf_page") == pdf_page:
            return entry["final_page"]
    return None


def edit_page_count(edit: PageEdit) -> int:
    """Return how many pages one edit contributes to the final PDF.

    An image is one page. A PDF holds what it holds: a missing leaf is
    often two pages, and the insert endpoint takes them all (#232). A
    rotation turns one page of the original.

    :param edit: A structural row.
    :returns: The page count. A deletion contributes none.
    :rtype: int
    """
    if edit.kind == PageEdit.Kind.DELETE_PAGE:
        return 0
    if edit.kind == PageEdit.Kind.ROTATE_PAGE:
        return 1
    if page_edits.uploaded_kind(edit) != "pdf":
        return 1
    with edit.image.open("rb") as fh:
        data = fh.read()
    with fitz.open(stream=data, filetype="pdf") as doc:
        return doc.page_count


def plan_run(
    scan: Scan, page_counts: dict[int, int] | None = None
) -> ApplyPlan:
    """Compute the final page order from the current structural rows.

    Pure over its inputs: the rows, the original's page count and the
    page count of each uploaded file. The walk is the one
    ``views_api.export_pdf`` proved for the deletes and the inserts,
    plus the two kinds the export used to skip:

    - a deleted page is dropped;
    - a replaced page gives its slot to the pages of the uploaded file;
    - a rotated page keeps its slot, as a shard of its own;
    - the images of a gap follow the page the gap names, in ``ordinal``
      order, and anchor 0 goes before page 1.

    A page both deleted and replaced is deleted: the deletion is the
    stronger decision, and a curator who wants the replacement takes
    the deletion back.

    :param scan: The scan whose rows to read.
    :param page_counts: ``{edit pk: pages}`` for the uploaded files,
        when the caller has them (the build has just cut the shards).
        A missing entry is read off the file.
    :returns: The plan.
    :rtype: ApplyPlan
    :raises ApplyError: If the scan has no page count yet.
    """
    if not scan.page_count:
        raise ApplyError(f"scan {scan.pk} has no page count to plan over")
    page_counts = dict(page_counts or {})

    deleted = page_edits.deleted_pages(scan)
    replacements = page_edits.replacements_by_page(scan)
    rotations = page_edits.rotations_by_page(scan)
    gaps = page_edits.inserts_by_gap(scan)
    edits = sorted(
        page_edits.current_edits(scan, *PageEdit.STRUCTURAL_KINDS),
        key=lambda edit: edit.pk,
    )

    def count_of(edit: PageEdit) -> int:
        if edit.pk not in page_counts:
            page_counts[edit.pk] = edit_page_count(edit)
        return page_counts[edit.pk]

    pages: list[dict] = []

    def emit(source: dict) -> None:
        pages.append({"final_page": len(pages) + 1, "source": source})

    def emit_upload(edit: PageEdit) -> None:
        for k in range(count_of(edit)):
            source = {
                "kind": "edit",
                "edit_id": edit.pk,
                "edit_kind": str(edit.kind),
                "page": k,
                "reference_pdf_page": reference_page(edit, last),
            }
            if edit.kind == PageEdit.Kind.REPLACE_PAGE:
                source["pdf_page"] = edit.pdf_page
            emit(source)

    last = scan.page_count
    for edit in gaps.get(0, []):
        emit_upload(edit)
    for pdf_page in range(1, last + 1):
        if pdf_page in deleted:
            pass
        elif pdf_page in replacements:
            emit_upload(replacements[pdf_page])
        elif pdf_page in rotations:
            edit = next(
                e
                for e in edits
                if e.kind == PageEdit.Kind.ROTATE_PAGE
                and e.pdf_page == pdf_page
            )
            emit(
                {
                    "kind": "edit",
                    "edit_id": edit.pk,
                    "edit_kind": str(edit.kind),
                    "page": 0,
                    "pdf_page": pdf_page,
                    "rotation": rotations[pdf_page],
                }
            )
        else:
            emit({"kind": "original", "pdf_page": pdf_page})
        for edit in gaps.get(pdf_page, []):
            emit_upload(edit)
    # An insert anchored past the last page has no gap to fill. It is
    # placed last rather than dropped: a curator uploaded it, and the
    # viewer shows it there too (``page_edits.project_inserts``).
    for anchor in sorted(anchor for anchor in gaps if anchor > last):
        for edit in gaps[anchor]:
            emit_upload(edit)

    return ApplyPlan(
        source_page_count=scan.page_count,
        deleted_pages=sorted(deleted),
        edits=edits,
        pages=pages,
    )


# ── the walk ───────────────────────────────────────────────────────
def read_edit_file(edit: PageEdit) -> bytes:
    """Return the bytes of the file a curator uploaded for one edit.

    The file lives on the default storage since #214, so it is read as
    bytes: a remote file has no path for fitz to open.

    :param edit: An insert or a replacement row.
    :returns: The file's bytes.
    :rtype: bytes
    """
    with edit.image.open("rb") as fh:
        return fh.read()


def _turned_copy(
    out: fitz.Document, source: fitz.Document, pdf_page: int, degrees: int
) -> None:
    """Append one page of the original to ``out``, turned by ``degrees``.

    The turn is added to the page's own ``/Rotate``: the curator judged
    the page as it displays, and that display already includes the
    rotation the scanner wrote.

    :param out: The document being built.
    :param source: The original.
    :param pdf_page: The 1-based page to copy.
    :param degrees: Clockwise quarter turns, in degrees.
    :return: None.
    """
    out.insert_pdf(source, from_page=pdf_page - 1, to_page=pdf_page - 1)
    page = out[-1]
    page.set_rotation((page.rotation + degrees) % 360)


def _place_image(
    out: fitz.Document, source: fitz.Document, reference: int, data: bytes
) -> None:
    """Append an uploaded image as a page the size of a reference page.

    The reference is the page the image replaces or follows, so the
    render at 200 dpi describes the same pixel space as the rest of
    the volume. The rule ``export_pdf`` always applied.

    :param out: The document being built.
    :param source: The original, for the reference MediaBox.
    :param reference: The 1-based original page to take the size from.
    :param data: The image bytes.
    :return: None.
    """
    reference = min(max(reference, 1), source.page_count)
    rect = source[reference - 1].rect
    page = out.new_page(width=rect.width, height=rect.height)
    page.insert_image(page.rect, stream=data)


def build_edit_shard(
    source: fitz.Document, edit: PageEdit, data: bytes | None
) -> fitz.Document:
    """Build the one-page shard the stages read for one edit.

    What the external workers get for a page a curator added or
    changed: a PDF, like every other shard, because doctor and dots.mocr
    read PDFs. An uploaded PDF is copied as it is. An uploaded image
    goes on a page the size of its reference page. A rotation is the
    original page with its ``/Rotate`` turned; doctor and dots.mocr
    render through fitz, which honours it, so the page is re-read the
    right way up. The same three rules the final PDF walk applies, so
    the shard's page ``k`` is the final PDF's page for map entry ``k``.

    :param source: The original.
    :param edit: The row.
    :param data: The uploaded file's bytes, or None for a rotation.
    :returns: The shard document. The caller closes it.
    :rtype: fitz.Document
    """
    out = fitz.open()
    if edit.kind == PageEdit.Kind.ROTATE_PAGE:
        _turned_copy(out, source, edit.pdf_page, int(edit.value))
        return out
    if page_edits.uploaded_kind(edit) == "pdf":
        with fitz.open(stream=data, filetype="pdf") as uploaded:
            out.insert_pdf(uploaded)
        return out
    if data is None:
        raise ApplyError(f"edit {edit.pk} has no file to build a shard from")
    _place_image(out, source, reference_page(edit, source.page_count), data)
    return out


def build_final_pdf(
    source: fitz.Document, plan: ApplyPlan, read_file=read_edit_file
) -> fitz.Document:
    """Build the final PDF from the original and the plan.

    One pass over the map. Runs of untouched pages are copied in one
    ``insert_pdf`` each, since a volume of 1300 pages with two edits is
    three ranges, not 1300 calls. An edit's pages come from the same
    rules :func:`build_edit_shard` applies, so the final PDF and the
    shards agree page for page.

    :param source: The original, open.
    :param plan: The plan.
    :param read_file: Returns the bytes of an edit's uploaded file.
        Injected so the tests need no storage.
    :returns: The final document. The caller saves and closes it.
    :rtype: fitz.Document
    """
    out = fitz.open()
    shards: dict[int, fitz.Document] = {}
    try:
        entries = plan.pages
        i = 0
        while i < len(entries):
            src = entries[i]["source"]
            if src["kind"] == "original":
                first = src["pdf_page"]
                last = first
                while (
                    i + 1 < len(entries)
                    and entries[i + 1]["source"]["kind"] == "original"
                    and entries[i + 1]["source"]["pdf_page"] == last + 1
                ):
                    i += 1
                    last += 1
                out.insert_pdf(source, from_page=first - 1, to_page=last - 1)
                i += 1
                continue
            edit = plan.edit_by_id(src["edit_id"])
            if edit.pk not in shards:
                data = (
                    None
                    if edit.kind == PageEdit.Kind.ROTATE_PAGE
                    else read_file(edit)
                )
                shards[edit.pk] = build_edit_shard(source, edit, data)
            shard = shards[edit.pk]
            if src["page"] >= shard.page_count:
                raise ApplyError(
                    f"edit {edit.pk} has {shard.page_count} page(s); the "
                    f"map asks for page {src['page']}"
                )
            out.insert_pdf(shard, from_page=src["page"], to_page=src["page"])
            i += 1
    finally:
        for shard in shards.values():
            shard.close()
    if out.page_count != plan.final_page_count:
        raise ApplyError(
            f"the final PDF has {out.page_count} page(s), the plan "
            f"{plan.final_page_count}"
        )
    return out


def write_final_pdf(
    scan: Scan, plan: ApplyPlan, source_path: Path, dest: Path
) -> Path:
    """Build the final PDF from the original on disk and save it.

    :param scan: The scan, for the log line.
    :param plan: The plan.
    :param source_path: The original PDF.
    :param dest: Where to save the final PDF.
    :returns: ``dest``.
    :rtype: Path
    """
    with fitz.open(str(source_path)) as source:
        with build_final_pdf(source, plan) as out:
            out.save(str(dest), garbage=3, deflate=True)
    logger.info(
        "apply: scan %s: built the final PDF, %d page(s) from %d (%d "
        "deleted, %d edit(s))",
        scan.pk,
        plan.final_page_count,
        plan.source_page_count,
        len(plan.deleted_pages),
        len(plan.edits),
    )
    return dest


# ── the runs ───────────────────────────────────────────────────────
def latest_run(scan: Scan) -> ApplyRun | None:
    """Return the scan's newest apply run, superseded or not.

    :param scan: The scan.
    :returns: The run with the highest number, or None.
    :rtype: ApplyRun | None
    """
    return scan.apply_runs.order_by("-number").first()


def current_run(scan: Scan) -> ApplyRun | None:
    """Return the scan's newest apply run that still stands.

    :param scan: The scan.
    :returns: The newest run without ``superseded_at``, or None.
    :rtype: ApplyRun | None
    """
    return (
        scan.apply_runs.filter(superseded_at__isnull=True)
        .order_by("-number")
        .first()
    )


def supersede_runs(scan: Scan, reason: str) -> int:
    """Close every standing apply run of a scan, and cancel its jobs.

    The reopen of a page review and the admin's way out of a run with
    a dead row. The run's outputs stay in S3, and so do its paid
    results: only the rows still waiting or in flight are cancelled,
    scoped to the run so the volume runs stay alive. A ``COMPLETED``
    row is left as it is, because the next build starts ``a{n+1}`` and
    carries every paid result the edits did not change -- which is
    exactly the row a cancel would have written off.

    :param scan: The scan.
    :param reason: Recorded on each cancelled job row.
    :returns: How many runs were superseded.
    :rtype: int
    """
    from scanning import jobs

    now = timezone.now()
    count = 0
    unstarted = frozenset({JobStatus.PENDING}) | IN_FLIGHT_JOB_STATUSES
    for run in scan.apply_runs.filter(superseded_at__isnull=True):
        jobs.abandon_open(scan, reason, statuses=unstarted, apply_run=run)
        ApplyRun.objects.filter(pk=run.pk, superseded_at__isnull=True).update(
            superseded_at=now
        )
        logger.info(
            "apply: scan %s: run %s superseded: %s", scan.pk, run.label, reason
        )
        count += 1
    return count


# ── the build phase ────────────────────────────────────────────────
def _ensure_page_shard(
    scan: Scan, source: fitz.Document, edit: PageEdit, tmp_dir: Path
) -> dict:
    """Make sure one edit's shard is in the bucket, and describe it.

    Built once per edit: a shard already at its key is described, not
    rebuilt, so a second run costs no upload and its rows keep the
    identity the carry matches on.

    :param scan: The scan.
    :param source: The original, open.
    :param edit: An insert, a replacement or a rotation row.
    :param tmp_dir: Scratch space for the shard file.
    :returns: ``{"key", "page_count", "size_bytes"}``.
    :rtype: dict
    :raises ApplyError: If the upload failed.
    """
    key = page_shard_key(scan, edit)
    if s3_sync.object_exists(key):
        return {
            "key": key,
            "page_count": edit_page_count(edit),
            "size_bytes": s3_sync.object_size(key) or 0,
        }
    data = (
        None
        if edit.kind == PageEdit.Kind.ROTATE_PAGE
        else read_edit_file(edit)
    )
    local = tmp_dir / f"e{edit.pk}.pdf"
    with build_edit_shard(source, edit, data) as shard:
        page_count = shard.page_count
        shard.save(str(local), garbage=3, deflate=True)
    if not s3_sync.upload_file_object(key, local, "application/pdf"):
        raise ApplyError(f"could not upload the shard of edit {edit.pk}")
    # The size the bucket reports, on the first build as on every later
    # one: the row identity carries it, and a value read two ways would
    # read as two shards and re-pay the read.
    return {
        "key": key,
        "page_count": page_count,
        "size_bytes": s3_sync.object_size(key) or local.stat().st_size,
    }


def shard_manifest(
    scan: Scan, plan: ApplyPlan, shards: dict[int, dict]
) -> dict:
    """Describe the run's one-page shards the way ``ensure_shard_jobs`` reads.

    The same shape as the volume manifest, with two additions per entry
    that :func:`scanning.jobs._shard_specs` honours: the shard's own
    ``key`` (under ``jobs/apply/pages/``, not ``shards/``) and the
    ``edit_id`` it was built from. Each entry's ``source_page_count`` is
    its own page count, so the identity of one edit's row never changes
    when another edit joins the run -- that is what lets the carry
    match it.

    :param scan: The scan.
    :param plan: The plan, for the shard order.
    :param shards: :func:`_ensure_page_shard`'s answer per edit pk.
    :returns: The manifest.
    :rtype: dict
    """
    entries = []
    for index, edit in enumerate(plan.shard_edits):
        info = shards[edit.pk]
        entries.append(
            {
                "name": f"e{edit.pk}.pdf",
                "index": index,
                "key": info["key"],
                "edit_id": edit.pk,
                "from_page": 0,
                "to_page": info["page_count"] - 1,
                "page_count": info["page_count"],
                "size_bytes": info["size_bytes"],
                "source_page_count": info["page_count"],
            }
        )
    return {
        "version": 1,
        "source": {
            "name": "page edits",
            "size_bytes": sum(e["size_bytes"] for e in entries),
            "page_count": sum(e["page_count"] for e in entries),
        },
        "shards": entries,
    }


def _ensure_rows(
    scan: Scan, run: ApplyRun, plan: ApplyPlan, shards: dict[int, dict]
) -> list[ExternalJob]:
    """Create the run's job rows, one per stage per shard, behind the gates.

    The gates mirror the pipeline's (``services._can_convert``,
    ``services._can_analyze``, and ``yolo.enabled`` with S3 active), so
    the new rows ride the same daemon, the same backpressure (#218) and
    the same concurrency caps. Every stage carries prior results
    (``reuse_results``), because the apply keeps its results where the
    volume bitonal merge deletes its own.

    :param scan: The scan.
    :param run: The apply run the rows work for.
    :param plan: The plan.
    :param shards: :func:`_ensure_page_shard`'s answer per edit pk.
    :returns: The rows created or found.
    :rtype: list[ExternalJob]
    """
    from scanning import dots_mocr, jobs, services, yolo

    if not plan.shard_edits:
        return []
    manifest = shard_manifest(scan, plan, shards)
    rows: list[ExternalJob] = []
    if services._can_convert(scan.pk, manifest):
        rows += jobs.ensure_shard_jobs(
            scan,
            manifest,
            stage=JobStage.CONVERT,
            engine=JobEngine.BITONAL,
            provider=JobProvider.DOCTOR,
            reuse_results=True,
            apply_run=run,
        )
    if services._can_analyze(scan.pk, manifest):
        rows += dots_mocr.ensure_analyze_jobs(scan, manifest, apply_run=run)
    if yolo.enabled() and s3_sync.s3_active():
        rows += yolo.ensure_detect_jobs(scan, manifest, apply_run=run)
    return rows


def _build(scan: Scan, run: ApplyRun) -> None:
    """Phase 1: the shards, the final PDF, the map, the rows, the stamps.

    Idempotent for a re-queue after a SIGTERM: a shard already in the
    bucket is described rather than rebuilt, the final PDF is written
    over the same key, and ``ensure_shard_jobs`` hands back the live
    rows when they still describe the specs.

    :param scan: The scan.
    :param run: The run to build, not yet built.
    :return: None.
    :raises ApplyError: If an input is missing or an upload failed.
    """
    started = time.monotonic()
    original = local_original_pdf(scan)
    if not original:
        raise ApplyError(f"the original of scan {scan.pk} is not available")

    prefix = run_prefix(scan, run)
    with tempfile.TemporaryDirectory(
        prefix=f"{BUILD_TMP_PREFIX}{scan.pk}-"
    ) as tmp:
        tmp_dir = Path(tmp)
        with fitz.open(original) as source:
            if scan.page_count != source.page_count:
                raise ApplyError(
                    f"scan {scan.pk} has {scan.page_count} page(s) on the "
                    f"row and {source.page_count} in the original"
                )
            shards = {
                edit.pk: _ensure_page_shard(scan, source, edit, tmp_dir)
                for edit in page_edits.current_edits(scan, *SHARD_KINDS)
            }
            plan = plan_run(
                scan,
                page_counts={
                    pk: info["page_count"] for pk, info in shards.items()
                },
            )
            if plan.is_identity:
                # No copy of gigabytes for a volume nobody changed: the
                # final PDF is the original.
                final_key = s3_sync.s3_original_key(scan) or ""
            else:
                final_path = tmp_dir / "final.pdf"
                with build_final_pdf(source, plan) as out:
                    out.save(str(final_path), garbage=3, deflate=True)
                final_key = f"{prefix}final.pdf"
                if not s3_sync.upload_file_object(
                    final_key, final_path, "application/pdf"
                ):
                    raise ApplyError("could not upload the final PDF")
    page_map = plan.to_map()
    if not s3_sync.upload_json_object(f"{prefix}page_map.json", page_map):
        raise ApplyError("could not upload the page map")

    rows = _ensure_rows(scan, run, plan, shards)

    now = timezone.now()
    edit_ids = [edit.pk for edit in plan.edits]
    numbers = [
        edit.pk
        for edit in page_edits.current_edits(scan, PageEdit.Kind.SET_NUMBER)
    ]
    with transaction.atomic():
        PageEdit.objects.filter(
            pk__in=edit_ids + numbers, withdrawn_at__isnull=True
        ).update(applied_at=now, applied_run=run, date_modified=now)
        ApplyRun.objects.filter(pk=run.pk).update(
            edit_ids=edit_ids,
            page_map=page_map,
            final_pdf_key=final_key,
            built_at=now,
            attempts=0,
            last_error="",
        )
    run.refresh_from_db()
    logger.info(
        "apply: scan %s: run %s built in %.1fs: %d final page(s) from %d, "
        "%d edit(s), %d shard(s), %d job row(s)",
        scan.pk,
        run.label,
        time.monotonic() - started,
        plan.final_page_count,
        plan.source_page_count,
        len(plan.edits),
        len(shards),
        len(rows),
    )


def record_failure(run: ApplyRun, exc: Exception) -> bool:
    """Count one failed attempt at the run's current phase.

    The same loud-then-quiet ledger as ``yolo.record_apply_failure``:
    the crossing into "out of tries" is the one ERROR-level event, and
    the way back is the admin action that supersedes the run.

    :param run: The run.
    :param exc: What the phase raised.
    :returns: Whether this failure spent the last attempt.
    :rtype: bool
    """
    attempts = run.attempts + 1
    ApplyRun.objects.filter(pk=run.pk).update(
        attempts=attempts,
        last_error=str(exc)[:2000],
        last_attempt_at=timezone.now(),
    )
    run.attempts = attempts
    gave_up = attempts >= APPLY_MAX_ATTEMPTS
    if gave_up:
        logger.exception(
            "apply: scan %s: run %s failed; giving up after %d attempt(s). "
            "Supersede the run from the admin to try again.",
            run.scan_id,
            run.label,
            attempts,
        )
    else:
        logger.warning(
            "apply: scan %s: run %s failed (attempt %d of %d): %s",
            run.scan_id,
            run.label,
            attempts,
            APPLY_MAX_ATTEMPTS,
            exc,
        )
    return gave_up


def build_run(scan: Scan) -> ApplyRun:
    """Run phase 1 for a scan: build the run that is due.

    A run not yet built is retried in place. A built run whose edit set
    no longer matches the standing rows is superseded, and the next
    number is built: that is the reopen path, and the carry keeps the
    paid results of the edits that did not change.

    :param scan: The scan, in the daemon's claim.
    :returns: The built run.
    :rtype: ApplyRun
    :raises ApplyError: If the build failed. The failure is counted on
        the run before it is raised.
    """
    edit_ids = [
        edit.pk
        for edit in sorted(
            page_edits.current_edits(scan, *PageEdit.STRUCTURAL_KINDS),
            key=lambda edit: edit.pk,
        )
    ]
    run = current_run(scan)
    if run is not None and run.is_built:
        if run.edit_ids == edit_ids:
            return run
        supersede_runs(scan, "the page edits changed after the build")
        run = None
    if run is None:
        latest = latest_run(scan)
        run = ApplyRun.objects.create(
            scan=scan,
            number=latest.number + 1 if latest else 1,
            source_fingerprint=scan.source_fingerprint,
        )
    try:
        _build(scan, run)
    except Exception as exc:
        gave_up = record_failure(run, exc)
        raise ApplyError(
            f"Building the corrected volume failed: {exc}. "
            + (
                "It has stopped; ask a staff member."
                if gave_up
                else "It runs again by itself."
            )
        ) from exc
    return run


# ── the trigger ────────────────────────────────────────────────────
#: The one status the apply takes a scan from, and gives it back to.
APPLY_STATUS = Status.PAGE_COMPLETENESS_REVIEW_DONE


def _current_edit_ids(scan: Scan) -> list[int]:
    """Return the standing structural rows' pks, sorted.

    :param scan: The scan.
    :returns: What ``ApplyRun.edit_ids`` must equal for a run to be
        current.
    """
    return sorted(
        edit.pk
        for edit in page_edits.current_edits(scan, *PageEdit.STRUCTURAL_KINDS)
    )


def phase_due(scan: Scan) -> str | None:
    """Return which phase the scan's apply owes, if any.

    ``"build"`` when no run stands, the standing run is not built and
    has attempts left, or the built run's edit set no longer matches
    the standing rows. ``"glue"`` when the built run's job rows are all
    finished and a glue with ready inputs is not written. None while
    the rows are in flight, when a row is dead (the bar shows it, and
    the admin supersedes the run), or when the attempts are spent.

    :param scan: The scan.
    :returns: ``"build"``, ``"glue"`` or None.
    :rtype: str | None
    """
    run = current_run(scan)
    if run is None:
        return "build"
    if not run.is_built:
        return "build" if run.attempts < APPLY_MAX_ATTEMPTS else None
    if run.edit_ids != _current_edit_ids(scan):
        return "build"
    if run.attempts >= APPLY_MAX_ATTEMPTS:
        return None
    rows = list(run.jobs.all())
    if any(row.status in DEAD_JOB_STATUSES for row in rows):
        return None
    unfinished = {JobStatus.PENDING} | IN_FLIGHT_JOB_STATUSES
    if any(row.status in unfinished for row in rows):
        return None
    if glue_due(scan, run, rows):
        return "glue"
    return None


def glue_due(scan: Scan, run: ApplyRun, rows: list[ExternalJob]) -> bool:
    """Return whether a glue of a built run can be written now.

    Each glue has its own inputs, and each is judged alone: the
    bitonal copy needs the run's CONVERT rows only; the OCR volume and
    the printed pages need the volume's glued OCR run too; the
    detections need the volume's merged detection run, which the staff
    button starts and #211 will automate. The first two do not wait for
    the third.

    :param scan: The scan.
    :param run: A built run whose rows are all finished.
    :param rows: The run's rows.
    :returns: Whether :func:`glue_run` has something to write.
    :rtype: bool
    """
    if not run.bitonal_key:
        return True
    if not (run.ocr_key and run.printed_pages_key) and _volume_ocr_run(scan):
        return True
    if not run.detections_key and _volume_detect_run(scan):
        return True
    return False


def _needs_attention(scan: Scan) -> bool:
    """Return whether the trigger should look at this scan at all.

    The cheap pre-check over every approved scan on every tick: a run
    that is built, fully glued and untouched since costs one query.

    :param scan: An approved scan.
    :returns: Whether :func:`phase_due` is worth computing.
    :rtype: bool
    """
    run = current_run(scan)
    if run is None or not run.is_built or not run.is_glued:
        return True
    if not run.detections_key:
        return True
    return scan.page_edits.filter(
        kind__in=PageEdit.STRUCTURAL_KINDS, date_modified__gt=run.built_at
    ).exists()


def queue_ready_scans() -> int:
    """Queue the apply for every approved scan that owes a phase.

    A pass on the collect tick, and a trigger rather than the work: it
    writes one status and returns. The build pulls the original and
    the glue pulls the volume bitonal copy, minutes of work on a large
    volume, and the tick's scheduler is serial (#156). So this queues
    ``APPLY_PAGE_EDITS`` and ``process_next_scan`` runs it where the
    long stages already run (the #196 shape).

    Only ``PAGE_COMPLETENESS_REVIEW_DONE`` is taken, with a
    compare-and-swap. A scan in any other status is deferred without a
    mark: it comes back when it holds that status again.

    :returns: How many scans were queued.
    :rtype: int
    """
    if not s3_sync.s3_active():
        return 0
    queued = 0
    for scan in Scan.objects.filter(status=APPLY_STATUS).select_related(
        "reporter"
    ):
        if not _needs_attention(scan):
            continue
        phase = phase_due(scan)
        if phase is None:
            continue
        claimed = Scan.objects.filter(pk=scan.pk, status=APPLY_STATUS).update(
            status=Status.QUEUED,
            queued_action=QueuedAction.APPLY_PAGE_EDITS,
            progress_message=(
                "Building the corrected volume from the page edits."
                if phase == "build"
                else "Assembling the corrected volume."
            ),
            progress_current=0,
            progress_total=0,
        )
        if claimed:
            logger.info("apply: scan %s: queued the %s phase", scan.pk, phase)
            queued += claimed
    return queued


def run_due_phases(scan: Scan) -> str:
    """Do every phase the scan owes, in order, and say what happened.

    The worker behind ``QueuedAction.APPLY_PAGE_EDITS``. A build is
    followed at once by the glue when the run has no job rows to wait
    for -- a volume with no structural edit, or with deletes only.
    Raises nothing: the failure is counted on the run and the message
    tells the curator whether it runs again by itself.

    :param scan: The scan, in the daemon's claim.
    :returns: The progress message to park the scan with.
    :rtype: str
    """
    try:
        phase = phase_due(scan)
        if phase == "build":
            build_run(scan)
            phase = phase_due(scan)
        if phase == "glue":
            glue_run(scan)
    except ApplyError as exc:
        return str(exc)
    run = current_run(scan)
    if run is None:
        return "The corrected volume is not built yet."
    state = run_state(scan, run)
    return state["message"] if state else "The corrected volume is built."


def _volume_ocr_run(scan: Scan) -> list[ExternalJob]:
    """Return the volume's glued dots.mocr run, or nothing.

    :param scan: The scan.
    :returns: The live volume ANALYZE rows when every one is CONSUMED
        (the glue wrote ``r{run}-volume.json``), else an empty list.
    :rtype: list[ExternalJob]
    """
    from scanning import dots_mocr

    rows = dots_mocr.live_analyze_jobs(scan)
    if rows and all(row.status == JobStatus.CONSUMED for row in rows):
        return rows
    return []


def _volume_detect_run(scan: Scan) -> list[ExternalJob]:
    """Return the volume's merged detection run, or nothing.

    :param scan: The scan.
    :returns: The live volume DETECT rows when every one is CONSUMED
        (the merge wrote its volume JSON), else an empty list.
    :rtype: list[ExternalJob]
    """
    from scanning import yolo

    rows = yolo.live_detect_jobs(scan)
    if rows and all(row.status == JobStatus.CONSUMED for row in rows):
        return rows
    return []


def _rows_by_edit(
    rows: list[ExternalJob], stage: str
) -> dict[int, ExternalJob]:
    """Return one stage's rows of a run, keyed by the edit they read.

    :param rows: The run's rows.
    :param stage: A ``JobStage`` value.
    :returns: ``{edit pk: row}``.
    :rtype: dict[int, ExternalJob]
    """
    return {
        row.input_manifest["edit_id"]: row
        for row in rows
        if row.stage == stage and "edit_id" in (row.input_manifest or {})
    }


def _volume_bitonal_key(scan: Scan) -> str:
    """Return the key of the review-1 bitonal copy, or of the original.

    A volume that skipped the conversion (its source is already 1-bit)
    has no ``bitonal.pdf``; the original is then the bitonal copy.

    :param scan: The scan.
    :returns: The key the kept pages are sliced out of.
    :rtype: str
    """
    key = f"{s3_sync.s3_processing_prefix(scan)}{s3_sync.PIPELINE_INPUT_NAME}"
    if s3_sync.object_exists(key):
        return key
    return s3_sync.s3_original_key(scan) or ""


def _glue_bitonal(
    scan: Scan, run: ApplyRun, rows: list[ExternalJob], tmp_dir: Path
) -> str:
    """Write the final bitonal copy: kept pages sliced out of the
    review-1 ``bitonal.pdf``, new pages from the one-page results.

    The per-shard conversion results of the volume were deleted at the
    first merge, but the volume ``bitonal.pdf`` holds every byte of
    them, so the kept pages come from there. The one-page results are
    **kept** after this, unlike the volume merge's: the next run
    carries them. A run with no structural edit aliases the review-1
    copy and writes nothing.

    :param scan: The scan.
    :param run: The built run.
    :param rows: The run's rows.
    :param tmp_dir: Scratch space.
    :returns: The key of the final bitonal copy.
    :rtype: str
    :raises ApplyError: If a result is missing or a page count is off.
    """
    page_map = run.page_map
    if not page_map.get("pages"):
        raise ApplyError(f"run {run.label} of scan {scan.pk} has no page map")
    entries = page_map["pages"]
    if all(entry["source"]["kind"] == "original" for entry in entries) and (
        len(entries) == page_map["source_page_count"]
    ):
        return _volume_bitonal_key(scan)

    converted = _rows_by_edit(rows, JobStage.CONVERT)
    volume_path = tmp_dir / "volume-bitonal.pdf"
    s3_sync.download_object(_volume_bitonal_key(scan), volume_path)
    shards: dict[int, fitz.Document] = {}

    def shard_of(edit_id: int) -> fitz.Document:
        if edit_id not in shards:
            local = tmp_dir / f"convert-e{edit_id}.pdf"
            row = converted.get(edit_id)
            if row is not None and row.result_key:
                s3_sync.download_object(row.result_key, local)
            else:
                # The stage was off at build time: the shard itself
                # stands in, unconverted, rather than a hole.
                edit = PageEdit.objects.get(pk=edit_id)
                s3_sync.download_object(page_shard_key(scan, edit), local)
            shards[edit_id] = fitz.open(str(local))
        return shards[edit_id]

    destination = tmp_dir / "final-bitonal.pdf"
    try:
        with fitz.open(str(volume_path)) as volume, fitz.open() as out:
            if volume.page_count != page_map["source_page_count"]:
                raise ApplyError(
                    f"the volume bitonal copy has {volume.page_count} "
                    f"page(s), the original {page_map['source_page_count']}"
                )
            for entry in entries:
                src = entry["source"]
                if src["kind"] == "original":
                    out.insert_pdf(
                        volume,
                        from_page=src["pdf_page"] - 1,
                        to_page=src["pdf_page"] - 1,
                    )
                    continue
                shard = shard_of(src["edit_id"])
                if src["page"] >= shard.page_count:
                    raise ApplyError(
                        f"the converted shard of edit {src['edit_id']} has "
                        f"{shard.page_count} page(s); the map asks for "
                        f"page {src['page']}"
                    )
                out.insert_pdf(
                    shard, from_page=src["page"], to_page=src["page"]
                )
            if out.page_count != page_map["final_page_count"]:
                raise ApplyError(
                    f"the final bitonal copy has {out.page_count} page(s), "
                    f"the map {page_map['final_page_count']}"
                )
            out.save(str(destination), garbage=3, deflate=True)
    finally:
        for shard in shards.values():
            shard.close()
    key = f"{run_prefix(scan, run)}{s3_sync.PIPELINE_INPUT_NAME}"
    if not s3_sync.upload_file_object(key, destination, "application/pdf"):
        raise ApplyError("could not upload the final bitonal copy")
    return key


def _result_payload(scan: Scan, row: ExternalJob, action: str) -> dict:
    """Download and check one apply row's result envelope.

    :param scan: The scan.
    :param row: A COMPLETED or CONSUMED apply row.
    :param action: The handler action the envelope must name.
    :returns: The payload.
    :rtype: dict
    :raises ApplyError: If the object is missing or is not this row's.
    """
    from scanning import jobs

    if not row.result_key:
        raise ApplyError(f"job {row.pk} of scan {scan.pk} has no result key")
    envelope = s3_sync.download_json_object(row.result_key)
    return jobs.check_result_envelope(scan, row, envelope, action, ApplyError)


def _glue_ocr(
    scan: Scan, run: ApplyRun, rows: list[ExternalJob]
) -> tuple[str, str]:
    """Write the final OCR volume and the frozen printed-page map.

    The kept pages come from the volume's glued ``r{run}-volume.json``
    (#202), the new pages from the one-page results, each page
    renumbered to its final page and stamped with its source. A page
    the worker could not read keeps its ``error``; a page whose stage
    was off at build time is a hole too. A run with no structural edit
    aliases the volume document and writes only the printed pages.

    :param scan: The scan.
    :param run: The built run.
    :param rows: The run's rows.
    :returns: The OCR key and the printed-pages key.
    :rtype: tuple[str, str]
    """
    from scanning import dots_mocr

    volume_rows = _volume_ocr_run(scan)
    if not volume_rows:
        raise ApplyError(f"scan {scan.pk} has no glued OCR run to read")
    volume_key = dots_mocr.glued_result_key(scan, volume_rows[0].run)
    volume = s3_sync.download_json_object(volume_key)
    page_map = run.page_map
    entries = page_map["pages"]
    prefix = run_prefix(scan, run)

    if all(entry["source"]["kind"] == "original" for entry in entries) and (
        len(entries) == page_map["source_page_count"]
    ):
        ocr_key = volume_key
        document = volume
    else:
        by_pdf_page = {page["pdf_page"]: page for page in volume["pages"]}
        read = _rows_by_edit(rows, JobStage.ANALYZE)
        edit_pages: dict[int, dict[int, dict]] = {}
        for edit_id, row in read.items():
            payload = _result_payload(scan, row, dots_mocr.ACTION)
            edit_pages[edit_id] = {
                page["page_no"]: page for page in payload.get("pages") or []
            }
        pages = []
        for entry in entries:
            src = entry["source"]
            if src["kind"] == "original":
                page = by_pdf_page.get(src["pdf_page"])
                if page is None:
                    raise ApplyError(
                        f"the volume OCR document has no page {src['pdf_page']}"
                    )
                page = dict(page)
            else:
                page = edit_pages.get(src["edit_id"], {}).get(src["page"])
                if page is None:
                    page = {
                        "cells": [],
                        "md": "",
                        "error": "not read: no result for this page",
                    }
                page = {k: v for k, v in page.items() if k != "raw"}
            page.pop("shard_index", None)
            page.pop("page_no", None)
            page["page_index"] = entry["final_page"] - 1
            page["pdf_page"] = entry["final_page"]
            page["source"] = src
            pages.append(page)
        document = {
            "schema_version": dots_mocr.GLUE_SCHEMA_VERSION,
            "engine": str(JobEngine.DOTS_MOCR),
            "action": dots_mocr.ACTION,
            "scan_pk": scan.pk,
            "run": volume["run"],
            "apply_run": run.label,
            "source_page_count": page_map["final_page_count"],
            "source_fingerprint": run.source_fingerprint,
            "dpi": volume.get("dpi", dots_mocr.DPI),
            "prompt_mode": volume.get("prompt_mode", dots_mocr.PROMPT_MODE),
            "generated_at": timezone.now().isoformat(),
            "pages": pages,
            **dots_mocr._page_lists(pages, "page_index"),
        }
        ocr_key = f"{prefix}ocr-volume.json"
        if not s3_sync.upload_json_object(ocr_key, document):
            raise ApplyError("could not upload the final OCR volume")

    printed = printed_pages(scan, run, document)
    printed_key = f"{prefix}printed_pages.json"
    if not s3_sync.upload_json_object(printed_key, printed):
        raise ApplyError("could not upload the printed pages")
    return ocr_key, printed_key


def printed_pages(scan: Scan, run: ApplyRun, document: dict) -> dict:
    """Return the frozen printed-page map, in the final page space.

    The glued read, through the same reading as review 1
    (``page_numbers.ocr_results_from_volume``), overlaid with the
    curator's decisions: a ``SET_NUMBER`` row lands on the slot its
    original page holds (a replaced or rotated page included), and an
    inserted page carries the label its row was uploaded under. The
    curator outranks the model. Citations use printed pages, so this
    map is a product of the apply, not review scaffolding.

    :param scan: The scan.
    :param run: The built run.
    :param document: The final OCR volume, or the volume's own when the
        run aliases it.
    :returns: ``pages`` of ``final_page``, ``printed``, ``type``,
        ``by`` (``model``, ``curator`` or None) and ``source``.
    :rtype: dict
    """
    from scanning import page_numbers

    page_map = run.page_map
    results = page_numbers.ocr_results_from_volume(document)
    by_final = {entry["pdf_page"]: entry for entry in results}
    pages = []
    for entry in page_map["pages"]:
        final = entry["final_page"]
        read = by_final.get(final, {})
        pages.append(
            {
                "final_page": final,
                "printed": read.get("detected"),
                "type": read.get("type"),
                "by": "model" if read.get("detected") else None,
                "source": entry["source"],
            }
        )
    by_final_out = {page["final_page"]: page for page in pages}

    def curator(page: dict, value: str) -> None:
        page["printed"] = value or None
        page["type"] = (
            None if not value else ("range" if "-" in value else "single")
        )
        page["by"] = "curator"

    for edit in page_edits.current_edits(scan, PageEdit.Kind.SET_NUMBER):
        final = final_slot_of(page_map, edit.pdf_page)
        if final is not None:
            curator(by_final_out[final], edit.value)
    for page in pages:
        src = page["source"]
        if src["kind"] == "edit" and src["edit_kind"] == str(
            PageEdit.Kind.INSERT_PAGE
        ):
            label = (
                PageEdit.objects.filter(pk=src["edit_id"])
                .values_list("logical_page", flat=True)
                .first()
            )
            if label:
                curator(page, label)
    return {
        "schema_version": MAP_SCHEMA_VERSION,
        "scan_pk": scan.pk,
        "apply_run": run.label,
        "source_fingerprint": run.source_fingerprint,
        "final_page_count": page_map["final_page_count"],
        "generated_at": timezone.now().isoformat(),
        "pages": pages,
    }


def _glue_detections(
    scan: Scan, run: ApplyRun, rows: list[ExternalJob]
) -> str:
    """Write the final detections volume, in the final page space.

    The kept pages' detections come from the volume's merged document
    (#196), renumbered through the map; a deleted, replaced or rotated
    page's detections are dropped, and the one-page results supply the
    new pages'. One model family for the whole volume, as the merge
    checks. A run with no structural edit aliases the merged document.

    :param scan: The scan.
    :param run: The built run.
    :param rows: The run's rows.
    :returns: The key of the final detections document.
    :rtype: str
    """
    from scanning import yolo

    volume_rows = _volume_detect_run(scan)
    if not volume_rows:
        raise ApplyError(f"scan {scan.pk} has no merged detection run to read")
    volume_key = yolo.merged_result_key(scan, volume_rows[0].run)
    page_map = run.page_map
    entries = page_map["pages"]
    if all(entry["source"]["kind"] == "original" for entry in entries) and (
        len(entries) == page_map["source_page_count"]
    ):
        return volume_key

    volume = yolo.load_merged_document(scan, volume_rows[0].run)
    if (
        run.source_fingerprint
        and volume.get("source_fingerprint")
        and volume["source_fingerprint"] != run.source_fingerprint
    ):
        raise ApplyError(
            "the merged detection document describes another original"
        )
    detections = []
    for det in volume.get("detections", []):
        final = final_page_of(page_map, det["pdf_page"])
        if final is None:
            continue
        detections.append(
            {
                **{k: v for k, v in det.items() if k != "shard_index"},
                "page_index": final - 1,
                "pdf_page": final,
                "source": {"kind": "original", "pdf_page": det["pdf_page"]},
            }
        )
    slots = {
        (entry["source"]["edit_id"], entry["source"]["page"]): entry
        for entry in entries
        if entry["source"]["kind"] == "edit"
    }
    models = volume.get("models")
    for edit_id, row in _rows_by_edit(rows, JobStage.DETECT).items():
        payload = _result_payload(scan, row, yolo.ACTION)
        if models is not None and payload.get("models") not in (None, models):
            raise ApplyError(
                f"edit {edit_id} was detected with {payload.get('models')}, "
                f"the volume with {models}"
            )
        for det in payload.get("detections") or []:
            entry = slots.get((edit_id, det["page_index"]))
            if entry is None:
                raise ApplyError(
                    f"edit {edit_id} answered page {det['page_index']}, "
                    "which the map does not hold"
                )
            detections.append(
                {
                    **det,
                    "page_index": entry["final_page"] - 1,
                    "pdf_page": entry["final_page"],
                    "source": entry["source"],
                }
            )
    document = {
        "schema_version": yolo.MERGE_SCHEMA_VERSION,
        "engine": str(JobEngine.BLACKLETTER),
        "action": yolo.ACTION,
        "scan_pk": scan.pk,
        "run": volume.get("run"),
        "apply_run": run.label,
        "source_page_count": page_map["final_page_count"],
        "source_fingerprint": run.source_fingerprint,
        "dpi": volume.get("dpi", yolo.DPI),
        "models": models,
        "confidence": volume.get("confidence", yolo.CONFIDENCE),
        "generated_at": timezone.now().isoformat(),
        "detections": detections,
        "pages_with_detections": len({d["page_index"] for d in detections}),
    }
    key = f"{run_prefix(scan, run)}detections-volume.json"
    if not s3_sync.upload_json_object(key, document):
        raise ApplyError("could not upload the final detections")
    return key


def _glue(scan: Scan, run: ApplyRun, rows: list[ExternalJob]) -> list[str]:
    """Phase 2: write every glue whose inputs are ready, once each.

    :param scan: The scan.
    :param run: The built run, rows all finished.
    :param rows: The run's rows.
    :returns: The names of the glues written.
    :rtype: list[str]
    """
    written = []
    consumed: list[str] = []
    with tempfile.TemporaryDirectory(
        prefix=f"{BUILD_TMP_PREFIX}{scan.pk}-glue-"
    ) as tmp:
        tmp_dir = Path(tmp)
        if not run.bitonal_key:
            run.bitonal_key = _glue_bitonal(scan, run, rows, tmp_dir)
            ApplyRun.objects.filter(pk=run.pk).update(
                bitonal_key=run.bitonal_key
            )
            written.append("bitonal")
            consumed.append(JobStage.CONVERT)
    if not (run.ocr_key and run.printed_pages_key) and _volume_ocr_run(scan):
        run.ocr_key, run.printed_pages_key = _glue_ocr(scan, run, rows)
        ApplyRun.objects.filter(pk=run.pk).update(
            ocr_key=run.ocr_key, printed_pages_key=run.printed_pages_key
        )
        written.append("ocr")
        consumed.append(JobStage.ANALYZE)
    if not run.detections_key and _volume_detect_run(scan):
        run.detections_key = _glue_detections(scan, run, rows)
        ApplyRun.objects.filter(pk=run.pk).update(
            detections_key=run.detections_key
        )
        written.append("detections")
        consumed.append(JobStage.DETECT)
    if consumed:
        # Consumed, and kept: the next run carries them.
        ExternalJob.objects.filter(
            apply_run=run, stage__in=consumed, status=JobStatus.COMPLETED
        ).update(status=JobStatus.CONSUMED, consumed_at=timezone.now())
    return written


def glue_run(scan: Scan) -> ApplyRun:
    """Run phase 2 for a scan: write the glues that are due.

    :param scan: The scan, in the daemon's claim.
    :returns: The run.
    :rtype: ApplyRun
    :raises ApplyError: If a glue failed. The failure is counted on the
        run before it is raised.
    """
    run = current_run(scan)
    if run is None or not run.is_built:
        raise ApplyError(f"scan {scan.pk} has no built run to glue")
    started = time.monotonic()
    try:
        written = _glue(scan, run, list(run.jobs.all()))
    except Exception as exc:
        gave_up = record_failure(run, exc)
        raise ApplyError(
            f"Assembling the corrected volume failed: {exc}. "
            + (
                "It has stopped; ask a staff member."
                if gave_up
                else "It runs again by itself."
            )
        ) from exc
    ApplyRun.objects.filter(pk=run.pk).update(attempts=0, last_error="")
    run.attempts = 0
    logger.info(
        "apply: scan %s: run %s glued %s in %.1fs",
        scan.pk,
        run.label,
        ", ".join(written) or "nothing",
        time.monotonic() - started,
    )
    return run


def run_state(scan: Scan, run: ApplyRun | None = None) -> dict | None:
    """Describe the scan's standing apply run for the step-1 bar.

    The scan stays in DONE while the run works, so the rows and the run
    row are the only place the progress lives, and this is how a viewer
    sees it.

    :param scan: The scan.
    :param run: The run, when the caller has it.
    :returns: ``{"label", "message", "summary", "failed", "open"}``, or
        None when no run stands.
    :rtype: dict | None
    """
    run = run or current_run(scan)
    if run is None:
        return None
    rows = list(run.jobs.all())
    unfinished = {JobStatus.PENDING} | IN_FLIGHT_JOB_STATUSES
    open_rows = sum(1 for row in rows if row.status in unfinished)
    dead = [row for row in rows if row.status in DEAD_JOB_STATUSES]
    done = len(rows) - open_rows - len(dead)
    if not run.is_built:
        if run.attempts >= APPLY_MAX_ATTEMPTS:
            message = (
                "The corrected volume could not be built; ask a staff member."
            )
        elif run.attempts:
            message = (
                "Building the corrected volume failed; it is tried again."
            )
        else:
            message = "The corrected volume is being built."
    elif dead:
        message = (
            f"Corrected volume: {len(dead)} page part(s) failed: "
            f"{dead[0].error_code}"
        )
    elif open_rows:
        message = (
            f"Corrected volume: {done} of {len(rows)} page part(s) processed"
        )
    elif not run.is_glued:
        if run.attempts >= APPLY_MAX_ATTEMPTS:
            message = "The corrected volume could not be assembled; ask a staff member."
        else:
            message = "Corrected volume: assembling"
    elif not run.detections_key:
        message = "Corrected volume built; detections pending"
    else:
        message = "Corrected volume built"
    return {
        "label": run.label,
        "message": message,
        "summary": (
            f"{len(rows)} page part(s), {done} done, {open_rows} open, "
            f"{len(dead)} failed; attempts {run.attempts}"
            + (
                f"; last error: {run.last_error[:120]}"
                if run.last_error
                else ""
            )
        ),
        "failed": bool(dead) or run.attempts >= APPLY_MAX_ATTEMPTS,
        "open": open_rows,
    }
