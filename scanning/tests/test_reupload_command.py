"""Tests for the reupload_scan_files management command."""

import pathlib
import tempfile
from io import StringIO
from unittest.mock import patch

from django.core.management import CommandError, call_command
from django.test import TestCase, override_settings

from scanning import s3_sync
from scanning.factories import ReporterFactory, ScanFactory

MEDIA_ROOT = tempfile.mkdtemp()


def _reporter_scan(**kwargs):
    """Build a scan with a reporter for predictable paths.

    :returns: A Scan instance.
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


class TestReuploadScanFiles(TestCase):
    """Exercise the reupload_scan_files command with mocked boto3."""

    def setUp(self):
        # _s3_client() is lru_cached; drop any client cached under a prior
        # test's boto3 patch so this test's mock is the one that's used.
        s3_sync._s3_client.cache_clear()

    def test_missing_scan_raises(self):
        with self.assertRaises(CommandError):
            call_command("reupload_scan_files", 999_999)

    @override_settings(
        MEDIA_ROOT=MEDIA_ROOT,
        DEVELOPMENT=False,
        TESTING=False,
        PROCESSING_TMP_DIR=tempfile.mkdtemp(),
    )
    def test_missing_dir_raises(self):
        scan = _reporter_scan()
        # Intentionally do NOT create the output_dir.
        with self.assertRaises(CommandError):
            call_command("reupload_scan_files", scan.pk)

    @override_settings(
        MEDIA_ROOT=MEDIA_ROOT,
        DEVELOPMENT=False,
        TESTING=False,
        AWS_ACCESS_KEY_ID="fake",
        AWS_SECRET_ACCESS_KEY="fake",
        AWS_PRIVATE_STORAGE_BUCKET_NAME="test-bucket",
        PROCESSING_TMP_DIR=tempfile.mkdtemp(),
    )
    def test_happy_path_uploads_all_files(self):
        import os
        from unittest.mock import MagicMock

        scan = _reporter_scan()
        local_root = pathlib.Path(scan.output_dir)
        local_root.mkdir(parents=True, exist_ok=True)
        (local_root / "bitonal.pdf").write_bytes(b"pdf")
        (local_root / "detections.json").write_text("[]")

        env = {"AWS_ACCESS_KEY_ID": "fake", "AWS_SECRET_ACCESS_KEY": "fake"}
        with (
            patch.dict(os.environ, env),
            patch("scanning.s3_sync.boto3") as mock_boto3,
        ):
            mock_boto3.client.return_value = MagicMock()
            out = StringIO()
            call_command("reupload_scan_files", scan.pk, stdout=out)

        self.assertIn("Re-uploaded 2", out.getvalue())

    @override_settings(
        MEDIA_ROOT=MEDIA_ROOT,
        DEVELOPMENT=False,
        TESTING=False,
        AWS_ACCESS_KEY_ID="fake",
        AWS_SECRET_ACCESS_KEY="fake",
        AWS_PRIVATE_STORAGE_BUCKET_NAME="test-bucket",
        PROCESSING_TMP_DIR=tempfile.mkdtemp(),
    )
    def test_files_subset_only_uploads_given(self):
        import os
        from unittest.mock import MagicMock

        scan = _reporter_scan()
        local_root = pathlib.Path(scan.output_dir)
        local_root.mkdir(parents=True, exist_ok=True)
        (local_root / "bitonal.pdf").write_bytes(b"pdf")
        (local_root / "detections.json").write_text("[]")

        env = {"AWS_ACCESS_KEY_ID": "fake", "AWS_SECRET_ACCESS_KEY": "fake"}
        mock_client = MagicMock()
        with (
            patch.dict(os.environ, env),
            patch("scanning.s3_sync.boto3") as mock_boto3,
        ):
            mock_boto3.client.return_value = mock_client
            call_command(
                "reupload_scan_files",
                scan.pk,
                "--files",
                "detections.json",
                stdout=StringIO(),
            )

        self.assertEqual(mock_client.upload_file.call_count, 1)
        _, _, key = mock_client.upload_file.call_args.args
        self.assertTrue(key.endswith("/detections.json"))
