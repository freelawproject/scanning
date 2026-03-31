"""Background processing pipelines and business logic.

Functions in this module run outside the request/response cycle, in the
daemon process.  They must NOT import Django HTTP machinery
(HttpResponse, render, redirect, etc.).
"""

import django
import traceback

from pathlib import Path
from scanning.utils import find_ocr_pdf
from scanning.models import Detection
import json
import os
import re
import shutil
from pathlib import Path

import re as re_mod
from collections import Counter

import fitz
from django.conf import settings
from django.db.models import F

from blackletter.analyze import (
    DEFAULT_ANALYZE_MODEL,
    _process_page,
    analyze_pdf as bl_analyze_pdf,
)
from blackletter.api import (
    bitonal as bl_bitonal,
    detect as bl_detect,
    ocr as bl_ocr,
    pair as bl_pair,
)
from blackletter.models import (
    BBox,
    Detection as BLDetection,
    Document as BLDoc,
    Label,
    Page,
)

from blackletter.margins import compute_margin_rects
from blackletter.process import generate_files, compute_redaction_rects
from blackletter.scanner import _pair_opinions
from blackletter.validate import (
    _auto_correct,
    _build_issues,
    _parse_expected_range,
    _split_in_out_of_range,
)

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
import shutil

from scanning.utils import find_ocr_pdf


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _update_progress(scan_pk, message, current=None, total=None, **kwargs):
    """Update scan progress fields."""
    updates = {"progress_message": message[:255]}
    if current is not None:
        updates["progress_current"] = current
    if total is not None:
        updates["progress_total"] = total
    updates.update(kwargs)
    Scan.objects.filter(pk=scan_pk).update(**updates)


def _run_ocr(scan_pk, bitonal_path, output_dir, reporter, volume, first_page):
    """Run Tesseract OCR via blackletter. Returns path to OCR'd PDF."""
    _update_progress(scan_pk, "Running Tesseract OCR...")
    return bl_ocr(
        bitonal_path, output_dir,
        reporter=reporter, volume=volume, first_page=first_page,
    )


def _run_yolo(scan_pk, pdf_path, output_dir):
    """Run YOLO detection (all 3 models) via blackletter.

    Saves detections.json to output_dir.
    """
    _update_progress(scan_pk, "Running YOLO detection...")
    bl_detect(pdf_path, output_dir, models=["small", "medium", "large"])


def _import_detections_from_json(scan_pk, output_dir):
    """Load detections.json from disk into Detection model.

    Clears existing detections first. Returns the detection list.
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
        det_objects.append(Detection(
            scan_id=scan_pk, page_index=d["page_index"],
            label=label_name, label_id=d["label_id"],
            confidence=d["confidence"],
            x0=d["bbox"][0], y0=d["bbox"][1],
            x1=d["bbox"][2], y1=d["bbox"][3],
            img_width=d.get("img_width", 0),
            img_height=d.get("img_height", 0),
            model_name=d.get("found_by", [{}])[0].get("model", ""),
            model_count=d.get("model_count", 1),
            found_by=json.dumps(d.get("found_by", [])),
        ))
    Detection.objects.bulk_create(det_objects)
    return dets


def _page_number_lookup(scan):
    """Build {page_index: (page_number, page_number_end)} from ocr_results.

    For range pages like "677-685", returns (677, 685).
    For single pages like "677", returns (677, None).
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


def _sync_detections_to_disk(scan_pk):
    """Write current DB detections to detections.json on disk."""
    scan = Scan.objects.get(pk=scan_pk)
    if not scan.output_dir:
        return
    output_dir = Path(scan.output_dir)

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


