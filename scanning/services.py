"""Background processing pipelines and business logic.

Functions in this module run outside the request/response cycle, in the
daemon process.  They must NOT import Django HTTP machinery
(HttpResponse, render, redirect, etc.).
"""

import json
import os
import re
import shutil
import traceback
from collections import Counter
from pathlib import Path

import django
import fitz
from blackletter.analyze import (
    analyze_pdf as bl_analyze_pdf,
)
from blackletter.api import (
    bitonal as bl_bitonal,
)
from blackletter.api import (
    detect as bl_detect,
)
from blackletter.api import (
    pair as bl_pair,
)
from blackletter.margins import compute_margin_rects
from blackletter.models import (
    BBox,
    Label,
    Page,
)
from blackletter.models import (
    Detection as BLDetection,
)
from blackletter.models import (
    Document as BLDoc,
)
from blackletter.process import compute_redaction_rects
from blackletter.scanner import _pair_opinions
from blackletter.validate import (
    _auto_correct,
    _build_issues,
    _parse_expected_range,
    _split_in_out_of_range,
)
from django.conf import settings
from django.db.models import F

from scanning.models import (
    Detection,
    Issue,
    LLMScan,
    OpinionScan,
    OpinionStatus,
    Scan,
    Stage,
    Status,
)
from scanning.utils import ensure_output_dir, find_ocr_pdf, has_s3_credentials

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _update_progress(
    scan_pk: int,
    message: str,
    current: int | None = None,
    total: int | None = None,
    **kwargs,
) -> None:
    """Update scan progress fields.

    :param scan_pk: Primary key of the scan to update.
    :param message: Human-readable progress message (truncated to 255 chars).
    :param current: Current step number, if applicable.
    :param total: Total step count, if applicable.
    :param kwargs: Additional Scan fields to update.
    """
    updates = {"progress_message": message[:255]}
    if current is not None:
        updates["progress_current"] = current
    if total is not None:
        updates["progress_total"] = total
    updates.update(kwargs)
    Scan.objects.filter(pk=scan_pk).update(**updates)


def _ensure_bitonal(scan: "Scan", output_dir: Path) -> Path:
    """Convert to bitonal if needed, returning the bitonal PDF path.

    Skips conversion if ``bitonal.pdf`` already exists. Updates the
    scan's ``page_count`` after conversion.

    :param scan: The Scan instance.
    :param output_dir: Directory where ``bitonal.pdf`` is stored.
    :return: Path to the bitonal PDF.
    :rtype: Path
    """
    bitonal_path = output_dir / "bitonal.pdf"
    if not bitonal_path.exists():

        def _bitonal_progress(current, total, message):
            _update_progress(scan.pk, message, current=current, total=total)

        bl_bitonal(
            scan.pdf_path,
            str(output_dir),
            progress_callback=_bitonal_progress,
        )

    pdf = fitz.open(str(bitonal_path))
    page_count = pdf.page_count
    pdf.close()
    Scan.objects.filter(pk=scan.pk).update(page_count=page_count)
    return bitonal_path


def _run_ocr(
    scan_pk: int,
    bitonal_path: str,
    output_dir: str,
    reporter: str,
    volume: str,
    first_page: int,
) -> str:
    """Run Tesseract OCR via blackletter.

    :param scan_pk: Primary key of the scan (for progress updates).
    :param bitonal_path: Path to the bitonal PDF.
    :param output_dir: Directory to write the OCR'd PDF into.
    :param reporter: Reporter short name (e.g. "f3d").
    :param volume: Volume number as a string.
    :param first_page: First logical page number in the PDF.
    :return: Path to the OCR'd PDF.
    """
    pdf = fitz.open(bitonal_path)
    total_pages = pdf.page_count
    pdf.close()
    _update_progress(
        scan_pk,
        f"Running Tesseract OCR (0/{total_pages} pages)...",
        current=0,
        total=total_pages,
    )
    return _ocr(
        scan_pk,
        bitonal_path,
        output_dir,
        reporter=reporter,
        volume=volume,
        first_page=first_page,
        total_pages=total_pages,
    )


def _ocr(
    scan_pk: int,
    pdf_path: str | Path,
    output_dir: str | Path,
    reporter: str = "",
    volume: str = "",
    first_page: int = 1,
    total_pages: int = 0,
    language: str = "eng",
) -> Path:
    """OCR a PDF (add text layer via ocrmypdf/Tesseract).

    Temporary local copy of blackletter's ``ocr`` function with
    progress callback support. Will be moved to blackletter once
    validated.

    :param scan_pk: Primary key of the scan (for progress updates).
    :param pdf_path: Path to the input PDF.
    :param output_dir: Directory to write the OCR'd PDF into.
    :param reporter: Reporter short name (e.g. "f3d").
    :param volume: Volume number as a string.
    :param first_page: First logical page number in the PDF.
    :param total_pages: Total page count (for progress messages).
    :param language: Tesseract language code.
    :return: Path to the OCR'd PDF.
    """
    import logging
    import time

    import ocrmypdf
    from ocrmypdf import hookimpl
    from ocrmypdf._plugin_manager import get_plugin_manager

    pdf_path = Path(pdf_path)
    output_dir = Path(output_dir)

    # Build output filename
    last_page = first_page + total_pages - 1
    parts = [
        p
        for p in [reporter, str(volume), str(first_page), str(last_page)]
        if p
    ]
    scan_name = ".".join(parts) if parts else pdf_path.stem
    output_path = output_dir / f"{scan_name}.pdf"

    # Suppress noisy loggers
    for name in (
        "pikepdf",
        "fontTools",
        "fontTools.subset",
        "fontTools.ttLib",
        "ocrmypdf",
    ):
        logging.getLogger(name).setLevel(logging.ERROR)
    for _n in list(logging.root.manager.loggerDict):
        if _n.startswith("ocrmypdf"):
            logging.getLogger(_n).setLevel(logging.ERROR)

    # Progress bar class that updates the scan record per page
    class _ScanProgressBar:
        def __init__(
            self, *, total=None, desc=None, unit=None, disable=False, **kw
        ):
            self._total = total or total_pages
            self._unit = unit
            self._desc = desc
            self._current = 0
            print(
                f"  [progress] unit={unit!r} desc={desc!r} total={total}",
                flush=True,
            )

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def update(self, n=1, *, completed=None):
            self._current += n
            if self._unit == "page" and (
                self._current % 10 == 0 or self._current == self._total
            ):
                _update_progress(
                    scan_pk,
                    f"Tesseract OCR: {self._current}/{self._total} pages...",
                    current=self._current,
                    total=self._total,
                )

    class _ScanProgressPlugin:
        @hookimpl
        def get_progressbar_class(self):
            return _ScanProgressBar

    pm = get_plugin_manager()
    pm._pm.register(_ScanProgressPlugin())

    print(f"  OCR {total_pages} pages...", flush=True)
    t0 = time.time()
    ocrmypdf.ocr(
        str(pdf_path),
        str(output_path),
        pdf_renderer="auto",
        optimize=1,
        output_type="pdf",
        language=[language],
        tesseract_timeout=120,
        progress_bar=True,
        plugin_manager=pm,
    )
    print(f"  OCR done ({time.time() - t0:.0f}s)", flush=True)
    return output_path


def _run_yolo(scan_pk: int, pdf_path: str, output_dir: str) -> None:
    """Run YOLO detection (all 3 models) via blackletter.

    Captures stdout from ``bl_detect`` to relay per-model and per-batch
    progress back to the scan's progress fields.

    :param scan_pk: Primary key of the scan (for progress updates).
    :param pdf_path: Path to the PDF to run detection on.
    :param output_dir: Directory where detections.json will be saved.
    """
    import io
    import sys

    _update_progress(scan_pk, "YOLO detection: loading models...")

    real_stdout = sys.stdout

    class _ProgressWriter(io.TextIOBase):
        """Intercept bl_detect stdout and relay to _update_progress."""

        def __init__(self):
            self._buf = ""

        def write(self, s):
            real_stdout.write(s)
            real_stdout.flush()
            self._buf += s
            while "\n" in self._buf:
                line, self._buf = self._buf.split("\n", 1)
                self._handle_line(line.strip())
            return len(s)

        def flush(self):
            real_stdout.flush()

        def _handle_line(self, line):
            if not line:
                return
            # "Detecting with small..."
            if line.startswith("Detecting with"):
                model = line.replace("Detecting with", "").strip().rstrip(".")
                _update_progress(scan_pk, f"YOLO detection: {model}...")
            # "  23/23 pages"
            elif "/" in line and "pages" in line:
                _update_progress(scan_pk, f"YOLO detection: {line}")
            # "  small done (2s)"
            elif "done" in line:
                _update_progress(scan_pk, f"YOLO: {line}")
            # "363 detections (786 raw from 3 models)"
            elif "detections" in line:
                _update_progress(scan_pk, f"YOLO: {line}")

    sys.stdout = _ProgressWriter()
    try:
        bl_detect(pdf_path, output_dir, models=["small", "medium", "large"])
    finally:
        sys.stdout = real_stdout


