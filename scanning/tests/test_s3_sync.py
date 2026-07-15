"""Tests for scanning.s3_sync with mocked boto3."""

import os
import pathlib
import tempfile
from unittest.mock import MagicMock, patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase, override_settings

from scanning import s3_sync
from scanning.factories import ReporterFactory, ScanFactory
from scanning.models import Status

MEDIA_ROOT = tempfile.mkdtemp()


def _reporter_scan(**kwargs):
    """Build a scan with a reporter for predictable S3 prefixes.

    :returns: A Scan instance with reporter/volume/start_page set.
    :rtype: Scan
    """
    reporter = ReporterFactory(short_name="tc")
    defaults = {
        "reporter": reporter,
        "volume": 164,
        "start_page": 1,
        "end_page": 2,
    }
    defaults.update(kwargs)
    return ScanFactory(**defaults)


@override_settings(
    MEDIA_ROOT=MEDIA_ROOT,
    DEVELOPMENT=True,
    AWS_PRIVATE_STORAGE_BUCKET_NAME="test-bucket",
)
class TestShortCircuitOnDevelopment(TestCase):
    """When DEVELOPMENT=True, sync helpers must never call boto3."""

    def test_upload_processing_files_noop(self):
        scan = _reporter_scan()
        with patch("scanning.s3_sync.boto3") as mock_boto3:
            result = s3_sync.upload_processing_files(scan)
        self.assertEqual(result, 0)
        mock_boto3.client.assert_not_called()

    def test_download_processing_files_noop(self):
        scan = _reporter_scan()
        with patch("scanning.s3_sync.boto3") as mock_boto3:
            result = s3_sync.download_processing_files(scan)
        self.assertIsNone(result)
        mock_boto3.client.assert_not_called()

    def test_upload_file_to_s3_noop(self):
        scan = _reporter_scan()
        with patch("scanning.s3_sync.boto3") as mock_boto3:
            result = s3_sync.upload_file_to_s3(scan, "detections.json")
        self.assertFalse(result)
        mock_boto3.client.assert_not_called()


