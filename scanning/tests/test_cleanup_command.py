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


def _make_old(*paths):
    """Force the mtime of every given path to 2 hours ago.

    :param paths: Files and directories to age.
    :return: None.
    """
    old = time.time() - 7200
    for path in paths:
        os.utime(path, (old, old))


def _make_scan_tree(tmp_root: Path, pk: str) -> tuple[Path, Path]:
    """Build one prod-shaped processing tree under ``tmp_root``.

    Mirrors the real layout, ``{pk}/{reporter}/{vol}/{start}/file``,
    because the sweep's staleness read must survive it: writes land at
    the leaf, levels below the directory the sweep iterates.

    :param tmp_root: The fake ``PROCESSING_TMP_DIR``.
    :param pk: Name of the top-level per-scan directory.
    :returns: The top directory and the deep file.
    :rtype: tuple[Path, Path]
    """
    top = tmp_root / pk
    leaf = top / "tc" / "164" / "1"
    leaf.mkdir(parents=True)
    deep_file = leaf / "bitonal.pdf"
    deep_file.write_text("x")
    return top, deep_file


class TestCleanupProcessingTmp(TestCase):
    """Exercise the TTL sweep with forged mtimes, no real AWS involved."""

    def setUp(self):
        self.tmp_root = Path(tempfile.mkdtemp())

        # Active tree: every directory is old, only the deep file is
        # fresh -- the shape of a scan the daemon wrote to a moment ago.
        self.fresh, _ = _make_scan_tree(self.tmp_root, "101")
        _make_old(
            self.fresh,
            self.fresh / "tc",
            self.fresh / "tc" / "164",
            self.fresh / "tc" / "164" / "1",
        )

        # Stale tree: everything old, the deep file included.
        self.stale, stale_file = _make_scan_tree(self.tmp_root, "102")
        _make_old(
            stale_file,
            self.stale / "tc" / "164" / "1",
            self.stale / "tc" / "164",
            self.stale / "tc",
            self.stale,
        )

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
    def test_a_deep_fresh_file_protects_an_old_top_dir(self):
        # The fresh tree's top directory is 2 hours old; only the file
        # at the leaf is new. The old sweep statted the top directory
        # and would have removed the tree of a scan still in work.
        with override_settings(
            PROCESSING_TMP_DIR=str(self.tmp_root),
            PROCESSING_TMP_TTL_HOURS=1.0,
        ):
            call_command("cleanup_processing_tmp")
        self.assertTrue(
            (self.fresh / "tc" / "164" / "1" / "bitonal.pdf").exists()
        )

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


class TestLeakedStageDirSweep(TestCase):
    """The sweep also reclaims leaked merge and glue scratch dirs."""

    def setUp(self):
        self.tmp_root = Path(tempfile.mkdtemp())
        self.temp_root = Path(tempfile.mkdtemp())

        def scratch_dir(name: str, old: bool) -> Path:
            d = self.temp_root / name
            d.mkdir()
            payload = d / "0001.pdf"
            payload.write_text("x")
            if old:
                _make_old(payload, d)
            return d

        self.leaked_merge = scratch_dir("bitonal-12-abc123", old=True)
        self.leaked_glue = scratch_dir("dotsmocr-9-xyz789", old=True)
        self.leaked_render = scratch_dir("mistralocr-k2j3h4", old=True)
        self.live_merge = scratch_dir("bitonal-13-def456", old=False)
        self.unrelated = scratch_dir("someother-1-ghi", old=True)

    def _run(self):
        with (
            override_settings(
                PROCESSING_TMP_DIR=str(self.tmp_root),
                PROCESSING_TMP_TTL_HOURS=1.0,
            ),
            patch(
                "scanning.management.commands.cleanup_processing_tmp"
                ".tempfile.gettempdir",
                return_value=str(self.temp_root),
            ),
        ):
            call_command("cleanup_processing_tmp")

    @override_settings(DEVELOPMENT=False, TESTING=False)
    def test_sweeps_leaked_dirs_and_keeps_the_rest(self):
        self._run()
        self.assertFalse(self.leaked_merge.exists())
        self.assertFalse(self.leaked_glue.exists())
        # The Mistral render dir (#191) is reclaimed like the others.
        self.assertFalse(self.leaked_render.exists())
        # A live stage's scratch dir is fresh and stays.
        self.assertTrue(self.live_merge.exists())
        # A directory that is not ours stays, however old.
        self.assertTrue(self.unrelated.exists())

    @override_settings(DEVELOPMENT=False)
    def test_noop_under_testing(self):
        # The guard that keeps the suite off the developer's real temp
        # dir: with TESTING left on, nothing is removed.
        self._run()
        self.assertTrue(self.leaked_merge.exists())
        self.assertTrue(self.leaked_glue.exists())


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

    @override_settings(DEVELOPMENT=False)
    def test_sweep_recovers_instead_of_deleting(self):
        # A stale pending whose object actually landed in S3 is recovered
        # (linked to the scan), never deleted.
        scan = ScanFactory(original_pdf="")
        pending = self._make_pending(scan, hours_old=48)
        with (
            patch(
                "scanning.s3_sync.verify_uploaded_object", return_value=True
            ),
            patch("scanning.s3_sync.delete_uploaded_object") as mock_delete,
        ):
            call_command("cleanup_processing_tmp")
        mock_delete.assert_not_called()
        self.assertFalse(PendingUpload.objects.filter(pk=pending.pk).exists())
        scan.refresh_from_db()
        self.assertTrue(scan.original_pdf.name)

    @override_settings(DEVELOPMENT=False)
    def test_sweep_keeps_s3_object_for_reupload_scan(self):
        # A re-upload's pending reuses the confirmed original's s3_key, so the
        # sweep must NOT delete the object when the scan still has a file.
        scan = ScanFactory(original_pdf="original_scans/x.original.pdf")
        self.assertTrue(scan.original_pdf.name)
        self._make_pending(scan, hours_old=48)
        with patch("scanning.s3_sync.delete_uploaded_object") as mock_delete:
            call_command("cleanup_processing_tmp")
        mock_delete.assert_not_called()
        self.assertTrue(Scan.objects.filter(pk=scan.pk).exists())
