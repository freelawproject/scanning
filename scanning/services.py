"""Background processing pipelines and business logic.

Functions in this module run outside the request/response cycle, typically
in background threads.  They must NOT import Django HTTP machinery
(HttpResponse, render, redirect, etc.).
"""

import json
import os
import re
import shutil
import tempfile
import threading

import fitz
from django.conf import settings

from blackletter.analyze import DEFAULT_ANALYZE_MODEL, _process_page
from blackletter.api import bitonal as bl_bitonal, ocr as bl_ocr, pair as bl_pair
from blackletter.models import Label
from blackletter.process import generate_files
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


class ProcessingCancelled(Exception):
    pass


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def run_incremental_validation(scan_pk, pdf_path):
    """Validate page by page, saving results incrementally."""
    os.environ["PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK"] = "True"
    from pathlib import Path as _P

    scan = Scan.objects.get(pk=scan_pk)
    total = scan.page_count
    if not total:
        pdf = fitz.open(pdf_path)
        total = pdf.page_count
        pdf.close()

    model_path = str(DEFAULT_ANALYZE_MODEL)
    exp_start = scan.start_page or 1
    exp_end = scan.end_page

    all_results = []
    all_detections = []

    for i in range(total):
        status = (
            Scan.objects.filter(pk=scan_pk)
            .values_list("status", flat=True)
            .first()
        )
        if status == Status.CANCELLED:
            return

        try:
            result = _process_page(
                (i, pdf_path, exp_start, exp_end, model_path)
            )
        except Exception as exc:
            import traceback

            print(f"  Page {i + 1} FAILED: {exc}", flush=True)
            traceback.print_exc()
            result = {
                "pdf_page": i + 1,
                "detected": None,
                "type": "error",
                "zone": "error",
                "score": 0,
                "ocr": "failed",
                "detections": [],
                "img_width": 0,
                "img_height": 0,
            }

        all_results.append(result)

        page_dets = result.get("detections", [])
        img_w = result.get("img_width", 0)
        img_h = result.get("img_height", 0)
        for d in page_dets:
            try:
                label_name = Label(d["label_id"]).name
            except (ValueError, KeyError):
                continue
            all_detections.append(
                Detection(
                    scan_id=scan_pk,
                    page_index=i,
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

        detected = result.get("detected")
        page_status = f"#{detected}" if detected else "no #"
        Scan.objects.filter(pk=scan_pk).update(
            progress_current=i + 1,
            progress_total=total,
            progress_message=f"Page {i + 1}/{total}: {page_status}",
            ocr_results=json.dumps(all_results),
        )

        if (i + 1) % 10 == 0 or i == total - 1:
            if all_detections:
                if i < 10:
                    Detection.objects.filter(scan_id=scan_pk).delete()
                Detection.objects.bulk_create(all_detections)
                all_detections = []

    if scan.output_dir:
        all_saved = Detection.objects.filter(scan_id=scan_pk).order_by(
            "page_index", "y0"
        )
        det_data = [
            {
                "page_index": d.page_index,
                "label": d.label,
                "label_id": d.label_id,
                "confidence": d.confidence,
                "bbox": [d.x0, d.y0, d.x1, d.y1],
                "img_width": d.img_width,
                "img_height": d.img_height,
                "model_count": d.model_count,
            }
            for d in all_saved
        ]
        det_path = _P(scan.output_dir) / "detections.json"
        det_path.write_text(json.dumps(det_data))

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
    scan.status = Status.APPROVED
    scan.progress_message = "Done"
    scan.save()

    Issue.objects.filter(scan=scan).delete()
    Issue.objects.bulk_create(
        [Issue(scan=scan, **issue_data) for issue_data in result.get("issues", [])]
    )


# ---------------------------------------------------------------------------
# Recalculate (rebuild issues without re-running OCR)
# ---------------------------------------------------------------------------


def recalculate_issues(scan):
    """Rebuild issues from scan.ocr_results without re-running OCR."""
    import re as re_mod
    from collections import Counter

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
    import django
    import traceback as _tb
    django.db.connections.close_all()
    from pathlib import Path

    try:
        scan = Scan.objects.get(pk=scan_pk)
    except Exception:
        _tb.print_exc()
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
        _tb.print_exc()
        print(f"[validate] ERROR: {exc}", flush=True)
        Scan.objects.filter(pk=scan_pk).update(
            status=Status.ERROR, progress_message=str(exc)[:255],
        )


# ---------------------------------------------------------------------------
# Reprocess (apply fixes, re-bitonal, re-OCR, re-validate)
# ---------------------------------------------------------------------------


def run_reprocess(scan_pk):
    """Apply PDF fixes (inserts/deletions), rebuild, bitonal -> OCR -> validate.

    Designed to run in a background thread.
    """
    import django
    django.db.connections.close_all()
    from pathlib import Path as _P

    scan = Scan.objects.get(pk=scan_pk)

    try:
        has_changes = scan.deletions.exists() or scan.inserts.exists()

        if has_changes:
            Scan.objects.filter(pk=scan_pk).update(
                progress_message="Applying fixes..."
            )

            pdf_doc = fitz.open(scan.pdf_path)
            page_map = json.loads(scan.page_map) if scan.page_map else []

            deleted_pdf_pages = sorted(d.pdf_page for d in scan.deletions.all())
            for pdf_page in sorted(deleted_pdf_pages, reverse=True):
                pdf_index = pdf_page - 1
                if 0 <= pdf_index < len(pdf_doc):
                    pdf_doc.delete_page(pdf_index)

            inserts = {ins.logical_page_number: ins for ins in scan.inserts.all()}
            insert_ops = []
            for i, entry in enumerate(page_map):
                if entry["type"] == "missing" and entry["logical_number"] in inserts:
                    insert_before = None
                    for j in range(i + 1, len(page_map)):
                        if page_map[j]["type"] == "pdf_page":
                            insert_before = page_map[j]["pdf_index"]
                            break
                    insert_ops.append(
                        (insert_before, entry["logical_number"], inserts[entry["logical_number"]])
                    )

            offset = 0
            for insert_before, logical_num, insert_obj in insert_ops:
                img_path = insert_obj.image.path
                if insert_before is not None:
                    adjusted = insert_before - len(
                        [d for d in deleted_pdf_pages if d <= insert_before]
                    )
                    pno = adjusted + offset
                    ref_page = pdf_doc.load_page(min(pno, len(pdf_doc) - 1))
                else:
                    pno = len(pdf_doc)
                    ref_page = pdf_doc.load_page(len(pdf_doc) - 1)
                w, h = ref_page.rect.width, ref_page.rect.height
                if img_path.lower().endswith(".pdf"):
                    insert_pdf = fitz.open(img_path)
                    pdf_doc.insert_pdf(
                        insert_pdf, from_page=0,
                        to_page=insert_pdf.page_count - 1, start_at=pno,
                    )
                    offset += insert_pdf.page_count - 1
                    insert_pdf.close()
                else:
                    new_page = pdf_doc.new_page(pno=pno, width=w, height=h)
                    new_page.insert_image(new_page.rect, filename=img_path)
                offset += 1

            tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
            pdf_doc.save(tmp.name, deflate=True)
            pdf_doc.close()
            shutil.move(tmp.name, scan.pdf_path)

            scan.deletions.all().delete()
            scan.inserts.all().delete()
            scan.save()

        if scan.output_dir:
            output_dir = _P(scan.output_dir)
            for old_file in output_dir.glob("*.pdf"):
                if old_file.name != _P(scan.pdf_path).name:
                    old_file.unlink(missing_ok=True)
            for old_file in output_dir.glob("*.json"):
                old_file.unlink(missing_ok=True)

        Detection.objects.filter(scan_id=scan_pk).delete()

        Scan.objects.filter(pk=scan_pk).update(
            ocr_results="", opinions_json="", page_map="", missing_pages="",
        )

        scan.refresh_from_db()
        if not scan.output_dir:
            output_dir = _P(settings.MEDIA_ROOT) / "processed" / str(scan_pk)
            output_dir.mkdir(parents=True, exist_ok=True)
            Scan.objects.filter(pk=scan_pk).update(output_dir=str(output_dir))
        else:
            output_dir = _P(scan.output_dir)
            output_dir.mkdir(parents=True, exist_ok=True)

        Scan.objects.filter(pk=scan_pk).update(progress_message="Converting to bitonal...")

        def _bitonal_progress(current, total, message):
            Scan.objects.filter(pk=scan_pk).update(
                progress_message=message, progress_current=current, progress_total=total,
            )

        bitonal_path = bl_bitonal(
            scan.pdf_path, str(output_dir), progress_callback=_bitonal_progress,
        )

        pdf = fitz.open(str(bitonal_path))
        Scan.objects.filter(pk=scan_pk).update(page_count=pdf.page_count)
        pdf.close()

        scan.refresh_from_db()
        Scan.objects.filter(pk=scan_pk).update(progress_message="Running Tesseract OCR...")
        ocr_path = bl_ocr(
            str(bitonal_path), str(output_dir),
            reporter=scan.reporter.short_name or "",
            volume=str(scan.volume) or "",
            first_page=scan.start_page or 1,
        )

        run_incremental_validation(scan_pk, str(ocr_path))

    except ProcessingCancelled:
        pass
    except Exception as exc:
        import traceback
        traceback.print_exc()
        Scan.objects.filter(pk=scan_pk).update(
            status=Status.ERROR, progress_message=str(exc)[:255],
        )


# ---------------------------------------------------------------------------
# Generate (split into redacted/masked/unredacted opinions)
# ---------------------------------------------------------------------------


def run_generate_files(scan_pk):
    """Generate redacted/split opinion files from existing detections.

    Designed to run in a background thread.
    """
    import django
    django.db.connections.close_all()
    from pathlib import Path

    from scanning.utils import find_ocr_pdf

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

        # Write current DB detections -> detections.json so manual edits are used
        output_dir = Path(scan.output_dir)
        active_dets = list(Detection.objects.filter(scan=scan, active=True).order_by("page_index", "y0"))
        det_data = [
            {
                "page_index": d.page_index,
                "label": d.label,
                "label_id": d.label_id,
                "confidence": d.confidence,
                "bbox": [d.x0, d.y0, d.x1, d.y1],
                "img_width": d.img_width,
                "img_height": d.img_height,
                "model_count": d.model_count,
            }
            for d in active_dets
        ]
        det_path = output_dir / "detections.json"
        det_path.write_text(json.dumps(det_data))
        Scan.objects.filter(pk=scan_pk).update(progress_message=f"Generating files ({len(det_data)} detections)...")

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
            ocr_pdf=str(ocr_pdf),
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
            page_start = op.get("caption_page", op.get("first_page", 0))
            if isinstance(page_start, int) and "caption_page" in op:
                page_start += scan.start_page or 1
            page_end = op.get("key_page", op.get("last_page", 0))
            if isinstance(page_end, int) and "key_page" in op:
                page_end += (scan.start_page or 1) + op.get("page_count", 1) - 1
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
                rp = redacted_dir / fname
                if rp.exists():
                    opinion.redacted_pdf.name = str(rp)
                up = unredacted_dir / fname if unredacted_dir.exists() else None
                if up and up.exists():
                    opinion.original_pdf.name = str(up)
                # Opinion suffix is 1-3 unpadded digits (e.g. -1, -2, -3).
                # Page numbers are always 4 zero-padded digits (e.g. -0006).
                # Only strip the suffix if it's 1-3 digits preceded by a 4-digit page number.
                masked_fname = re.sub(r"(\d{4})-\d{1,3}\.pdf$", r"\1.pdf", fname)
                mp = masked_dir / masked_fname if masked_dir.exists() else None
                if mp and mp.exists():
                    opinion.masked_pdf.name = str(mp)
                opinion.save()

            if opinion.masked_pdf.name:
                llm_scan = LLMScan.objects.create(
                    scan=scan, masked_pdf=opinion.masked_pdf.name,
                    status=LLMScan.Status.PENDING,
                )
                llm_scan.opinions.add(opinion)

    except Exception as exc:
        import traceback
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
    import subprocess
    import sys
    import time as _time
    import django
    django.db.connections.close_all()
    from pathlib import Path as _P

    scan = Scan.objects.get(pk=scan_pk)
    try:
        output_dir = _P(scan.output_dir)
        bitonal = output_dir / "bitonal.pdf"

        ocr_exists = any(
            f.name not in ("bitonal.pdf",) and not f.name.endswith(".redacted.pdf") and not f.name.endswith(".original.pdf")
            for f in output_dir.glob("*.pdf")
        )
        if not ocr_exists and bitonal.exists():
            def _run_ocr_bg(scan_pk, bitonal_str, output_dir_str):
                try:
                    import django as _dj
                    _dj.db.connections.close_all()
                    _scan = Scan.objects.get(pk=scan_pk)
                    bl_ocr(
                        bitonal_str, output_dir_str,
                        reporter=_scan.reporter.short_name or "",
                        volume=str(_scan.volume) or "",
                        first_page=_scan.start_page or 1,
                    )
                except Exception as _e:
                    print(f"  Background OCR failed: {_e}", flush=True)
            threading.Thread(
                target=_run_ocr_bg,
                args=(scan_pk, str(bitonal), str(output_dir)),
                daemon=True,
            ).start()

        pdf_path = str(bitonal) if bitonal.exists() else scan.pdf_path

        script = f"""
import os
os.environ["PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK"] = "True"
from blackletter.api import detect as bl_detect
dets = bl_detect("{pdf_path}", "{output_dir}", models=["small", "medium", "large"])
print(f"\\nDetect complete: {{len(dets)}} detections", flush=True)
"""
        log_path = _P(settings.MEDIA_ROOT) / "processed" / str(scan_pk) / "detect.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        Scan.objects.filter(pk=scan_pk).update(
            progress_message="Running YOLO detection...",
            progress_log="",
        )
        proc = subprocess.Popen(
            [sys.executable, "-c", script],
            stdout=open(log_path, "w"),
            stderr=subprocess.STDOUT,
            cwd=str(_P(settings.INSTALL_ROOT)),
        )
        while proc.poll() is None:
            _time.sleep(1)
            try:
                log_text = log_path.read_text(errors="replace").replace("\x00", "")
                lines = [l for l in log_text.strip().split("\n") if l.strip()]
                msg = lines[-1].strip() if lines else "Running YOLO detection..."
                Scan.objects.filter(pk=scan_pk).update(
                    progress_message=msg[:255],
                    progress_log=log_text[-5000:],
                )
            except Exception:
                pass

        log_text = log_path.read_text(errors="replace").replace("\x00", "")
        if proc.returncode != 0:
            raise RuntimeError(
                f"YOLO detection failed (exit {proc.returncode}): {log_text[-500:]}"
            )

        det_path = output_dir / "detections.json"
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

        pair_log = log_text + f"\n{len(dets)} detections saved. Pairing opinions..."
        Scan.objects.filter(pk=scan_pk).update(
            progress_message=f"{len(dets)} detections. Pairing opinions...",
            progress_log=pair_log[-5000:],
        )

        opinions = bl_pair(
            str(det_path), pdf_path,
            reporter=scan.reporter.short_name or "",
            volume=str(scan.volume) or "",
            first_page=scan.start_page or 1,
        )

        done_log = pair_log + f"\nPairing complete: {len(opinions)} opinions."
        Scan.objects.filter(pk=scan_pk).update(
            opinions_json=json.dumps(opinions),
            status=Status.APPROVED,
            progress_message=f"Done — {len(dets)} detections, {len(opinions)} opinions",
            progress_log=done_log[-5000:],
        )

    except Exception as exc:
        import traceback
        traceback.print_exc()
        Scan.objects.filter(pk=scan_pk).update(
            status=Status.ERROR, progress_message=str(exc)[:255],
        )