"""Tests for scanning.services pipeline helpers and functions.

Uses the fixture PDF at scanning/tests/fixtures/a3d.332.1.1.pdf.
"""

import json
import pathlib
import shutil
import tempfile
from unittest.mock import patch

import fitz
from django.db.models import F
from django.test import SimpleTestCase, TestCase, override_settings

from scanning import s3_sync
from scanning.factories import (
    ReporterFactory,
    ScanFactory,
    UserFactory,
    VolumeFactory,
)
from scanning.models import (
    Detection,
    QueueStatus,
    Stage,
    Status,
)
from scanning.services import refresh_volume_queue_status
from scanning.tests.pdf_fixtures import (
    BOTTOM_BAR,
    COLUMN_LEFT,
    COLUMN_RIGHT,
    CONTENT,
    PAGE_H,
    PAGE_W,
    write_bitonal_page,
    write_text_page,
    write_two_column_page,
)

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


def _make_scan_with_output(tmpdir=None, **kwargs):
    """Create a scan pointing at the fixture PDF.

    Uses the scan's computed output_dir property. If tmpdir is provided
    (legacy), creates a symlink from the computed path to it.
    """
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
    scan.page_count = 1
    scan.save(update_fields=["original_pdf", "page_count"])

    # Create the computed output_dir
    output = pathlib.Path(scan.output_dir)
    output.mkdir(parents=True, exist_ok=True)

    # If a tmpdir was provided, copy fixture files there too
    if tmpdir:
        _write_bitonal_copy(pathlib.Path(tmpdir) / "bitonal.pdf")
        _write_bitonal_copy(output / "bitonal.pdf")
    return scan


def _write_bitonal_copy(dest):
    """Copy the fixture as ``bitonal.pdf``, distinguishable from the original.

    The processing PDF and the original are the same fixture, so a step that
    reads the multi-GB original where it should read the small processing
    copy produces byte-identical output and no test can tell. A trailing
    comment (ignored by every PDF reader, since it sits after ``%%EOF``)
    makes "which PDF did this read" assertable.
    """
    shutil.copy2(PDF_PATH, dest)
    with open(dest, "ab") as fh:
        fh.write(b"\n% bitonal\n")


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
            output = pathlib.Path(scan.output_dir)
            _run_detect_on_fixture(tmpdir)
            _import_detections_from_json(scan.pk, tmpdir)

            # Delete the file from output_dir and re-sync from DB
            det_path = output / "detections.json"
            # Copy detections.json from tmpdir to output_dir first
            src = pathlib.Path(tmpdir) / "detections.json"
            if src.exists():
                shutil.copy2(src, det_path)
            det_path.unlink(missing_ok=True)
            self.assertFalse(det_path.exists())

            det_data = _sync_detections_to_disk(scan.pk)
            self.assertTrue(det_path.exists())
            on_disk = json.loads(det_path.read_text())
            self.assertEqual(len(on_disk), len(det_data))

    def test_returns_none_without_detections(self):
        from scanning.services import _sync_detections_to_disk

        scan = ScanFactory()
        # output_dir is computed but directory doesn't exist on disk
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
            stored = scan.opinions_json
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

            rects = _compute_and_save_redaction_rects(scan.pk, str(PDF_PATH))
            self.assertGreater(len(rects), 0)

            scan.refresh_from_db()
            self.assertTrue(scan.redaction_rects)

    def test_corrects_the_column_boxes_before_measuring(self):
        """The rects and the margins must read the same column boxes.

        Margin strips are computed from ink-corrected columns. A reviewer
        who hand-draws a missed ``TEXT_COLUMN`` puts it in the DB exactly as
        drawn, so without this the headnote rects on that page would snap to
        the raw box while the margins used the corrected one.
        """
        from scanning import services

        with tempfile.TemporaryDirectory() as tmpdir:
            scan = _make_scan_with_output(
                tmpdir,
                reporter=ReporterFactory(short_name="a3d"),
            )
            _run_detect_on_fixture(tmpdir)
            services._import_detections_from_json(scan.pk, tmpdir)

            with patch.object(services, "snap_document_columns") as snap:
                services._compute_and_save_redaction_rects(
                    scan.pk, str(PDF_PATH)
                )
            snap.assert_called_once()
            document = snap.call_args.args[0]
            self.assertTrue(document.pages, "snapped an empty document")


@override_settings(MEDIA_ROOT=MEDIA_ROOT)
class TestComputeAndSaveMarginRects(TestCase):
    """Test _compute_and_save_margin_rects."""

    def setUp(self):
        _require_fixture(self)

    @staticmethod
    def _with_column(scan):
        """Give a scan the one detection the margin bounds need."""
        Detection.objects.create(
            scan=scan,
            page_index=0,
            label="TEXT_COLUMN",
            label_id=16,
            confidence=0.95,
            x0=100,
            y0=200,
            x1=1600,
            y1=2000,
            img_width=1700,
            img_height=2200,
        )

    def test_writes_margin_rects_to_model(self):
        from scanning.services import _compute_and_save_margin_rects

        with tempfile.TemporaryDirectory() as tmpdir:
            scan = _make_scan_with_output(
                tmpdir,
                reporter=ReporterFactory(short_name="a3d"),
            )
            self._with_column(scan)
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
            self._with_column(scan)
            first = _compute_and_save_margin_rects(
                scan.pk, str(PDF_PATH), tmpdir
            )
            second = _compute_and_save_margin_rects(
                scan.pk, str(PDF_PATH), tmpdir
            )
            self.assertEqual(first, second)

    def test_does_not_cache_a_result_computed_without_detections(self):
        """Margins measured from marks alone lose a page's top strip.

        detections.json belongs to whichever process ran the pipeline, and
        ``/tmp`` is per-container in dev and per-pod in production, so a
        viewer request can arrive before this machine has it. Caching that
        answer means it is never recomputed once the detections land.
        """
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
            self.assertFalse(scan.margin_rects, "cached a detection-less run")

            # ...and once the detections exist, the next call caches.
            self._with_column(scan)
            _compute_and_save_margin_rects(scan.pk, str(PDF_PATH), tmpdir)
            scan.refresh_from_db()
            self.assertTrue(scan.margin_rects)

    def test_reads_detections_from_the_db_not_the_file(self):
        """The DB is the source of truth, and is always reachable."""
        from scanning.services import _detections_for_geometry

        with tempfile.TemporaryDirectory() as tmpdir:
            scan = _make_scan_with_output(
                tmpdir,
                reporter=ReporterFactory(short_name="a3d"),
            )
            self._with_column(scan)
            # No detections.json anywhere near this scan.
            self.assertFalse(
                (pathlib.Path(scan.output_dir) / "detections.json").exists()
            )
            dets = _detections_for_geometry(scan.pk, scan.output_dir)
            self.assertEqual([d["label"] for d in dets], ["TEXT_COLUMN"])
            self.assertEqual(dets[0]["bbox"], [100, 200, 1600, 2000])

    def test_does_not_measure_anything_without_detections(self):
        """Refuse before rendering, not after.

        The viewer asks for these on a sync request and does not cache the
        reply, so measuring a result that is then thrown away un-cached
        renders the whole volume at 100 dpi on every poll, in the executor
        every other sync view shares.
        """
        from scanning import services

        with tempfile.TemporaryDirectory() as tmpdir:
            scan = _make_scan_with_output(
                tmpdir,
                reporter=ReporterFactory(short_name="a3d"),
            )
            with patch.object(services, "compute_margin_rects") as measure:
                rects = services._compute_and_save_margin_rects(
                    scan.pk, str(PDF_PATH), tmpdir
                )
            measure.assert_not_called()
            self.assertEqual(rects, [])


