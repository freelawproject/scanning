"""Tests for the two external-job daemon commands (issue #176).

The commands are thin: they delegate to :mod:`scanning.jobs` and
:mod:`scanning.bitonal` and own only the DB-retry boilerplate every
daemon tick shares. What is worth pinning is the delegation, the
argument passing, and that a transient DB blip is survived quietly
rather than raised into the scheduler (issue #116).
"""

from unittest.mock import patch

from django.core.management import call_command
from django.db import OperationalError
from django.test import TestCase

from scanning.jobs import SubmitSummary, SweepSummary


class TestSubmitCommand(TestCase):
    """``submit_external_jobs``."""

    def test_it_submits_a_wave(self):
        with patch(
            "scanning.jobs.submit_pending",
            return_value=SubmitSummary(submitted=3),
        ) as submit:
            with patch("django.db.connections.close_all"):
                call_command("submit_external_jobs")

        submit.assert_called_once_with(limit=None)

    def test_the_limit_is_passed_through(self):
        with patch(
            "scanning.jobs.submit_pending", return_value=SubmitSummary()
        ) as submit:
            with patch("django.db.connections.close_all"):
                call_command("submit_external_jobs", "--limit", "2")

        submit.assert_called_once_with(limit=2)

    def test_a_transient_db_error_is_retried_then_logged_as_a_warning(self):
        """A self-healing blip must not become a Sentry error event."""
        with patch(
            "scanning.jobs.submit_pending",
            side_effect=OperationalError("ssl gone"),
        ) as submit:
            with patch("django.db.connections.close_all"):
                with patch(
                    "scanning.management.commands."
                    "submit_external_jobs.time.sleep"
                ):
                    with self.assertLogs(
                        "scanning.management.commands.submit_external_jobs",
                        level="WARNING",
                    ) as logs:
                        call_command("submit_external_jobs")

        self.assertEqual(submit.call_count, 3)
        self.assertNotIn("ERROR", "".join(logs.output))

    def test_a_recovered_blip_still_submits(self):
        with patch(
            "scanning.jobs.submit_pending",
            side_effect=[
                OperationalError("ssl gone"),
                SubmitSummary(submitted=1),
            ],
        ) as submit:
            with patch("django.db.connections.close_all"):
                with patch(
                    "scanning.management.commands."
                    "submit_external_jobs.time.sleep"
                ):
                    call_command("submit_external_jobs")

        self.assertEqual(submit.call_count, 2)


class TestCollectCommand(TestCase):
    """``collect_external_jobs``."""

    def test_it_retries_then_confirms_then_applies(self):
        """Order matters twice over.

        A job confirmed this tick should finish its scan in the same
        tick; and a failed shard must go back to PENDING *before* the
        apply pass judges its scan, or that scan is written off in the
        tick that revived it.
        """
        calls = []
        with patch(
            "scanning.jobs.retry_dead",
            side_effect=lambda *a, **kw: calls.append("retry") or 1,
        ):
            with patch(
                "scanning.jobs.sweep_jobs",
                side_effect=lambda *a, **kw: (
                    calls.append("sweep") or SweepSummary(completed=2)
                ),
            ):
                with patch(
                    "scanning.bitonal.finish_ready_scans",
                    side_effect=lambda: calls.append("finish") or 1,
                ):
                    with patch("django.db.connections.close_all"):
                        call_command("collect_external_jobs")

        self.assertEqual(calls, ["retry", "sweep", "finish"])

    def test_a_transient_db_error_is_survived_quietly(self):
        with patch(
            "scanning.jobs.sweep_jobs",
            side_effect=OperationalError("ssl gone"),
        ) as sweep:
            with patch("scanning.bitonal.finish_ready_scans") as finish:
                with patch("django.db.connections.close_all"):
                    with patch(
                        "scanning.management.commands."
                        "collect_external_jobs.time.sleep"
                    ):
                        with self.assertLogs(
                            "scanning.management.commands."
                            "collect_external_jobs",
                            level="WARNING",
                        ):
                            call_command("collect_external_jobs")

        self.assertEqual(sweep.call_count, 3)
        finish.assert_not_called()
