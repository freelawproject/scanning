"""Tests for the cleanup_processing_tmp management command."""

import os
import tempfile
import time
from pathlib import Path

from django.core.management import call_command
from django.test import TestCase, override_settings


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