def _import_detections_from_json(scan_pk: int, output_dir: str) -> list:
    """Load detections.json from disk into Detection model.

    Clears existing detections first.

    :param scan_pk: Primary key of the scan to import detections for.
    :param output_dir: Directory containing detections.json.
    :return: The raw detection list from the JSON file.
    """
    det_path = Path(output_dir) / "detections.json"
    dets = json.loads(det_path.read_text())

    Detection.objects.filter(scan_id=scan_pk).delete()
    det_objects = []
    for d in dets:
        try:
            label_name = Label(d["label_id"]).name
        except (ValueError, KeyError):
            label_name = d.get("label", "UNKNOWN")
        det_objects.append(
            Detection(
                scan_id=scan_pk,
                page_index=d["page_index"],
                label=label_name,
                label_id=d["label_id"],
                confidence=d["confidence"],
                x0=d["bbox"][0],
                y0=d["bbox"][1],
                x1=d["bbox"][2],
                y1=d["bbox"][3],
                img_width=d.get("img_width", 0),
                img_height=d.get("img_height", 0),
                model_name=d.get("found_by", [{}])[0].get("model", ""),
                model_count=d.get("model_count", 1),
                found_by=json.dumps(d.get("found_by", [])),
            )
        )
    Detection.objects.bulk_create(det_objects)
    return dets


def _page_number_lookup(scan: "Scan") -> dict:
    """Build {page_index: (page_number, page_number_end)} from ocr_results.

    For range pages like "677-685", returns (677, 685).
    For single pages like "677", returns (677, None).

    :param scan: The Scan instance whose ocr_results to parse.
    :return: Mapping of page index to (start, end) page number tuple.
    """
    ocr_results = json.loads(scan.ocr_results) if scan.ocr_results else []
    lookup = {}
    range_re = re.compile(r"^(\d{1,4})\s*[–\-]\s*(\d{1,4})$")
    for r in ocr_results:
        pdf_idx = r["pdf_page"] - 1
        detected = r.get("detected")
        if not detected:
            continue
        if r.get("type") == "range":
            m = range_re.match(str(detected).replace("\u2013", "-"))
            if m:
                lookup[pdf_idx] = (int(m.group(1)), int(m.group(2)))
            continue
        try:
            lookup[pdf_idx] = (int(detected), None)
        except (ValueError, TypeError):
            pass
    return lookup


def _sync_detections_to_disk(scan_pk: int) -> list | None:
    """Write current DB detections to detections.json on disk.

    :param scan_pk: Primary key of the scan whose detections to sync.
    :return: The detection data written, or None if no output_dir.
    """
    scan = Scan.objects.get(pk=scan_pk)
    output_dir = Path(scan.output_dir)
    if not output_dir.is_dir():
        return

    # Build page_number lookup from ocr_results
    page_numbers = _page_number_lookup(scan)

    all_saved = Detection.objects.filter(
        scan_id=scan_pk, active=True
    ).order_by("page_index", "y0")
    det_data = []
    for d in all_saved:
        entry = {
            "page_index": d.page_index,
            "label": d.label,
            "label_id": d.label_id,
            "confidence": d.confidence,
            "bbox": [d.x0, d.y0, d.x1, d.y1],
            "img_width": d.img_width,
            "img_height": d.img_height,
            "model_count": d.model_count,
        }
        pn = page_numbers.get(d.page_index)
        if pn:
            entry["page_number"] = pn[0]
            if pn[1] is not None:
                entry["page_number_end"] = pn[1]
        det_data.append(entry)
    (output_dir / "detections.json").write_text(json.dumps(det_data))
    return det_data


def _build_document_from_detections(
    scan: "Scan", det_data: list, pdf_path: str
) -> "BLDoc":
    """Build a blackletter Document from detection data and a PDF.

    :param scan: The Scan instance for reporter/volume metadata.
    :param det_data: List of detection dicts (from detections.json).
    :param pdf_path: Path to the PDF to read page dimensions from.
    :return: The constructed Document.
    """
    src_pdf = fitz.open(str(pdf_path))
    pages_data = {}
    for entry in det_data:
        pi = entry["page_index"]
        if pi not in pages_data:
            pages_data[pi] = {
                "img_width": entry.get("img_width", 1),
                "img_height": entry.get("img_height", 1),
                "detections": [],
            }
        pages_data[pi]["detections"].append(entry)
    pages = []
    for pi in sorted(pages_data.keys()):
        pd = pages_data[pi]
        if pi < src_pdf.page_count:
            pw, ph = src_pdf[pi].rect.width, src_pdf[pi].rect.height
        else:
            pw, ph = 612.0, 792.0
        page = Page(
            index=pi,
            pdf_width=pw,
            pdf_height=ph,
            img_width=pd["img_width"],
            img_height=pd["img_height"],
        )
        for d in pd["detections"]:
            b = d.get("bbox", [0, 0, 1, 1])
            page.detections.append(
                BLDetection(
                    bbox=BBox(x1=b[0], y1=b[1], x2=b[2], y2=b[3]),
                    label=Label(d["label_id"]),
                    confidence=d["confidence"],
                    page_index=pi,
                )
            )
        pages.append(page)
    src_pdf.close()

    scan_obj = scan if isinstance(scan, Scan) else Scan.objects.get(pk=scan)
    document = BLDoc(
        pdf_path=str(pdf_path),
        pages=pages,
        reporter=scan_obj.reporter.short_name or "",
        volume=str(scan_obj.volume) or "",
        first_page=scan_obj.start_page or 1,
        ocr_applied=True,
    )
    return document


def _compute_and_save_redaction_rects(
    scan_pk: int, pdf_path: str, output_dir: str
) -> list:
    """Compute redaction rects and save to the Scan model.

    :param scan_pk: Primary key of the scan to compute rects for.
    :param pdf_path: Path to the PDF used for page dimensions.
    :param output_dir: Directory (used for detection sync only).
    :return: The computed rects list.
    """
    scan = Scan.objects.get(pk=scan_pk)
    output_dir = Path(output_dir)

    det_data = _sync_detections_to_disk(scan_pk)
    if not det_data:
        return []

    document = _build_document_from_detections(scan, det_data, pdf_path)
    opinions = _pair_opinions(document)
    rects = compute_redaction_rects(document, opinions, skip_doctr=True)

    # Snap headnote rect x-bounds to TEXT_COLUMN detections for consistency.
    # Only snap to a TEXT_COLUMN on the same side of the page midpoint —
    # otherwise a missing left TEXT_COLUMN causes the left rect to be
    # snapped to the right column, destroying it.
    tc_by_page = {}
    for d in Detection.objects.filter(scan=scan, active=True, label_id=16):
        tc_by_page.setdefault(d.page_index, []).append(d)
    img_mid = document.pages[0].img_width / 2 if document.pages else 850
    for page_entry in rects:
        pi = page_entry["page_index"]
        cols = tc_by_page.get(pi, [])
        if not cols:
            continue
        for r in page_entry["rects"]:
            if r.get("type") != "headnote":
                continue
            cx = (r["x0"] + r["x1"]) / 2
            # Only consider TEXT_COLUMNs on the same side of the midpoint
            same_side = (
                [c for c in cols if (c.x0 + c.x1) / 2 < img_mid]
                if cx < img_mid
                else [c for c in cols if (c.x0 + c.x1) / 2 >= img_mid]
            )
            if not same_side:
                continue
            best = min(same_side, key=lambda c: abs((c.x0 + c.x1) / 2 - cx))
            r["x0"] = round(best.x0, 1)
            r["x1"] = round(best.x1, 1)

    # Split headnote blocks at HEADNOTE detection boundaries
    _GAP = 6
    hn_dets_by_page = {}
    for d in Detection.objects.filter(
        scan=scan, active=True, label="HEADNOTE"
    ):
        hn_dets_by_page.setdefault(d.page_index, []).append(d)
    for page_entry in rects:
        pi = page_entry["page_index"]
        if pi not in hn_dets_by_page:
            continue
        hn_dets = sorted(hn_dets_by_page[pi], key=lambda d: d.y0)
        new_page_rects = []
        for r in page_entry["rects"]:
            if r.get("type") != "headnote":
                new_page_rects.append(r)
                continue
            col_dets = sorted(
                (
                    d
                    for d in hn_dets
                    if r["y0"] + _GAP < d.y0 < r["y1"] - _GAP
                    and d.x0 < r["x1"]
                    and d.x1 > r["x0"]
                ),
                key=lambda d: d.y0,
            )
            merged = []
            for d in col_dets:
                if merged and d.y0 < merged[-1][1]:
                    merged[-1] = (merged[-1][0], max(merged[-1][1], d.y1))
                else:
                    merged.append((d.y0, d.y1))
            splits = [m[0] for m in merged]
            if not splits:
                new_page_rects.append(r)
                continue
            prev_y = r["y0"]
            for sp in splits:
                if sp - _GAP / 2 > prev_y:
                    new_page_rects.append(
                        {
                            **r,
                            "y0": round(prev_y, 1),
                            "y1": round(sp - _GAP / 2, 1),
                        }
                    )
                prev_y = sp + _GAP / 2
            new_page_rects.append(
                {
                    **r,
                    "y0": round(prev_y, 1),
                    "y1": round(r["y1"], 1),
                }
            )
        page_entry["rects"] = new_page_rects

    Scan.objects.filter(pk=scan_pk).update(
        redaction_rects=json.dumps(rects),
    )
    return rects