@override_settings(MEDIA_ROOT=MEDIA_ROOT)
class TestSmartDeleteIndexShifting(TestCase):
    """Test that run_smart_delete shifts detection page_index correctly."""

    def _make_scan_with_detections(self, n_pages=3):
        """Create a scan with one detection per page.

        :param n_pages: Number of pages (and detections) to create.
        :returns: The created Scan instance.
        """
        scan = ScanFactory(page_count=n_pages)
        for page_idx in range(n_pages):
            Detection.objects.create(
                scan=scan,
                page_index=page_idx,
                label="CASE_CAPTION",
                label_id=0,
                confidence=0.95,
                x0=0,
                y0=0,
                x1=50,
                y1=50,
            )
        return scan

    def _delete_and_shift(self, scan, delete_idx):
        """Simulate the DB-side page delete: remove detections and shift indices.

        :param scan: The Scan whose detections to update.
        :param delete_idx: 0-based page index to remove.
        :returns: Sorted list of remaining page_index values.
        :rtype: list[int]
        """
        Detection.objects.filter(scan=scan, page_index=delete_idx).delete()
        Detection.objects.filter(scan=scan, page_index__gt=delete_idx).update(
            page_index=F("page_index") - 1
        )
        return list(
            Detection.objects.filter(scan=scan)
            .order_by("page_index")
            .values_list("page_index", flat=True)
        )

    def test_deleting_page_shifts_subsequent_detections(self):
        """Detections on pages after the deleted one should shift down by 1."""
        scan = self._make_scan_with_detections()
        self.assertEqual(Detection.objects.filter(scan=scan).count(), 3)
        remaining = self._delete_and_shift(scan, delete_idx=1)
        self.assertEqual(remaining, [0, 1])

    def test_deleting_first_page_shifts_all(self):
        """Deleting page 0 should shift all remaining detections down by 1."""
        scan = self._make_scan_with_detections()
        remaining = self._delete_and_shift(scan, delete_idx=0)
        self.assertEqual(remaining, [0, 1])

    def test_deleting_last_page_no_shift_needed(self):
        """Deleting the last page should leave preceding indices unchanged."""
        scan = self._make_scan_with_detections()
        remaining = self._delete_and_shift(scan, delete_idx=2)
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
        # output_dir is computed but the directory doesn't exist on disk
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
            scan.opinions_json = [{"dummy": True}]
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
            opinions_json=opinions_data,
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
        scan.save(update_fields=["original_pdf", "page_count"])

        # Create computed output_dir and copy PDF there
        output = pathlib.Path(scan.output_dir)
        output.mkdir(parents=True, exist_ok=True)
        shutil.copy2(pdf_dest, output / pdf_dest.name)
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
            output_dir = scan.output_dir

            from blackletter.api import detect

            dets = detect(
                str(damaged_path),
                output_dir,
                models=["small", "medium", "large"],
            )
            self.assertGreater(len(dets), 0)
            _import_detections_from_json(scan.pk, output_dir)

            det_count = Detection.objects.filter(scan=scan).count()
            self.assertGreater(det_count, 0)
            print(f"\n  YOLO: {det_count} detections on 20-page PDF")

            # --- Step 3: Run PaddleOCR validation → should find gaps ---
            run_paddleocr_validation(scan.pk, str(damaged_path))
            scan.refresh_from_db()

            missing = scan.missing_pages
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
            ocr_results = scan.ocr_results
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
            final_missing = scan.missing_pages
            self.assertEqual(
                final_missing,
                [],
                f"No missing pages expected after re-insert, got: {final_missing}",
            )


@override_settings(MEDIA_ROOT=MEDIA_ROOT)
class TestUploadApprovedFiles(TestCase):
    """Test the upload_approved_files helper."""

    def setUp(self):
        # _s3_client() is lru_cached; drop any client cached under a prior
        # test's boto3 patch so this test's mock is the one that's used.
        s3_sync._cached_s3_client.cache_clear()

    def _make_scan_with_generated_files(self):
        """Create a scan that has been through file generation."""
        scan = ScanFactory(start_page=1, end_page=95, stage=Stage.APPROVED)
        output = pathlib.Path(scan.output_dir)
        output.mkdir(parents=True, exist_ok=True)

        redacted_dir = output / "redacted"
        redacted_dir.mkdir()

        for name in ["a.1.0001-0010.pdf", "a.1.0011-0020.pdf"]:
            (redacted_dir / name).write_bytes(b"%PDF-1.4 redacted")

        short = scan.reporter.short_name
        (output / f"{short}.{scan.volume}.1.95.original.pdf").write_bytes(
            b"%PDF-1.4 original"
        )
        (output / f"{short}.{scan.volume}.1.95.redacted.pdf").write_bytes(
            b"%PDF-1.4 redacted-full"
        )
        return scan

    def test_pre_generation_returns_error(self):
        """Return error when the scan hasn't reached the APPROVED stage."""
        from scanning.services import upload_approved_files

        scan = ScanFactory(start_page=1, end_page=95)
        self.assertNotEqual(scan.stage, Stage.APPROVED)

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
        DEVELOPMENT=False,
        TESTING=False,
    )
    def test_copy_calls_s3_copy_object(self):
        """Approve should issue copy_object per deliverable, not upload_file."""
        from unittest.mock import MagicMock

        from scanning.services import upload_approved_files

        scan = self._make_scan_with_generated_files()
        mock_client = MagicMock()
        # Simulate processing/ has deliverables + non-deliverables.
        short = scan.reporter.short_name
        src_prefix = f"processing/{scan.pk}/{short}/{scan.volume}/1/"
        mock_paginator = MagicMock()
        mock_paginator.paginate.return_value = [
            {
                "Contents": [
                    {"Key": f"{src_prefix}redacted/a.pdf"},
                    {"Key": f"{src_prefix}redacted/b.pdf"},
                    {
                        "Key": f"{src_prefix}{short}.{scan.volume}.1.95.original.pdf"
                    },
                    {
                        "Key": f"{src_prefix}{short}.{scan.volume}.1.95.redacted.pdf"
                    },
                    {"Key": f"{src_prefix}bitonal.pdf"},  # not a deliverable
                    {
                        "Key": f"{src_prefix}detections.json"
                    },  # not a deliverable
                ]
            }
        ]
        mock_client.get_paginator.return_value = mock_paginator

        env = {"AWS_ACCESS_KEY_ID": "test", "AWS_SECRET_ACCESS_KEY": "test"}
        with (
            patch.dict("os.environ", env),
            patch("boto3.client", return_value=mock_client),
        ):
            result = upload_approved_files(scan.pk)

        self.assertIn("4 files", result)
        # upload_file must NOT be used; copy_object handles everything.
        self.assertEqual(mock_client.upload_file.call_count, 0)
        self.assertEqual(mock_client.copy_object.call_count, 4)
        scan.refresh_from_db()
        self.assertTrue(scan.s3_uploaded)

    @override_settings(
        AWS_PRIVATE_STORAGE_BUCKET_NAME="test-bucket",
        DEVELOPMENT=False,
        TESTING=False,
    )
    def test_re_upload_after_changes(self):
        """After reprocessing (s3_uploaded reset), copy runs again."""
        from unittest.mock import MagicMock

        from scanning.services import upload_approved_files

        scan = self._make_scan_with_generated_files()
        scan.s3_uploaded = False
        scan.s3_path = "approved/a/1/1/"
        scan.save(update_fields=["s3_uploaded", "s3_path"])
        mock_client = MagicMock()
        mock_paginator = MagicMock()
        mock_paginator.paginate.return_value = [{"Contents": []}]
        mock_client.get_paginator.return_value = mock_paginator

        env = {"AWS_ACCESS_KEY_ID": "test", "AWS_SECRET_ACCESS_KEY": "test"}
        with (
            patch.dict("os.environ", env),
            patch("boto3.client", return_value=mock_client),
        ):
            upload_approved_files(scan.pk)

        self.assertEqual(mock_client.upload_file.call_count, 0)
        scan.refresh_from_db()
        self.assertTrue(scan.s3_uploaded)