@override_settings(
    MEDIA_ROOT=MEDIA_ROOT,
    DEVELOPMENT=False,
    TESTING=False,
    AWS_ACCESS_KEY_ID="fake",
    AWS_SECRET_ACCESS_KEY="fake",
    AWS_PRIVATE_STORAGE_BUCKET_NAME="test-bucket",
    PROCESSING_TMP_DIR=tempfile.mkdtemp(),
)
class TestSyncHelpersWithCredentials(TestCase):
    """With creds and DEV=False, helpers should hit boto3."""

    def setUp(self):
        # _s3_client() is lru_cached; drop any client cached under a prior
        # test's boto3 patch so this test's mock is the one that's used.
        s3_sync._cached_s3_client.cache_clear()
        # has_s3_credentials reads os.environ, not Django settings.
        self._env_patch = patch.dict(
            os.environ,
            {"AWS_ACCESS_KEY_ID": "fake", "AWS_SECRET_ACCESS_KEY": "fake"},
        )
        self._env_patch.start()

    def tearDown(self):
        self._env_patch.stop()

    def test_prefix_is_deterministic(self):
        scan = _reporter_scan()
        expected = f"processing/{scan.pk}/tc/164/1/"
        self.assertEqual(s3_sync.s3_processing_prefix(scan), expected)

    def test_upload_processing_files_walks_local_root(self):
        scan = _reporter_scan()
        local_root = pathlib.Path(scan.output_dir)
        local_root.mkdir(parents=True, exist_ok=True)
        (local_root / "bitonal.pdf").write_bytes(b"pdf")
        (local_root / "detections.json").write_text("[]")
        (local_root / "images").mkdir()
        (local_root / "images" / "1-001.png").write_bytes(b"png")
        # Now these should also be uploaded (recursive walk).
        (local_root / "redacted").mkdir()
        (local_root / "redacted" / "op.pdf").write_bytes(b"r")
        (local_root / "unredacted").mkdir()
        (local_root / "unredacted" / "op.pdf").write_bytes(b"u")

        mock_s3 = MagicMock()
        with patch("scanning.s3_sync.boto3") as mock_boto3:
            mock_boto3.client.return_value = mock_s3
            count = s3_sync.upload_processing_files(scan)

        self.assertEqual(count, 5)
        uploaded_keys = {
            call.args[2] for call in mock_s3.upload_file.call_args_list
        }
        prefix = f"processing/{scan.pk}/tc/164/1/"
        self.assertEqual(
            uploaded_keys,
            {
                f"{prefix}bitonal.pdf",
                f"{prefix}detections.json",
                f"{prefix}images/1-001.png",
                f"{prefix}redacted/op.pdf",
                f"{prefix}unredacted/op.pdf",
            },
        )

    def test_is_approved_deliverable(self):
        self.assertTrue(s3_sync._is_approved_deliverable("redacted/foo.pdf"))
        self.assertTrue(s3_sync._is_approved_deliverable("images/1-001.png"))
        self.assertTrue(
            s3_sync._is_approved_deliverable("tc.1.1.2.original.pdf")
        )
        self.assertTrue(
            s3_sync._is_approved_deliverable("tc.1.1.2.redacted.pdf")
        )
        self.assertFalse(
            s3_sync._is_approved_deliverable("unredacted/foo.pdf")
        )
        self.assertFalse(s3_sync._is_approved_deliverable("bitonal.pdf"))
        self.assertFalse(s3_sync._is_approved_deliverable("detections.json"))
        # Guard against nested paths under one of the approved subdirs.
        self.assertFalse(
            s3_sync._is_approved_deliverable("redacted/sub/x.pdf")
        )

    def test_copy_processing_to_approved_copies_only_deliverables(self):
        scan = _reporter_scan()
        src_prefix = s3_sync.s3_processing_prefix(scan)
        mock_s3 = MagicMock()
        mock_paginator = MagicMock()
        mock_paginator.paginate.return_value = [
            {
                "Contents": [
                    {"Key": f"{src_prefix}bitonal.pdf"},
                    {"Key": f"{src_prefix}detections.json"},
                    {"Key": f"{src_prefix}tc.164.1.2.original.pdf"},
                    {"Key": f"{src_prefix}tc.164.1.2.redacted.pdf"},
                    {"Key": f"{src_prefix}redacted/a.pdf"},
                    {"Key": f"{src_prefix}unredacted/a.pdf"},
                    {"Key": f"{src_prefix}images/1-001.png"},
                ]
            }
        ]
        mock_s3.get_paginator.return_value = mock_paginator

        with patch("scanning.s3_sync.boto3") as mock_boto3:
            mock_boto3.client.return_value = mock_s3
            prefix, count = s3_sync.copy_processing_to_approved(scan)

        expected_prefix = "approved/tc/164/1/"
        self.assertEqual(prefix, expected_prefix)
        self.assertEqual(count, 4)
        copied = {c.kwargs["Key"] for c in mock_s3.copy_object.call_args_list}
        self.assertEqual(
            copied,
            {
                f"{expected_prefix}tc.164.1.2.original.pdf",
                f"{expected_prefix}tc.164.1.2.redacted.pdf",
                f"{expected_prefix}redacted/a.pdf",
                f"{expected_prefix}images/1-001.png",
            },
        )

    def test_download_processing_files_skips_same_size(self):
        scan = _reporter_scan()
        scan.status = Status.PENDING_REVIEW
        scan.save(update_fields=["status"])
        tmp_root = s3_sync.tmp_output_dir(scan)
        tmp_root.mkdir(parents=True, exist_ok=True)
        existing = tmp_root / "detections.json"
        existing.write_text("abc")

        prefix = s3_sync.s3_processing_prefix(scan)
        mock_s3 = MagicMock()
        mock_paginator = MagicMock()
        mock_paginator.paginate.return_value = [
            {
                "Contents": [
                    {"Key": f"{prefix}detections.json", "Size": 3},
                    {"Key": f"{prefix}bitonal.pdf", "Size": 5},
                ]
            }
        ]
        mock_s3.get_paginator.return_value = mock_paginator

        def _fake_download(bucket, key, dest):
            pathlib.Path(dest).write_bytes(b"12345")

        mock_s3.download_file.side_effect = _fake_download

        with patch("scanning.s3_sync.boto3") as mock_boto3:
            mock_boto3.client.return_value = mock_s3
            result = s3_sync.download_processing_files(scan)

        self.assertEqual(result, tmp_root)
        # Only the new file (bitonal.pdf) should be downloaded.
        self.assertEqual(mock_s3.download_file.call_count, 1)
        self.assertEqual(
            mock_s3.download_file.call_args.args[1],
            f"{prefix}bitonal.pdf",
        )

    def test_download_preview_pdf_skips_original_and_images(self):
        """Only the bitonal/OCR preview is pulled, not original or images."""
        scan = _reporter_scan()
        scan.status = Status.PENDING_REVIEW
        scan.save(update_fields=["status"])

        prefix = s3_sync.s3_processing_prefix(scan)
        mock_s3 = MagicMock()
        mock_paginator = MagicMock()
        mock_paginator.paginate.return_value = [
            {
                "Contents": [
                    {"Key": f"{prefix}bitonal.pdf", "Size": 5},
                    {"Key": f"{prefix}a.164.1.pdf", "Size": 5},  # OCR pdf
                    {"Key": f"{prefix}a.164.1.original.pdf", "Size": 5},
                    {"Key": f"{prefix}stamped.pdf", "Size": 5},
                    {"Key": f"{prefix}images/p1.png", "Size": 5},
                    {"Key": f"{prefix}redacted/op1.pdf", "Size": 5},
                    {"Key": f"{prefix}detections.json", "Size": 5},
                ]
            }
        ]
        mock_s3.get_paginator.return_value = mock_paginator

        def _fake_download(bucket, key, dest):
            pathlib.Path(dest).parent.mkdir(parents=True, exist_ok=True)
            pathlib.Path(dest).write_bytes(b"12345")

        mock_s3.download_file.side_effect = _fake_download

        with patch("scanning.s3_sync.boto3") as mock_boto3:
            mock_boto3.client.return_value = mock_s3
            s3_sync.download_preview_pdf(scan)

        pulled = {
            call.args[1] for call in mock_s3.download_file.call_args_list
        }
        self.assertEqual(
            pulled,
            {f"{prefix}bitonal.pdf", f"{prefix}a.164.1.pdf"},
        )

    def test_delete_uploaded_object(self):
        mock_s3 = MagicMock()
        key = "processing/9/tc/164/1/x.original.pdf"
        with patch("scanning.s3_sync.boto3") as mock_boto3:
            mock_boto3.client.return_value = mock_s3
            result = s3_sync.delete_uploaded_object(key)
        self.assertTrue(result)
        mock_s3.delete_object.assert_called_once()
        self.assertEqual(mock_s3.delete_object.call_args.kwargs["Key"], key)

    def test_delete_uploaded_object_empty_key_noop(self):
        with patch("scanning.s3_sync.boto3") as mock_boto3:
            result = s3_sync.delete_uploaded_object("")
        self.assertFalse(result)
        mock_boto3.client.assert_not_called()

    def test_upload_file_to_s3_missing_local_file(self):
        scan = _reporter_scan()
        with patch("scanning.s3_sync.boto3") as mock_boto3:
            result = s3_sync.upload_file_to_s3(scan, "missing.json")
        self.assertFalse(result)
        mock_boto3.client.assert_not_called()

    def test_upload_file_to_s3_happy_path(self):
        scan = _reporter_scan()
        scan.status = Status.PENDING_REVIEW
        scan.save(update_fields=["status"])
        # output_dir now resolves to /tmp/...
        local_dir = pathlib.Path(scan.output_dir)
        local_dir.mkdir(parents=True, exist_ok=True)
        (local_dir / "detections.json").write_text("[]")

        mock_s3 = MagicMock()
        with patch("scanning.s3_sync.boto3") as mock_boto3:
            mock_boto3.client.return_value = mock_s3
            ok = s3_sync.upload_file_to_s3(scan, "detections.json")

        self.assertTrue(ok)
        mock_s3.upload_file.assert_called_once()
        _, bucket, key = mock_s3.upload_file.call_args.args
        self.assertEqual(bucket, "test-bucket")
        self.assertEqual(key, f"processing/{scan.pk}/tc/164/1/detections.json")

    def test_upload_fileobj_to_s3_streams_small_file(self):
        scan = _reporter_scan()
        scan.status = Status.PENDING_REVIEW
        scan.save(update_fields=["status"])
        upload = SimpleUploadedFile(
            "x.original.pdf",
            b"%PDF-1.4 body",
            content_type="application/pdf",
        )

        mock_s3 = MagicMock()
        with patch("scanning.s3_sync.boto3") as mock_boto3:
            mock_boto3.client.return_value = mock_s3
            ok = s3_sync.upload_fileobj_to_s3(scan, upload, "x.original.pdf")

        self.assertTrue(ok)
        # Small in-memory upload has no temp path, so it streams the
        # file object directly. No local file is touched.
        mock_s3.upload_fileobj.assert_called_once()
        mock_s3.upload_file.assert_not_called()
        _, bucket, key = mock_s3.upload_fileobj.call_args.args
        self.assertEqual(bucket, "test-bucket")
        self.assertEqual(key, f"processing/{scan.pk}/tc/164/1/x.original.pdf")
        self.assertEqual(
            mock_s3.upload_fileobj.call_args.kwargs["ExtraArgs"],
            {"ContentType": "application/pdf"},
        )

    def test_upload_fileobj_to_s3_uses_temporary_file_path(self):
        scan = _reporter_scan()
        scan.status = Status.PENDING_REVIEW
        scan.save(update_fields=["status"])
        # Large uploads spool to a TemporaryUploadedFile exposing
        # temporary_file_path(); boto3 reads that path directly.
        upload = MagicMock()
        upload.temporary_file_path.return_value = "/tmp/spool.pdf"
        upload.size = 2048

        mock_s3 = MagicMock()
        with patch("scanning.s3_sync.boto3") as mock_boto3:
            mock_boto3.client.return_value = mock_s3
            ok = s3_sync.upload_fileobj_to_s3(scan, upload, "x.original.pdf")

        self.assertTrue(ok)
        mock_s3.upload_file.assert_called_once()
        mock_s3.upload_fileobj.assert_not_called()
        path_arg, bucket, key = mock_s3.upload_file.call_args.args
        self.assertEqual(path_arg, "/tmp/spool.pdf")
        self.assertEqual(bucket, "test-bucket")
        self.assertEqual(key, f"processing/{scan.pk}/tc/164/1/x.original.pdf")
        self.assertEqual(
            mock_s3.upload_file.call_args.kwargs["ExtraArgs"],
            {"ContentType": "application/pdf"},
        )