def _compute_and_save_margin_rects(
    scan_pk: int, pdf_path: str, output_dir: str
) -> list:
    """Compute margin rects, adjust for detections, and save to the Scan model.

    After computing raw margins from the PDF, shrink any margin rect
    that overlaps with an active detection so that key icons, captions,
    and other content near page edges are not masked.

    :param scan_pk: Primary key of the scan.
    :param pdf_path: Path to the PDF to compute margins for.
    :param output_dir: Directory (used for detection lookup only).
    :return: The computed margin rects list.
    """
    scan = Scan.objects.get(pk=scan_pk)
    if scan.margin_rects:
        return json.loads(scan.margin_rects)
    output_dir = Path(output_dir)
    margin_rects = compute_margin_rects(str(pdf_path))
    margin_rects = _adjust_margins_for_detections(margin_rects, output_dir)
    Scan.objects.filter(pk=scan_pk).update(
        margin_rects=json.dumps(margin_rects),
    )
    return margin_rects


def _build_combined_redactions(scan_pk: int) -> Path:
    """Combine margin_rects, redaction_rects, and opinions into redactions.json.

    All coordinates in the output are in PDF points. This file is passed
    to blackletter's ``generate`` API as the single source of redaction data.

    :param scan_pk: Primary key of the scan.
    :return: Path to the generated redactions.json.
    """
    scan = Scan.objects.get(pk=scan_pk)
    output_dir = Path(scan.output_dir)

    det_path = output_dir / "detections.json"

    margins_data = json.loads(scan.margin_rects) if scan.margin_rects else []
    rects_data = (
        json.loads(scan.redaction_rects) if scan.redaction_rects else []
    )
    opinions = json.loads(scan.opinions_json) if scan.opinions_json else []

    # Add filenames based on reporter, volume, and page numbers
    reporter = scan.reporter.short_name or ""
    volume = str(scan.volume) or ""
    prefix = f"{reporter}.{volume}" if reporter and volume else ""
    for op in opinions:
        if prefix:
            first = op.get("first_page_number", 0)
            last = op.get("last_page_number", first)
            op["filename"] = f"{prefix}.{first:04d}-{last:04d}.pdf"

    # Image dims for pixel→PDF conversion
    img_dims = {}
    if det_path.exists():
        for d in json.loads(det_path.read_text()):
            pi = d["page_index"]
            if pi not in img_dims:
                img_dims[pi] = (d.get("img_width", 1), d.get("img_height", 1))

    # PDF page dims
    pdf_path = find_ocr_pdf(scan.output_dir) or scan.pdf_path
    src = fitz.open(str(pdf_path))
    pdf_dims = {}
    for i in range(src.page_count):
        r = src[i].rect
        pdf_dims[i] = (r.width, r.height)
    src.close()

    pages: dict[int, list] = {}

    # Margin rects (already in PDF points)
    for entry in margins_data:
        pi = entry["page_index"]
        if pi not in pages:
            pages[pi] = []
        for r in entry.get("rects", []):
            pages[pi].append(
                {
                    "x0": round(r["x0"], 1),
                    "y0": round(r["y0"], 1),
                    "x1": round(r["x1"], 1),
                    "y1": round(r["y1"], 1),
                    "fill": "white",
                    "type": "margin",
                }
            )

    # Redaction rects (pixels → PDF points)
    for entry in rects_data:
        pi = entry["page_index"]
        if pi not in pages:
            pages[pi] = []

        iw, ih = img_dims.get(pi, (1, 1))
        pdf_w, pdf_h = pdf_dims.get(pi, (612.0, 792.0))
        to_x = pdf_w / iw if iw > 1 else 1.0
        to_y = pdf_h / ih if ih > 1 else 1.0

        for r in entry.get("rects", []):
            x0 = r["x0"] * to_x
            y0 = r["y0"] * to_y
            x1 = r["x1"] * to_x
            y1 = r["y1"] * to_y
            if x0 >= x1 or y0 >= y1:
                continue
            pages[pi].append(
                {
                    "x0": round(x0, 1),
                    "y0": round(y0, 1),
                    "x1": round(x1, 1),
                    "y1": round(y1, 1),
                    "fill": r["fill"],
                    "type": r["type"],
                }
            )

    combined = {
        "opinions": opinions,
        "pages": {str(k): v for k, v in sorted(pages.items())},
    }

    out_path = output_dir / "redactions.json"
    out_path.write_text(json.dumps(combined))
    n_rects = sum(len(v) for v in pages.values())
    print(
        f"  Combined redactions: {len(pages)} pages, "
        f"{n_rects} rects, {len(opinions)} opinions",
        flush=True,
    )
    return out_path


def _adjust_margins_for_detections(
    margin_rects: list, output_dir: Path
) -> list:
    """Shrink margin rects that overlap with active detections.

    For each page, convert detection bboxes from image coords to PDF
    coords and push back the edge of any margin rect that would cover
    a detection.

    :param margin_rects: List of ``{"page_index": int, "rects": [...]}``
        dicts from ``compute_margin_rects``.
    :param output_dir: Output directory containing detections.json.
    :return: The adjusted margin rects list (modified in place).
    """
    det_path = output_dir / "detections.json"
    if not det_path.exists():
        return margin_rects

    detections = json.loads(det_path.read_text())

    # Labels that should not push back margins (noisy edge detections)
    ignore_labels = {"PAGE_NUMBER", "PAGE_HEADER", "STATE_ABBREVIATION"}

    # Group detections by page_index, skipping ignored labels
    dets_by_page: dict[int, list] = {}
    for d in detections:
        if d.get("label", "") in ignore_labels:
            continue
        dets_by_page.setdefault(d["page_index"], []).append(d)

    padding = 0.0  # extra PDF-pt clearance around detections

    for page_entry in margin_rects:
        if not page_entry["rects"]:
            continue
        page_idx = page_entry["page_index"]
        page_dets = dets_by_page.get(page_idx)
        if not page_dets:
            continue

        # Get image dimensions from the first detection on this page
        img_w = page_dets[0].get("img_width", 1)
        img_h = page_dets[0].get("img_height", 1)
        if not img_w or not img_h:
            continue

        # Determine PDF page size from the margin rects themselves
        # (full-width rects span x0=0 to x1=page_width, etc.)
        pdf_w = max(r["x1"] for r in page_entry["rects"])
        pdf_h = max(r["y1"] for r in page_entry["rects"])
        sx = pdf_w / img_w
        sy = pdf_h / img_h

        # Convert detection bboxes to PDF coords
        pdf_dets = []
        for d in page_dets:
            bb = d["bbox"]
            pdf_dets.append((bb[0] * sx, bb[1] * sy, bb[2] * sx, bb[3] * sy))

        # Adjust each margin rect
        for rect in page_entry["rects"]:
            for dx0, dy0, dx1, dy1 in pdf_dets:
                # Check if detection overlaps this rect
                if (
                    dx0 < rect["x1"]
                    and dx1 > rect["x0"]
                    and dy0 < rect["y1"]
                    and dy1 > rect["y0"]
                ):
                    # Shrink the rect edge that intrudes on the detection
                    # Bottom margin (rect covers lower portion of page)
                    if rect["y0"] > 0 and rect["y1"] >= pdf_h - 1:
                        rect["y0"] = max(rect["y0"], dy1 + padding)
                    # Top margin (rect covers upper portion of page)
                    if rect["y0"] <= 1 and rect["y1"] < pdf_h - 1:
                        rect["y1"] = min(rect["y1"], dy0 - padding)
                    # Left margin (rect covers left portion of page)
                    if rect["x0"] <= 1 and rect["x1"] < pdf_w - 1:
                        rect["x1"] = min(rect["x1"], dx0 - padding)
                    # Right margin (rect covers right portion of page)
                    if (
                        rect["x0"] > 0
                        and rect["y0"] <= 1
                        and rect["y1"] >= pdf_h - 1
                    ):
                        rect["x0"] = max(rect["x0"], dx1 + padding)

    return margin_rects


