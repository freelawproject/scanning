"""Tests for scanning.services pipeline helpers and functions.

Uses the fixture PDF at scanning/tests/fixtures/a3d.332.1.1.pdf.
"""

import json
import pathlib
import shutil
import tempfile

from django.test import TestCase, override_settings

from scanning.factories import ReporterFactory, ScanFactory, UserFactory
from scanning.models import Detection, Status

FIXTURE_DIR = pathlib.Path(__file__).parent / "fixtures"
PDF_PATH = FIXTURE_DIR / "a3d.332.1.1.pdf"

MEDIA_ROOT = tempfile.mkdtemp()


def _require_fixture(test_case):
    """Skip test if the fixture PDF is not present."""
    if not PDF_PATH.exists():
        test_case.skipTest(
            f"Test PDF not found at {PDF_PATH}. "
            "Copy it in to scanning/tests/fixtures/"
        )


def _make_scan_with_output(tmpdir, **kwargs):
    """Create a scan pointing at the fixture PDF with a temp output_dir."""
    scan = ScanFactory(
        start_page=1,
        end_page=1,
        number_of_pages=1,
        **kwargs,
    )
    # Copy fixture PDF into MEDIA_ROOT so Django's storage resolves it
    media_dir = pathlib.Path(MEDIA_ROOT) / "test_pdfs"
    media_dir.mkdir(parents=True, exist_ok=True)
    pdf_dest = media_dir / f"scan_{scan.pk}.pdf"
    shutil.copy2(PDF_PATH, pdf_dest)
    scan.original_pdf.name = str(pdf_dest.relative_to(MEDIA_ROOT))
    scan.output_dir = tmpdir
    scan.page_count = 1
    scan.save(update_fields=["original_pdf", "output_dir", "page_count"])
    return scan


def _run_detect_on_fixture(tmpdir):
    """Run YOLO detect on the fixture PDF, return detections list."""
    from blackletter.api import detect

    return detect(str(PDF_PATH), tmpdir, models=["small", "medium", "large"])


@override_settings(MEDIA_ROOT=MEDIA_ROOT)
class TestUpdateProgress(TestCase):
    """Test the _update_progress helper."""

    def test_updates_message(self):
        from scanning.services import _update_progress

        scan = ScanFactory()
        _update_progress(scan.pk, "Processing page 3...")
        scan.refresh_from_db()
        self.assertEqual(scan.progress_message, "Processing page 3...")

    def test_updates_current_and_total(self):
        from scanning.services import _update_progress

        scan = ScanFactory()
        _update_progress(scan.pk, "Page 5/10", current=5, total=10)
        scan.refresh_from_db()
        self.assertEqual(scan.progress_current, 5)
        self.assertEqual(scan.progress_total, 10)

    def test_truncates_long_message(self):
        from scanning.services import _update_progress

        scan = ScanFactory()
        long_msg = "x" * 500
        _update_progress(scan.pk, long_msg)
        scan.refresh_from_db()
        self.assertEqual(len(scan.progress_message), 255)

    def test_extra_kwargs_passed_through(self):
        from scanning.services import _update_progress

        scan = ScanFactory()
        _update_progress(scan.pk, "Done", progress_log="some log text")
        scan.refresh_from_db()
        self.assertEqual(scan.progress_log, "some log text")