class TestHandlePipelineExceptionRetryCap(TestCase):
    """Test the retry-cap logic in _handle_pipeline_exception."""

    def _make_processing_scan(self, retry_count=0):
        """Return a scan in PROCESSING status with the given retry_count."""
        scan = ScanFactory(status=Status.PROCESSING, retry_count=retry_count)
        return scan

    def test_transient_error_increments_retry_count_and_requeues(self):
        """A RunpodTransientError below the cap re-queues and increments retry_count."""
        from scanning.runpod_client import RunpodTransientError
        from scanning.services import _handle_pipeline_exception

        scan = self._make_processing_scan(retry_count=0)
        exc = RunpodTransientError("NO_GPU")

        with self.settings(RUNPOD_MAX_TRANSIENT_RETRIES=5):
            _handle_pipeline_exception(scan.pk, exc, context="test")

        scan.refresh_from_db()
        self.assertEqual(scan.status, Status.QUEUED)
        self.assertEqual(scan.retry_count, 1)
        self.assertIn("Retrying", scan.progress_message)

    def test_transient_error_at_cap_escalates_to_error(self):
        """When retry_count already equals the cap, the next failure marks ERROR."""
        from scanning.runpod_client import RunpodTransientError
        from scanning.services import _handle_pipeline_exception

        scan = self._make_processing_scan(retry_count=5)
        exc = RunpodTransientError("NO_GPU")

        with self.settings(RUNPOD_MAX_TRANSIENT_RETRIES=5):
            _handle_pipeline_exception(scan.pk, exc, context="test")

        scan.refresh_from_db()
        self.assertEqual(scan.status, Status.ERROR_MAX_RETRIES)
        self.assertEqual(scan.retry_count, 6)
        self.assertIn("Max retries exceeded", scan.progress_message)


def _make_scan_for_volume(volume, start=1, end=100, status=Status.UPLOADED):
    """Create a Scan attached to ``volume`` with sensible defaults."""
    return ScanFactory(
        volume_obj=volume,
        reporter=volume.reporter,
        volume=volume.volume_number,
        start_page=start,
        end_page=end,
        number_of_pages=end - start + 1,
        status=status,
    )


class TestRefreshVolumeQueueStatus(TestCase):
    """Test ``refresh_volume_queue_status`` and its derivation logic."""

    def test_no_scans_unassigned(self):
        volume = VolumeFactory()
        refresh_volume_queue_status(volume)
        volume.refresh_from_db()
        self.assertEqual(volume.queue_status, QueueStatus.NEEDS_SCANNING)

    def test_no_scans_assigned_user(self):
        user = UserFactory()
        volume = VolumeFactory(assigned_to=user)
        refresh_volume_queue_status(volume)
        volume.refresh_from_db()
        self.assertEqual(volume.queue_status, QueueStatus.ASSIGNED)

    def test_partial_coverage_is_scanning(self):
        volume = VolumeFactory()
        _make_scan_for_volume(volume, start=1, end=50)
        refresh_volume_queue_status(volume)
        volume.refresh_from_db()
        self.assertEqual(volume.queue_status, QueueStatus.SCANNING)

    def test_full_coverage_not_approved_is_scanned(self):
        volume = VolumeFactory()
        _make_scan_for_volume(
            volume, start=1, end=100, status=Status.PENDING_REVIEW
        )
        refresh_volume_queue_status(volume)
        volume.refresh_from_db()
        self.assertEqual(volume.queue_status, QueueStatus.SCANNED)

    def test_full_coverage_all_approved_is_complete(self):
        volume = VolumeFactory()
        _make_scan_for_volume(volume, start=1, end=100, status=Status.APPROVED)
        refresh_volume_queue_status(volume)
        volume.refresh_from_db()
        self.assertEqual(volume.queue_status, QueueStatus.COMPLETE)

    def test_unavailable_status_preserved(self):
        volume = VolumeFactory(queue_status=QueueStatus.UNAVAILABLE)
        _make_scan_for_volume(volume, start=1, end=100, status=Status.APPROVED)
        refresh_volume_queue_status(volume)
        volume.refresh_from_db()
        self.assertEqual(volume.queue_status, QueueStatus.UNAVAILABLE)

    def test_expected_parts_fallback_complete(self):
        """All approved + expected_parts met → COMPLETE even without page range."""
        volume = VolumeFactory(
            expected_start_page=None,
            expected_end_page=None,
            expected_parts=2,
        )
        _make_scan_for_volume(volume, start=1, end=50, status=Status.APPROVED)
        _make_scan_for_volume(
            volume, start=51, end=100, status=Status.APPROVED
        )
        refresh_volume_queue_status(volume)
        volume.refresh_from_db()
        self.assertEqual(volume.queue_status, QueueStatus.COMPLETE)

    def test_no_expectations_stays_scanning(self):
        """Volumes without expected_*_page or expected_parts can't be
        auto-completed: the helper has no way to know all work is in,
        so it leaves them at SCANNING until a curator marks otherwise.
        """
        volume = VolumeFactory(
            expected_start_page=None,
            expected_end_page=None,
        )
        _make_scan_for_volume(volume, start=1, end=100, status=Status.APPROVED)
        refresh_volume_queue_status(volume)
        volume.refresh_from_db()
        self.assertEqual(volume.queue_status, QueueStatus.SCANNING)


@override_settings(MEDIA_ROOT=MEDIA_ROOT)
class TestImmediateS3PushOnGeneration(TestCase):
    """Bitonal and OCR PDFs are pushed to S3 the moment they're generated."""

    def test_push_generated_delegates_to_upload(self):
        """The helper forwards to s3_sync.upload_file_to_s3."""
        from scanning import services

        scan = ScanFactory(start_page=1, end_page=2)
        with patch("scanning.s3_sync.upload_file_to_s3") as up:
            services._push_generated_file_to_s3(scan.pk, "bitonal.pdf")

        up.assert_called_once_with(scan, "bitonal.pdf")

    def test_push_generated_skips_in_prod_without_creds(self):
        """In prod with no AWS creds, skip the upload (don't raise)."""
        from scanning import services

        scan = ScanFactory(start_page=1, end_page=2)
        with (
            override_settings(DEVELOPMENT=False, TESTING=False),
            patch.dict("os.environ", {}, clear=True),
            patch("scanning.s3_sync.upload_file_to_s3") as up,
        ):
            services._push_generated_file_to_s3(scan.pk, "bitonal.pdf")

        up.assert_not_called()

    def test_ensure_bitonal_pushes_on_fresh_generation(self):
        """A newly converted bitonal is uploaded immediately."""
        from scanning import services

        scan = ScanFactory(start_page=1, end_page=2)
        output = pathlib.Path(scan.output_dir)
        output.mkdir(parents=True, exist_ok=True)

        def _fake_bitonal(src, out, progress_callback=None):
            (pathlib.Path(out) / "bitonal.pdf").write_bytes(b"%PDF-1.4 bw")

        with (
            patch.object(services, "bl_bitonal", side_effect=_fake_bitonal),
            patch.object(services, "_push_generated_file_to_s3") as push,
            patch.object(services.fitz, "open") as fitz_open,
        ):
            fitz_open.return_value.__enter__.return_value.page_count = 2
            services._ensure_bitonal(scan, output)

        push.assert_called_once_with(scan.pk, "bitonal.pdf")

    def test_ensure_bitonal_skips_push_when_already_present(self):
        """An existing bitonal is neither reconverted nor re-uploaded."""
        from scanning import services

        scan = ScanFactory(start_page=1, end_page=2)
        output = pathlib.Path(scan.output_dir)
        output.mkdir(parents=True, exist_ok=True)
        (output / "bitonal.pdf").write_bytes(b"%PDF-1.4 existing")

        with (
            patch.object(services, "bl_bitonal") as bitonal,
            patch.object(services, "_push_generated_file_to_s3") as push,
            patch.object(services.fitz, "open") as fitz_open,
        ):
            fitz_open.return_value.__enter__.return_value.page_count = 2
            services._ensure_bitonal(scan, output)

        bitonal.assert_not_called()
        push.assert_not_called()