def _re_pair_opinions(scan_pk: int) -> list:
    """Re-pair opinions from current DB detections.

    :param scan_pk: Primary key of the scan to re-pair.
    :return: The list of paired opinion dicts.
    """
    scan = Scan.objects.get(pk=scan_pk)
    det_data = _sync_detections_to_disk(scan_pk)
    if not det_data:
        return []

    pdf_path = find_ocr_pdf(scan.output_dir) or scan.pdf_path
    opinions = bl_pair(
        det_data,
        str(pdf_path),
        reporter=scan.reporter.short_name or "",
        volume=str(scan.volume) or "",
        first_page=scan.start_page or 1,
    )
    Scan.objects.filter(pk=scan_pk).update(
        opinions_json=json.dumps(opinions),
    )
    return opinions


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def run_paddleocr_validation(scan_pk: int, pdf_path: str) -> None:
    """Validate page numbers using blackletter's analyze_pdf.

    Delegates all OCR/YOLO work to blackletter, keeping only the Django-
    specific parts here: progress updates, cancellation checks, and saving
    results to the DB.  Saves partial ``ocr_results`` every 5 pages so the
    frontend can render the sidebar incrementally.

    :param scan_pk: Primary key of the scan to validate.
    :param pdf_path: Path to the PDF to run validation on.
    """
    scan = Scan.objects.get(pk=scan_pk)
    exp_start = scan.start_page or 1
    exp_end = scan.end_page

    def _progress(current, total, message):
        _update_progress(
            scan_pk,
            message,
            current=current,
            total=total,
        )

    result = bl_analyze_pdf(
        pdf_path,
        exp_start=exp_start,
        exp_end=exp_end,
        num_workers=1,
        progress_callback=_progress,
    )

    all_results = result["results"]
    Scan.objects.filter(pk=scan_pk).update(
        ocr_results=json.dumps(all_results),
    )
    _rebuild_issues_from_results(scan_pk, all_results)


def run_incremental_validation(scan_pk: int, pdf_path: str) -> None:
    """Run YOLO + PaddleOCR on every page via blackletter's analyze_pdf.

    Saves detections to the DB and updates progress incrementally.

    :param scan_pk: Primary key of the scan to validate.
    :param pdf_path: Path to the PDF to run validation on.
    """
    scan = Scan.objects.get(pk=scan_pk)
    exp_start = scan.start_page or 1
    exp_end = scan.end_page

    all_results = []

    def _progress(current, total, message):
        _update_progress(
            scan_pk,
            message,
            current=current,
            total=total,
        )
        # Store partial results for live display
        if current <= len(all_results):
            Scan.objects.filter(pk=scan_pk).update(
                ocr_results=json.dumps(all_results[:current]),
            )

    result = bl_analyze_pdf(
        pdf_path,
        exp_start=exp_start,
        exp_end=exp_end,
        progress_callback=_progress,
    )
    all_results = result["results"]

    # Save detections from each page result
    Detection.objects.filter(scan_id=scan_pk).delete()
    all_detections = []
    for r in all_results:
        page_idx = r["pdf_page"] - 1
        img_w = r.get("img_width", 0)
        img_h = r.get("img_height", 0)
        for d in r.get("detections", []):
            try:
                label_name = Label(d["label_id"]).name
            except (ValueError, KeyError):
                continue
            all_detections.append(
                Detection(
                    scan_id=scan_pk,
                    page_index=page_idx,
                    label=label_name,
                    label_id=d["label_id"],
                    confidence=d["confidence"],
                    x0=d["bbox"][0],
                    y0=d["bbox"][1],
                    x1=d["bbox"][2],
                    y1=d["bbox"][3],
                    img_width=img_w,
                    img_height=img_h,
                    model_name="large",
                    model_count=1,
                    found_by=json.dumps(
                        [{"model": "large", "confidence": d["confidence"]}]
                    ),
                )
            )
    if all_detections:
        Detection.objects.bulk_create(all_detections)

    Scan.objects.filter(pk=scan_pk).update(
        ocr_results=json.dumps(all_results),
    )

    _sync_detections_to_disk(scan_pk)

    _rebuild_issues_from_results(scan_pk, all_results)


# ---------------------------------------------------------------------------
# Recalculate (rebuild issues without re-running OCR)
# ---------------------------------------------------------------------------


def recalculate_issues(scan: "Scan") -> None:
    """Rebuild issues from scan.ocr_results without re-running OCR.

    :param scan: The Scan instance to recalculate issues for.
    """

    ocr_results = json.loads(scan.ocr_results) if scan.ocr_results else []
    if not ocr_results:
        return

    exp_start, exp_end = _parse_expected_range(scan.pdf_path)

    def _split(results):
        oor, s = [], {}
        for r in results:
            if not r["detected"] or r.get("type") == "range":
                continue
            try:
                num = int(r["detected"])
            except ValueError:
                continue
            if num < 1:
                oor.append(r)
            elif exp_start is not None and (
                num < exp_start - 5 or num > exp_end + 5
            ):
                oor.append(r)
            else:
                s.setdefault(num, []).append(r["pdf_page"])
        return oor, s

    out_of_range, seen_nums = _split(ocr_results)

    auto_corrected = []
    if out_of_range and seen_nums:
        in_range_by_page = {
            p: num for num, pages in seen_nums.items() for p in pages
        }
        in_range_sorted = sorted(in_range_by_page.items())
        offsets = {}
        for r in out_of_range:
            p, detected = r["pdf_page"], int(r["detected"])
            before = [(pp, n) for pp, n in in_range_sorted if pp < p]
            after = [(pp, n) for pp, n in in_range_sorted if pp > p]
            if before and after:
                pp_b, n_b = before[-1]
                pp_a, n_a = after[0]
                expected = round(
                    n_b + (n_a - n_b) / max(pp_a - pp_b, 1) * (p - pp_b)
                )
            elif before:
                pp_b, n_b = before[-1]
                expected = n_b + (p - pp_b)
            elif after:
                pp_a, n_a = after[0]
                expected = n_a - (pp_a - p)
            else:
                continue
            offsets[p] = (detected, expected, expected - detected)

        if offsets:
            offset_vals = [v[2] for v in offsets.values()]
            modal_offset, modal_count = Counter(offset_vals).most_common(1)[0]
            if modal_count >= len(offset_vals) * 0.5 and modal_offset != 0:
                to_fix = {
                    p for p, (d, e, o) in offsets.items() if o == modal_offset
                }
                new_results = []
                for r in ocr_results:
                    if r["pdf_page"] in to_fix and r.get("detected"):
                        old_val = r["detected"]
                        r = dict(r)
                        r["detected"] = str(int(old_val) + modal_offset)
                        auto_corrected.append(
                            (r["pdf_page"], old_val, r["detected"])
                        )
                    new_results.append(r)
                ocr_results = new_results
                scan.ocr_results = json.dumps(ocr_results)
                out_of_range, seen_nums = _split(ocr_results)

    out_of_range_pages = {r["pdf_page"] for r in out_of_range}
    all_nums = sorted(seen_nums.keys())
    duplicates = {k: v for k, v in seen_nums.items() if len(v) > 1}

    prev_num = prev_pdf = None
    seq_issues = []
    for r in ocr_results:
        if not r["detected"] or r.get("type") == "range":
            prev_num = None
            continue
        try:
            num = int(r["detected"])
        except ValueError:
            continue
        if r["pdf_page"] in out_of_range_pages:
            continue
        if prev_num is not None:
            diff = num - prev_num
            if diff == 0:
                seq_issues.append(
                    ("DUPLICATE", r["pdf_page"], num, prev_pdf, prev_num)
                )
            elif diff < 0:
                seq_issues.append(
                    ("BACKWARD", r["pdf_page"], num, prev_pdf, prev_num)
                )
            elif diff > 2:
                seq_issues.append(
                    (
                        "GAP",
                        r["pdf_page"],
                        num,
                        prev_pdf,
                        prev_num,
                        list(range(prev_num + 1, num)),
                    )
                )
        prev_num = num
        prev_pdf = r["pdf_page"]

    range_re = re.compile(r"^(\d{1,4})\s*[–\-]\s*(\d{1,4})$")
    range_pages = set()
    ranges_found = [r for r in ocr_results if r.get("type") == "range"]
    for r in ranges_found:
        m = range_re.match(r["detected"].replace("\u2013", "-"))
        if m:
            for pg in range(int(m.group(1)), int(m.group(2)) + 1):
                range_pages.add(pg)

    if exp_start is not None and all_nums:
        missing_pages = sorted(
            (set(range(exp_start, exp_end + 1)) - set(all_nums))
            - range_pages
            - {0}
        )
    elif all_nums:
        missing_pages = sorted(
            (set(range(all_nums[0], all_nums[-1] + 1)) - set(all_nums))
            - range_pages
            - {0}
        )
    else:
        missing_pages = []

    analysis = {
        "results": ocr_results,
        "seq_issues": seq_issues,
        "duplicates": duplicates,
        "seen_nums": seen_nums,
        "all_nums": all_nums,
        "missing_pages": missing_pages,
        "ranges_found": ranges_found,
        "not_detected": [r for r in ocr_results if not r["detected"]],
        "out_of_range": out_of_range,
    }

    result = _build_issues(
        analysis, scan.page_count, exp_start=exp_start, exp_end=exp_end
    )

    for pdf_page, old_val, new_val in auto_corrected:
        result["issues"].append(
            {
                "page_number": pdf_page,
                "check_name": "auto_corrected",
                "severity": "warning",
                "message": (
                    f"PDF page {pdf_page}: OCR read '{old_val}', "
                    f"auto-corrected to '{new_val}' "
                    f"based on surrounding page numbers."
                ),
            }
        )

    pdf_fitz = fitz.open(scan.pdf_path)
    scan.page_count = len(pdf_fitz)
    pdf_fitz.close()

    scan.page_map = json.dumps(result["page_map"])
    scan.missing_pages = json.dumps(result["missing_pages"])
    scan.has_issues = len(result["issues"]) > 0
    scan.status = Status.PENDING_REVIEW
    scan.s3_uploaded = False
    scan.progress_message = "Done"
    scan.save()

    scan.issues.all().delete()
    Issue.objects.bulk_create(
        [Issue(scan=scan, **i) for i in result["issues"]]
    )


