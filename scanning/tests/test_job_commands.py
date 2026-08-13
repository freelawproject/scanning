"""Tests for the submit and collect management commands.

The sweeps themselves are covered in ``test_jobs``; these are the
wrappers the daemon schedules -- their DB-blip retry and what they
print.
"""

from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.db import OperationalError
from django.test import TestCase

from scanning.jobs import CollectSummary, SubmitSummary


class TestCollectCommand(TestCase):
    """Command-level behaviour: retries, output, and failure handling."""

    def _run(self, **kwargs):
        """Invoke the command with ``collect_once`` stubbed.

        :param kwargs: Passed to ``patch`` for ``collect_once``.
        :returns: ``(stdout, mock)``.
        :rtype: tuple
        """
        out = StringIO()
        with patch("scanning.jobs.collect_once", **kwargs) as mock:
            call_command("collect_external_jobs", stdout=out)
        return out.getvalue(), mock

    def test_reports_what_moved(self):
        summary = CollectSummary(polled=3, completed=1, retried=1)
        output, _ = self._run(return_value=summary)
        self.assertIn("polled 3", output)
        self.assertIn("completed 1", output)

    def test_an_empty_sweep_says_nothing(self):
        # It runs every 15s forever; a quiet tick should not fill the
        # daemon's logs.
        output, _ = self._run(return_value=CollectSummary())
        self.assertEqual(output, "")

    def test_a_db_blip_is_retried(self):
        summary = CollectSummary(polled=1)
        output, mock = self._run(
            side_effect=[OperationalError("ssl closed"), summary]
        )
        self.assertEqual(mock.call_count, 2)
        self.assertIn("polled 1", output)

    def test_a_db_outage_gives_up_quietly(self):
        # The next tick picks up the whole sweep, so a failed one is not
        # worth a Sentry error event.
        output, mock = self._run(side_effect=OperationalError("down"))
        self.assertEqual(mock.call_count, 3)
        self.assertEqual(output, "")


class TestSubmitCommand(TestCase):
    """The submit half's wrapper: retries, output, and the limit flag."""

    def _run(self, argv=(), **kwargs):
        """Invoke the command with ``submit_pending`` stubbed.

        :param argv: Extra command-line arguments.
        :param kwargs: Passed to ``patch`` for ``submit_pending``.
        :returns: ``(stdout, mock)``.
        :rtype: tuple
        """
        out = StringIO()
        with patch("scanning.jobs.submit_pending", **kwargs) as mock:
            call_command("submit_external_jobs", *argv, stdout=out)
        return out.getvalue(), mock

    def test_reports_what_went_out(self):
        output, _ = self._run(return_value=SubmitSummary(submitted=7))
        self.assertIn("submitted 7", output)

    def test_a_quiet_tick_says_nothing(self):
        output, _ = self._run(return_value=SubmitSummary())
        self.assertEqual(output, "")

    def test_a_deferred_batch_is_still_reported(self):
        # A paused endpoint is worth seeing in the daemon log even
        # though nothing moved.
        output, _ = self._run(return_value=SubmitSummary(deferred=3))
        self.assertIn("deferred 3", output)

    def test_limit_is_passed_through(self):
        _, mock = self._run(
            argv=("--limit", "5"), return_value=SubmitSummary()
        )
        self.assertEqual(mock.call_args.kwargs["limit"], 5)

    def test_a_db_blip_is_retried(self):
        summary = SubmitSummary(submitted=1)
        output, mock = self._run(
            side_effect=[OperationalError("ssl closed"), summary]
        )
        self.assertEqual(mock.call_count, 2)
        self.assertIn("submitted 1", output)
