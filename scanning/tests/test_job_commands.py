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

    def test_the_detection_sweep_runs_before_the_wave(self):
        """So the rows the sweep creates go out on the same tick (#250)."""
        order = []
        with (
            patch(
                "scanning.yolo.enqueue_missing_runs",
                side_effect=lambda: order.append("sweep") or 2,
            ),
            patch(
                "scanning.jobs.submit_pending",
                side_effect=lambda limit=None: (
                    order.append("wave") or SubmitSummary(submitted=2)
                ),
            ),
            patch("django.db.connections.close_all"),
        ):
            call_command("submit_external_jobs")

        self.assertEqual(order, ["sweep", "wave"])

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

    def test_it_confirms_then_applies(self):
        """Order matters: a job confirmed this tick should finish it."""
        calls = []
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
                with patch(
                    "scanning.dots_mocr.finish_ready_runs",
                    side_effect=lambda: calls.append("glue") or 1,
                ):
                    with patch(
                        "scanning.dots_mocr.apply_ready_runs",
                        side_effect=lambda: calls.append("apply") or 1,
                    ):
                        with patch("django.db.connections.close_all"):
                            call_command("collect_external_jobs")

        self.assertEqual(calls, ["sweep", "finish", "glue", "apply"])

    def test_a_transient_db_error_is_survived_quietly(self):
        with patch(
            "scanning.jobs.sweep_jobs",
            side_effect=OperationalError("ssl gone"),
        ) as sweep:
            with patch("scanning.bitonal.finish_ready_scans") as finish:
                with patch("scanning.dots_mocr.finish_ready_runs") as glue:
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
        glue.assert_not_called()
