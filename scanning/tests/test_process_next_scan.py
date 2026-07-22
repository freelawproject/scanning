"""Tests for the process_next_scan daemon tick, focused on the transient
DB-connection retry behavior added for issue #116 (SCANNING-20)."""

from datetime import timedelta
from unittest.mock import patch

from django.db import OperationalError
from django.test import TestCase, override_settings
from django.utils import timezone

from scanning.factories import ScanFactory
from scanning.management.commands.process_next_scan import (
    MAX_DB_RETRIES,
    Command,
)
from scanning.models import Scan, Status


class TestProcessNextScanRetry(TestCase):
    """A transient OperationalError while claiming should be retried.

    The tick calls ``connections.close_all()`` every attempt; that would tear
    down the connection this TestCase's wrapping transaction rides on, so it's
    patched to a no-op. We're exercising the retry control flow, not the real
    connection teardown/reconnect.
    """

    def test_retry_recovers_and_claims_scan(self):
        """A blip on the first attempt is retried; the scan is then claimed."""
        scan = ScanFactory(status=Status.QUEUED)
        cmd = Command()

        with (
            patch("django.db.connections.close_all"),
            patch.object(
                Command,
                "_recover_stale",
                side_effect=[
                    OperationalError("SSL error: unexpected eof"),
                    None,
                ],
            ),
            patch(
                "scanning.management.commands.process_next_scan.time.sleep"
            ) as mock_sleep,
            patch("scanning.services.run_full_pipeline") as mock_pipeline,
        ):
            cmd.handle()

        scan.refresh_from_db()
        self.assertEqual(scan.status, Status.PROCESSING)
        mock_pipeline.assert_called_once_with(scan.pk)
        # One failed attempt -> exactly one backoff sleep.
        self.assertEqual(mock_sleep.call_count, 1)

    def test_exhausted_retries_logs_warning_and_leaves_scan_queued(self):
        """When every attempt fails, warn (no Sentry error) and give up cleanly."""
        scan = ScanFactory(status=Status.QUEUED)
        cmd = Command()

        with (
            patch("django.db.connections.close_all"),
            patch.object(
                Command,
                "_recover_stale",
                side_effect=OperationalError("SSL error: unexpected eof"),
            ),
            patch(
                "scanning.management.commands.process_next_scan.time.sleep"
            ) as mock_sleep,
            patch("scanning.services.run_full_pipeline") as mock_pipeline,
            self.assertLogs(
                "scanning.management.commands.process_next_scan",
                level="WARNING",
            ) as logs,
        ):
            # Must not raise: the daemon tick swallows the transient error.
            cmd.handle()

        scan.refresh_from_db()
        self.assertEqual(scan.status, Status.QUEUED)
        mock_pipeline.assert_not_called()
        # Backoff sleeps between attempts, none after the final failure.
        self.assertEqual(mock_sleep.call_count, MAX_DB_RETRIES - 1)
        self.assertEqual(len(logs.records), 1)
        self.assertEqual(logs.records[0].levelname, "WARNING")
        self.assertIn("DB connection failed", logs.records[0].getMessage())

    def test_no_queued_scan_is_a_noop(self):
        """With nothing queued, the tick claims nothing and dispatches nothing."""
        ScanFactory(status=Status.UPLOADED)
        cmd = Command()

        with (
            patch("django.db.connections.close_all"),
            patch("scanning.services.run_full_pipeline") as mock_pipeline,
        ):
            cmd.handle()

        mock_pipeline.assert_not_called()