@override_settings(MEDIA_ROOT=MEDIA_ROOT)
class TestGenerateFilesWithoutOcrPdf(TestCase):
    """``run_generate_files`` works from ``bitonal.pdf`` alone.

    The pipeline no longer produces an OCR'd PDF, so generation has to
    build on the processing PDF instead of refusing to run.
    """

    def setUp(self):
        _require_fixture(self)

    def test_generates_from_bitonal(self):
        from scanning import services

        scan = _make_scan_with_output(
            reporter=ReporterFactory(short_name="a3d"),
        )
        output = pathlib.Path(scan.output_dir)
        bitonal = output / "bitonal.pdf"
        _write_bitonal_copy(bitonal)
        scan.opinions_json = [
            {
                "caption_page": 0,
                "key_page": 0,
                "end_page": 0,
                "page_count": 1,
                "first_page_number": 1,
                "last_page_number": 1,
            }
        ]
        scan.save(update_fields=["opinions_json"])

        with (
            # close_all() resets connections for daemon-process forking;
            # in tests it kills the test transaction -- patch it out.
            patch("django.db.connections.close_all"),
            patch("blackletter.api.generate") as generate,
            patch.object(services, "_push_processing_files_to_s3"),
            patch.object(services, "_pull_processing_files_from_s3"),
        ):
            generate.return_value = {
                "opinion_count": 1,
                "full_redacted": "",
                "redacted_dir": str(output / "redacted"),
            }
            services.run_generate_files(scan.pk)

        generate.assert_called_once()
        gen_pdf = pathlib.Path(generate.call_args.kwargs["pdf_path"])
        # Generation runs on a stamped *copy* of the bitonal, never on
        # the bitonal itself.
        self.assertEqual(gen_pdf.name, "stamped.pdf")
        self.assertEqual(gen_pdf.read_bytes(), bitonal.read_bytes())

        scan.refresh_from_db()
        self.assertEqual(scan.stage, Stage.APPROVED)
        self.assertEqual(scan.status, Status.PENDING_REVIEW)

    def test_computes_margins_when_absent(self):
        """Whiteouts must not depend on the viewer having asked for them.

        Margin rects are computed on demand elsewhere, so a scan taken
        straight from review to Generate used to ship with no whiteouts at
        all -- platen bands, fold shadows and corner bleed left in the
        deliverable.
        """
        from scanning import services

        scan = _make_scan_with_output(
            reporter=ReporterFactory(short_name="a3d"),
        )
        output = pathlib.Path(scan.output_dir)
        shutil.copy2(PDF_PATH, output / "bitonal.pdf")
        self.assertFalse(scan.margin_rects)
        # A scan reaching Generate has been detected, and the margin bounds
        # are only cached once detections back them up.
        Detection.objects.create(
            scan=scan,
            page_index=0,
            label="TEXT_COLUMN",
            label_id=16,
            confidence=0.95,
            x0=100,
            y0=200,
            x1=1600,
            y1=2000,
            img_width=1700,
            img_height=2200,
        )

        with (
            patch("django.db.connections.close_all"),
            patch("blackletter.api.generate") as generate,
            patch.object(services, "_push_processing_files_to_s3"),
            patch.object(services, "_pull_processing_files_from_s3"),
        ):
            generate.return_value = {
                "opinion_count": 0,
                "full_redacted": "",
                "redacted_dir": str(output / "redacted"),
            }
            services.run_generate_files(scan.pk)

        scan.refresh_from_db()
        self.assertTrue(scan.margin_rects, "no margin rects computed")
        self.assertTrue(
            any(e["rects"] for e in scan.margin_rects),
            "margin rects are all empty",
        )

    def test_computes_redaction_rects_when_absent(self):
        """Generate is self-sufficient now that the upload path skips rects.

        ``run_full_pipeline`` no longer computes them, and the step 2
        overlay only asks for them if a reviewer opens it, so Generate
        cannot assume they exist.
        """
        from scanning import services

        scan = _make_scan_with_output(
            reporter=ReporterFactory(short_name="a3d"),
        )
        output = pathlib.Path(scan.output_dir)
        _write_bitonal_copy(output / "bitonal.pdf")
        self.assertFalse(scan.redaction_rects)

        with (
            patch("django.db.connections.close_all"),
            patch("blackletter.api.generate") as generate,
            patch.object(services, "_push_processing_files_to_s3"),
            patch.object(services, "_pull_processing_files_from_s3"),
            patch.object(
                services, "_snap_text_columns_to_ink", return_value=0
            ) as snap,
            patch.object(
                services, "_compute_and_save_redaction_rects", return_value=[]
            ) as rects,
        ):
            generate.return_value = {
                "opinion_count": 0,
                "full_redacted": "",
                "redacted_dir": str(output / "redacted"),
            }
            services.run_generate_files(scan.pk)

        rects.assert_called_once()
        self.assertEqual(rects.call_args.args[1], str(output / "bitonal.pdf"))
        # The columns are corrected first: the rects and the margin strips
        # are both measured against them.
        snap.assert_called_once()
        self.assertEqual(snap.call_args.args[1], str(output / "bitonal.pdf"))

    def test_keeps_redaction_rects_a_reviewer_edited(self):
        """Stored rects win, because a reviewer may have moved them.

        ``save_redaction_rect`` writes straight into ``redaction_rects``,
        so recomputing here would throw away every drag, delete and
        hand-added box from step 3.
        """
        from scanning import services

        scan = _make_scan_with_output(
            reporter=ReporterFactory(short_name="a3d"),
        )
        output = pathlib.Path(scan.output_dir)
        _write_bitonal_copy(output / "bitonal.pdf")
        edited = [
            {
                "page_index": 0,
                "rects": [
                    {
                        "x0": 10,
                        "y0": 20,
                        "x1": 30,
                        "y1": 40,
                        "fill": "black",
                        "type": "headnote",
                    }
                ],
            }
        ]
        scan.redaction_rects = edited
        scan.save(update_fields=["redaction_rects"])
        # The rects are stored in image pixels, so the page they name has to
        # still have a detection to scale them by.
        Detection.objects.create(
            scan=scan,
            page_index=0,
            label="TEXT_COLUMN",
            label_id=16,
            confidence=0.95,
            x0=100,
            y0=200,
            x1=1600,
            y1=2000,
            img_width=1700,
            img_height=2200,
        )

        with (
            patch("django.db.connections.close_all"),
            patch("blackletter.api.generate") as generate,
            patch.object(services, "_push_processing_files_to_s3"),
            patch.object(services, "_pull_processing_files_from_s3"),
            patch.object(
                services, "_snap_text_columns_to_ink", return_value=0
            ),
            patch.object(
                services, "_compute_and_save_redaction_rects"
            ) as rects,
        ):
            generate.return_value = {
                "opinion_count": 0,
                "full_redacted": "",
                "redacted_dir": str(output / "redacted"),
            }
            services.run_generate_files(scan.pk)

        rects.assert_not_called()
        scan.refresh_from_db()
        self.assertEqual(scan.redaction_rects, edited)

    def test_raises_without_any_processing_pdf(self):
        """An empty output dir is still an error, just a clearer one."""
        from scanning import services

        scan = _make_scan_with_output(
            reporter=ReporterFactory(short_name="a3d"),
        )
        with (
            patch("django.db.connections.close_all"),
            patch.object(services, "_pull_processing_files_from_s3"),
            patch.object(services, "_handle_pipeline_exception") as handler,
        ):
            services.run_generate_files(scan.pk)

        handler.assert_called_once()
        self.assertIn("bitonal.pdf", str(handler.call_args.args[1]))