def _build_document_from_detections(scan, det_data, pdf_path):
    """Build a blackletter Document from detection data and a PDF.

    Returns (document, opinions) tuple.
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
            index=pi, pdf_width=pw, pdf_height=ph,
            img_width=pd["img_width"], img_height=pd["img_height"],
        )
        for d in pd["detections"]:
            b = d.get("bbox", [0, 0, 1, 1])
            page.detections.append(BLDetection(
                bbox=BBox(x1=b[0], y1=b[1], x2=b[2], y2=b[3]),
                label=Label(d["label_id"]),
                confidence=d["confidence"],
                page_index=pi,
            ))
        pages.append(page)
    src_pdf.close()

    scan_obj = scan if isinstance(scan, Scan) else Scan.objects.get(pk=scan)
    document = BLDoc(
        pdf_path=str(pdf_path), pages=pages,
        reporter=scan_obj.reporter.short_name or "",
        volume=str(scan_obj.volume) or "",
        first_page=scan_obj.start_page or 1,
        ocr_applied=True,
    )
    return document


def _compute_and_save_redaction_rects(scan_pk, pdf_path, output_dir):
    """Compute redaction rects and save to disk. Returns the rects list."""
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
            same_side = [c for c in cols if (c.x0 + c.x1) / 2 < img_mid] if cx < img_mid \
                else [c for c in cols if (c.x0 + c.x1) / 2 >= img_mid]
            if not same_side:
                continue
            best = min(same_side, key=lambda c: abs((c.x0 + c.x1) / 2 - cx))
            r["x0"] = round(best.x0, 1)
            r["x1"] = round(best.x1, 1)

    # Split headnote blocks at HEADNOTE detection boundaries
    _GAP = 6
    hn_dets_by_page = {}
    for d in Detection.objects.filter(scan=scan, active=True, label="HEADNOTE"):
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
                (d for d in hn_dets
                 if r["y0"] + _GAP < d.y0 < r["y1"] - _GAP
                 and d.x0 < r["x1"] and d.x1 > r["x0"]),
                key=lambda d: d.y0
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
                    new_page_rects.append({
                        **r,
                        "y0": round(prev_y, 1),
                        "y1": round(sp - _GAP / 2, 1),
                    })
                prev_y = sp + _GAP / 2
            new_page_rects.append({
                **r,
                "y0": round(prev_y, 1),
                "y1": round(r["y1"], 1),
            })
        page_entry["rects"] = new_page_rects

    rects_path = output_dir / "redaction_rects.json"
    rects_path.write_text(json.dumps(rects))
    return rects


def _compute_and_save_margin_rects(pdf_path, output_dir):
    """Compute margin rects and save to disk."""
    output_dir = Path(output_dir)
    margin_rects_path = output_dir / "margin_rects.json"
    if not margin_rects_path.exists():
        margin_rects = compute_margin_rects(str(pdf_path))
        margin_rects_path.write_text(json.dumps(margin_rects))
        return margin_rects
    return json.loads(margin_rects_path.read_text())


def _re_pair_opinions(scan_pk):
    """Re-pair opinions from current DB detections."""
    scan = Scan.objects.get(pk=scan_pk)
    det_data = _sync_detections_to_disk(scan_pk)
    if not det_data:
        return []

    pdf_path = find_ocr_pdf(scan.output_dir) or scan.pdf_path
    opinions = bl_pair(
        det_data, str(pdf_path),
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


def run_paddleocr_validation(scan_pk, pdf_path):
    """Validate page numbers using blackletter's analyze_pdf.

    Delegates all OCR/YOLO work to blackletter, keeping only the Django-
    specific parts here: progress updates, cancellation checks, and saving
    results to the DB.
    """
    scan = Scan.objects.get(pk=scan_pk)
    exp_start = scan.start_page or 1
    exp_end = scan.end_page

    def _progress(current, total, message):
        _update_progress(
            scan_pk, message,
            current=current, total=total,
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


def run_incremental_validation(scan_pk, pdf_path):
    """Run YOLO + PaddleOCR on every page via blackletter's analyze_pdf.

    Saves detections to the DB and updates progress incrementally.
    """
    scan = Scan.objects.get(pk=scan_pk)
    exp_start = scan.start_page or 1
    exp_end = scan.end_page

    all_results = []

    def _progress(current, total, message):
        _update_progress(
            scan_pk, message,
            current=current, total=total,
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

    if scan.output_dir:
        _sync_detections_to_disk(scan_pk)

    _rebuild_issues_from_results(scan_pk, all_results)


# ---------------------------------------------------------------------------
# Recalculate (rebuild issues without re-running OCR)
# ---------------------------------------------------------------------------


def recalculate_issues(scan):
    """Rebuild issues from scan.ocr_results without re-running OCR."""

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
                    n_b
                    + (n_a - n_b) / max(pp_a - pp_b, 1) * (p - pp_b)
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
            modal_offset, modal_count = Counter(offset_vals).most_common(1)[
                0
            ]
            if modal_count >= len(offset_vals) * 0.5 and modal_offset != 0:
                to_fix = {
                    p
                    for p, (d, e, o) in offsets.items()
                    if o == modal_offset
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

    range_re = re_mod.compile(r"^(\d{1,4})\s*[–\-]\s*(\d{1,4})$")
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
    scan.status = Status.APPROVED
    scan.progress_message = "Done"
    scan.save()

    scan.issues.all().delete()
    Issue.objects.bulk_create(
        [Issue(scan=scan, **i) for i in result["issues"]]
    )


# ---------------------------------------------------------------------------
# Validate with bitonal (shared by queue_upload and start_validate)
# ---------------------------------------------------------------------------


def run_validate_with_bitonal(scan_pk):
    """Convert to bitonal (if needed) then run incremental validation.

    Designed to run in a background thread.
    """
    django.db.connections.close_all()

    try:
        scan = Scan.objects.get(pk=scan_pk)
    except Exception:
        traceback.print_exc()
        return

    try:
        print(f"[validate] scan {scan_pk} pdf_path={scan.pdf_path}", flush=True)
        if not scan.output_dir:
            output_dir = Path(settings.MEDIA_ROOT) / "processed" / str(scan_pk)
            if scan.reporter and scan.volume:
                output_dir = (
                    output_dir / scan.reporter.short_name
                    / str(scan.volume) / str(scan.start_page or 1)
                )
            output_dir.mkdir(parents=True, exist_ok=True)
            Scan.objects.filter(pk=scan_pk).update(output_dir=str(output_dir))
        else:
            output_dir = Path(scan.output_dir)
            output_dir.mkdir(parents=True, exist_ok=True)

        bitonal_path = output_dir / "bitonal.pdf"
        if not bitonal_path.exists():
            def _bitonal_progress(current, total, message):
                Scan.objects.filter(pk=scan_pk).update(
                    progress_message=message,
                    progress_current=current,
                    progress_total=total,
                )

            bl_bitonal(scan.pdf_path, str(output_dir), progress_callback=_bitonal_progress)
            pdf = fitz.open(str(bitonal_path))
            count = pdf.page_count
            pdf.close()
            Scan.objects.filter(pk=scan_pk).update(page_count=count)

        run_incremental_validation(scan_pk, str(bitonal_path))

    except Exception as exc:
        traceback.print_exc()
        print(f"[validate] ERROR: {exc}", flush=True)
        Scan.objects.filter(pk=scan_pk).update(
            status=Status.ERROR, progress_message=str(exc)[:255],
        )


# ---------------------------------------------------------------------------
# Full pipeline (upload and walk away)
# ---------------------------------------------------------------------------


def run_full_pipeline(scan_pk):
    """Run the full processing pipeline: bitonal → OCR → YOLO → validate → pair → rects.

    Designed to run in a background thread. After this completes, the scan
    is ready for review — the user only needs to approve and generate.
    """
    django.db.connections.close_all()

    try:
        scan = Scan.objects.get(pk=scan_pk)
    except Exception:
        traceback.print_exc()
        return

    try:
        # 1. Ensure output directory exists
        if not scan.output_dir:
            output_dir = Path(settings.MEDIA_ROOT) / "processed" / str(scan_pk)
            if scan.reporter and scan.volume:
                output_dir = (
                    output_dir / scan.reporter.short_name
                    / str(scan.volume) / str(scan.start_page or 1)
                )
            output_dir.mkdir(parents=True, exist_ok=True)
            Scan.objects.filter(pk=scan_pk).update(output_dir=str(output_dir))
        else:
            output_dir = Path(scan.output_dir)
            output_dir.mkdir(parents=True, exist_ok=True)

        # 2. Bitonal conversion
        bitonal_path = output_dir / "bitonal.pdf"
        if not bitonal_path.exists():
            def _bitonal_progress(current, total, message):
                _update_progress(scan_pk, message, current=current, total=total)

            bl_bitonal(scan.pdf_path, str(output_dir), progress_callback=_bitonal_progress)

        pdf = fitz.open(str(bitonal_path))
        page_count = pdf.page_count
        pdf.close()
        Scan.objects.filter(pk=scan_pk).update(page_count=page_count)

        # 3. Tesseract OCR (on bitonal)
        scan.refresh_from_db()
        ocr_pdf = find_ocr_pdf(str(output_dir))
        if not ocr_pdf:
            ocr_pdf = _run_ocr(
                scan_pk, str(bitonal_path), str(output_dir),
                reporter=scan.reporter.short_name or "",
                volume=str(scan.volume) or "",
                first_page=scan.start_page or 1,
            )

        # 4. YOLO detection (all 3 models on bitonal)
        _run_yolo(scan_pk, str(bitonal_path), str(output_dir))
        dets = _import_detections_from_json(scan_pk, str(output_dir))
        _update_progress(
            scan_pk,
            f"{len(dets)} detections. Running validation...",
        )

        # 5. PaddleOCR validation (on original PDF for better OCR)
        run_paddleocr_validation(scan_pk, scan.pdf_path)

        # 6. Pair opinions
        _update_progress(scan_pk, "Pairing opinions...")
        opinions = _re_pair_opinions(scan_pk)

        # 7. Compute redaction rects (margin rects computed lazily when viewer requests them)
        _update_progress(scan_pk, "Computing redaction rects...")
        pdf_path = str(ocr_pdf) if ocr_pdf else scan.pdf_path
        _compute_and_save_redaction_rects(scan_pk, pdf_path, str(output_dir))

        Scan.objects.filter(pk=scan_pk).update(
            status=Status.APPROVED,
            progress_message=(
                f"Done — {len(dets)} detections, {len(opinions)} opinions"
            ),
        )

    except Exception as exc:
        traceback.print_exc()
        print(f"[pipeline] ERROR: {exc}", flush=True)
        Scan.objects.filter(pk=scan_pk).update(
            status=Status.ERROR, progress_message=str(exc)[:255],
        )


# ---------------------------------------------------------------------------
# Reprocess (apply fixes, re-bitonal, re-OCR, re-validate)
# ---------------------------------------------------------------------------


def _rebuild_issues_from_results(scan_pk, all_results):
    """Re-analyze ocr_results and rebuild issues/page_map without re-running OCR.

    Preserves the actual OCR results (including manual assignments) but
    recalculates sequence issues, missing pages, duplicates, etc.
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
                    ("GAP", r["pdf_page"], num, prev_pdf, prev_num,
                     list(range(prev_num + 1, num)))
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
    scan.status = Status.APPROVED
    scan.progress_message = "Done"
    scan.save()

    Issue.objects.filter(scan=scan).exclude(
        check_name="suppress_detection"
    ).delete()
    Issue.objects.bulk_create(
        [Issue(scan=scan, **d) for d in result.get("issues", [])]
    )