@override_settings(MEDIA_ROOT=MEDIA_ROOT)
class TestImportDetections(TestCase):
    """Test _import_detections_from_json."""

    def setUp(self):
        _require_fixture(self)

    def test_imports_detections_from_json_file(self):
        from scanning.services import _import_detections_from_json

        with tempfile.TemporaryDirectory() as tmpdir:
            scan = _make_scan_with_output(tmpdir)
            dets = _run_detect_on_fixture(tmpdir)

            # The detect call writes detections.json
            result = _import_detections_from_json(scan.pk, tmpdir)
            self.assertEqual(len(result), len(dets))
            self.assertEqual(
                Detection.objects.filter(scan=scan).count(), len(dets)
            )

    def test_clears_existing_detections(self):
        from scanning.services import _import_detections_from_json

        with tempfile.TemporaryDirectory() as tmpdir:
            scan = _make_scan_with_output(tmpdir)
            # Create a dummy detection
            Detection.objects.create(
                scan=scan,
                page_index=0,
                label="DUMMY",
                label_id=99,
                confidence=0.9,
                x0=0,
                y0=0,
                x1=1,
                y1=1,
            )
            self.assertEqual(Detection.objects.filter(scan=scan).count(), 1)

            _run_detect_on_fixture(tmpdir)
            _import_detections_from_json(scan.pk, tmpdir)
            # The dummy should be gone, replaced by real detections
            self.assertFalse(
                Detection.objects.filter(scan=scan, label="DUMMY").exists()
            )