class TestRedactionGeometryFromInk(SimpleTestCase):
    """The library contract this app now depends on for headnote rects.

    The app used to patch ``blackletter.process``'s text-bound helpers from
    here, because ``_text_bottom`` returned ``clip.y0`` on a page with no
    words, which collapsed every headnote rect and dropped it: a text-less
    ``bitonal.pdf`` produced *no* headnote rects while every other rect type
    came out unchanged. blackletter measures ink itself now (#68), keyed off
    ``Document.ocr_applied``, which ``_build_document_from_detections``
    always sets. These tests pin that contract, so a library release that
    regressed it would fail here rather than silently ship empty redactions.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = pathlib.Path(self._tmp.name)

    def test_bottom_comes_from_the_ink_without_a_text_layer(self):
        from blackletter.scanner import _text_bottom

        pdf = self.tmp / "bitonal.pdf"
        write_bitonal_page(pdf)
        clip = fitz.Rect(CONTENT.x0, CONTENT.y0, CONTENT.x1, PAGE_H)
        with fitz.open(str(pdf)) as doc:
            bottom = _text_bottom(doc[0], clip)
        self.assertGreater(bottom, clip.y0, "headnote rects would collapse")
        self.assertAlmostEqual(bottom, CONTENT.y1, delta=8.0)

    def test_ocr_applied_prefers_ink_over_our_own_word_boxes(self):
        """Documents are built with ``ocr_applied=True``, which selects ink."""
        from blackletter.scanner import _tighten_to_text

        pdf = self.tmp / "text.pdf"
        write_text_page(pdf, bottom_bar=True)
        rect = fitz.Rect(0, 0, PAGE_W, PAGE_H)
        with fitz.open(str(pdf)) as doc:
            tight = _tighten_to_text(doc[0], rect, ocr_applied=True)
        self.assertIsNotNone(tight)
        # The platen band at the page foot is outside the content box, so
        # ink measurement stops at the last line of text instead.
        self.assertLess(tight.y1, BOTTOM_BAR.y0)

    def test_side_bounds_are_not_narrowed_from_ink(self):
        """Shrinking a headnote rect horizontally is the unsafe direction."""
        from blackletter.scanner import _text_x_bounds

        pdf = self.tmp / "bitonal.pdf"
        write_bitonal_page(pdf)
        clip = fitz.Rect(CONTENT.x0 - 20, 200, CONTENT.x1 + 20, 400)
        with fitz.open(str(pdf)) as doc:
            left, right = _text_x_bounds(doc[0], clip)
        self.assertEqual((left, right), (clip.x0, clip.x1))


class TestSnapTextColumnsToInk(TestCase):
    """``_snap_text_columns_to_ink`` corrects the column boxes at the source.

    YOLO's ``TEXT_COLUMN`` boxes land slightly inside the printed text, and
    three consumers depend on them: headnote rects snap their x-bounds to
    these boxes, margin strips take them as the text band, and the
    outside-opinion masks white out whole columns with them. Widening the
    boxes once means none of those has to compensate.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.pdf = pathlib.Path(self._tmp.name) / "two_col.pdf"
        write_two_column_page(self.pdf)
        self.scan = ScanFactory(reporter=ReporterFactory(short_name="a3d"))

    def _column(self, x0, x1, y0=100.0, y1=700.0):
        """A TEXT_COLUMN detection, in image pixels == PDF points."""
        return Detection.objects.create(
            scan=self.scan,
            page_index=0,
            label="TEXT_COLUMN",
            label_id=16,
            confidence=0.97,
            x0=x0,
            y0=y0,
            x1=x1,
            y1=y1,
            img_width=PAGE_W,
            img_height=PAGE_H,
        )

    def test_widens_a_narrow_box_to_the_text(self):
        from scanning.services import _snap_text_columns_to_ink

        det = self._column(COLUMN_LEFT.x0 + 6, COLUMN_LEFT.x1 - 6)
        changed = _snap_text_columns_to_ink(self.scan.pk, str(self.pdf))
        det.refresh_from_db()
        self.assertEqual(changed, 1)
        self.assertAlmostEqual(det.x0, COLUMN_LEFT.x0, delta=2.0)
        self.assertAlmostEqual(det.x1, COLUMN_LEFT.x1, delta=2.0)

    def test_leaves_the_vertical_bounds_alone(self):
        """Only x is consumed; y feeds nothing and must not move."""
        from scanning.services import _snap_text_columns_to_ink

        det = self._column(COLUMN_LEFT.x0 + 6, COLUMN_LEFT.x1 - 6, 150, 650)
        _snap_text_columns_to_ink(self.scan.pk, str(self.pdf))
        det.refresh_from_db()
        self.assertEqual((det.y0, det.y1), (150, 650))

    def test_does_not_cross_the_gutter(self):
        """A box may not grow into its neighbour's column."""
        from scanning.services import _snap_text_columns_to_ink

        left = self._column(COLUMN_LEFT.x0, COLUMN_LEFT.x1)
        right = self._column(COLUMN_RIGHT.x0, COLUMN_RIGHT.x1)
        _snap_text_columns_to_ink(self.scan.pk, str(self.pdf))
        left.refresh_from_db()
        right.refresh_from_db()
        self.assertLessEqual(
            left.x1, right.x0, f"columns overlap: {left.x1} > {right.x0}"
        )

    def test_is_idempotent(self):
        from scanning.services import _snap_text_columns_to_ink

        self._column(COLUMN_LEFT.x0 + 6, COLUMN_LEFT.x1 - 6)
        self._column(COLUMN_RIGHT.x0 + 6, COLUMN_RIGHT.x1 - 6)
        first = _snap_text_columns_to_ink(self.scan.pk, str(self.pdf))
        second = _snap_text_columns_to_ink(self.scan.pk, str(self.pdf))
        self.assertEqual(first, 2)
        self.assertEqual(second, 0, "snapping moved the boxes twice")

    def test_leaves_an_inconclusive_edge_alone(self):
        """An edge that never finds the end of the ink is not moved.

        A one-column detection over a full-width table would otherwise
        slide out by the growth limit on every run, never settling.
        """
        from scanning.services import _snap_text_columns_to_ink

        # Inset far enough on both sides that growth runs to its limit.
        det = self._column(COLUMN_RIGHT.x0 + 40, COLUMN_RIGHT.x1 - 40)
        before = (det.x0, det.x1)
        changed = _snap_text_columns_to_ink(self.scan.pk, str(self.pdf))
        det.refresh_from_db()
        self.assertEqual(changed, 0)
        self.assertEqual((det.x0, det.x1), before)

    def test_no_columns_is_a_no_op(self):
        from scanning.services import _snap_text_columns_to_ink

        self.assertEqual(
            _snap_text_columns_to_ink(self.scan.pk, str(self.pdf)), 0
        )


@override_settings(MEDIA_ROOT=MEDIA_ROOT)
class TestBuildCombinedRedactionsStaleRects(TestCase):
    """Saved rects and live detections can disagree about which pages exist.

    ``redaction_rects`` is a snapshot on the Scan; the pages come from the
    detections as they stand now. Deactivating the last detection on a page
    that a multi-page headnote block still has rects for leaves a page whose
    pixel coordinates cannot be scaled to points.
    """

    def setUp(self):
        _require_fixture(self)

    def test_says_what_to_do_about_it(self):
        from scanning import services

        with tempfile.TemporaryDirectory() as tmpdir:
            scan = _make_scan_with_output(
                tmpdir,
                reporter=ReporterFactory(short_name="a3d"),
            )
            shutil.copy2(PDF_PATH, pathlib.Path(tmpdir) / "bitonal.pdf")
            # Rects for page 0, and no detection anywhere to scale them by.
            scan.redaction_rects = [
                {
                    "page_index": 0,
                    "rects": [
                        {
                            "x0": 100,
                            "y0": 200,
                            "x1": 800,
                            "y1": 400,
                            "fill": "black",
                            "type": "headnote",
                        }
                    ],
                }
            ]
            scan.save(update_fields=["redaction_rects"])

            with self.assertRaises(RuntimeError) as caught:
                services._build_combined_redactions(scan.pk)
            self.assertIn(
                "Re-add a detection on that page", str(caught.exception)
            )


