"""Tests for the run_daemon scheduler."""

from unittest.mock import patch

from django.test import TestCase, override_settings

from scanning.management.commands.run_daemon import Command, ScheduledTask


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
