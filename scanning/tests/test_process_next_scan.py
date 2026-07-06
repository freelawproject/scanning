"""Tests for the process_next_scan daemon tick, focused on the transient
DB-connection retry behavior added for issue #116 (SCANNING-20)."""

from unittest.mock import patch

from django.db import OperationalError
from django.test import TestCase

from scanning.factories import ScanFactory
from scanning.management.commands.process_next_scan import (
    MAX_DB_RETRIES,
    Command,
)
from scanning.models import Status


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