# ---------------------------------------------------------------------------
# Validate with bitonal (shared by queue_upload and start_validate)
# ---------------------------------------------------------------------------


def run_validate_with_bitonal(scan_pk: int) -> None:
    """Convert to bitonal (if needed) then run incremental validation.

    Designed to run in the daemon process.

    :param scan_pk: Primary key of the scan to validate.
    """
    django.db.connections.close_all()

    try:
        scan = Scan.objects.get(pk=scan_pk)
    except Exception:
        traceback.print_exc()
        return

    try:
        print(
            f"[validate] scan {scan_pk} pdf_path={scan.pdf_path}", flush=True
        )
        output_dir = ensure_output_dir(scan)
        bitonal_path = _ensure_bitonal(scan, output_dir)
        run_incremental_validation(scan_pk, str(bitonal_path))

    except Exception as exc:
        traceback.print_exc()
        print(f"[validate] ERROR: {exc}", flush=True)
        Scan.objects.filter(pk=scan_pk).update(
            status=Status.ERROR,
            progress_message=str(exc)[:255],
        )


# ---------------------------------------------------------------------------
# Full pipeline (upload and walk away)
# ---------------------------------------------------------------------------


def run_full_pipeline(scan_pk: int) -> None:
    """Run the full processing pipeline: bitonal → OCR → YOLO → validate → pair → rects.

    Designed to run in the daemon process. After this completes, the scan
    is ready for review -- the user only needs to approve and generate.

    :param scan_pk: Primary key of the scan to process.
    """
    django.db.connections.close_all()

    try:
        scan = Scan.objects.get(pk=scan_pk)
    except Exception:
        traceback.print_exc()
        return

    try:
        # 1. Ensure output directory exists
        output_dir = ensure_output_dir(scan)

        # 2. Bitonal conversion
        bitonal_path = _ensure_bitonal(scan, output_dir)

        # 3. Tesseract OCR (on bitonal)
        scan.refresh_from_db()
        ocr_pdf = find_ocr_pdf(str(output_dir))
        if not ocr_pdf:
            ocr_pdf = _run_ocr(
                scan_pk,
                str(bitonal_path),
                str(output_dir),
                reporter=scan.reporter.short_name or "",
                volume=str(scan.volume) or "",
                first_page=scan.start_page or 1,
            )

        # 4. YOLO detection (all 3 models on bitonal)
        _run_yolo(scan_pk, str(bitonal_path), str(output_dir))

        # 5. Import detections into DB
        _update_progress(scan_pk, "Importing detections...")
        dets = _import_detections_from_json(scan_pk, str(output_dir))

        # 6. PaddleOCR validation (on original PDF for better OCR)
        _update_progress(
            scan_pk,
            f"{len(dets)} detections imported. Running page number validation...",
        )
        run_paddleocr_validation(scan_pk, scan.pdf_path)

        # 7. Pair opinions
        _update_progress(scan_pk, "Pairing opinions...")
        opinions = _re_pair_opinions(scan_pk)

        # 8. Compute redaction rects (margin rects computed lazily when viewer requests them)
        _update_progress(scan_pk, "Computing redaction rects...")
        pdf_path = str(ocr_pdf) if ocr_pdf else scan.pdf_path
        _compute_and_save_redaction_rects(scan_pk, pdf_path, str(output_dir))

        Scan.objects.filter(pk=scan_pk).update(
            status=Status.PENDING_REVIEW,
            progress_message=(
                f"Done — {len(dets)} detections, {len(opinions)} opinions"
            ),
        )

    except Exception as exc:
        traceback.print_exc()
        print(f"[pipeline] ERROR: {exc}", flush=True)
        Scan.objects.filter(pk=scan_pk).update(
            status=Status.ERROR,
            progress_message=str(exc)[:255],
        )


# ---------------------------------------------------------------------------
# Reprocess (apply fixes, re-bitonal, re-OCR, re-validate)
# ---------------------------------------------------------------------------


def _rebuild_issues_from_results(scan_pk: int, all_results: list) -> None:
    """Re-analyze ocr_results and rebuild issues/page_map without re-running OCR.

    Preserves the actual OCR results (including manual assignments) but
    recalculates sequence issues, missing pages, duplicates, etc.

    :param scan_pk: Primary key of the scan to rebuild issues for.
    :param all_results: List of per-page OCR result dicts.
    """
    scan = Scan.objects.get(pk=scan_pk)
    total = len(all_results)
    exp_start = scan.start_page or 1
    exp_end = scan.end_page

    out_of_range, seen_nums = _split_in_out_of_range(
        all_results, exp_start, exp_end
    )
    all_results, corrections = _auto_correct(
        all_results, out_of_range, seen_nums
    )
    if corrections:
        out_of_range, seen_nums = _split_in_out_of_range(
            all_results, exp_start, exp_end
        )

    all_nums = sorted(seen_nums.keys())
    duplicates = {k: v for k, v in seen_nums.items() if len(v) > 1}
    not_detected = [x for x in all_results if not x["detected"]]
    out_of_range_pages = {r["pdf_page"] for r in out_of_range}

    seq_issues = []
    prev_num = prev_pdf = None
    for r in all_results:
        if not r["detected"] or r.get("type") == "range":
            prev_num = None
            continue
        try:
            num = int(r["detected"])
        except ValueError:
            continue
        if r["pdf_page"] in out_of_range_pages:
            continue
        if prev_num is not None:
            diff = num - prev_num
            if diff == 0:
                seq_issues.append(
                    ("DUPLICATE", r["pdf_page"], num, prev_pdf, prev_num)
                )
            elif diff < 0:
                seq_issues.append(
                    ("BACKWARD", r["pdf_page"], num, prev_pdf, prev_num)
                )
            elif diff > 2:
                seq_issues.append(
                    (
                        "GAP",
                        r["pdf_page"],
                        num,
                        prev_pdf,
                        prev_num,
                        list(range(prev_num + 1, num)),
                    )
                )
        prev_num = num
        prev_pdf = r["pdf_page"]

    if exp_start is not None and exp_end is not None:
        expected = set(range(exp_start, exp_end + 1))
        found = {n for n in seen_nums if exp_start <= n <= exp_end}
        missing_pages = sorted(expected - found)
    else:
        missing_pages = []

    ranges_found = [r for r in all_results if r.get("type") == "range"]

    analysis = {
        "total_pages": total,
        "results": all_results,
        "seen_nums": seen_nums,
        "all_nums": all_nums,
        "duplicates": duplicates,
        "not_detected": not_detected,
        "seq_issues": seq_issues,
        "missing_pages": missing_pages,
        "ranges_found": ranges_found,
    }

    result = _build_issues(analysis, total, exp_start, exp_end)

    scan.refresh_from_db()
    scan.page_count = total
    scan.page_map = json.dumps(result.get("page_map", []))
    scan.missing_pages = json.dumps(result.get("missing_pages", []))
    scan.ocr_results = json.dumps(all_results)
    scan.has_issues = len(result.get("issues", [])) > 0
    scan.checked = True
    scan.status = Status.PENDING_REVIEW
    scan.s3_uploaded = False
    scan.progress_message = "Done"
    scan.save()

    Issue.objects.filter(scan=scan).exclude(
        check_name="suppress_detection"
    ).delete()
    Issue.objects.bulk_create(
        [Issue(scan=scan, **d) for d in result.get("issues", [])]
    )


