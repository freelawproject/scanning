"""Tests for the run_daemon scheduler."""

from unittest.mock import patch

from django.db import OperationalError
from django.test import TestCase, override_settings

from scanning import stopping
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
        DAEMON_SUBMIT_INTERVAL=5,
        DAEMON_COLLECT_INTERVAL=15,
        PROCESSING_TMP_CLEANUP_INTERVAL_SECONDS=900,
    )
    def test_build_schedule_has_every_task(self):
        cmd = Command()
        schedule = cmd._build_schedule()
        names = [t.name for t in schedule]
        self.assertIn("process_next_scan", names)
        # The external-job loop (#176): submit waves, then confirm and
        # apply. Without both scheduled, a scan parked in AWAITING never
        # moves again.
        self.assertIn("submit_external_jobs", names)
        self.assertIn("collect_external_jobs", names)
        self.assertIn("cleanup_processing_tmp", names)
        # The loop walks the list in order each round, so the passes
        # that block it for a whole write or score come after every
        # fast tick (#336, #436). Their order among themselves is no rule.
        blocking = {
            "build_opinion_pdfs",
            "cut_opinion_figures",
            "score_bad_pages",
        }
        self.assertEqual(set(names[-3:]), blocking)
        intervals = {t.name: t.interval_seconds for t in schedule}
        self.assertEqual(intervals["submit_external_jobs"], 5.0)
        self.assertEqual(intervals["collect_external_jobs"], 15.0)

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

        # Every task is due on the first tick (last_ran=0.0), but the
        # signal set during the first one ends the round (#534).
        self.assertEqual(len(calls), 1)
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

    def setUp(self):
        stopping.reset()
        self.addCleanup(stopping.reset)

    def test_signal_asks_the_running_pass_to_stop(self):
        """The Mistral wave reads the flag between its uploads (#534)."""
        cmd = Command()
        with patch("sentry_sdk.capture_message"):
            cmd._handle_signal(15, None)
        self.assertTrue(stopping.requested())

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

    def test_signal_leaves_awaiting_scans_alone(self):
        """A deploy must not re-queue work a provider is still doing.

        The scan is parked in AWAITING precisely because nothing of ours
        is running (#176); re-queueing it on every daemon restart would
        re-convert volumes and burn the interruption budget for waiting.
        """
        cmd = Command()
        waiting = ScanFactory(status=Status.AWAITING, interruption_count=0)

        with patch("sentry_sdk.capture_message"):
            cmd._handle_signal(15, None)

        waiting.refresh_from_db()
        self.assertEqual(waiting.status, Status.AWAITING)
        self.assertEqual(waiting.interruption_count, 0)

    def test_signal_with_no_processing_scans(self):
        """No-op on the DB side if nothing is in flight, but Sentry still fires."""
        cmd = Command()
        ScanFactory(status=Status.UPLOADED)

        with patch("sentry_sdk.capture_message") as mock_capture:
            cmd._handle_signal(2, None)

        self.assertTrue(cmd.shutdown)
        self.assertEqual(Scan.objects.filter(status=Status.QUEUED).count(), 0)
        mock_capture.assert_called_once()

    def test_signal_bumps_interruption_count(self):
        """Each SIGTERM re-queue increments interruption_count (issue #124)."""
        cmd = Command()
        scan = ScanFactory(status=Status.PROCESSING, interruption_count=0)

        with patch("sentry_sdk.capture_message"):
            cmd._handle_signal(15, None)

        scan.refresh_from_db()
        self.assertEqual(scan.status, Status.QUEUED)
        self.assertEqual(scan.interruption_count, 1)

    @override_settings(DAEMON_MAX_INTERRUPTIONS=3)
    def test_signal_flags_scan_after_max_interruptions(self):
        """Past the interruption ceiling the scan is flagged, not re-queued.

        Guards against a churning daemon pod re-queueing the same scan
        forever without ever consuming its RunPod retry budget (issue #124).
        """
        cmd = Command()
        # Already interrupted at the ceiling; this signal is the one over.
        scan = ScanFactory(status=Status.PROCESSING, interruption_count=3)

        with patch("sentry_sdk.capture_message"):
            cmd._handle_signal(15, None)

        scan.refresh_from_db()
        self.assertEqual(scan.status, Status.ERROR_INTERRUPTED)
        self.assertEqual(scan.interruption_count, 4)
        # retry_count is untouched: interruptions are not scan failures.
        self.assertEqual(scan.retry_count, 0)

    @override_settings(DAEMON_MAX_INTERRUPTIONS=3)
    def test_signal_requeues_up_to_the_ceiling(self):
        """A scan just below the ceiling is still re-queued, not flagged."""
        cmd = Command()
        scan = ScanFactory(status=Status.PROCESSING, interruption_count=2)

        with patch("sentry_sdk.capture_message"):
            cmd._handle_signal(15, None)

        scan.refresh_from_db()
        self.assertEqual(scan.status, Status.QUEUED)
        self.assertEqual(scan.interruption_count, 3)


class TestCeleryMode(TestCase):
    """With the switch on, the daemon is the clock and runs no pass."""

    @override_settings(CELERY_ENABLED=True)
    def test_the_schedule_is_the_tick_table(self):
        from scanning import tasks

        cmd = Command()
        schedule = cmd._build_celery_schedule()
        self.assertEqual(
            [t.name for t in schedule], [t.name for t in tasks.TICKS]
        )

    @override_settings(CELERY_ENABLED=True)
    def test_a_due_entry_sends_its_tick_and_calls_no_command(self):
        cmd = Command()
        sent = []

        def fake_send(self):
            sent.append(self.name)
            cmd.shutdown = True
            return True

        with (
            patch("scanning.tasks.Tick.send", fake_send),
            patch(
                "scanning.management.commands.run_daemon.call_command"
            ) as call,
            patch("scanning.management.commands.run_daemon.signal.signal"),
            patch("scanning.management.commands.run_daemon.time.sleep"),
            patch("scanning.opinion_pdf.release_mirrors") as release,
        ):
            cmd.handle()
        self.assertTrue(sent)
        call.assert_not_called()
        # The opinions worker frees the mirrors, not the clock.
        release.assert_not_called()

    @override_settings(CELERY_ENABLED=True)
    def test_the_signal_requeues_nothing(self):
        """The workers hold the scans."""
        self.addCleanup(stopping.reset)
        scan = ScanFactory(status=Status.PROCESSING)
        cmd = Command()
        cmd.celery = True
        with patch("sentry_sdk.capture_message"):
            cmd._handle_signal(15, None)
        self.assertTrue(cmd.shutdown)
        scan.refresh_from_db()
        self.assertEqual(scan.status, Status.PROCESSING)