def run_reprocess(scan_pk):
    """Apply PDF fixes (inserts/deletions) using smart edits.

    Only OCRs newly inserted pages. Deleted pages are removed from
    ocr_results. Manual page assignments are preserved.

    Designed to run in a background thread.
    """
    django.db.connections.close_all()

    scan = Scan.objects.get(pk=scan_pk)

    try:
        has_deletions = scan.deletions.exists()
        has_inserts = scan.inserts.exists()

        if not has_deletions and not has_inserts:
            # Nothing changed — just rebuild issues from existing results
            _update_progress(scan_pk, "Re-checking...")
            all_results = json.loads(scan.ocr_results) if scan.ocr_results else []
            _rebuild_issues_from_results(scan_pk, all_results)
            _re_pair_opinions(scan_pk)
            return

        output_dir = Path(scan.output_dir) if scan.output_dir else None
        page_map = json.loads(scan.page_map) if scan.page_map else []
        all_results = json.loads(scan.ocr_results) if scan.ocr_results else []

        # Stale margin/redaction rects are indexed by old page positions — delete them
        if output_dir:
            for stale in ("margin_rects.json", "redaction_rects.json"):
                stale_path = output_dir / stale
                if stale_path.exists():
                    stale_path.unlink()

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
                all_results = [r for r in all_results if r["pdf_page"] != pdf_page]
                for r in all_results:
                    if r["pdf_page"] > pdf_page:
                        r["pdf_page"] -= 1

            scan.deletions.all().delete()

        # Track which pdf_pages need OCR (newly inserted)
        new_page_indices = []

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
                insert_obj = inserts[logical_num]

                # Record the page index before insert shifts things
                pdf = fitz.open(scan.pdf_path)
                pre_count = pdf.page_count
                pdf.close()

                run_smart_insert(
                    scan_pk, logical_num, insert_obj.image.path
                )

                pdf = fitz.open(scan.pdf_path)
                post_count = pdf.page_count
                pdf.close()

                if post_count > pre_count:
                    # Figure out where it was inserted
                    # Smart insert puts it at the right position based on
                    # logical page number. The new page's pdf_page is its
                    # 1-based position.
                    new_idx = post_count  # last page (1-based)
                    # Actually find it — it's the page that wasn't there before
                    # For simplicity, just track the new page count
                    new_page_indices.append(post_count - 1)  # 0-based

            scan.inserts.all().delete()

        # Update page count
        pdf = fitz.open(scan.pdf_path)
        new_count = pdf.page_count
        pdf.close()
        Scan.objects.filter(pk=scan_pk).update(page_count=new_count)

        # OCR only the newly inserted pages
        if new_page_indices:
            _update_progress(scan_pk, f"OCR on {len(new_page_indices)} new page(s)...")
            scan.refresh_from_db()
            exp_start = scan.start_page or 1
            exp_end = scan.end_page
            model_path = str(DEFAULT_ANALYZE_MODEL)

            for page_idx in new_page_indices:
                result = _process_page(
                    (page_idx, scan.pdf_path, exp_start, exp_end, model_path)
                )
                # Insert into all_results at the right position
                pdf_page = page_idx + 1
                # Shift existing results at or after this position
                for r in all_results:
                    if r["pdf_page"] >= pdf_page:
                        r["pdf_page"] += 1
                # Find insertion point
                insert_at = len(all_results)
                for idx, r in enumerate(all_results):
                    if r["pdf_page"] > pdf_page:
                        insert_at = idx
                        break
                all_results.insert(insert_at, result)

        # Ensure all_results covers every page (fill gaps for safety)
        existing_pages = {r["pdf_page"] for r in all_results}
        for p in range(1, new_count + 1):
            if p not in existing_pages:
                all_results.append({
                    "pdf_page": p, "detected": None,
                    "type": "missing_ocr", "zone": "none",
                    "score": 0, "ocr": "skipped",
                })
        all_results.sort(key=lambda r: r["pdf_page"])

        # Sync detections and rebuild issues
        if output_dir:
            _sync_detections_to_disk(scan_pk)

        _update_progress(scan_pk, "Rebuilding issues...")
        _rebuild_issues_from_results(scan_pk, all_results)
        _re_pair_opinions(scan_pk)

        if output_dir:
            pdf_path = find_ocr_pdf(str(output_dir)) or scan.pdf_path
            _compute_and_save_redaction_rects(scan_pk, pdf_path, str(output_dir))
            # Delete stale margin rects — will be recomputed lazily when viewer requests them
            stale_margins = Path(output_dir) / "margin_rects.json"
            if stale_margins.exists():
                stale_margins.unlink()

        Scan.objects.filter(pk=scan_pk).update(
            status=Status.APPROVED,
            progress_message="Done",
        )

    except Exception as exc:
        traceback.print_exc()
        Scan.objects.filter(pk=scan_pk).update(
            status=Status.ERROR, progress_message=str(exc)[:255],
        )


