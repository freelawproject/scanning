"""Tests for the cleanup_processing_tmp management command."""

import os
import tempfile
import time
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone

from scanning.factories import ScanFactory, UserFactory
from scanning.models import PendingUpload, Scan


class TestCleanupProcessingTmp(TestCase):
    """Exercise the TTL sweep with forged mtimes, no real AWS involved."""

    def setUp(self):
        self.tmp_root = Path(tempfile.mkdtemp())
        self.fresh = self.tmp_root / "fresh"
        self.stale = self.tmp_root / "stale"
        for d in (self.fresh, self.stale):
            d.mkdir()
            (d / "marker").write_text("x")

        # Force the stale directory's mtime to 2 hours ago.
        old = time.time() - 7200
        os.utime(self.stale, (old, old))

    @override_settings(DEVELOPMENT=True)
    def test_noop_in_development(self):
        with override_settings(
            PROCESSING_TMP_DIR=str(self.tmp_root),
            PROCESSING_TMP_TTL_HOURS=0.001,
        ):
            call_command("cleanup_processing_tmp")
        self.assertTrue(self.fresh.exists())
        self.assertTrue(self.stale.exists())

    @override_settings(DEVELOPMENT=False)
    def test_sweeps_stale_dirs(self):
        with override_settings(
            PROCESSING_TMP_DIR=str(self.tmp_root),
            PROCESSING_TMP_TTL_HOURS=1.0,
        ):
            call_command("cleanup_processing_tmp")
        self.assertTrue(self.fresh.exists())
        self.assertFalse(self.stale.exists())

    @override_settings(DEVELOPMENT=False)
    def test_ttl_override_flag(self):
        # 3h TTL keeps everything.
        with override_settings(
            PROCESSING_TMP_DIR=str(self.tmp_root),
            PROCESSING_TMP_TTL_HOURS=1.0,
        ):
            call_command("cleanup_processing_tmp", "--ttl-hours", "3")
        self.assertTrue(self.fresh.exists())
        self.assertTrue(self.stale.exists())

    @override_settings(DEVELOPMENT=False)
    def test_missing_tmp_dir_is_safe(self):
        with override_settings(
            PROCESSING_TMP_DIR="/nonexistent/path/for/tests",
            PROCESSING_TMP_TTL_HOURS=1.0,
        ):
            call_command("cleanup_processing_tmp")


@override_settings(PENDING_UPLOAD_TTL_HOURS=24.0)
class TestPendingUploadSweep(TestCase):
    """The DB sweep removes abandoned direct-to-S3 uploads.

    String ``original_pdf`` overrides set the FileField name without
    writing a real file, so no MEDIA_ROOT juggling is needed here.
    """

    def _make_pending(self, scan, hours_old):
        pending = PendingUpload.objects.create(
            scan=scan,
            s3_key="processing/x/x.original.pdf",
            expected_size=1024,
            created_by=UserFactory(),
        )
        # date_created is auto_now_add, so back-date it via update().
        old = timezone.now() - timedelta(hours=hours_old)
        PendingUpload.objects.filter(pk=pending.pk).update(date_created=old)
        return pending

    @override_settings(DEVELOPMENT=False)
    def test_sweeps_stale_pending_and_orphan_scan(self):
        scan = ScanFactory(original_pdf="")
        self.assertFalse(scan.original_pdf.name)
        pending = self._make_pending(scan, hours_old=48)
        call_command("cleanup_processing_tmp")
        self.assertFalse(PendingUpload.objects.filter(pk=pending.pk).exists())
        self.assertFalse(Scan.objects.filter(pk=scan.pk).exists())

    @override_settings(DEVELOPMENT=False)
    def test_keeps_fresh_pending(self):
        scan = ScanFactory(original_pdf="")
        pending = self._make_pending(scan, hours_old=1)
        call_command("cleanup_processing_tmp")
        self.assertTrue(PendingUpload.objects.filter(pk=pending.pk).exists())
        self.assertTrue(Scan.objects.filter(pk=scan.pk).exists())

    @override_settings(DEVELOPMENT=False)
    def test_stale_pending_keeps_scan_that_has_a_file(self):
        scan = ScanFactory(original_pdf="original_scans/x.original.pdf")
        self.assertTrue(scan.original_pdf.name)
        pending = self._make_pending(scan, hours_old=48)
        call_command("cleanup_processing_tmp")
        self.assertFalse(PendingUpload.objects.filter(pk=pending.pk).exists())
        self.assertTrue(Scan.objects.filter(pk=scan.pk).exists())

    @override_settings(DEVELOPMENT=True)
    def test_db_sweep_runs_even_in_development(self):
        # The /tmp/ sweep is a DEV no-op, but the DB sweep still runs.
        scan = ScanFactory(original_pdf="")
        pending = self._make_pending(scan, hours_old=48)
        call_command("cleanup_processing_tmp")
        self.assertFalse(PendingUpload.objects.filter(pk=pending.pk).exists())

    @override_settings(DEVELOPMENT=False)
    def test_sweep_deletes_abandoned_s3_object(self):
        # The stranded S3 object (browser POSTed but never confirmed) is
        # reclaimed, not just the DB rows.
        scan = ScanFactory(original_pdf="")
        pending = self._make_pending(scan, hours_old=48)
        with patch("scanning.s3_sync.delete_uploaded_object") as mock_delete:
            call_command("cleanup_processing_tmp")
        mock_delete.assert_called_once_with(pending.s3_key)