def run_reprocess(scan_pk: int) -> None:
    """Apply PDF fixes (inserts/deletions) using smart edits.

    Only OCRs newly inserted pages. Deleted pages are removed from
    ocr_results. Manual page assignments are preserved.

    Designed to run in the daemon process.

    :param scan_pk: Primary key of the scan to reprocess.
    """
    django.db.connections.close_all()

    scan = Scan.objects.get(pk=scan_pk)

    try:
        has_deletions = scan.deletions.exists()
        has_inserts = scan.inserts.exists()

        if not has_deletions and not has_inserts:
            # Nothing changed — just rebuild issues from existing results
            _update_progress(scan_pk, "Re-checking...")
            all_results = (
                json.loads(scan.ocr_results) if scan.ocr_results else []
            )
            _rebuild_issues_from_results(scan_pk, all_results)
            _re_pair_opinions(scan_pk)
            return

        output_dir = Path(scan.output_dir) if scan.output_dir else None
        page_map = json.loads(scan.page_map) if scan.page_map else []
        all_results = json.loads(scan.ocr_results) if scan.ocr_results else []

        # Stale margin/redaction rects are indexed by old page positions — clear them
        Scan.objects.filter(pk=scan_pk).update(
            margin_rects="", redaction_rects=""
        )

        # Apply deletions (process in reverse order to keep indices stable)
        if has_deletions:
            _update_progress(scan_pk, "Deleting pages...")
            deleted_pdf_pages = sorted(
                (d.pdf_page for d in scan.deletions.all()), reverse=True
            )
            for pdf_page in deleted_pdf_pages:
                page_idx = pdf_page - 1
                for pdf_path in _collect_pdf_paths(scan, output_dir):
                    doc = fitz.open(str(pdf_path))
                    if 0 <= page_idx < doc.page_count:
                        doc.delete_page(page_idx)
                        doc.saveIncr()
                    doc.close()

                Detection.objects.filter(
                    scan_id=scan_pk, page_index=page_idx
                ).delete()
                Detection.objects.filter(
                    scan_id=scan_pk, page_index__gt=page_idx
                ).update(page_index=F("page_index") - 1)

                # Remove from ocr_results and shift pdf_page numbers
                all_results = [
                    r for r in all_results if r["pdf_page"] != pdf_page
                ]
                for r in all_results:
                    if r["pdf_page"] > pdf_page:
                        r["pdf_page"] -= 1

            scan.deletions.all().delete()

        # After deletions, rebuild page_map so inserts use correct pdf indices
        if has_deletions:
            Scan.objects.filter(pk=scan_pk).update(
                ocr_results=json.dumps(all_results)
            )
            _rebuild_issues_from_results(scan_pk, all_results)
            scan.refresh_from_db()
            page_map = json.loads(scan.page_map) if scan.page_map else []

        # Apply inserts
        if has_inserts:
            _update_progress(scan_pk, "Inserting pages...")
            inserts = {
                ins.logical_page_number: ins for ins in scan.inserts.all()
            }
            for i, entry in enumerate(page_map):
                if entry.get("type") != "missing":
                    continue
                logical_num = entry.get("logical_number")
                if logical_num not in inserts:
                    continue
                run_smart_insert(
                    scan_pk, logical_num, inserts[logical_num].image.path
                )

            scan.inserts.all().delete()

        # run_smart_insert already handled OCR, detection, validation,
        # re-pairing, and rect computation for each insert. Reload the
        # final state from the DB.
        scan.refresh_from_db()
        all_results = json.loads(scan.ocr_results) if scan.ocr_results else []

        # Final rebuild with the fully updated results
        _update_progress(scan_pk, "Rebuilding issues...")
        _rebuild_issues_from_results(scan_pk, all_results)
        _re_pair_opinions(scan_pk)

        if output_dir:
            pdf_path = find_ocr_pdf(str(output_dir)) or scan.pdf_path
            Scan.objects.filter(pk=scan_pk).update(
                margin_rects="", redaction_rects=""
            )
            _compute_and_save_margin_rects(scan_pk, pdf_path, str(output_dir))
            _compute_and_save_redaction_rects(
                scan_pk, pdf_path, str(output_dir)
            )

        Scan.objects.filter(pk=scan_pk).update(
            status=Status.PENDING_REVIEW,
            s3_uploaded=False,
            progress_message="Done",
        )

    except Exception as exc:
        traceback.print_exc()
        Scan.objects.filter(pk=scan_pk).update(
            status=Status.ERROR,
            progress_message=str(exc)[:255],
        )


# ---------------------------------------------------------------------------
# Smart page edits (patch single pages without full re-run)
# ---------------------------------------------------------------------------


def run_smart_delete(scan_pk: int, pdf_page: int) -> None:
    """Delete a single page and patch detections/validation in place.

    Removes the page from the original PDF, bitonal PDF, and OCR PDF;
    deletes detections on that page; shifts subsequent detection
    page_index values down by 1; re-runs PaddleOCR validation and re-pairs.

    :param scan_pk: Primary key of the scan to edit.
    :param pdf_page: 1-based PDF page number to delete.
    """
    scan = Scan.objects.get(pk=scan_pk)
    page_idx = pdf_page - 1
    output_dir = Path(scan.output_dir) if scan.output_dir else None

    # Remove page from all PDFs
    for pdf_path in _collect_pdf_paths(scan, output_dir):
        doc = fitz.open(str(pdf_path))
        if 0 <= page_idx < doc.page_count:
            doc.delete_page(page_idx)
            doc.saveIncr()
        doc.close()

    # Delete detections on the removed page
    Detection.objects.filter(scan_id=scan_pk, page_index=page_idx).delete()

    # Shift subsequent detections down
    Detection.objects.filter(scan_id=scan_pk, page_index__gt=page_idx).update(
        page_index=F("page_index") - 1
    )

    # Update page count
    pdf = fitz.open(scan.pdf_path)
    new_count = pdf.page_count
    pdf.close()
    Scan.objects.filter(pk=scan_pk).update(page_count=new_count)

    # Sync detections to disk and re-validate/re-pair
    if output_dir:
        _sync_detections_to_disk(scan_pk)

    run_paddleocr_validation(scan_pk, scan.pdf_path)
    _re_pair_opinions(scan_pk)

    if output_dir:
        pdf_path = find_ocr_pdf(str(output_dir)) or scan.pdf_path
        _compute_and_save_redaction_rects(scan_pk, pdf_path, str(output_dir))


def _ocr_single_page(ocr_pdf_path: str, page_idx: int) -> None:
    """Run Tesseract OCR on a single page of a PDF to add a text layer.

    Extracts the page to a temp file, OCRs it, then replaces the page
    in the original PDF with the OCR'd version.

    :param ocr_pdf_path: Path to the OCR PDF to update.
    :param page_idx: Zero-based page index to OCR.
    """
    import tempfile

    import ocrmypdf

    ocr_pdf_path = str(ocr_pdf_path)
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        # Extract the single page
        single_path = tmp / "page.pdf"
        doc = fitz.open(ocr_pdf_path)
        single = fitz.open()
        single.insert_pdf(doc, from_page=page_idx, to_page=page_idx)
        single.save(str(single_path))
        single.close()

        # OCR it
        ocrd_path = tmp / "page_ocr.pdf"
        ocrmypdf.ocr(
            str(single_path),
            str(ocrd_path),
            pdf_renderer="auto",
            optimize=1,
            output_type="pdf",
            language=["eng"],
            tesseract_timeout=120,
            progress_bar=False,
        )

        # Replace the page in the OCR PDF
        ocrd_doc = fitz.open(str(ocrd_path))
        doc.delete_page(page_idx)
        doc.insert_pdf(ocrd_doc, from_page=0, to_page=0, start_at=page_idx)
        doc.saveIncr()
        ocrd_doc.close()
        doc.close()