@override_settings(MEDIA_ROOT=MEDIA_ROOT)
class TestLlmPageTextLayer(TestCase):
    """The post-redaction text layer is opt-in and never implicit.

    Dropping the Tesseract pre-pass is the point of scanning #145, so
    Generate Files must not quietly reintroduce an OCR run over every page.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.llm = pathlib.Path(self._tmp.name) / "llm"
        self.llm.mkdir()
        (self.llm / "page_0001.pdf").write_bytes(b"%PDF-1.4\n")
        self.scan = ScanFactory(reporter=ReporterFactory(short_name="a3d"))

    def test_off_by_default(self):
        from scanning.services import _add_llm_page_text_layer

        with patch("blackletter.api.add_text_layer") as ocr:
            added = _add_llm_page_text_layer(self.scan.pk, self.llm)
        ocr.assert_not_called()
        self.assertEqual(added, 0)

    @override_settings(LLM_PAGE_TEXT_LAYER=True)
    def test_runs_over_the_llm_pages_when_switched_on(self):
        from scanning.services import _add_llm_page_text_layer

        with patch("blackletter.api.add_text_layer") as ocr:
            ocr.return_value = [self.llm / "page_0001.pdf"]
            added = _add_llm_page_text_layer(self.scan.pk, self.llm)
        ocr.assert_called_once_with(self.llm)
        self.assertEqual(added, 1)

    @override_settings(LLM_PAGE_TEXT_LAYER=True)
    def test_skips_a_scan_with_no_llm_directory(self):
        from scanning.services import _add_llm_page_text_layer

        with patch("blackletter.api.add_text_layer") as ocr:
            added = _add_llm_page_text_layer(
                self.scan.pk, self.llm / "missing"
            )
        ocr.assert_not_called()
        self.assertEqual(added, 0)


@override_settings(MEDIA_ROOT=MEDIA_ROOT)
class TestBuildCombinedRedactionsPayload(TestCase):
    """The payload ``generate`` reads mixes two coordinate spaces.

    Redaction rects are stored in image pixels and margin rects in PDF
    points, and both come out of this step in points. Getting that backwards
    puts every blackout in the wrong place, and nothing downstream notices.
    """

    def setUp(self):
        _require_fixture(self)

    def test_pixel_rects_are_scaled_and_point_rects_are_not(self):
        from scanning import services

        with tempfile.TemporaryDirectory() as tmpdir:
            scan = _make_scan_with_output(
                tmpdir,
                reporter=ReporterFactory(short_name="a3d"),
            )
            output = pathlib.Path(scan.output_dir)
            with fitz.open(str(output / "bitonal.pdf")) as doc:
                page_w = doc[0].rect.width
            # An image twice the page's width in points, so the scale is 1:2
            # and a mistake cannot hide behind a factor of one.
            Detection.objects.create(
                scan=scan,
                page_index=0,
                label="TEXT_COLUMN",
                label_id=16,
                confidence=0.95,
                x0=100,
                y0=200,
                x1=800,
                y1=400,
                img_width=int(page_w * 2),
                img_height=2200,
            )
            scan.redaction_rects = [
                {
                    "page_index": 0,
                    "rects": [
                        {
                            "x0": 100,
                            "y0": 200,
                            "x1": 800,
                            "y1": 400,
                            "fill": "black",
                            "type": "headnote",
                        }
                    ],
                }
            ]
            scan.margin_rects = [
                {
                    "page_index": 0,
                    "rects": [{"x0": 0.0, "y0": 0.0, "x1": 20.0, "y1": 50.0}],
                }
            ]
            scan.save(update_fields=["redaction_rects", "margin_rects"])

            payload = json.loads(
                services._build_combined_redactions(scan.pk).read_text()
            )
            page_rects = payload["pages"]["0"]

            headnote = next(r for r in page_rects if r["type"] == "headnote")
            self.assertAlmostEqual(headnote["x0"], 50.0, delta=0.2)
            self.assertAlmostEqual(headnote["x1"], 400.0, delta=0.2)

            margin = next(r for r in page_rects if r["type"] == "margin")
            self.assertEqual(
                margin["x1"], 20.0, "margin rects are already points"
            )
            self.assertEqual(margin["fill"], "white")


@override_settings(MEDIA_ROOT=MEDIA_ROOT)
class TestServeMarginRectsView(TestCase):
    """The endpoint the margin work exists for.

    It must go through the service helper rather than measuring on its own:
    computing here with a separate detection lookup is how the viewer once
    cached margins with no top strips, permanently.
    """

    def setUp(self):
        _require_fixture(self)
        self.user = UserFactory()
        self.client.force_login(self.user)

    def test_computes_through_the_service_helper(self):
        from scanning import services

        with tempfile.TemporaryDirectory() as tmpdir:
            scan = _make_scan_with_output(
                tmpdir,
                uploaded_by=self.user,
                reporter=ReporterFactory(short_name="a3d"),
            )
            output = pathlib.Path(scan.output_dir)
            with patch.object(
                services, "_compute_and_save_margin_rects", return_value=[]
            ) as compute:
                response = self.client.get(f"/scans/{scan.pk}/margin-rects/")
            self.assertEqual(response.status_code, 200)
            compute.assert_called_once_with(
                scan.pk, str(output / "bitonal.pdf"), str(output)
            )

    def test_a_detection_less_scan_is_not_cached(self):
        """It must stay recomputable once the detections land."""
        with tempfile.TemporaryDirectory() as tmpdir:
            scan = _make_scan_with_output(
                tmpdir,
                uploaded_by=self.user,
                reporter=ReporterFactory(short_name="a3d"),
            )
            response = self.client.get(f"/scans/{scan.pk}/margin-rects/")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json(), [])
            scan.refresh_from_db()
            self.assertFalse(scan.margin_rects)

    def test_returns_empty_without_a_processing_pdf(self):
        scan = ScanFactory(uploaded_by=self.user)
        response = self.client.get(f"/scans/{scan.pk}/margin-rects/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [])


@override_settings(MEDIA_ROOT=MEDIA_ROOT)
class TestMarginRectsUseTheDetections(TestCase):
    """The detected pages must reach the measurement.

    They are what intersects the ink content box with the text band and the
    header row: without them one bleed-through blob in a corner drags the
    box out to the page edge and that page's top strip collapses.
    blackletter owns that behaviour and tests it; what this app has to get
    right is handing the pages over, corrected, rather than measuring bare.
    """

    def setUp(self):
        _require_fixture(self)

    def test_passes_the_detected_pages_to_the_measurement(self):
        from blackletter.models import Label

        from scanning import services

        with tempfile.TemporaryDirectory() as tmpdir:
            scan = _make_scan_with_output(
                tmpdir,
                reporter=ReporterFactory(short_name="a3d"),
            )
            Detection.objects.create(
                scan=scan,
                page_index=0,
                label="TEXT_COLUMN",
                label_id=16,
                confidence=0.95,
                x0=100,
                y0=200,
                x1=1600,
                y1=2000,
                img_width=1700,
                img_height=2200,
            )
            with patch.object(
                services, "compute_margin_rects", return_value=[]
            ) as measure:
                services._compute_and_save_margin_rects(
                    scan.pk, str(PDF_PATH), tmpdir
                )
            pages = measure.call_args.kwargs["pages"]
            self.assertTrue(pages, "measured with no detected pages")
            self.assertIn(
                Label.TEXT_COLUMN,
                [d.label for d in pages[0].detections],
            )


@override_settings(MEDIA_ROOT=MEDIA_ROOT)
class TestPagesForGeometry(TestCase):
    """The correction is applied in memory and never written back here.

    ``_snap_text_columns_to_ink`` owns persistence. This helper exists for
    boxes that reached the DB uncorrected, which is what a reviewer's
    hand-drawn column is, and it must leave the row alone.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = pathlib.Path(self._tmp.name)
        self.scan = ScanFactory(reporter=ReporterFactory(short_name="a3d"))
        output = pathlib.Path(self.scan.output_dir)
        output.mkdir(parents=True, exist_ok=True)
        self.pdf = output / "bitonal.pdf"
        write_two_column_page(self.pdf, tmp_dir=self.tmp)
        self.det = Detection.objects.create(
            scan=self.scan,
            page_index=0,
            label="TEXT_COLUMN",
            label_id=16,
            confidence=0.95,
            x0=COLUMN_LEFT.x0 + 6,
            y0=COLUMN_LEFT.y0,
            x1=COLUMN_LEFT.x1 - 6,
            y1=COLUMN_LEFT.y1,
            img_width=PAGE_W,
            img_height=PAGE_H,
        )

    def test_widens_an_uncorrected_box_without_persisting_it(self):
        from scanning.services import _pages_for_geometry

        pages = _pages_for_geometry(
            self.scan, str(self.pdf), self.scan.output_dir
        )
        box = pages[0].detections[0].bbox
        self.assertAlmostEqual(box.x1, COLUMN_LEFT.x0, delta=2.0)
        self.assertAlmostEqual(box.x2, COLUMN_LEFT.x1, delta=2.0)

        self.det.refresh_from_db()
        self.assertEqual(self.det.x0, COLUMN_LEFT.x0 + 6, "persisted the snap")

    def test_snap_false_leaves_the_box_as_stored(self):
        """The steps that never read a column box must not pay to fix one."""
        from scanning.services import _pages_for_geometry

        pages = _pages_for_geometry(
            self.scan, str(self.pdf), self.scan.output_dir, snap=False
        )
        self.assertEqual(pages[0].detections[0].bbox.x1, COLUMN_LEFT.x0 + 6)