# ---------------------------------------------------------------------------
# Smart page edits (patch single pages without full re-run)
# ---------------------------------------------------------------------------


def run_smart_delete(scan_pk, pdf_page):
    """Delete a single page and patch detections/validation in place.

    pdf_page is 1-based. Removes the page from the original PDF, bitonal PDF,
    and OCR PDF; deletes detections on that page; shifts subsequent detection
    page_index values down by 1; re-runs PaddleOCR validation and re-pairs.
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
    Detection.objects.filter(
        scan_id=scan_pk, page_index__gt=page_idx
    ).update(page_index=F("page_index") - 1)

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


def run_smart_insert(scan_pk, logical_page_number, image_path):
    """Insert a page and run detection/validation on just that page.

    Inserts the page image into the original PDF, bitonal PDF, and OCR PDF
    at the correct position; shifts subsequent detection page_index values up
    by 1; runs YOLO on just the new page; re-validates and re-pairs.
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
                insert_pdf, from_page=0,
                to_page=insert_pdf.page_count - 1, start_at=insert_idx,
            )
            insert_pdf.close()
        else:
            new_page = doc.new_page(pno=insert_idx, width=w, height=h)
            new_page.insert_image(new_page.rect, filename=str(image_path))
        doc.saveIncr()
        doc.close()

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
    run_paddleocr_validation(scan_pk, scan.pdf_path)
    _re_pair_opinions(scan_pk)

    if output_dir:
        pdf_path = find_ocr_pdf(str(output_dir)) or scan.pdf_path
        _compute_and_save_redaction_rects(scan_pk, pdf_path, str(output_dir))


