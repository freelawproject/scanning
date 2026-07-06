"""Tests for the run_daemon scheduler."""

from unittest.mock import patch

from django.db import OperationalError
from django.test import TestCase, override_settings

from scanning.factories import ScanFactory
from scanning.management.commands.run_daemon import Command, ScheduledTask
from scanning.models import Scan, Status


class TestScheduledTask(TestCase):
    """ScheduledTask due/mark_ran timing behavior."""

    def test_new_task_is_due_once_interval_has_elapsed(self):
        task = ScheduledTask("x", interval_seconds=10)
        # last_ran defaults to 0.0, so any now >= interval fires.
        self.assertTrue(task.due(now=10.0))
        self.assertTrue(task.due(now=999.0))

    def test_not_due_before_interval_elapsed(self):
        task = ScheduledTask("x", interval_seconds=10, last_ran=100.0)
        self.assertFalse(task.due(now=105.0))
        self.assertTrue(task.due(now=110.0))


class TestRunDaemonSchedule(TestCase):
    """run_daemon builds the expected schedule from settings."""

    @override_settings(
        DAEMON_POLL_INTERVAL=5,
        PROCESSING_TMP_CLEANUP_INTERVAL_SECONDS=900,
    )
    def test_build_schedule_has_both_tasks(self):
        cmd = Command()
        names = [t.name for t in cmd._build_schedule()]
        self.assertIn("process_next_scan", names)
        self.assertIn("cleanup_processing_tmp", names)

    def test_handle_invokes_due_tasks_and_shuts_down(self):
        """The scheduler should call_command each due task then exit on signal."""
        cmd = Command()
        cmd.shutdown = False

        calls = []

        def fake_call_command(name, *a, **kw):
            calls.append(name)
            # Stop after first tick so the test terminates.
            cmd.shutdown = True

        with (
            patch(
                "scanning.management.commands.run_daemon.call_command",
                side_effect=fake_call_command,
            ),
            patch("scanning.management.commands.run_daemon.signal.signal"),
            patch("scanning.management.commands.run_daemon.time.sleep"),
        ):
            cmd.handle()

        # At least one task fires on the first tick (both are due since
        # last_ran=0.0).
        self.assertGreaterEqual(len(calls), 1)
        self.assertIn(
            calls[0], {"process_next_scan", "cleanup_processing_tmp"}
        )

    def test_transient_db_error_logged_as_warning_not_exception(self):
        """A transient OperationalError from a task is a WARNING, not an error.

        Downgrading it keeps a self-recovering DB blip out of Sentry's error
        stream while the loop continues ticking (issue #116).
        """
        cmd = Command()
        cmd.shutdown = False

        def failing_call_command(name, *a, **kw):
            cmd.shutdown = True
            raise OperationalError("SSL error: unexpected eof while reading")

        with (
            patch(
                "scanning.management.commands.run_daemon.call_command",
                side_effect=failing_call_command,
            ),
            patch("scanning.management.commands.run_daemon.signal.signal"),
            patch("scanning.management.commands.run_daemon.time.sleep"),
            self.assertLogs(
                "scanning.management.commands.run_daemon", level="WARNING"
            ) as logs,
        ):
            cmd.handle()

        # Both scheduled tasks are due on the first tick, so one warning per
        # task; the point is every record is a WARNING (logger.exception would
        # have logged at ERROR) mentioning the transient DB error.
        self.assertTrue(cmd.shutdown)
        self.assertGreaterEqual(len(logs.records), 1)
        self.assertTrue(all(r.levelname == "WARNING" for r in logs.records))
        self.assertTrue(
            all("transient DB error" in r.getMessage() for r in logs.records)
        )


class TestHandleSignal(TestCase):
    """The signal handler should re-queue in-flight scans and report to Sentry."""

    def test_signal_requeues_processing_scans(self):
        """PROCESSING scans get reset to QUEUED so the next tick retries them."""
        cmd = Command()
        in_flight = ScanFactory(status=Status.PROCESSING)
        other = ScanFactory(status=Status.UPLOADED)

        with patch("sentry_sdk.capture_message") as mock_capture:
            cmd._handle_signal(15, None)

        in_flight.refresh_from_db()
        other.refresh_from_db()
        self.assertTrue(cmd.shutdown)
        self.assertEqual(in_flight.status, Status.QUEUED)
        self.assertIn("signal 15", in_flight.progress_message)
        self.assertEqual(other.status, Status.UPLOADED)
        mock_capture.assert_called_once()

    def test_signal_with_no_processing_scans(self):
        """No-op on the DB side if nothing is in flight, but Sentry still fires."""
        cmd = Command()
        ScanFactory(status=Status.UPLOADED)

        with patch("sentry_sdk.capture_message") as mock_capture:
            cmd._handle_signal(2, None)

        self.assertTrue(cmd.shutdown)
        self.assertEqual(Scan.objects.filter(status=Status.QUEUED).count(), 0)
        mock_capture.assert_called_once()