@override_settings(MEDIA_ROOT=MEDIA_ROOT)
class TestPipelineCorrectsColumnsAfterDetecting(TestCase):
    """``run_detect`` corrects the column boxes it just imported.

    Re-detect is an explicit request for new geometry, and the reviewer is
    watching one scan rather than waiting on an upload, so it persists the
    correction rather than deferring it the way ``run_full_pipeline`` does.
    Boxes left uncorrected sit a few points inside the printed text, and the
    first or last character of every masked line survives in the
    deliverable.
    """

    def setUp(self):
        _require_fixture(self)

    def test_run_detect_snaps_after_importing(self):
        from scanning import services

        with tempfile.TemporaryDirectory() as tmpdir:
            scan = _make_scan_with_output(
                tmpdir,
                reporter=ReporterFactory(short_name="a3d"),
            )
            output = pathlib.Path(scan.output_dir)
            _run_detect_on_fixture(str(output))

            with (
                patch("django.db.connections.close_all"),
                patch.object(services, "_run_yolo"),
                patch.object(services, "_re_pair_opinions"),
                patch.object(services, "_pull_processing_files_from_s3"),
                patch.object(services, "_push_processing_files_to_s3"),
                patch.object(
                    services, "_snap_text_columns_to_ink", return_value=0
                ) as snap,
            ):
                services.run_detect(scan.pk)

            snap.assert_called_once()
            self.assertEqual(
                snap.call_args.args[1], str(output / "bitonal.pdf")
            )


@override_settings(MEDIA_ROOT=MEDIA_ROOT)
class TestUploadPathSkipsRedactionGeometry(TestCase):
    """The upload path stops at "is this volume complete?".

    Review 1 asks nothing about redaction: no detection overlay, no rects.
    Measuring either here cost three full-volume renders at 100 dpi that a
    scanner sat through before the scan even reached PENDING_REVIEW, which
    is most of what dropping the Tesseract pass was meant to give back.
    """

    def setUp(self):
        _require_fixture(self)

    def test_run_full_pipeline_defers_both(self):
        from scanning import services

        scan = _make_scan_with_output(
            reporter=ReporterFactory(short_name="a3d"),
        )
        output = pathlib.Path(scan.output_dir)
        bitonal = output / "bitonal.pdf"
        _write_bitonal_copy(bitonal)

        with (
            patch("django.db.connections.close_all"),
            patch.object(services, "_pull_processing_files_from_s3"),
            patch.object(services, "_push_processing_files_to_s3"),
            patch.object(services, "_ensure_bitonal", return_value=bitonal),
            patch.object(services, "_run_yolo"),
            patch.object(
                services, "_import_detections_from_json", return_value=[]
            ),
            patch.object(services, "run_paddleocr_validation"),
            patch.object(services, "_re_pair_opinions", return_value=[]),
            patch.object(services, "_snap_text_columns_to_ink") as snap,
            patch.object(
                services, "_compute_and_save_redaction_rects"
            ) as rects,
            patch.object(
                services, "_compute_and_save_margin_rects"
            ) as margins,
        ):
            services.run_full_pipeline(scan.pk)

        snap.assert_not_called()
        rects.assert_not_called()
        margins.assert_not_called()

        scan.refresh_from_db()
        self.assertEqual(scan.status, Status.PENDING_REVIEW)


@override_settings(MEDIA_ROOT=MEDIA_ROOT)
class TestSnapIgnoresDetectionsPastTheEnd(TestCase):
    """A detection can outlive the page it was found on.

    Deleting a page shifts the rows after it down, and a stale row can end
    up pointing past the end of the PDF. Indexing that page raises, and it
    would take the whole pipeline step down with it.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = pathlib.Path(self._tmp.name)
        self.pdf = self.tmp / "two_col.pdf"
        write_two_column_page(self.pdf, tmp_dir=self.tmp)
        self.scan = ScanFactory(reporter=ReporterFactory(short_name="a3d"))

    def _column(self, page_index, x0, x1):
        return Detection.objects.create(
            scan=self.scan,
            page_index=page_index,
            label="TEXT_COLUMN",
            label_id=16,
            confidence=0.97,
            x0=x0,
            y0=100.0,
            x1=x1,
            y1=700.0,
            img_width=PAGE_W,
            img_height=PAGE_H,
        )

    def test_a_detection_past_the_last_page_is_skipped(self):
        from scanning.services import _snap_text_columns_to_ink

        self._column(0, COLUMN_LEFT.x0 + 6, COLUMN_LEFT.x1 - 6)
        ghost = self._column(5, COLUMN_LEFT.x0 + 6, COLUMN_LEFT.x1 - 6)

        self.assertEqual(
            _snap_text_columns_to_ink(self.scan.pk, str(self.pdf)), 1
        )
        ghost.refresh_from_db()
        self.assertEqual(ghost.x0, COLUMN_LEFT.x0 + 6, "moved a ghost box")


@override_settings(MEDIA_ROOT=MEDIA_ROOT)
class TestRebuildIssuesUsesTheCorrectedSplit(TestCase):
    """Auto-correction decides which pages are out of range.

    ``_auto_correct`` can pull a misread page number back into range, so the
    in/out split computed after it is the one that applies. Letting
    ``build_analysis`` recompute the split from scratch would be reading the
    same results a second time and reaching a different answer.
    """

    def test_the_split_is_handed_over_not_recomputed(self):
        from scanning import services

        scan = ScanFactory(start_page=100, end_page=110, page_count=3)
        results = [
            {"pdf_page": 1, "detected": "100", "type": "single"},
            {"pdf_page": 2, "detected": "101", "type": "single"},
            {"pdf_page": 3, "detected": "102", "type": "single"},
        ]
        with patch.object(
            services, "build_analysis", wraps=services.build_analysis
        ) as analyse:
            services._rebuild_issues_from_results(scan.pk, results)

        self.assertIn("out_of_range", analyse.call_args.kwargs)
        self.assertEqual(
            analyse.call_args.kwargs["out_of_range"],
            [],
            "every reading is in range, so nothing should be flagged",
        )

    def test_a_scan_without_an_end_page_still_reports_gaps(self):
        """The old hand-built analysis returned no missing pages at all."""
        from scanning import services
        from scanning.models import Issue

        scan = ScanFactory(start_page=10, end_page=None, page_count=4)
        results = [
            {"pdf_page": 1, "detected": "10", "type": "single"},
            {"pdf_page": 2, "detected": "11", "type": "single"},
            {"pdf_page": 3, "detected": "15", "type": "single"},
            {"pdf_page": 4, "detected": "16", "type": "single"},
        ]
        services._rebuild_issues_from_results(scan.pk, results)

        scan.refresh_from_db()
        self.assertEqual(scan.missing_pages, [12, 13, 14])
        self.assertTrue(Issue.objects.filter(scan=scan).exists())


@override_settings(MEDIA_ROOT=MEDIA_ROOT)
class TestRebuildIssuesFromResults(TestCase):
    """The daemon rebuild behind 'Rebuild & Validate' auto-corrects stray
    OCR readings but leaves hand-entered page numbers alone, matching
    Recheck. run_reprocess never re-OCRs existing pages, so a manual entry
    reaches this path with its ``manual`` markers intact.
    """

    def _results(self, last, **extra):
        """Three good pages plus a fourth reading, optionally manual."""
        return [
            {"pdf_page": 1, "detected": "1", "type": "single"},
            {"pdf_page": 2, "detected": "2", "type": "single"},
            {"pdf_page": 3, "detected": "3", "type": "single"},
            {"pdf_page": 4, "detected": last, "type": "single", **extra},
        ]

    def test_auto_corrects_stray_ocr_reading(self):
        """An out-of-range OCR reading is interpolated from its neighbours."""
        from scanning import services

        scan = ScanFactory(start_page=1, end_page=4, page_count=4)
        results = self._results("999")

        services._rebuild_issues_from_results(scan.pk, results)

        scan.refresh_from_db()
        self.assertEqual(scan.ocr_results[3]["detected"], "4")

    def test_manual_page_number_preserved(self):
        """A curator's typed number survives the rebuild untouched."""
        from scanning import services

        scan = ScanFactory(start_page=1, end_page=4, page_count=4)
        results = self._results("999", zone="manual", ocr="manual")

        services._rebuild_issues_from_results(scan.pk, results)

        scan.refresh_from_db()
        self.assertEqual(scan.ocr_results[3]["detected"], "999")