class TestS3EnabledBranches(SimpleTestCase):
    """Coverage for the ``_s3_enabled()`` branch matrix.

    ``TESTING`` is auto-set during the test run, so each case overrides
    it explicitly to exercise the intended branch. ``has_s3_credentials``
    is patched to keep the result independent of the developer's
    environment.
    """

    @override_settings(DEVELOPMENT=True, RUNPOD_ENABLED=True, TESTING=False)
    def test_dev_with_runpod_enabled_returns_true(self):
        with patch("scanning.s3_sync.has_s3_credentials", return_value=True):
            self.assertTrue(s3_sync._s3_enabled())

    @override_settings(DEVELOPMENT=True, RUNPOD_ENABLED=False, TESTING=False)
    def test_dev_without_runpod_returns_false(self):
        with patch("scanning.s3_sync.has_s3_credentials", return_value=True):
            self.assertFalse(s3_sync._s3_enabled())

    @override_settings(DEVELOPMENT=False, RUNPOD_ENABLED=False, TESTING=True)
    def test_testing_short_circuits_regardless_of_runpod(self):
        with patch("scanning.s3_sync.has_s3_credentials", return_value=True):
            self.assertFalse(s3_sync._s3_enabled())
        with self.settings(RUNPOD_ENABLED=True):
            with patch(
                "scanning.s3_sync.has_s3_credentials", return_value=True
            ):
                self.assertFalse(s3_sync._s3_enabled())