def run_smart_insert(
    scan_pk: int, logical_page_number: int, image_path: str
) -> None:
    """Insert a page and run detection/validation on just that page.

    Inserts the page image into the original PDF, bitonal PDF, and OCR PDF
    at the correct position; shifts subsequent detection page_index values up
    by 1; re-validates and re-pairs.

    :param scan_pk: Primary key of the scan to edit.
    :param logical_page_number: The logical page number to insert at.
    :param image_path: Path to the image or PDF file to insert.
    """
    scan = Scan.objects.get(pk=scan_pk)
    output_dir = Path(scan.output_dir) if scan.output_dir else None

    # Determine insertion position from page_map
    page_map = json.loads(scan.page_map) if scan.page_map else []
    insert_idx = len(fitz.open(scan.pdf_path))  # default: append
    for i, entry in enumerate(page_map):
        if entry.get("logical_number") == logical_page_number:
            # Insert before the next pdf_page entry
            for j in range(i + 1, len(page_map)):
                if page_map[j].get("type") == "pdf_page":
                    insert_idx = page_map[j]["pdf_index"]
                    break
            break

    # Insert into all PDFs
    for pdf_path in _collect_pdf_paths(scan, output_dir):
        doc = fitz.open(str(pdf_path))
        ref_page = doc[min(insert_idx, doc.page_count - 1)]
        w, h = ref_page.rect.width, ref_page.rect.height

        if str(image_path).lower().endswith(".pdf"):
            insert_pdf = fitz.open(str(image_path))
            doc.insert_pdf(
                insert_pdf,
                from_page=0,
                to_page=insert_pdf.page_count - 1,
                start_at=insert_idx,
            )
            insert_pdf.close()
        else:
            new_page = doc.new_page(pno=insert_idx, width=w, height=h)
            new_page.insert_image(new_page.rect, filename=str(image_path))
        doc.saveIncr()
        doc.close()

    # OCR the inserted page in the OCR PDF so it gets a text layer
    if output_dir:
        ocr_pdf_path = find_ocr_pdf(str(output_dir))
        if ocr_pdf_path:
            _ocr_single_page(ocr_pdf_path, insert_idx)

    # Shift subsequent detections up
    Detection.objects.filter(
        scan_id=scan_pk, page_index__gte=insert_idx
    ).update(page_index=F("page_index") + 1)

    # Update page count
    pdf = fitz.open(scan.pdf_path)
    new_count = pdf.page_count
    pdf.close()
    Scan.objects.filter(pk=scan_pk).update(page_count=new_count)

    # Sync detections to disk
    if output_dir:
        _sync_detections_to_disk(scan_pk)

    # Re-validate, re-pair, recompute rects
    scan.refresh_from_db()
    pdf_path = find_ocr_pdf(str(output_dir)) if output_dir else scan.pdf_path
    run_incremental_validation(scan_pk, pdf_path or scan.pdf_path)
    _re_pair_opinions(scan_pk)

    if output_dir:
        Scan.objects.filter(pk=scan_pk).update(
            margin_rects="", redaction_rects=""
        )
        _compute_and_save_margin_rects(
            scan_pk, pdf_path or scan.pdf_path, str(output_dir)
        )
        _compute_and_save_redaction_rects(
            scan_pk, pdf_path or scan.pdf_path, str(output_dir)
        )


def _collect_pdf_paths(scan: "Scan", output_dir: str | None) -> list[Path]:
    """Collect all PDF paths that need page edits applied.

    :param scan: The Scan instance to collect paths for.
    :param output_dir: Output directory to check for bitonal/OCR PDFs.
    :return: List of Path objects for all relevant PDFs.
    """
    paths = [Path(scan.pdf_path)]
    if output_dir:
        output_dir = Path(output_dir)
        bitonal = output_dir / "bitonal.pdf"
        if bitonal.exists():
            paths.append(bitonal)
        ocr_pdf = find_ocr_pdf(str(output_dir))
        if ocr_pdf and Path(ocr_pdf) not in paths:
            paths.append(Path(ocr_pdf))
    return paths


# ---------------------------------------------------------------------------
# Generate (split into redacted/masked/unredacted opinions)
# ---------------------------------------------------------------------------


def _stamp_original_images(scan: "Scan", ocr_pdf_path: str) -> str:
    """Overlay original-quality image regions onto a *copy* of the OCR'd PDF.

    For each active IMAGE detection, renders the bounding box from the
    original scan PDF and inserts it into a copy of the OCR'd PDF at the
    same position.  This preserves full-quality photographs/illustrations
    that would otherwise be degraded by bitonal conversion.

    :param scan: The Scan instance with the original PDF path.
    :param ocr_pdf_path: Path to the OCR'd PDF to stamp onto.
    :return: Path to the stamped copy, or the original path if no
        images needed stamping (so the OCR PDF is never modified).
    """

    stamped_path = os.path.join(os.path.dirname(ocr_pdf_path), "stamped.pdf")

    image_dets = list(
        Detection.objects.filter(scan=scan, label="IMAGE", active=True)
        .order_by("page_index")
        .values(
            "page_index", "x0", "y0", "x1", "y1", "img_width", "img_height"
        )
    )
    if not image_dets:
        # Always copy so the OCR PDF is never modified by downstream steps
        shutil.copy2(ocr_pdf_path, stamped_path)
        return stamped_path

    original_doc = fitz.open(scan.pdf_path)
    ocr_doc = fitz.open(ocr_pdf_path)

    # Save extracted images to images/ directory
    images_dir = Path(os.path.dirname(ocr_pdf_path)) / "images"
    images_dir.mkdir(exist_ok=True)
    page_numbers = _page_number_lookup(scan)
    img_count_by_page: dict[int, int] = {}

    try:
        for det in image_dets:
            page_idx = det["page_index"]
            if (
                page_idx >= original_doc.page_count
                or page_idx >= ocr_doc.page_count
            ):
                continue

            orig_page = original_doc[page_idx]
            ocr_page = ocr_doc[page_idx]

            # Convert image-pixel bbox to PDF points
            page_rect = orig_page.rect
            img_w = det["img_width"] or 1
            img_h = det["img_height"] or 1
            sx = page_rect.width / img_w
            sy = page_rect.height / img_h

            pdf_rect = fitz.Rect(
                det["x0"] * sx,
                det["y0"] * sy,
                det["x1"] * sx,
                det["y1"] * sy,
            )

            # Render the region from the original (non-bitonal) PDF
            pix = orig_page.get_pixmap(clip=pdf_rect, dpi=150)
            png_bytes = pix.tobytes("png")

            # Stamp onto the OCR'd PDF
            ocr_page.insert_image(pdf_rect, stream=png_bytes)

            # Save image to images/ directory
            pn = page_numbers.get(page_idx)
            page_num = pn[0] if pn else page_idx + (scan.start_page or 1)
            img_count_by_page[page_idx] = (
                img_count_by_page.get(page_idx, 0) + 1
            )
            img_name = f"{page_num}-{img_count_by_page[page_idx]:03d}.png"
            (images_dir / img_name).write_bytes(png_bytes)

        ocr_doc.save(stamped_path, garbage=3, deflate=True)
    finally:
        original_doc.close()
        ocr_doc.close()
    return stamped_path