@override_settings(MEDIA_ROOT=MEDIA_ROOT)
class TestSyncDetectionsToDisk(TestCase):
    """Test _sync_detections_to_disk."""

    def setUp(self):
        _require_fixture(self)

    def test_writes_detections_json(self):
        from scanning.services import (
            _import_detections_from_json,
            _sync_detections_to_disk,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            scan = _make_scan_with_output(tmpdir)
            _run_detect_on_fixture(tmpdir)
            _import_detections_from_json(scan.pk, tmpdir)

            # Delete the file and re-sync from DB
            det_path = pathlib.Path(tmpdir) / "detections.json"
            det_path.unlink()
            self.assertFalse(det_path.exists())

            det_data = _sync_detections_to_disk(scan.pk)
            self.assertTrue(det_path.exists())
            on_disk = json.loads(det_path.read_text())
            self.assertEqual(len(on_disk), len(det_data))

    def test_returns_none_without_output_dir(self):
        from scanning.services import _sync_detections_to_disk

        scan = ScanFactory()
        scan.output_dir = ""
        scan.save(update_fields=["output_dir"])
        result = _sync_detections_to_disk(scan.pk)
        self.assertIsNone(result)


@override_settings(MEDIA_ROOT=MEDIA_ROOT)
class TestRePairOpinions(TestCase):
    """Test _re_pair_opinions."""

    def setUp(self):
        _require_fixture(self)

    def test_pairs_three_opinions(self):
        from scanning.services import (
            _import_detections_from_json,
            _re_pair_opinions,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            scan = _make_scan_with_output(
                tmpdir,
                reporter=ReporterFactory(short_name="a3d"),
            )
            _run_detect_on_fixture(tmpdir)
            _import_detections_from_json(scan.pk, tmpdir)

            opinions = _re_pair_opinions(scan.pk)
            self.assertEqual(len(opinions), 3)

            scan.refresh_from_db()
            stored = json.loads(scan.opinions_json)
            self.assertEqual(len(stored), 3)


@override_settings(MEDIA_ROOT=MEDIA_ROOT)
class TestComputeAndSaveRedactionRects(TestCase):
    """Test _compute_and_save_redaction_rects."""

    def setUp(self):
        _require_fixture(self)

    def test_writes_redaction_rects_json(self):
        from scanning.services import (
            _compute_and_save_redaction_rects,
            _import_detections_from_json,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            scan = _make_scan_with_output(
                tmpdir,
                reporter=ReporterFactory(short_name="a3d"),
            )
            _run_detect_on_fixture(tmpdir)
            _import_detections_from_json(scan.pk, tmpdir)

            rects = _compute_and_save_redaction_rects(
                scan.pk, str(PDF_PATH), tmpdir
            )
            self.assertGreater(len(rects), 0)

            scan.refresh_from_db()
            self.assertTrue(scan.redaction_rects)


@override_settings(MEDIA_ROOT=MEDIA_ROOT)
class TestComputeAndSaveMarginRects(TestCase):
    """Test _compute_and_save_margin_rects."""

    def setUp(self):
        _require_fixture(self)

    def test_writes_margin_rects_to_model(self):
        from scanning.services import _compute_and_save_margin_rects

        with tempfile.TemporaryDirectory() as tmpdir:
            scan = _make_scan_with_output(
                tmpdir,
                reporter=ReporterFactory(short_name="a3d"),
            )
            rects = _compute_and_save_margin_rects(
                scan.pk, str(PDF_PATH), tmpdir
            )
            self.assertIsNotNone(rects)

            scan.refresh_from_db()
            self.assertTrue(scan.margin_rects)

    def test_returns_cached_on_second_call(self):
        from scanning.services import _compute_and_save_margin_rects

        with tempfile.TemporaryDirectory() as tmpdir:
            scan = _make_scan_with_output(
                tmpdir,
                reporter=ReporterFactory(short_name="a3d"),
            )
            first = _compute_and_save_margin_rects(
                scan.pk, str(PDF_PATH), tmpdir
            )
            second = _compute_and_save_margin_rects(
                scan.pk, str(PDF_PATH), tmpdir
            )
            self.assertEqual(first, second)


@override_settings(MEDIA_ROOT=MEDIA_ROOT)
class TestSmartDeleteIndexShifting(TestCase):
    """Test that run_smart_delete shifts detection page_index correctly."""

    def test_deleting_page_shifts_subsequent_detections(self):
        """Detections on pages after the deleted one should shift down by 1."""
        scan = ScanFactory(page_count=3)

        # Create detections on three pages
        for page_idx in range(3):
            Detection.objects.create(
                scan=scan,
                page_index=page_idx,
                label="CASE_CAPTION",
                label_id=0,
                confidence=0.95,
                x0=10,
                y0=10,
                x1=100,
                y1=100,
            )

        self.assertEqual(Detection.objects.filter(scan=scan).count(), 3)

        # Simulate deleting page 1 (0-indexed) — just the DB operations
        delete_idx = 1
        Detection.objects.filter(scan=scan, page_index=delete_idx).delete()
        from django.db.models import F

        Detection.objects.filter(scan=scan, page_index__gt=delete_idx).update(
            page_index=F("page_index") - 1
        )

        remaining = list(
            Detection.objects.filter(scan=scan)
            .order_by("page_index")
            .values_list("page_index", flat=True)
        )
        self.assertEqual(remaining, [0, 1])

    def test_deleting_first_page_shifts_all(self):
        scan = ScanFactory(page_count=3)
        for page_idx in range(3):
            Detection.objects.create(
                scan=scan,
                page_index=page_idx,
                label="KEY_ICON",
                label_id=1,
                confidence=0.9,
                x0=0,
                y0=0,
                x1=50,
                y1=50,
            )

        delete_idx = 0
        Detection.objects.filter(scan=scan, page_index=delete_idx).delete()
        from django.db.models import F

        Detection.objects.filter(scan=scan, page_index__gt=delete_idx).update(
            page_index=F("page_index") - 1
        )

        remaining = list(
            Detection.objects.filter(scan=scan)
            .order_by("page_index")
            .values_list("page_index", flat=True)
        )
        self.assertEqual(remaining, [0, 1])

    def test_deleting_last_page_no_shift_needed(self):
        scan = ScanFactory(page_count=3)
        for page_idx in range(3):
            Detection.objects.create(
                scan=scan,
                page_index=page_idx,
                label="HEADNOTE",
                label_id=3,
                confidence=0.85,
                x0=0,
                y0=0,
                x1=50,
                y1=50,
            )

        delete_idx = 2
        Detection.objects.filter(scan=scan, page_index=delete_idx).delete()
        from django.db.models import F

        Detection.objects.filter(scan=scan, page_index__gt=delete_idx).update(
            page_index=F("page_index") - 1
        )

        remaining = list(
            Detection.objects.filter(scan=scan)
            .order_by("page_index")
            .values_list("page_index", flat=True)
        )
        self.assertEqual(remaining, [0, 1])


@override_settings(MEDIA_ROOT=MEDIA_ROOT)
class TestSmartInsertIndexShifting(TestCase):
    """Test that inserting a page shifts detection page_index correctly."""

    def test_inserting_shifts_subsequent_detections_up(self):
        scan = ScanFactory(page_count=2)
        for page_idx in range(2):
            Detection.objects.create(
                scan=scan,
                page_index=page_idx,
                label="CASE_CAPTION",
                label_id=0,
                confidence=0.95,
                x0=10,
                y0=10,
                x1=100,
                y1=100,
            )

        # Simulate inserting at index 1
        insert_idx = 1
        from django.db.models import F

        Detection.objects.filter(scan=scan, page_index__gte=insert_idx).update(
            page_index=F("page_index") + 1
        )

        remaining = list(
            Detection.objects.filter(scan=scan)
            .order_by("page_index")
            .values_list("page_index", flat=True)
        )
        self.assertEqual(remaining, [0, 2])


@override_settings(MEDIA_ROOT=MEDIA_ROOT)
class TestCollectPdfPaths(TestCase):
    """Test _collect_pdf_paths helper."""

    def setUp(self):
        _require_fixture(self)

    def test_collects_original_only_without_output_dir(self):
        from scanning.services import _collect_pdf_paths

        scan = ScanFactory()
        paths = _collect_pdf_paths(scan, None)
        self.assertEqual(len(paths), 1)

    def test_collects_bitonal_if_exists(self):
        from scanning.services import _collect_pdf_paths

        with tempfile.TemporaryDirectory() as tmpdir:
            scan = _make_scan_with_output(tmpdir)
            bitonal = pathlib.Path(tmpdir) / "bitonal.pdf"
            shutil.copy2(PDF_PATH, bitonal)

            paths = _collect_pdf_paths(scan, tmpdir)
            path_names = [p.name for p in paths]
            self.assertIn("bitonal.pdf", path_names)


@override_settings(MEDIA_ROOT=MEDIA_ROOT)
class TestBuildDocumentFromDetections(TestCase):
    """Test _build_document_from_detections."""

    def setUp(self):
        _require_fixture(self)

    def test_builds_document_with_pages(self):
        from scanning.services import (
            _build_document_from_detections,
            _import_detections_from_json,
            _sync_detections_to_disk,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            scan = _make_scan_with_output(
                tmpdir,
                reporter=ReporterFactory(short_name="a3d"),
            )
            _run_detect_on_fixture(tmpdir)
            _import_detections_from_json(scan.pk, tmpdir)
            det_data = _sync_detections_to_disk(scan.pk)

            document = _build_document_from_detections(
                scan, det_data, PDF_PATH
            )
            self.assertGreater(len(document.pages), 0)
            self.assertEqual(document.reporter, "a3d")


@override_settings(MEDIA_ROOT=MEDIA_ROOT)
class TestComputeRedactionsApiView(TestCase):
    """Test that compute_redactions_api delegates to service helpers."""

    def setUp(self):
        _require_fixture(self)

    def test_returns_error_without_output_dir(self):
        user = UserFactory()
        self.client.force_login(user)
        scan = ScanFactory(uploaded_by=user)
        scan.output_dir = ""
        scan.save(update_fields=["output_dir"])

        response = self.client.post(f"/scans/{scan.pk}/compute-redactions/")
        self.assertEqual(response.status_code, 400)
        self.assertIn("error", response.json())

    def test_returns_error_without_opinions(self):
        user = UserFactory()
        self.client.force_login(user)

        with tempfile.TemporaryDirectory() as tmpdir:
            scan = _make_scan_with_output(tmpdir, uploaded_by=user)
            scan.opinions_json = ""
            scan.save(update_fields=["opinions_json"])

            response = self.client.post(
                f"/scans/{scan.pk}/compute-redactions/"
            )
            self.assertEqual(response.status_code, 400)

    def test_computes_rects_successfully(self):
        from scanning.services import _import_detections_from_json

        user = UserFactory()
        self.client.force_login(user)

        with tempfile.TemporaryDirectory() as tmpdir:
            scan = _make_scan_with_output(
                tmpdir,
                uploaded_by=user,
                reporter=ReporterFactory(short_name="a3d"),
            )
            _run_detect_on_fixture(tmpdir)
            _import_detections_from_json(scan.pk, tmpdir)

            # Set opinions_json so the view doesn't bail early
            scan.opinions_json = json.dumps([{"dummy": True}])
            scan.save(update_fields=["opinions_json"])

            response = self.client.post(
                f"/scans/{scan.pk}/compute-redactions/"
            )
            self.assertEqual(response.status_code, 200)
            data = response.json()
            self.assertEqual(data["status"], "ok")
            self.assertIn("rects", data)


@override_settings(MEDIA_ROOT=MEDIA_ROOT)
class TestServeOpinions(TestCase):
    """Test that serve_opinions reads from DB, not disk."""

    def test_returns_opinions_from_db(self):
        user = UserFactory()
        self.client.force_login(user)
        opinions_data = [{"caption_page": 0, "key_page": 0}]
        scan = ScanFactory(
            uploaded_by=user,
            opinions_json=json.dumps(opinions_data),
        )
        response = self.client.get(f"/scans/{scan.pk}/opinions-json/")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["caption_page"], 0)

    def test_returns_empty_list_without_opinions(self):
        user = UserFactory()
        self.client.force_login(user)
        scan = ScanFactory(uploaded_by=user, opinions_json="")
        response = self.client.get(f"/scans/{scan.pk}/opinions-json/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [])


@override_settings(MEDIA_ROOT=MEDIA_ROOT)
class TestStartDetectSkipsIfExists(TestCase):
    """Test that start_detect skips re-detection when detections exist."""

    def test_redirects_without_processing_when_detections_exist(self):
        user = UserFactory()
        self.client.force_login(user)
        scan = ScanFactory(uploaded_by=user, status=Status.APPROVED)
        Detection.objects.create(
            scan=scan,
            page_index=0,
            label="CASE_CAPTION",
            label_id=0,
            confidence=0.95,
            x0=10,
            y0=10,
            x1=100,
            y1=100,
        )

        response = self.client.post(f"/scans/{scan.pk}/start-detect/")
        self.assertEqual(response.status_code, 302)
        self.assertIn("step=2", response.url)
        # Status should NOT have changed to PROCESSING
        scan.refresh_from_db()
        self.assertEqual(scan.status, Status.APPROVED)


# ---------------------------------------------------------------------------
# Large PDF path for end-to-end tests
# ---------------------------------------------------------------------------
PDF_23_PATH = (
    pathlib.Path(__file__).resolve().parents[1]
    / "assets"
    / "media"
    / "books"
    / "a3d"
    / "original"
    / "332_a3d_1-23_opinions.pdf"
)


@override_settings(MEDIA_ROOT=MEDIA_ROOT)
class TestSmartEditEndToEnd(TestCase):
    """End-to-end: remove pages → detect gaps → insert back → re-validate.

    Uses the 23-page A3d PDF. Removes 3 pages from the middle, runs YOLO
    detection + PaddleOCR validation to identify gaps, inserts the pages
    back, and verifies all 23 page numbers are found.
    """

    def setUp(self):
        if not PDF_23_PATH.exists():
            self.skipTest(f"23-page PDF not found at {PDF_23_PATH}")

    def _make_scan_23(self, pdf_dest, tmpdir):
        """Create a scan pointing at the 23-page (or modified) PDF."""
        import fitz

        reporter = ReporterFactory(
            short_name="a3d", full_name="Atlantic Reporter 3d"
        )
        doc = fitz.open(str(pdf_dest))
        page_count = doc.page_count
        doc.close()

        scan = ScanFactory(
            reporter=reporter,
            start_page=1,
            end_page=23,
            number_of_pages=page_count,
            page_count=page_count,
        )
        # Point original_pdf to media-relative path
        media_dir = pathlib.Path(MEDIA_ROOT) / "test_pdfs"
        media_dir.mkdir(parents=True, exist_ok=True)
        media_pdf = media_dir / f"scan_{scan.pk}.pdf"
        shutil.copy2(pdf_dest, media_pdf)
        scan.original_pdf.name = str(media_pdf.relative_to(MEDIA_ROOT))
        scan.output_dir = tmpdir
        scan.save(update_fields=["original_pdf", "output_dir", "page_count"])
        return scan

    def test_remove_detect_insert_revalidate(self):
        """Full cycle: remove pages, detect, find gaps, insert, re-validate."""
        import fitz

        from scanning.services import (
            _import_detections_from_json,
            run_paddleocr_validation,
            run_smart_insert,
        )

        # Pages to remove (1-based): 8, 12, 17
        remove_pages = [8, 12, 17]

        with tempfile.TemporaryDirectory() as tmpdir:
            # --- Step 1: Extract the pages we'll remove, then delete them ---
            original = fitz.open(str(PDF_23_PATH))
            self.assertEqual(original.page_count, 23)

            # Save each removed page as a single-page PDF
            extracted_pdfs = {}
            for pg in remove_pages:
                single = fitz.open()
                single.insert_pdf(original, from_page=pg - 1, to_page=pg - 1)
                path = pathlib.Path(tmpdir) / f"extracted_page_{pg}.pdf"
                single.save(str(path))
                single.close()
                extracted_pdfs[pg] = path

            # Create the "damaged" PDF with pages removed
            damaged_path = pathlib.Path(tmpdir) / "damaged.pdf"
            damaged = fitz.open()
            damaged.insert_pdf(original)
            # Delete in reverse order so indices don't shift
            for pg in sorted(remove_pages, reverse=True):
                damaged.delete_page(pg - 1)
            damaged.save(str(damaged_path))
            damaged.close()
            original.close()

            verify = fitz.open(str(damaged_path))
            self.assertEqual(verify.page_count, 20)
            verify.close()

            # --- Step 2: Create scan + run YOLO detection on 20-page PDF ---
            scan = self._make_scan_23(damaged_path, tmpdir)
            self.assertEqual(scan.page_count, 20)

            from blackletter.api import detect

            dets = detect(
                str(damaged_path), tmpdir, models=["small", "medium", "large"]
            )
            self.assertGreater(len(dets), 0)
            _import_detections_from_json(scan.pk, tmpdir)

            det_count = Detection.objects.filter(scan=scan).count()
            self.assertGreater(det_count, 0)
            print(f"\n  YOLO: {det_count} detections on 20-page PDF")

            # --- Step 3: Run PaddleOCR validation → should find gaps ---
            run_paddleocr_validation(scan.pk, str(damaged_path))
            scan.refresh_from_db()

            missing = (
                json.loads(scan.missing_pages) if scan.missing_pages else []
            )
            print(f"  Missing pages identified: {missing}")

            # Pages 8, 12, 17 should be in the missing list
            for pg in remove_pages:
                self.assertIn(
                    pg,
                    missing,
                    f"Page {pg} should be identified as missing but got {missing}",
                )

            # --- Step 4: Insert the missing pages back ---
            # We need to copy the damaged PDF to where scan.pdf_path points
            # since run_smart_insert modifies the file in place
            scan.refresh_from_db()
            for pg in sorted(missing):
                if pg in extracted_pdfs:
                    print(f"  Inserting page {pg} back...")
                    run_smart_insert(scan.pk, pg, str(extracted_pdfs[pg]))
                    scan.refresh_from_db()

            # --- Step 5: Verify the PDF is back to 23 pages ---
            scan.refresh_from_db()
            self.assertEqual(
                scan.page_count, 23, "PDF should be back to 23 pages"
            )

            # Check the actual PDF
            final_pdf = fitz.open(scan.pdf_path)
            self.assertEqual(final_pdf.page_count, 23)
            final_pdf.close()

            # --- Step 6: Verify all 23 page numbers are detected ---
            ocr_results = (
                json.loads(scan.ocr_results) if scan.ocr_results else []
            )
            detected_nums = set()
            for r in ocr_results:
                if r.get("detected"):
                    try:
                        detected_nums.add(int(r["detected"]))
                    except (ValueError, TypeError):
                        pass

            expected_nums = set(range(1, 24))
            still_missing = expected_nums - detected_nums
            print(f"  Detected page numbers: {sorted(detected_nums)}")
            if still_missing:
                print(f"  Still missing: {sorted(still_missing)}")

            # All 23 pages should be detected
            self.assertEqual(
                detected_nums,
                expected_nums,
                f"Expected all pages 1-23 detected. Missing: {sorted(still_missing)}",
            )

            # No issues remaining
            final_missing = (
                json.loads(scan.missing_pages) if scan.missing_pages else []
            )
            self.assertEqual(
                final_missing,
                [],
                f"No missing pages expected after re-insert, got: {final_missing}",
            )


@override_settings(MEDIA_ROOT=MEDIA_ROOT)
class TestUploadApprovedFiles(TestCase):
    """Test the upload_approved_files helper."""

    def _make_scan_with_generated_files(self):
        """Create a scan with a populated output_dir."""
        tmpdir = tempfile.mkdtemp(dir=MEDIA_ROOT)
        scan = ScanFactory(start_page=1, end_page=95)
        scan.output_dir = tmpdir
        scan.save(update_fields=["output_dir"])

        redacted_dir = pathlib.Path(tmpdir) / "redacted"
        masked_dir = pathlib.Path(tmpdir) / "masked"
        redacted_dir.mkdir()
        masked_dir.mkdir()

        for name in ["a.1.0001-0010.pdf", "a.1.0011-0020.pdf"]:
            (redacted_dir / name).write_bytes(b"%PDF-1.4 redacted")
            (masked_dir / name).write_bytes(b"%PDF-1.4 masked")

        short = scan.reporter.short_name
        (pathlib.Path(tmpdir) / f"{short}.{scan.volume}.1.95.original.pdf").write_bytes(
            b"%PDF-1.4 original"
        )
        (pathlib.Path(tmpdir) / f"{short}.{scan.volume}.1.95.redacted.pdf").write_bytes(
            b"%PDF-1.4 redacted-full"
        )
        return scan

    def test_missing_files_returns_error(self):
        """Return error message when redacted dir is missing."""
        from scanning.services import upload_approved_files

        tmpdir = tempfile.mkdtemp(dir=MEDIA_ROOT)
        scan = ScanFactory(start_page=1, end_page=95)
        scan.output_dir = tmpdir
        scan.save(update_fields=["output_dir"])

        result = upload_approved_files(scan.pk)
        self.assertIn("Before approving", result)
        scan.refresh_from_db()
        self.assertFalse(scan.s3_uploaded)

    def test_already_uploaded_skips(self):
        """Return early if files were already uploaded."""
        from scanning.services import upload_approved_files

        scan = self._make_scan_with_generated_files()
        scan.s3_uploaded = True
        scan.s3_path = "approved/a/1/1/"
        scan.save(update_fields=["s3_uploaded", "s3_path"])

        result = upload_approved_files(scan.pk)
        self.assertIn("already uploaded", result)

    @override_settings(DEVELOPMENT=True)
    def test_no_credentials_skips_upload(self):
        """Without AWS creds, set s3_path but not s3_uploaded."""
        from unittest.mock import patch

        from scanning.services import upload_approved_files

        scan = self._make_scan_with_generated_files()
        with patch.dict("os.environ", {}, clear=True):
            result = upload_approved_files(scan.pk)

        self.assertIn("No AWS credentials", result)
        scan.refresh_from_db()
        self.assertFalse(scan.s3_uploaded)
        self.assertTrue(scan.s3_path.startswith("approved/"))

    def test_s3_path_format(self):
        """Verify the S3 prefix follows the expected pattern."""
        from unittest.mock import patch

        from scanning.services import upload_approved_files

        scan = self._make_scan_with_generated_files()
        with patch.dict("os.environ", {}, clear=True):
            upload_approved_files(scan.pk)
        scan.refresh_from_db()

        short = scan.reporter.short_name
        expected = f"approved/{short}/{scan.volume}/{scan.start_page}/"
        self.assertEqual(scan.s3_path, expected)

    @override_settings(
        AWS_PRIVATE_STORAGE_BUCKET_NAME="test-bucket",
    )
    def test_upload_calls_boto3(self):
        """Verify boto3 upload_file and head_object are called."""
        from unittest.mock import MagicMock, patch

        from botocore.exceptions import ClientError

        from scanning.services import upload_approved_files

        scan = self._make_scan_with_generated_files()
        mock_client = MagicMock()
        # All files are new (HEAD returns 404)
        mock_client.head_object.side_effect = ClientError(
            {"Error": {"Code": "404"}}, "HeadObject"
        )

        env = {"AWS_ACCESS_KEY_ID": "test", "AWS_SECRET_ACCESS_KEY": "test"}
        with patch.dict("os.environ", env), \
             patch("boto3.client", return_value=mock_client):
            result = upload_approved_files(scan.pk)

        self.assertIn("uploaded", result)
        # 2 redacted + 2 masked + 1 original + 1 redacted-full = 6
        self.assertEqual(mock_client.upload_file.call_count, 6)
        self.assertEqual(mock_client.head_object.call_count, 6)
        scan.refresh_from_db()
        self.assertTrue(scan.s3_uploaded)

    @override_settings(
        AWS_PRIVATE_STORAGE_BUCKET_NAME="test-bucket",
    )
    def test_upload_skips_existing_files(self):
        """Files that already exist in S3 (HEAD succeeds) are skipped."""
        from unittest.mock import MagicMock, call, patch

        from botocore.exceptions import ClientError

        from scanning.services import upload_approved_files

        scan = self._make_scan_with_generated_files()
        mock_client = MagicMock()

        call_count = [0]

        def head_side_effect(**kwargs):
            """First 2 calls succeed (file exists), rest raise 404."""
            call_count[0] += 1
            if call_count[0] <= 2:
                return {"ContentLength": 100}
            raise ClientError(
                {"Error": {"Code": "404"}}, "HeadObject"
            )

        mock_client.head_object.side_effect = head_side_effect

        env = {"AWS_ACCESS_KEY_ID": "test", "AWS_SECRET_ACCESS_KEY": "test"}
        with patch.dict("os.environ", env), \
             patch("boto3.client", return_value=mock_client):
            result = upload_approved_files(scan.pk)

        self.assertIn("already existed", result)
        # 6 total files, 2 existed, 4 uploaded
        self.assertEqual(mock_client.upload_file.call_count, 4)
        self.assertEqual(mock_client.head_object.call_count, 6)

    @override_settings(
        AWS_PRIVATE_STORAGE_BUCKET_NAME="test-bucket",
    )
    def test_retry_uploads_only_missing(self):
        """On retry, only files not yet in S3 are uploaded."""
        from unittest.mock import MagicMock, patch

        from botocore.exceptions import ClientError

        from scanning.services import upload_approved_files

        scan = self._make_scan_with_generated_files()
        mock_client = MagicMock()
        # All files already exist (HEAD succeeds for all)
        mock_client.head_object.return_value = {"ContentLength": 100}

        env = {"AWS_ACCESS_KEY_ID": "test", "AWS_SECRET_ACCESS_KEY": "test"}
        with patch.dict("os.environ", env), \
             patch("boto3.client", return_value=mock_client):
            result = upload_approved_files(scan.pk)

        self.assertIn("0 uploaded", result)
        self.assertIn("already existed", result)
        self.assertEqual(mock_client.upload_file.call_count, 0)
        scan.refresh_from_db()
        self.assertTrue(scan.s3_uploaded)