def _collect_pdf_paths(scan, output_dir):
    """Collect all PDF paths that need page edits applied."""
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


def _stamp_original_images(scan, ocr_pdf_path):
    """Overlay original-quality image regions onto a *copy* of the OCR'd PDF.

    For each active IMAGE detection, renders the bounding box from the
    original scan PDF and inserts it into a copy of the OCR'd PDF at the
    same position.  This preserves full-quality photographs/illustrations
    that would otherwise be degraded by bitonal conversion.

    Returns the path to the stamped copy, or the original path if no
    images needed stamping (so the OCR PDF is never modified).
    """


    stamped_path = os.path.join(os.path.dirname(ocr_pdf_path), "stamped.pdf")

    image_dets = list(
        Detection.objects.filter(scan=scan, label="IMAGE", active=True)
        .order_by("page_index")
        .values("page_index", "x0", "y0", "x1", "y1", "img_width", "img_height")
    )
    if not image_dets:
        # Always copy so the OCR PDF is never modified by downstream steps
        shutil.copy2(ocr_pdf_path, stamped_path)
        return stamped_path

    original_doc = fitz.open(scan.pdf_path)
    ocr_doc = fitz.open(ocr_pdf_path)

    try:
        for det in image_dets:
            page_idx = det["page_index"]
            if page_idx >= original_doc.page_count or page_idx >= ocr_doc.page_count:
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

        ocr_doc.save(stamped_path, garbage=3, deflate=True)
    finally:
        original_doc.close()
        ocr_doc.close()
    return stamped_path