def run_generate_files(scan_pk: int) -> None:
    """Generate redacted/split opinion files from existing detections.

    Designed to run in the daemon process.

    :param scan_pk: Primary key of the scan to generate files for.
    """
    django.db.connections.close_all()

    scan = Scan.objects.get(pk=scan_pk)

    try:
        Scan.objects.filter(pk=scan_pk).update(
            progress_message="Generating files...", progress_log=""
        )

        output_base = Path(settings.MEDIA_ROOT) / "processed" / str(scan_pk)

        ocr_pdf = find_ocr_pdf(scan.output_dir) if scan.output_dir else None
        if not ocr_pdf:
            raise ValueError("No OCR'd PDF found in output directory")

        # Stamp original-quality images into a copy — leaves OCR PDF untouched
        gen_pdf = _stamp_original_images(scan, str(ocr_pdf))

        # Write current DB detections -> detections.json (includes page numbers)
        det_data = _sync_detections_to_disk(scan_pk)
        Scan.objects.filter(pk=scan_pk).update(
            progress_message=f"Generating files ({len(det_data or [])} detections)..."
        )

        # Build combined redactions.json (margins + redaction rects + opinions)
        Scan.objects.filter(pk=scan_pk).update(
            progress_message="Building combined redactions...",
        )
        redactions_path = _build_combined_redactions(scan_pk)

        output = Path(scan.output_dir if scan.output_dir else str(output_base))

        Scan.objects.filter(pk=scan_pk).update(
            progress_message="Generating files...",
        )

        from blackletter.api import generate as bl_generate

        result = bl_generate(
            pdf_path=str(gen_pdf),
            redactions=str(redactions_path),
            output_dir=output,
            reporter=scan.reporter.short_name or "",
            volume=str(scan.volume) or "",
            unredacted=True,
        )

        opinion_count = result.get("opinion_count", 0)
        full_redacted = result.get("full_redacted", "")
        redacted_dir = Path(result.get("redacted_dir", output / "redacted"))
        masked_dir = Path(result.get("masked_dir", output / "masked"))
        unredacted_dir = output / "unredacted"
        output_dir = redacted_dir.parent

        redacted_files = (
            sorted(redacted_dir.glob("*.pdf")) if redacted_dir.is_dir() else []
        )

        scan.refresh_from_db()
        existing_opinions = (
            json.loads(scan.opinions_json) if scan.opinions_json else []
        )

        if existing_opinions and "caption_page" in existing_opinions[0]:
            for i, op in enumerate(existing_opinions):
                if i < len(redacted_files):
                    op["filename"] = redacted_files[i].name
        else:
            existing_opinions = []
            for f in redacted_files:
                existing_opinions.append(
                    {"filename": f.name, "first_page": 0, "last_page": 0}
                )

        scan.redacted_pdf_path = str(full_redacted) if full_redacted else ""
        scan.opinions_json = json.dumps(existing_opinions)
        scan.stage = Stage.APPROVED
        scan.status = Status.APPROVED
        scan.s3_uploaded = False
        scan.progress_message = f"Generated {opinion_count} opinions"
        scan.progress_log = ""
        scan.save()

        OpinionScan.objects.filter(scan=scan).delete()
        LLMScan.objects.filter(scan=scan).delete()
        for i, op in enumerate(existing_opinions):
            page_start = op.get("first_page_number", 1)
            page_end = op.get("last_page_number", page_start)
            fname = op.get("filename", "")
            opinion = OpinionScan.objects.create(
                scan=scan,
                reporter=scan.reporter,
                volume=scan.volume,
                opinion_order=i,
                page_start=page_start or 1,
                page_end=page_end or page_start or 1,
                caption_page_index=op.get("caption_page"),
                key_page_index=op.get("key_page"),
                has_image=op.get("has_image", False),
                status=OpinionStatus.OK,
                uploaded_by=scan.uploaded_by,
            )
            if fname:
                media_root = Path(settings.MEDIA_ROOT).resolve()
                rp = redacted_dir / fname
                if rp.exists():
                    opinion.redacted_pdf.name = str(
                        rp.resolve().relative_to(media_root)
                    )
                up = (
                    unredacted_dir / fname if unredacted_dir.exists() else None
                )
                if up and up.exists():
                    opinion.original_pdf.name = str(
                        up.resolve().relative_to(media_root)
                    )
                masked_fname = re.sub(
                    r"(\d{4})-\d{1,3}\.pdf$", r"\1.pdf", fname
                )
                mp = masked_dir / masked_fname if masked_dir.exists() else None
                if mp and mp.exists():
                    opinion.masked_pdf.name = str(
                        mp.resolve().relative_to(media_root)
                    )
                opinion.save()

            if opinion.masked_pdf.name:
                llm_scan = LLMScan.objects.create(
                    scan=scan,
                    masked_pdf=opinion.masked_pdf.name,
                    status=LLMScan.Status.PENDING,
                )
                llm_scan.opinions.add(opinion)

    except Exception as exc:
        traceback.print_exc()
        Scan.objects.filter(pk=scan_pk).update(
            status=Status.ERROR,
            progress_message=str(exc)[:255],
        )


# ---------------------------------------------------------------------------
# Detect (run YOLO detection + opinion pairing)
# ---------------------------------------------------------------------------


def run_detect(scan_pk: int) -> None:
    """Run YOLO detection and pair opinions.

    Designed to run in the daemon process.

    :param scan_pk: Primary key of the scan to detect on.
    """
    django.db.connections.close_all()

    scan = Scan.objects.get(pk=scan_pk)
    try:
        output_dir = Path(scan.output_dir)
        bitonal = output_dir / "bitonal.pdf"

        # OCR if needed (no existing OCR PDF)
        if not find_ocr_pdf(str(output_dir)) and bitonal.exists():
            _run_ocr(
                scan_pk,
                str(bitonal),
                str(output_dir),
                reporter=scan.reporter.short_name or "",
                volume=str(scan.volume) or "",
                first_page=scan.start_page or 1,
            )

        pdf_path = str(bitonal) if bitonal.exists() else scan.pdf_path

        _run_yolo(scan_pk, pdf_path, str(output_dir))
        dets = _import_detections_from_json(scan_pk, str(output_dir))

        _update_progress(
            scan_pk,
            f"{len(dets)} detections. Pairing opinions...",
        )

        opinions = _re_pair_opinions(scan_pk)

        Scan.objects.filter(pk=scan_pk).update(
            status=Status.PENDING_REVIEW,
            s3_uploaded=False,
            progress_message=f"Done — {len(dets)} detections, {len(opinions)} opinions",
        )

    except Exception as exc:
        traceback.print_exc()
        Scan.objects.filter(pk=scan_pk).update(
            status=Status.ERROR,
            progress_message=str(exc)[:255],
        )


# ---------------------------------------------------------------------------
# S3 upload of approved files
# ---------------------------------------------------------------------------


def upload_approved_files(scan_pk: int) -> str:
    """Upload final deliverables to the private S3 bucket.

    Uploads redacted/, masked/ opinion PDFs and the original + redacted
    full PDFs to ``approved/{reporter}/{volume}/{start_page}/``.

    Skips the upload (with a message) if the scan was already uploaded
    or if no AWS credentials are configured.

    :param scan_pk: Primary key of the scan to upload.
    :return: A user-facing message describing the result.
    :rtype: str
    """
    scan = Scan.objects.get(pk=scan_pk)

    if scan.s3_uploaded and scan.s3_path:
        return f"Files were already uploaded to S3 ({scan.s3_path})."

    output_dir = Path(scan.output_dir)

    # Verify final files exist
    redacted_dir = output_dir / "redacted"
    masked_dir = output_dir / "masked"
    if not redacted_dir.is_dir() or not list(redacted_dir.glob("*.pdf")):
        return "Before approving you need to generate the files."

    # Build S3 prefix
    short = scan.reporter.short_name
    start = scan.start_page or 1
    s3_prefix = f"approved/{short}/{scan.volume}/{start}/"

    if not has_s3_credentials():
        Scan.objects.filter(pk=scan_pk).update(
            s3_path=s3_prefix,
        )
        return (
            "No AWS credentials configured, skipping S3 upload. "
            "Path would be: " + s3_prefix
        )

    # Collect files to upload
    files_to_upload = []

    for pdf in redacted_dir.glob("*.pdf"):
        files_to_upload.append((pdf, f"{s3_prefix}redacted/{pdf.name}"))

    if masked_dir.is_dir():
        for pdf in masked_dir.glob("*.pdf"):
            files_to_upload.append((pdf, f"{s3_prefix}masked/{pdf.name}"))

    # Original and redacted full PDFs
    for f in output_dir.iterdir():
        if f.is_file() and f.suffix == ".pdf":
            if ".original.pdf" in f.name:
                files_to_upload.append((f, f"{s3_prefix}{f.name}"))
            elif ".redacted.pdf" in f.name:
                files_to_upload.append((f, f"{s3_prefix}{f.name}"))

    # Upload to S3 via boto3 directly (not Django storage).
    # upload_file() streams from disk with automatic multipart.
    # The early return above prevents duplicate uploads when
    # s3_uploaded is already True, so we always upload all files here.
    import boto3

    bucket = settings.AWS_PRIVATE_STORAGE_BUCKET_NAME
    s3_client = boto3.client("s3")
    for local_path, s3_key in files_to_upload:
        s3_client.upload_file(str(local_path), bucket, s3_key)

    Scan.objects.filter(pk=scan_pk).update(
        s3_uploaded=True,
        s3_path=s3_prefix,
    )
    return f"Files uploaded successfully to S3 ({len(files_to_upload)} files)."
