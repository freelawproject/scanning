"""Tests for scanning.services pipeline helpers and functions.

Uses the fixture PDF at scanning/tests/fixtures/a3d.332.1.1.pdf.
"""

import json
import pathlib
import shutil
import tempfile
from unittest.mock import patch

import fitz
from django.test import SimpleTestCase, TestCase, override_settings

from scanning import s3_sync
from scanning.factories import (
    PageEditFactory,
    ReporterFactory,
    ScanFactory,
    UserFactory,
    VolumeFactory,
)
from scanning.models import (
    CheckName,
    Detection,
    Issue,
    PageEdit,
    QueueStatus,
    Scan,
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


def _import_detections(scan_pk, output_dir):
    """Load a ``detections.json`` into Detection rows, clearing old ones.

    Test-local copy of the pipeline importer that left with the legacy
    detect stage (issue #173). The geometry tests still need DB rows
    that mirror what ``_run_detect_on_fixture`` wrote to disk.
    """
    from blackletter.models import Label

    dets = json.loads(
        (pathlib.Path(output_dir) / "detections.json").read_text()
    )
    Detection.objects.filter(scan_id=scan_pk).delete()
    Detection.objects.bulk_create(
        Detection(
            scan_id=scan_pk,
            page_index=d["page_index"],
            label=Label(d["label_id"]).name,
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
            found_by=d.get("found_by", []),
        )
        for d in dets
    )
    return dets


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
class TestSyncDetectionsToDisk(TestCase):
    """Test _sync_detections_to_disk."""

    def setUp(self):
        _require_fixture(self)

    def test_writes_detections_json(self):
        from scanning.services import _sync_detections_to_disk

        with tempfile.TemporaryDirectory() as tmpdir:
            scan = _make_scan_with_output(tmpdir)
            output = pathlib.Path(scan.output_dir)
            _run_detect_on_fixture(tmpdir)
            _import_detections(scan.pk, tmpdir)

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
class TestComputeAndSaveRedactionRects(TestCase):
    """Test _compute_and_save_redaction_rects."""

    def setUp(self):
        _require_fixture(self)

    def test_writes_redaction_rects_json(self):
        from scanning.services import _compute_and_save_redaction_rects

        with tempfile.TemporaryDirectory() as tmpdir:
            scan = _make_scan_with_output(
                tmpdir,
                reporter=ReporterFactory(short_name="a3d"),
            )
            _run_detect_on_fixture(tmpdir)
            _import_detections(scan.pk, tmpdir)

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
            _import_detections(scan.pk, tmpdir)

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
class TestBuildDocumentFromDetections(TestCase):
    """Test _build_document_from_detections."""

    def setUp(self):
        _require_fixture(self)

    def test_builds_document_with_pages(self):
        from scanning.services import (
            _build_document_from_detections,
            _sync_detections_to_disk,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            scan = _make_scan_with_output(
                tmpdir,
                reporter=ReporterFactory(short_name="a3d"),
            )
            _run_detect_on_fixture(tmpdir)
            _import_detections(scan.pk, tmpdir)
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
        user = UserFactory()
        self.client.force_login(user)

        with tempfile.TemporaryDirectory() as tmpdir:
            scan = _make_scan_with_output(
                tmpdir,
                uploaded_by=user,
                reporter=ReporterFactory(short_name="a3d"),
            )
            _run_detect_on_fixture(tmpdir)
            _import_detections(scan.pk, tmpdir)

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


class TestHandlePipelineExceptionReleasesLocalFiles(TestCase):
    """Terminal failures free the local tree; a re-queue keeps it (#215)."""

    def _handle(self, scan, exc):
        from scanning.services import _handle_pipeline_exception

        with (
            self.settings(RUNPOD_MAX_TRANSIENT_RETRIES=5),
            patch("scanning.s3_sync.release_local_processing") as release,
        ):
            _handle_pipeline_exception(scan.pk, exc, context="test")
        return release

    def test_a_requeue_keeps_the_local_files(self):
        from scanning.runpod_client import RunpodTransientError

        scan = ScanFactory(status=Status.PROCESSING, retry_count=0)

        release = self._handle(scan, RunpodTransientError("NO_GPU"))

        release.assert_not_called()

    def test_max_retries_releases_the_local_files(self):
        from scanning.runpod_client import RunpodTransientError

        scan = ScanFactory(status=Status.PROCESSING, retry_count=5)

        release = self._handle(scan, RunpodTransientError("NO_GPU"))

        release.assert_called_once()
        self.assertEqual(release.call_args.args[0].pk, scan.pk)

    def test_a_terminal_error_releases_the_local_files(self):
        scan = ScanFactory(status=Status.PROCESSING)

        release = self._handle(scan, ValueError("boom"))

        release.assert_called_once()
        self.assertEqual(release.call_args.args[0].pk, scan.pk)

    def test_a_lost_guard_keeps_the_local_files(self):
        """The scan left PROCESSING first; someone else owns it now."""
        scan = ScanFactory(status=Status.CANCELLED)

        release = self._handle(scan, ValueError("boom"))

        release.assert_not_called()


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

    def test_recheck_keeps_the_review_1_statuses(self):
        """A recheck must not move a scan between review states (#154):
        a scan in a page-completeness review state keeps it, and only
        the legacy PENDING_REVIEW rows keep getting PENDING_REVIEW."""
        from scanning import services

        for status in (
            Status.READY_FOR_PAGE_COMPLETENESS_REVIEW,
            Status.PAGE_COMPLETENESS_REVIEW_DONE,
        ):
            with self.subTest(status=status):
                scan = self._make_scan(
                    start_page=1,
                    end_page=2,
                    page_count=2,
                    ocr_results=[
                        {"pdf_page": 1, "detected": "1", "type": "single"},
                        {"pdf_page": 2, "detected": "2", "type": "single"},
                    ],
                )
                scan.status = status
                scan.save(update_fields=["status"])

                services.recalculate_issues(scan)

                scan.refresh_from_db()
                self.assertEqual(scan.status, status)
                self.assertTrue(scan.page_map)

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


class TestRunComputeIssues(TestCase):
    """The apply of the glued dots.mocr output (#149/#204, #212).

    Called by ``dots_mocr.apply_ready_runs`` with the scan and the
    glued document's key; the download is patched in. The scan never
    transits QUEUED/PROCESSING, and like the recheck the apply must
    not need a local copy of the PDF.
    """

    def _make_scan(self, **kwargs):
        """Create a scan with no local PDF, parked for the apply.

        :param kwargs: ScanFactory overrides.
        :returns: The scan.
        """
        kwargs.setdefault("status", Status.AWAITING_VALIDATION)
        scan = ScanFactory(start_page=1, end_page=2, page_count=2, **kwargs)
        pathlib.Path(scan.original_pdf.path).unlink()
        return scan

    def _document(self, texts):
        """Build a glued volume document with one header cell per page.

        :param texts: One header text per page; None makes the page a
            filtered one.
        :returns: The document dict.
        """
        from scanning.tests.test_page_numbers import cell, make_page

        return {
            "pages": [
                make_page(index + 1, None if text is None else [cell(text)])
                for index, text in enumerate(texts)
            ]
        }

    def _run(self, scan, document):
        from scanning import services

        with patch(
            "scanning.s3_sync.download_json_object", return_value=document
        ) as download:
            done = services.run_compute_issues(scan, "jobs/x/r1-volume.json")
        return done, download

    def test_the_apply_reads_pages_and_readies_the_scan(self):
        scan = self._make_scan()

        done, download = self._run(scan, self._document(["1", None]))

        scan.refresh_from_db()
        self.assertTrue(done)
        self.assertEqual(
            scan.status, Status.READY_FOR_PAGE_COMPLETENESS_REVIEW
        )
        self.assertEqual(scan.progress_message, "Done")
        self.assertEqual(
            [(r["pdf_page"], r["detected"]) for r in scan.ocr_results],
            [(1, "1"), (2, None)],
        )
        self.assertTrue(scan.page_map)
        self.assertTrue(
            scan.issues.filter(
                check_name=CheckName.NO_PAGE_NUMBER, page_number=2
            ).exists()
        )
        download.assert_called_once_with("jobs/x/r1-volume.json")

    def test_a_legacy_pending_review_scan_takes_the_edge(self):
        scan = self._make_scan(status=Status.PENDING_REVIEW)

        done, _ = self._run(scan, self._document(["1", "2"]))

        scan.refresh_from_db()
        self.assertTrue(done)
        self.assertEqual(
            scan.status, Status.READY_FOR_PAGE_COMPLETENESS_REVIEW
        )

    def test_a_ready_scan_recomputes_without_a_status_write(self):
        scan = self._make_scan(
            status=Status.READY_FOR_PAGE_COMPLETENESS_REVIEW
        )

        done, _ = self._run(scan, self._document(["1", "2"]))

        scan.refresh_from_db()
        self.assertTrue(done)
        self.assertEqual(
            scan.status, Status.READY_FOR_PAGE_COMPLETENESS_REVIEW
        )
        self.assertEqual(scan.ocr_results[1]["detected"], "2")

    def test_a_curators_number_survives_the_apply(self):
        # The row outranks the run, and the blob is rebuilt from both
        # (#214). It used to be carried over from the previous blob by
        # a "manual" stamp on two of its fields.
        scan = self._make_scan()
        PageEditFactory(
            scan=scan,
            kind=PageEdit.Kind.SET_NUMBER,
            pdf_page=1,
            value="9",
        )

        self._run(scan, self._document(["1", "2"]))

        scan.refresh_from_db()
        self.assertEqual(scan.ocr_results[0]["detected"], "9")
        self.assertEqual(scan.ocr_results[0]["zone"], "manual")
        self.assertEqual(scan.ocr_results[1]["detected"], "2")

    def test_a_curators_range_survives_the_apply(self):
        # One PDF page can carry several book pages, which is what
        # CheckName.PAGE_RANGE is raised for.
        scan = self._make_scan()
        PageEditFactory(
            scan=scan,
            kind=PageEdit.Kind.SET_NUMBER,
            pdf_page=1,
            value="678-686",
        )

        self._run(scan, self._document(["1", "2"]))

        scan.refresh_from_db()
        self.assertEqual(scan.ocr_results[0]["detected"], "678-686")
        self.assertEqual(scan.ocr_results[0]["type"], "range")

    def test_a_cleared_number_survives_the_apply(self):
        scan = self._make_scan()
        PageEditFactory(
            scan=scan,
            kind=PageEdit.Kind.SET_NUMBER,
            pdf_page=1,
            value="",
        )

        self._run(scan, self._document(["1", "2"]))

        scan.refresh_from_db()
        self.assertIsNone(scan.ocr_results[0]["detected"])
        self.assertEqual(scan.ocr_results[0]["zone"], "manual")

    def test_an_edit_against_another_original_is_reported(self):
        # An address names a page of the original it was made on. An
        # edit from another one is dropped and said out loud, never
        # placed on whatever page now holds that number.
        scan = self._make_scan()
        Scan.objects.filter(pk=scan.pk).update(source_fingerprint="200:2")
        scan.refresh_from_db()
        PageEditFactory(
            scan=scan,
            kind=PageEdit.Kind.SET_NUMBER,
            pdf_page=1,
            value="9",
            source_fingerprint="100:2",
        )

        self._run(scan, self._document(["1", "2"]))

        scan.refresh_from_db()
        self.assertEqual(scan.ocr_results[0]["detected"], "1")
        self.assertTrue(
            scan.issues.filter(
                check_name=CheckName.STALE_PAGE_EDIT, page_number=1
            ).exists()
        )

    def test_an_edit_naming_an_absent_page_is_reported(self):
        scan = self._make_scan()
        PageEditFactory(
            scan=scan,
            kind=PageEdit.Kind.SET_NUMBER,
            pdf_page=7,
            value="9",
        )

        self._run(scan, self._document(["1", "2"]))

        scan.refresh_from_db()
        self.assertEqual(len(scan.ocr_results), 2)
        self.assertTrue(
            scan.issues.filter(
                check_name=CheckName.STALE_PAGE_EDIT, page_number=7
            ).exists()
        )

    def test_suppressed_detection_issues_are_kept(self):
        scan = self._make_scan()
        Issue.objects.create(
            scan=scan,
            check_name=CheckName.SUPPRESS_DETECTION,
            severity=Issue.Severity.INFO,
            message="curator decision",
        )

        self._run(scan, self._document(["1", "2"]))

        self.assertTrue(
            scan.issues.filter(
                check_name=CheckName.SUPPRESS_DETECTION
            ).exists()
        )

    def test_a_second_apply_recomputes_idempotently(self):
        scan = self._make_scan()

        self._run(scan, self._document(["1", "2"]))
        scan.refresh_from_db()
        done, _ = self._run(scan, self._document(["1", "2"]))

        scan.refresh_from_db()
        self.assertTrue(done)
        self.assertEqual(
            scan.status, Status.READY_FOR_PAGE_COMPLETENESS_REVIEW
        )
        self.assertEqual(len(scan.ocr_results), 2)
        self.assertEqual(
            scan.issues.filter(check_name=CheckName.NO_PAGE_NUMBER).count(),
            0,
        )

    def test_a_lost_edge_leaves_the_scan_alone(self):
        """The #210 review race, closed: a scan cancelled between the
        pass's read and the edge write keeps its status, and its
        Issues are not rebuilt over a decision somebody just made."""
        scan = self._make_scan()
        Scan.objects.filter(pk=scan.pk).update(status=Status.CANCELLED)

        done, _ = self._run(scan, self._document(["1", "2"]))

        scan.refresh_from_db()
        self.assertFalse(done)
        self.assertEqual(scan.status, Status.CANCELLED)
        self.assertFalse(scan.issues.exists())

    def test_a_download_failure_raises_to_the_caller(self):
        from scanning import services

        scan = self._make_scan()

        with patch(
            "scanning.s3_sync.download_json_object",
            side_effect=RuntimeError("boom"),
        ):
            with self.assertRaises(RuntimeError):
                services.run_compute_issues(scan, "jobs/x/r1-volume.json")

        scan.refresh_from_db()
        self.assertEqual(scan.status, Status.AWAITING_VALIDATION)
        self.assertFalse(scan.issues.exists())


@override_settings(MEDIA_ROOT=MEDIA_ROOT, DEVELOPMENT=True)
class TestFullPipelineConvertBranches(TestCase):
    """Where ``run_full_pipeline`` leaves a scan (issue #176).

    Four outcomes, and which one a volume gets is the whole decision
    this stage makes: hand the shards to doctor, or park because there
    is nothing to convert with, nothing worth converting, or no shards.
    """

    def _scan(self, pages=2):
        """Build a QUEUED scan whose original PDF exists on disk."""
        scan = ScanFactory(
            reporter=ReporterFactory(short_name="tc"),
            volume=176,
            start_page=1,
            end_page=pages,
            status=Status.PROCESSING,
        )
        output_dir = pathlib.Path(scan.output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        original = output_dir / pathlib.Path(scan.original_pdf.name).name
        doc = fitz.open()
        for _ in range(pages):
            doc.new_page(width=PAGE_W, height=PAGE_H)
        doc.save(str(original))
        doc.close()
        return scan

    def _run(self, scan, manifest, is_bitonal=False, doctor=True):
        """Run the pipeline with sharding and the skip check stubbed."""
        from scanning import services

        with (
            patch("scanning.services._ensure_shards", return_value=manifest),
            patch(
                "scanning.bitonal.source_is_bitonal",
                return_value=is_bitonal,
            ),
            patch("scanning.doctor_client.enabled", return_value=doctor),
            # S3 is inert under TESTING, and the pipeline refuses to
            # create jobs whose shards doctor could not fetch.
            patch("scanning.s3_sync.s3_active", return_value=True),
            patch("django.db.connections.close_all"),
        ):
            services.run_full_pipeline(scan.pk)
        scan.refresh_from_db()
        return scan

    @staticmethod
    def _manifest(shard_count=3, pages_per_shard=1):
        from scanning.tests.test_jobs import make_manifest

        return make_manifest(shard_count, pages_per_shard)

    def test_a_convertible_volume_waits_on_its_jobs(self):
        from scanning.models import ExternalJob, JobStage, JobStatus

        scan = self._scan(pages=3)

        scan = self._run(scan, self._manifest(shard_count=3))

        self.assertEqual(scan.status, Status.AWAITING)
        self.assertEqual(scan.page_count, 3)
        self.assertEqual(scan.progress_total, 3)
        self.assertIn("Converting 3 part", scan.progress_message)
        rows = ExternalJob.objects.filter(scan=scan, stage=JobStage.CONVERT)
        self.assertEqual(rows.count(), 3)
        self.assertEqual({row.status for row in rows}, {JobStatus.PENDING})

    def test_an_already_bitonal_volume_skips_the_stage(self):
        """Converting it would cost a full raster pass to save ~11%."""
        from scanning.models import ExternalJob

        scan = self._scan(pages=2)

        scan = self._run(scan, self._manifest(), is_bitonal=True)

        self.assertEqual(scan.status, Status.AWAITING_VALIDATION)
        self.assertEqual(scan.page_count, 2)
        self.assertIn("already bitonal", scan.progress_message)
        self.assertFalse(ExternalJob.objects.filter(scan=scan).exists())

    def test_without_doctor_the_scan_parks_as_before(self):
        """No in-process fallback exists, so this is the #173 behaviour."""
        from scanning.models import ExternalJob

        scan = self._scan(pages=2)

        scan = self._run(scan, self._manifest(), doctor=False)

        self.assertEqual(scan.status, Status.AWAITING_VALIDATION)
        self.assertIn("temporarily disabled", scan.progress_message)
        self.assertFalse(ExternalJob.objects.filter(scan=scan).exists())

    def test_without_s3_no_jobs_are_created(self):
        """Doctor fetches shards from S3, so no S3 means no conversion.

        This is what makes DOCTOR_ENABLED=True safe as a default:
        TESTING and a dev environment without credentials never uploaded
        the shards, so a job created there could never be submitted and
        would park its scan in AWAITING until its queue deadline expired
        hours later. Parking it unconverted is the honest outcome.
        """
        from scanning import services
        from scanning.models import ExternalJob

        scan = self._scan(pages=2)

        with (
            patch(
                "scanning.services._ensure_shards",
                return_value=self._manifest(),
            ),
            patch("scanning.doctor_client.enabled", return_value=True),
            patch("scanning.s3_sync.s3_active", return_value=False),
            patch("django.db.connections.close_all"),
        ):
            services.run_full_pipeline(scan.pk)

        scan.refresh_from_db()
        self.assertEqual(scan.status, Status.AWAITING_VALIDATION)
        self.assertFalse(ExternalJob.objects.filter(scan=scan).exists())

    def test_without_shards_no_jobs_are_created(self):
        """Sharding disabled means there is nothing for a job to read."""
        from scanning.models import ExternalJob

        scan = self._scan(pages=2)

        scan = self._run(scan, None)

        self.assertEqual(scan.status, Status.AWAITING_VALIDATION)
        self.assertFalse(ExternalJob.objects.filter(scan=scan).exists())

    def test_a_second_run_reuses_the_jobs(self):
        """The re-queue path must not pay for the conversion twice."""
        from scanning.models import ExternalJob

        scan = self._scan(pages=3)
        manifest = self._manifest(shard_count=3)

        self._run(scan, manifest)
        first = set(
            ExternalJob.objects.filter(scan=scan).values_list("pk", flat=True)
        )
        Scan = type(scan)
        Scan.objects.filter(pk=scan.pk).update(status=Status.PROCESSING)
        self._run(scan, manifest)

        self.assertEqual(
            set(
                ExternalJob.objects.filter(scan=scan).values_list(
                    "pk", flat=True
                )
            ),
            first,
        )

    def test_local_files_release_after_the_push(self):
        """S3 holds every byte after the push; the tree is a cache (#215)."""
        scan = self._scan(pages=3)

        with patch("scanning.s3_sync.release_local_processing") as release:
            self._run(scan, self._manifest(shard_count=3))

        release.assert_called_once()
        self.assertEqual(release.call_args.args[0].pk, scan.pk)

    def test_a_parked_unconverted_scan_also_releases(self):
        scan = self._scan(pages=2)

        with patch("scanning.s3_sync.release_local_processing") as release:
            self._run(scan, self._manifest(), doctor=False)

        release.assert_called_once()

    def test_a_failed_push_keeps_the_local_files(self):
        """The local tree may hold bytes S3 never received."""
        scan = self._scan(pages=2)

        with (
            patch(
                "scanning.services._push_processing_files_to_s3",
                return_value=False,
            ),
            patch("scanning.s3_sync.release_local_processing") as release,
        ):
            self._run(scan, self._manifest(), doctor=False)

        release.assert_not_called()


@override_settings(MEDIA_ROOT=MEDIA_ROOT, DEVELOPMENT=True)
class TestFullPipelineOcrEnqueue(TestCase):
    """The pipeline enqueues the dots.mocr read (issue #207).

    The OCR rows are independent of the bitonal branch: the stage
    reads the original shards, so a volume that parks unconverted, or
    skips the conversion, still gets its read. ``_can_analyze`` is the
    gate, mirror of ``_can_convert``.
    """

    def _scan(self, pages=2, status=Status.PROCESSING):
        scan = ScanFactory(
            reporter=ReporterFactory(short_name="tc"),
            volume=207,
            start_page=1,
            end_page=pages,
            status=status,
        )
        output_dir = pathlib.Path(scan.output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        original = output_dir / pathlib.Path(scan.original_pdf.name).name
        doc = fitz.open()
        for _ in range(pages):
            doc.new_page(width=PAGE_W, height=PAGE_H)
        doc.save(str(original))
        doc.close()
        return scan

    @staticmethod
    def _manifest(shard_count=2):
        from scanning.tests.test_jobs import make_manifest

        return make_manifest(shard_count, 1)

    def _run(
        self,
        scan,
        manifest,
        is_bitonal=False,
        doctor=True,
        dots=True,
        s3=True,
        cancel_midway=False,
    ):
        from scanning import services
        from scanning.models import Scan as ScanModel

        def _shard(inner_scan):
            if cancel_midway:
                ScanModel.objects.filter(pk=inner_scan.pk).update(
                    status=Status.CANCELLED
                )
            return manifest

        with (
            patch("scanning.services._ensure_shards", side_effect=_shard),
            patch(
                "scanning.bitonal.source_is_bitonal",
                return_value=is_bitonal,
            ),
            patch("scanning.doctor_client.enabled", return_value=doctor),
            patch("scanning.dots_mocr.enabled", return_value=dots),
            patch("scanning.s3_sync.s3_active", return_value=s3),
            patch("django.db.connections.close_all"),
        ):
            services.run_full_pipeline(scan.pk)
        scan.refresh_from_db()
        return scan

    @staticmethod
    def _analyze_rows(scan):
        from scanning.models import ExternalJob, JobStage

        return list(
            ExternalJob.objects.filter(
                scan=scan, stage=JobStage.ANALYZE
            ).order_by("shard_index")
        )

    def test_a_new_upload_gets_ocr_rows_beside_the_convert_rows(self):
        from scanning.models import ExternalJob, JobStage, JobStatus

        scan = self._scan(pages=2)

        scan = self._run(scan, self._manifest(shard_count=2))

        self.assertEqual(scan.status, Status.AWAITING)
        rows = self._analyze_rows(scan)
        self.assertEqual(len(rows), 2)
        self.assertEqual({row.status for row in rows}, {JobStatus.PENDING})
        self.assertEqual(
            ExternalJob.objects.filter(
                scan=scan, stage=JobStage.CONVERT
            ).count(),
            2,
        )

    def test_a_volume_doctor_cannot_serve_still_gets_ocr(self):
        from scanning.models import ExternalJob, JobStage, JobStatus

        scan = self._scan(pages=2)

        scan = self._run(scan, self._manifest(), doctor=False)

        self.assertEqual(scan.status, Status.AWAITING_VALIDATION)
        rows = self._analyze_rows(scan)
        self.assertEqual(len(rows), 2)
        self.assertEqual({row.status for row in rows}, {JobStatus.PENDING})
        self.assertFalse(
            ExternalJob.objects.filter(
                scan=scan, stage=JobStage.CONVERT
            ).exists()
        )

    def test_an_already_bitonal_volume_still_gets_ocr(self):
        scan = self._scan(pages=2)

        scan = self._run(scan, self._manifest(), is_bitonal=True)

        self.assertEqual(scan.status, Status.AWAITING_VALIDATION)
        self.assertEqual(len(self._analyze_rows(scan)), 2)

    def test_dots_mocr_off_creates_no_ocr_rows(self):
        scan = self._scan(pages=2)

        scan = self._run(scan, self._manifest(), dots=False)

        self.assertEqual(scan.status, Status.AWAITING)
        self.assertEqual(self._analyze_rows(scan), [])

    def test_without_s3_no_ocr_rows_are_created(self):
        """The worker fetches its shard through a presigned GET."""
        scan = self._scan(pages=2)

        scan = self._run(scan, self._manifest(), s3=False)

        self.assertEqual(scan.status, Status.AWAITING_VALIDATION)
        self.assertEqual(self._analyze_rows(scan), [])

    def test_without_shards_no_ocr_rows_are_created(self):
        scan = self._scan(pages=2)

        scan = self._run(scan, None)

        self.assertEqual(scan.status, Status.AWAITING_VALIDATION)
        self.assertEqual(self._analyze_rows(scan), [])

    def test_a_scan_cancelled_mid_shard_hands_back_its_ocr_rows(self):
        from scanning.models import JobStatus

        scan = self._scan(pages=2)

        with self.assertLogs("scanning.services", level="WARNING"):
            scan = self._run(scan, self._manifest(), cancel_midway=True)

        self.assertEqual(scan.status, Status.CANCELLED)
        rows = self._analyze_rows(scan)
        self.assertEqual(len(rows), 2)
        self.assertEqual({row.status for row in rows}, {JobStatus.CANCELLED})

    def test_a_lost_claim_keeps_carried_results_carryable(self):
        """The claim is lost most often to the daemon's own shutdown,
        which re-queues the scan and returns -- a retry, not an end. A
        carried COMPLETED row is a paid result the retry re-reads, so
        the hand-back cancels only the unstarted rows."""
        from scanning import dots_mocr
        from scanning.models import ExternalJob, JobStatus

        scan = self._scan(pages=2)
        manifest = self._manifest(shard_count=2)
        old = dots_mocr.ensure_analyze_jobs(scan, manifest)
        ExternalJob.objects.filter(pk=old[0].pk).update(
            status=JobStatus.COMPLETED,
            result_key="jobs/analyze/dots_mocr/r1-s0-a1.json",
        )
        ExternalJob.objects.filter(pk=old[1].pk).update(
            status=JobStatus.FAILED
        )

        with (
            patch("scanning.s3_sync.object_exists", return_value=True),
            self.assertLogs("scanning.services", level="WARNING"),
        ):
            scan = self._run(scan, manifest, cancel_midway=True)

        self.assertEqual(scan.status, Status.CANCELLED)
        carried, pending = dots_mocr.live_analyze_jobs(scan)
        self.assertEqual(carried.status, JobStatus.COMPLETED)
        self.assertEqual(pending.status, JobStatus.CANCELLED)

        # The retry (an admin re-queue) carries the kept result again.
        type(scan).objects.filter(pk=scan.pk).update(status=Status.PROCESSING)
        with patch("scanning.s3_sync.object_exists", return_value=True):
            scan = self._run(scan, manifest)
        fresh = dots_mocr.live_analyze_jobs(scan)
        self.assertEqual(fresh[0].status, JobStatus.COMPLETED)
        self.assertEqual(
            fresh[0].result_key, "jobs/analyze/dots_mocr/r1-s0-a1.json"
        )
        self.assertEqual(fresh[1].status, JobStatus.PENDING)

    def test_a_requeue_reuses_a_consumed_ocr_run(self):
        """An applied run is history nobody pays for twice."""
        from scanning import dots_mocr
        from scanning.models import ExternalJob, JobStatus

        scan = self._scan(pages=2)
        manifest = self._manifest(shard_count=2)
        first = dots_mocr.ensure_analyze_jobs(scan, manifest)
        ExternalJob.objects.filter(pk__in=[row.pk for row in first]).update(
            status=JobStatus.CONSUMED
        )

        scan = self._run(scan, manifest)

        rows = self._analyze_rows(scan)
        self.assertEqual({row.pk for row in rows}, {row.pk for row in first})
        self.assertEqual({row.status for row in rows}, {JobStatus.CONSUMED})


class TestApplyUploadAction(TestCase):
    """Both upload actions queue the pipeline (issue #176)."""

    def test_upload_validate_queues_for_processing(self):
        from scanning.models import QueuedAction, UploadAction
        from scanning.services import apply_upload_action

        scan = ScanFactory(status=Status.UPLOADED)

        apply_upload_action(scan, UploadAction.UPLOAD_VALIDATE)

        scan.refresh_from_db()
        self.assertEqual(scan.status, Status.QUEUED)
        self.assertEqual(scan.stage, Stage.VALIDATE)
        self.assertEqual(scan.queued_action, QueuedAction.FULL_PIPELINE)
        self.assertIn("processing", scan.progress_message)

    def test_upload_only_is_queued_too(self):
        """It used to stay UPLOADED, so it never got a preview at all."""
        from scanning.models import QueuedAction, UploadAction
        from scanning.services import apply_upload_action

        scan = ScanFactory(status=Status.UPLOADED)

        apply_upload_action(scan, UploadAction.UPLOAD_ONLY)

        scan.refresh_from_db()
        self.assertEqual(scan.status, Status.QUEUED)
        self.assertEqual(scan.queued_action, QueuedAction.FULL_PIPELINE)
        self.assertIn("conversion", scan.progress_message)


@override_settings(MEDIA_ROOT=MEDIA_ROOT, DEVELOPMENT=True)
class TestFullPipelineRequeue(TestCase):
    """Re-queueing a scan that has already been through the stage."""

    def _scan_with_jobs(self, statuses):
        """Build a PROCESSING scan whose convert run has these statuses."""
        from scanning import jobs
        from scanning.models import ExternalJob
        from scanning.tests.test_jobs import make_manifest

        scan = ScanFactory(
            reporter=ReporterFactory(short_name="tc"),
            volume=176,
            start_page=1,
            end_page=len(statuses),
            status=Status.PROCESSING,
        )
        output_dir = pathlib.Path(scan.output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        original = output_dir / pathlib.Path(scan.original_pdf.name).name
        doc = fitz.open()
        for _ in statuses:
            doc.new_page(width=PAGE_W, height=PAGE_H)
        doc.save(str(original))
        doc.close()

        manifest = make_manifest(len(statuses), 1)
        rows = jobs.ensure_convert_jobs(scan, manifest)
        for job, status in zip(rows, statuses, strict=True):
            ExternalJob.objects.filter(pk=job.pk).update(status=status)
        return scan, manifest

    def _run(self, scan, manifest):
        from scanning import services

        with (
            patch("scanning.services._ensure_shards", return_value=manifest),
            patch("scanning.bitonal.source_is_bitonal", return_value=False),
            patch("scanning.doctor_client.enabled", return_value=True),
            patch("scanning.s3_sync.s3_active", return_value=True),
            patch("django.db.connections.close_all"),
        ):
            services.run_full_pipeline(scan.pk)
        scan.refresh_from_db()
        return scan

    def test_an_already_converted_volume_is_not_converted_again(self):
        """Its shard results are deleted, so a re-merge would fail too."""
        from scanning.models import ExternalJob, JobStatus

        scan, manifest = self._scan_with_jobs(
            [JobStatus.CONSUMED, JobStatus.CONSUMED]
        )

        scan = self._run(scan, manifest)

        self.assertEqual(scan.status, Status.AWAITING_VALIDATION)
        self.assertEqual(
            ExternalJob.objects.filter(scan=scan).count(),
            2,
            "no second run should have been created",
        )

    def test_a_cancelled_run_is_started_over(self):
        """The admin re-queue path: abandoned rows must not park a scan."""
        from scanning.models import ExternalJob, JobStatus

        scan, manifest = self._scan_with_jobs(
            [JobStatus.CANCELLED, JobStatus.CANCELLED]
        )

        scan = self._run(scan, manifest)

        self.assertEqual(scan.status, Status.AWAITING)
        pending = ExternalJob.objects.filter(
            scan=scan, status=JobStatus.PENDING
        )
        self.assertEqual(pending.count(), 2)
        self.assertEqual({job.run for job in pending}, {2})


@override_settings(MEDIA_ROOT=MEDIA_ROOT, DEVELOPMENT=True)
class TestFullPipelineStatusGuard(TestCase):
    """The pipeline only moves a scan it still owns (issue #176).

    The daemon claims a scan by moving it to PROCESSING, so anything
    else means somebody took it away -- and writing AWAITING anyway
    would start real external work on a volume that was stopped.
    """

    def _scan(self, status):
        scan = ScanFactory(
            reporter=ReporterFactory(short_name="tc"),
            volume=176,
            start_page=1,
            end_page=2,
            status=status,
        )
        output_dir = pathlib.Path(scan.output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        original = output_dir / pathlib.Path(scan.original_pdf.name).name
        doc = fitz.open()
        doc.new_page(width=PAGE_W, height=PAGE_H)
        doc.new_page(width=PAGE_W, height=PAGE_H)
        doc.save(str(original))
        doc.close()
        return scan

    def _run(self, scan, cancel_midway=False):
        from scanning import services
        from scanning.models import Scan as ScanModel
        from scanning.tests.test_jobs import make_manifest

        def _shard(inner_scan):
            if cancel_midway:
                ScanModel.objects.filter(pk=inner_scan.pk).update(
                    status=Status.CANCELLED
                )
            return make_manifest(shard_count=2, pages_per_shard=1)

        with (
            patch("scanning.services._ensure_shards", side_effect=_shard),
            patch("scanning.bitonal.source_is_bitonal", return_value=False),
            patch("scanning.doctor_client.enabled", return_value=True),
            patch("scanning.s3_sync.s3_active", return_value=True),
            patch("django.db.connections.close_all"),
        ):
            services.run_full_pipeline(scan.pk)
        scan.refresh_from_db()
        return scan

    def test_a_scan_cancelled_mid_shard_is_not_resurrected(self):
        from scanning.models import ExternalJob, JobStatus

        scan = self._scan(Status.PROCESSING)

        with self.assertLogs("scanning.services", level="WARNING"):
            scan = self._run(scan, cancel_midway=True)

        self.assertEqual(scan.status, Status.CANCELLED)
        # Its rows were created before the status write failed, so they
        # are handed back rather than left for a wave to convert.
        self.assertEqual(
            set(
                ExternalJob.objects.filter(scan=scan).values_list(
                    "status", flat=True
                )
            ),
            {JobStatus.CANCELLED},
        )

    def test_the_park_paths_are_guarded_too(self):
        from scanning import services

        scan = self._scan(Status.CANCELLED)

        with self.assertLogs("scanning.services", level="WARNING"):
            services._park_unconverted(scan.pk, 2, "parked")

        scan.refresh_from_db()
        self.assertEqual(scan.status, Status.CANCELLED)
        self.assertNotEqual(scan.progress_message, "parked")