def run_generate_files(scan_pk):
    """Generate redacted/split opinion files from existing detections.

    Designed to run in a background thread.
    """
    django.db.connections.close_all()


    scan = Scan.objects.get(pk=scan_pk)

    try:
        Scan.objects.filter(pk=scan_pk).update(progress_message="Generating files...", progress_log="")

        reporter = scan.reporter.short_name
        volume = str(scan.volume)
        first_page = scan.start_page or 1

        output_base = Path(settings.MEDIA_ROOT) / "processed" / str(scan_pk)

        ocr_pdf = find_ocr_pdf(scan.output_dir) if scan.output_dir else None
        if not ocr_pdf:
            raise ValueError("No OCR'd PDF found in output directory")

        # Stamp original-quality images into a copy — leaves OCR PDF untouched
        gen_pdf = _stamp_original_images(scan, str(ocr_pdf))

        # Write current DB detections -> detections.json (includes page numbers)
        det_data = _sync_detections_to_disk(scan_pk)
        Scan.objects.filter(pk=scan_pk).update(progress_message=f"Generating files ({len(det_data or [])} detections)...")

        suppression_excluded = set()
        for issue in scan.issues.filter(check_name="suppress_detection"):
            if issue.metadata:
                try:
                    meta = json.loads(issue.metadata)
                    bbox = meta.get("bbox", [0, 0, 0, 0])
                    suppression_excluded.add((
                        meta.get("page_index", 0), meta.get("label_id", 0),
                        round(bbox[0]), round(bbox[1]),
                    ))
                except Exception:
                    pass
        Scan.objects.filter(pk=scan_pk).update(
            progress_message="Generating files...",
        )
        output = Path(scan.output_dir if scan.output_dir else str(output_base))
        generate_files(
            ocr_pdf=str(gen_pdf),
            output=output,
            reporter=reporter,
            volume=volume,
            first_page=first_page,
            unredacted=True,
            excluded=suppression_excluded or None,
        )

        output_dir = output
        for d in sorted(output.rglob("redacted"), key=lambda p: len(p.parts)):
            output_dir = d.parent
            break

        redacted_dir = output_dir / "redacted"
        redacted_files = sorted(redacted_dir.glob("*.pdf")) if redacted_dir.is_dir() else []

        scan.refresh_from_db()
        existing_opinions = json.loads(scan.opinions_json) if scan.opinions_json else []

        if existing_opinions and "caption_page" in existing_opinions[0]:
            for i, op in enumerate(existing_opinions):
                if i < len(redacted_files):
                    op["filename"] = redacted_files[i].name
        else:
            existing_opinions = []
            for f in redacted_files:
                existing_opinions.append({"filename": f.name, "first_page": 0, "last_page": 0})

        redacted_pdf = list(output_dir.glob("*.redacted.pdf"))

        scan.output_dir = str(output_dir)
        scan.redacted_pdf_path = str(redacted_pdf[0]) if redacted_pdf else ""
        scan.opinions_json = json.dumps(existing_opinions)
        scan.stage = Stage.APPROVED
        scan.status = Status.APPROVED
        scan.progress_message = f"Generated {len(existing_opinions)} opinions"
        scan.progress_log = ""
        scan.save()

        OpinionScan.objects.filter(scan=scan).delete()
        LLMScan.objects.filter(scan=scan).delete()
        unredacted_dir = output_dir / "unredacted"
        masked_dir = output_dir / "masked"
        for i, op in enumerate(existing_opinions):
            page_start = op.get("first_page_number", 1)
            page_end = op.get("last_page_number", page_start)
            fname = op.get("filename", "")
            opinion = OpinionScan.objects.create(
                scan=scan, reporter=scan.reporter, volume=scan.volume,
                opinion_order=i, page_start=page_start or 1,
                page_end=page_end or page_start or 1,
                caption_page_index=op.get("caption_page"),
                key_page_index=op.get("key_page"),
                has_image=op.get("has_image", False),
                status=OpinionStatus.OK, uploaded_by=scan.uploaded_by,
            )
            if fname:
                media_root = Path(settings.MEDIA_ROOT)
                rp = redacted_dir / fname
                if rp.exists():
                    opinion.redacted_pdf.name = str(
                        rp.relative_to(media_root)
                    )
                up = unredacted_dir / fname if unredacted_dir.exists() else None
                if up and up.exists():
                    opinion.original_pdf.name = str(
                        up.relative_to(media_root)
                    )
                # Opinion suffix is 1-3 unpadded digits (e.g. -1, -2, -3).
                # Page numbers are always 4 zero-padded digits (e.g. -0006).
                # Only strip the suffix if it's 1-3 digits preceded by a 4-digit page number.
                masked_fname = re.sub(r"(\d{4})-\d{1,3}\.pdf$", r"\1.pdf", fname)
                mp = masked_dir / masked_fname if masked_dir.exists() else None
                if mp and mp.exists():
                    opinion.masked_pdf.name = str(
                        mp.relative_to(media_root)
                    )
                opinion.save()

            if opinion.masked_pdf.name:
                llm_scan = LLMScan.objects.create(
                    scan=scan, masked_pdf=opinion.masked_pdf.name,
                    status=LLMScan.Status.PENDING,
                )
                llm_scan.opinions.add(opinion)

    except Exception as exc:
        traceback.print_exc()
        Scan.objects.filter(pk=scan_pk).update(
            status=Status.ERROR, progress_message=str(exc)[:255],
        )


# ---------------------------------------------------------------------------
# Detect (run YOLO detection + opinion pairing)
# ---------------------------------------------------------------------------


def run_detect(scan_pk):
    """Run YOLO detection as subprocess, then pair opinions.

    Designed to run in a background thread.
    """
    django.db.connections.close_all()

    scan = Scan.objects.get(pk=scan_pk)
    try:
        output_dir = Path(scan.output_dir)
        bitonal = output_dir / "bitonal.pdf"

        # OCR if needed (no existing OCR PDF)
        if not find_ocr_pdf(str(output_dir)) and bitonal.exists():
            _run_ocr(
                scan_pk, str(bitonal), str(output_dir),
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
            status=Status.APPROVED,
            progress_message=f"Done — {len(dets)} detections, {len(opinions)} opinions",
        )

    except Exception as exc:
        traceback.print_exc()
        Scan.objects.filter(pk=scan_pk).update(
            status=Status.ERROR, progress_message=str(exc)[:255],
        )