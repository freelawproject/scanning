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
from dataclasses import dataclass, field
from pathlib import Path

import fitz
from django.utils import timezone

from scanning import page_edits, s3_sync
from scanning.models import ApplyRun, PageEdit, Scan

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

    def emit_upload(edit: PageEdit, reference: int) -> None:
        for k in range(count_of(edit)):
            source = {
                "kind": "edit",
                "edit_id": edit.pk,
                "edit_kind": str(edit.kind),
                "page": k,
                "reference_pdf_page": reference,
            }
            if edit.kind == PageEdit.Kind.REPLACE_PAGE:
                source["pdf_page"] = edit.pdf_page
            emit(source)

    last = scan.page_count
    for edit in gaps.get(0, []):
        emit_upload(edit, 1)
    for pdf_page in range(1, last + 1):
        if pdf_page in deleted:
            pass
        elif pdf_page in replacements:
            emit_upload(replacements[pdf_page], pdf_page)
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
            emit_upload(edit, pdf_page)
    # An insert anchored past the last page has no gap to fill. It is
    # placed last rather than dropped: a curator uploaded it, and the
    # viewer shows it there too (``page_edits.project_inserts``).
    for anchor in sorted(anchor for anchor in gaps if anchor > last):
        for edit in gaps[anchor]:
            emit_upload(edit, last)

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
    source: fitz.Document, edit: PageEdit, plan: ApplyPlan, data: bytes | None
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
    :param plan: The plan, for the reference page of an image.
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
    reference = next(
        entry["source"]["reference_pdf_page"]
        for entry in plan.pages
        if entry["source"].get("edit_id") == edit.pk
    )
    _place_image(out, source, reference, data)
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
                shards[edit.pk] = build_edit_shard(source, edit, plan, data)
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
    a dead row. The run's outputs stay in S3; only its open job rows
    are cancelled, scoped to the run so the volume runs stay alive. The
    next build starts ``a{n+1}`` and carries every paid result the
    edits did not change.

    :param scan: The scan.
    :param reason: Recorded on each cancelled job row.
    :returns: How many runs were superseded.
    :rtype: int
    """
    from scanning import jobs

    now = timezone.now()
    count = 0
    for run in scan.apply_runs.filter(superseded_at__isnull=True):
        jobs.abandon_open(scan, reason, apply_run=run)
        ApplyRun.objects.filter(pk=run.pk, superseded_at__isnull=True).update(
            superseded_at=now
        )
        logger.info(
            "apply: scan %s: run %s superseded: %s", scan.pk, run.label, reason
        )
        count += 1
    return count