@override_settings(MEDIA_ROOT=MEDIA_ROOT)
class TestRecalculateIssues(TestCase):
    """Recheck rebuilds issues from stored data.

    It must not need a local copy of the PDF (production web pods never
    download one, SCANNING-1S), and the expected page range comes from
    the scan's start_page/end_page, the same source the validate stage
    uses, rather than from the uploaded filename.
    """

    def _make_scan(self, **kwargs):
        """Create a scan whose original PDF is absent locally, as in prod."""
        scan = ScanFactory(status=Status.PENDING_REVIEW, **kwargs)
        pathlib.Path(scan.original_pdf.path).unlink()
        return scan

    def test_no_local_pdf(self):
        """Rechecking a scan with no local PDF rebuilds issues instead of
        raising FileNotFoundError."""
        from scanning import services

        scan = self._make_scan(
            start_page=1,
            end_page=3,
            page_count=3,
            ocr_results=[
                {"pdf_page": 1, "detected": "1", "type": "single"},
                {"pdf_page": 2, "detected": None, "type": None},
                {"pdf_page": 3, "detected": "3", "type": "single"},
            ],
        )
        with self.assertRaises(FileNotFoundError):
            scan.pdf_path

        services.recalculate_issues(scan)

        scan.refresh_from_db()
        self.assertEqual(scan.status, Status.PENDING_REVIEW)
        self.assertEqual(scan.page_count, 3)
        self.assertEqual(
            [e["pdf_index"] for e in scan.page_map if "pdf_index" in e],
            [0, 1, 2],
        )
        self.assertTrue(
            scan.issues.filter(check_name="no_page_number").exists()
        )

    def test_missing_pages_use_scan_page_range(self):
        """Pages the volume should contain but OCR never saw are reported,
        even when they fall past the last detected number."""
        from scanning import services

        scan = self._make_scan(
            start_page=1,
            end_page=5,
            page_count=3,
            ocr_results=[
                {"pdf_page": 1, "detected": "1", "type": "single"},
                {"pdf_page": 2, "detected": "2", "type": "single"},
                {"pdf_page": 3, "detected": "3", "type": "single"},
            ],
        )

        services.recalculate_issues(scan)

        scan.refresh_from_db()
        self.assertEqual(scan.missing_pages, [4, 5])

    def test_out_of_range_reading_flagged(self):
        """A number far outside the volume's range is flagged as a stray
        reading rather than accepted as a real page number."""
        from scanning import services

        scan = self._make_scan(
            start_page=1,
            end_page=5,
            page_count=1,
            ocr_results=[
                {"pdf_page": 1, "detected": "999", "type": "single"},
            ],
        )

        services.recalculate_issues(scan)

        scan.refresh_from_db()
        self.assertTrue(
            scan.issues.filter(check_name="suspicious_reading").exists()
        )

    def test_no_end_page_falls_back_to_detected_numbers(self):
        """Without an end_page there is no expected range, so missing
        pages are derived from the detected numbers alone."""
        from scanning import services

        scan = self._make_scan(
            start_page=1,
            end_page=None,
            number_of_pages=None,
            page_count=2,
            ocr_results=[
                {"pdf_page": 1, "detected": "4", "type": "single"},
                {"pdf_page": 2, "detected": "6", "type": "single"},
            ],
        )

        services.recalculate_issues(scan)

        scan.refresh_from_db()
        self.assertEqual(scan.missing_pages, [5])

    def test_page_count_refreshed_when_pdf_available(self):
        """When the PDF is on disk, page_count is re-read from it."""
        from scanning import services

        _require_fixture(self)
        scan = _make_scan_with_output(
            status=Status.PENDING_REVIEW,
            ocr_results=[{"pdf_page": 1, "detected": "1", "type": "single"}],
        )
        scan.page_count = 99
        scan.save(update_fields=["page_count"])

        services.recalculate_issues(scan)

        scan.refresh_from_db()
        self.assertEqual(scan.page_count, 1)

    def test_auto_corrects_stray_ocr_reading(self):
        """A stray OCR reading that sits at a consistent offset from its
        neighbours is corrected to the number the sequence implies."""
        from scanning import services

        scan = self._make_scan(
            start_page=1,
            end_page=4,
            page_count=4,
            ocr_results=[
                {"pdf_page": 1, "detected": "1", "type": "single"},
                {"pdf_page": 2, "detected": "2", "type": "single"},
                {"pdf_page": 3, "detected": "3", "type": "single"},
                {"pdf_page": 4, "detected": "999", "type": "single"},
            ],
        )

        services.recalculate_issues(scan)

        scan.refresh_from_db()
        self.assertEqual(scan.ocr_results[3]["detected"], "4")
        self.assertTrue(
            scan.issues.filter(check_name="auto_corrected").exists()
        )

    def test_manual_page_number_not_auto_corrected(self):
        """A page number a curator typed is left alone even when it falls
        outside the volume's range: it is flagged, not overwritten."""
        from scanning import services

        scan = self._make_scan(
            start_page=1,
            end_page=4,
            page_count=4,
            ocr_results=[
                {"pdf_page": 1, "detected": "1", "type": "single"},
                {"pdf_page": 2, "detected": "2", "type": "single"},
                {"pdf_page": 3, "detected": "3", "type": "single"},
                {
                    "pdf_page": 4,
                    "detected": "999",
                    "type": "single",
                    "zone": "manual",
                    "ocr": "manual",
                },
            ],
        )

        services.recalculate_issues(scan)

        scan.refresh_from_db()
        self.assertEqual(scan.ocr_results[3]["detected"], "999")
        self.assertFalse(
            scan.issues.filter(check_name="auto_corrected").exists()
        )
        self.assertTrue(
            scan.issues.filter(check_name="suspicious_reading").exists()
        )

    def test_keeps_suppressed_detection_issues(self):
        """Recheck rebuilds page-number issues but leaves detection
        suppressions, which are curator decisions, in place."""
        from scanning import services
        from scanning.models import CheckName, Issue

        scan = self._make_scan(
            start_page=1,
            end_page=2,
            page_count=2,
            ocr_results=[
                {"pdf_page": 1, "detected": "1", "type": "single"},
                {"pdf_page": 2, "detected": None, "type": None},
            ],
        )
        Issue.objects.create(
            scan=scan,
            check_name=CheckName.SUPPRESS_DETECTION,
            severity="info",
            message="detection 42 suppressed",
        )
        stale = Issue.objects.create(
            scan=scan,
            check_name=CheckName.NO_PAGE_NUMBER,
            page_number=1,
            severity="info",
            message="stale",
        )

        services.recalculate_issues(scan)

        self.assertTrue(
            scan.issues.filter(
                check_name=CheckName.SUPPRESS_DETECTION
            ).exists()
        )
        self.assertFalse(Issue.objects.filter(pk=stale.pk).exists())

    def test_rebuild_page_map_without_local_pdf(self):
        """rebuild_page_map (manual page edits) also runs off stored data
        and applies the scan's page range."""
        from scanning import services

        scan = self._make_scan(
            start_page=1,
            end_page=4,
            page_count=2,
            ocr_results=[
                {"pdf_page": 1, "detected": "1", "type": "single"},
                {"pdf_page": 2, "detected": "2", "type": "single"},
            ],
        )

        services.rebuild_page_map(scan)

        scan.refresh_from_db()
        self.assertEqual(scan.missing_pages, [3, 4])
        self.assertFalse(scan.issues.exists())