@override_settings(DAEMON_PROCESSING_TIMEOUT=3600)
class TestRecoverStale(TestCase):
    """_recover_stale re-queues timed-out scans and bounds the retry loop.

    A scan usually goes stale because the daemon was killed (SIGKILL/OOM,
    which never runs the SIGTERM handler) mid-pipeline, so this path bumps
    interruption_count and flags chronic offenders just like the signal
    handler does (issue #124).
    """

    def _stale(self, **kwargs):
        """Create a PROCESSING scan whose processed_at is past the timeout."""
        old = timezone.now() - timedelta(seconds=7200)
        scan = ScanFactory(status=Status.PROCESSING, **kwargs)
        # processed_at is set by the pipeline, not the factory; force it old.
        Scan.objects.filter(pk=scan.pk).update(processed_at=old)
        return scan

    def test_stale_scan_requeued_and_interruption_bumped(self):
        scan = self._stale(interruption_count=0)

        with self.assertLogs("scanning.models", level="INFO") as logs:
            Command()._recover_stale()

        scan.refresh_from_db()
        self.assertEqual(scan.status, Status.QUEUED)
        self.assertEqual(scan.interruption_count, 1)
        # Re-queue is an INFO breadcrumb, not a Sentry error event.
        self.assertTrue(
            any(
                r.levelname == "INFO" and "Re-queued" in r.getMessage()
                for r in logs.records
            )
        )

    @override_settings(DAEMON_MAX_INTERRUPTIONS=3)
    def test_stale_scan_flagged_after_max_interruptions(self):
        scan = self._stale(interruption_count=3)

        with self.assertLogs("scanning.models", level="ERROR") as logs:
            Command()._recover_stale()

        scan.refresh_from_db()
        self.assertEqual(scan.status, Status.ERROR_INTERRUPTED)
        self.assertEqual(scan.interruption_count, 4)
        self.assertEqual(scan.retry_count, 0)
        # Flagging raises a Sentry error event so a human notices.
        self.assertEqual(logs.records[0].levelname, "ERROR")
        self.assertIn("ERROR_INTERRUPTED", logs.records[0].getMessage())

    def test_fresh_processing_scan_is_left_alone(self):
        """A PROCESSING scan within the timeout is not touched."""
        scan = ScanFactory(status=Status.PROCESSING, interruption_count=0)
        Scan.objects.filter(pk=scan.pk).update(processed_at=timezone.now())

        Command()._recover_stale()

        scan.refresh_from_db()
        self.assertEqual(scan.status, Status.PROCESSING)
        self.assertEqual(scan.interruption_count, 0)

    @override_settings(
        DAEMON_PROCESSING_TIMEOUT=3600,
        RUNPOD_REQUEST_TIMEOUT=1800,
        RUNPOD_REQUEST_TIMEOUT_PER_PAGE=2,
    )
    def test_stale_timeout_scales_with_page_count(self):
        """The cutoff is the larger of the daemon floor and twice the
        page-aware request ceiling (detect + analyze) (issue #127)."""
        # max(3600, 2 * (1800 + 2 * 2000)) = 11600
        self.assertEqual(
            Command._stale_timeout_for(ScanFactory(page_count=2000)), 11600
        )
        # Small scan falls back to the daemon floor: max(3600, 2*1800).
        self.assertEqual(
            Command._stale_timeout_for(ScanFactory(page_count=0)), 3600
        )

    @override_settings(
        DAEMON_PROCESSING_TIMEOUT=3600,
        RUNPOD_REQUEST_TIMEOUT=1800,
        RUNPOD_REQUEST_TIMEOUT_PER_PAGE=2,
    )
    def test_large_scan_within_page_aware_ceiling_not_recovered(self):
        """A big volume past the daemon floor but within its own page-aware
        ceiling is left running, not recovered out from under its job."""
        scan = ScanFactory(status=Status.PROCESSING, page_count=2000)
        # 2h old: past DAEMON_PROCESSING_TIMEOUT (3600) but under 11600.
        Scan.objects.filter(pk=scan.pk).update(
            processed_at=timezone.now() - timedelta(seconds=7200)
        )

        Command()._recover_stale()

        scan.refresh_from_db()
        self.assertEqual(scan.status, Status.PROCESSING)

    @override_settings(
        DAEMON_PROCESSING_TIMEOUT=3600,
        RUNPOD_REQUEST_TIMEOUT=1800,
        RUNPOD_REQUEST_TIMEOUT_PER_PAGE=2,
    )
    def test_large_scan_past_page_aware_ceiling_is_recovered(self):
        """Once even the page-aware ceiling is exceeded, it is recovered."""
        scan = ScanFactory(
            status=Status.PROCESSING, page_count=2000, interruption_count=0
        )
        Scan.objects.filter(pk=scan.pk).update(
            processed_at=timezone.now() - timedelta(seconds=12000)
        )

        Command()._recover_stale()

        scan.refresh_from_db()
        self.assertEqual(scan.status, Status.QUEUED)
