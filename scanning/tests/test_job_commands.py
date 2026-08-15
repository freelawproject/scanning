"""Tests for the daemon's job and scan commands.

The sweeps and the pipeline itself are covered in ``test_jobs`` and
``test_pipeline``; these are the wrappers the daemon schedules -- what
they claim, their DB-blip retry, and what they print.

Every command here calls ``connections.close_all()`` on each attempt,
which would tear down the connection holding this TestCase's
transaction. It is patched out throughout.
"""

from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.db import OperationalError
from django.test import TestCase, override_settings

from scanning.factories import ExternalJobFactory, ScanFactory
from scanning.jobs import CollectSummary, SubmitSummary
from scanning.models import JobStatus, Scan, Status


class TestCollectCommand(TestCase):
    """Command-level behaviour: retries, output, and failure handling."""

    def _run(self, **kwargs):
        """Invoke the command with ``collect_once`` stubbed.

        :param kwargs: Passed to ``patch`` for ``collect_once``.
        :returns: ``(stdout, mock)``.
        :rtype: tuple
        """
        out = StringIO()
        with (
            patch("django.db.connections.close_all"),
            patch("scanning.jobs.collect_once", **kwargs) as mock,
        ):
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
        with (
            patch("django.db.connections.close_all"),
            patch("scanning.jobs.submit_pending", **kwargs) as mock,
        ):
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


class TestAdvanceScansCommand(TestCase):
    """``advance_scans`` claims parked scans and re-enters them."""

    def _run(self):
        """Invoke the command with ``advance_scan`` stubbed.

        :returns: ``(stdout, mock)``.
        :rtype: tuple
        """
        out = StringIO()
        with (
            patch("django.db.connections.close_all"),
            patch("scanning.pipeline.advance_scan") as mock,
        ):
            call_command("advance_scans", stdout=out)
        return out.getvalue(), mock

    def _awaiting_with(self, job_status):
        """Create a parked scan with one job in ``job_status``.

        :param job_status: The job's status.
        :returns: The scan.
        :rtype: Scan
        """
        scan = ScanFactory(status=Status.AWAITING)
        ExternalJobFactory(scan=scan, status=job_status)
        return scan

    def test_a_ready_scan_is_claimed_and_advanced(self):
        scan = self._awaiting_with(JobStatus.COMPLETED)

        _, mock = self._run()

        scan.refresh_from_db()
        # Claimed into PROCESSING first: while the pipeline runs, a
        # daemon really is holding the scan, so the process-death
        # timeout should apply again.
        self.assertEqual(scan.status, Status.PROCESSING)
        mock.assert_called_once_with(scan.pk)

    def test_a_scan_still_waiting_is_left_parked(self):
        scan = self._awaiting_with(JobStatus.IN_PROGRESS)

        _, mock = self._run()

        scan.refresh_from_db()
        self.assertEqual(scan.status, Status.AWAITING)
        mock.assert_not_called()

    def test_every_ready_scan_moves_on_one_tick(self):
        for _ in range(3):
            self._awaiting_with(JobStatus.COMPLETED)

        _, mock = self._run()

        self.assertEqual(mock.call_count, 3)

    def test_a_db_blip_is_retried(self):
        scan = self._awaiting_with(JobStatus.COMPLETED)
        out = StringIO()
        with (
            patch("django.db.connections.close_all"),
            patch("scanning.pipeline.advance_scan") as mock,
            patch(
                "scanning.pipeline.resumable_scans",
                side_effect=[
                    OperationalError("ssl closed"),
                    Scan.objects.filter(pk=scan.pk),
                ],
            ),
        ):
            call_command("advance_scans", stdout=out)
        mock.assert_called_once_with(scan.pk)


class TestBatchClaim(TestCase):
    """How wide a claim tick is depends on whether steps still block."""

    def _claim(self, batch=False):
        """Run one claim tick.

        :param batch: Whether the batch cycle is enabled.
        :returns: The scan pks claimed.
        :rtype: list[int]
        """
        out = StringIO()
        with (
            patch("django.db.connections.close_all"),
            patch("scanning.jobs.batch_enabled", return_value=batch),
            patch("scanning.pipeline.advance_scan") as mock,
        ):
            call_command("process_next_scan", stdout=out)
        return [c.args[0] for c in mock.call_args_list]

    def test_blocking_mode_still_takes_one_scan(self):
        # A claimed scan is worked to completion before the next
        # starts, so claiming several would run them in series while
        # holding the rest hostage.
        for _ in range(3):
            ScanFactory(status=Status.QUEUED)

        self.assertEqual(len(self._claim(batch=False)), 1)

    @override_settings(DAEMON_BATCH_SIZE=0)
    def test_batch_mode_takes_the_whole_queue(self):
        for _ in range(4):
            ScanFactory(status=Status.QUEUED)

        self.assertEqual(len(self._claim(batch=True)), 4)

    @override_settings(DAEMON_BATCH_SIZE=2)
    def test_the_batch_can_be_capped(self):
        for _ in range(5):
            ScanFactory(status=Status.QUEUED)

        self.assertEqual(len(self._claim(batch=True)), 2)

    @override_settings(DAEMON_BATCH_SIZE=0)
    def test_claiming_marks_every_scan_processing(self):
        scans = [ScanFactory(status=Status.QUEUED) for _ in range(3)]

        self._claim(batch=True)

        for scan in scans:
            scan.refresh_from_db()
            self.assertEqual(scan.status, Status.PROCESSING)
            self.assertIsNotNone(scan.processed_at)

    def test_an_empty_queue_claims_nothing(self):
        ScanFactory(status=Status.PENDING_REVIEW)
        self.assertEqual(self._claim(batch=True), [])


class TestClaimWindow(TestCase):
    """A snapshotted scan is claimed at its turn, not up front."""

    def _run(self, batch=True):
        """Run one claim tick with the pipeline stubbed.

        :param batch: Whether the batch cycle is enabled.
        :returns: The mock standing in for ``advance_scan``.
        :rtype: MagicMock
        """
        out = StringIO()
        with (
            patch("django.db.connections.close_all"),
            patch("scanning.jobs.batch_enabled", return_value=batch),
            patch("scanning.pipeline.advance_scan") as mock,
        ):
            call_command("process_next_scan", stdout=out)
        return mock

    @override_settings(DAEMON_BATCH_SIZE=0)
    def test_the_tail_of_a_batch_stays_queued_until_its_turn(self):
        # Marking the whole batch PROCESSING up front would leave the
        # tail in it for as long as the head takes, which a second
        # replica's stale sweep would read as a dead daemon.
        scans = [ScanFactory(status=Status.QUEUED) for _ in range(3)]
        seen = []

        def record_statuses(scan_pk):
            seen.append(dict(Scan.objects.values_list("pk", "status")))

        out = StringIO()
        with (
            patch("django.db.connections.close_all"),
            patch("scanning.jobs.batch_enabled", return_value=True),
            patch(
                "scanning.pipeline.advance_scan",
                side_effect=record_statuses,
            ),
        ):
            call_command("process_next_scan", stdout=out)

        # While the first scan is being worked, the others are still
        # queued rather than sitting in PROCESSING waiting their turn.
        self.assertEqual(seen[0][scans[0].pk], Status.PROCESSING)
        self.assertEqual(seen[0][scans[1].pk], Status.QUEUED)
        self.assertEqual(seen[0][scans[2].pk], Status.QUEUED)

    @override_settings(DAEMON_BATCH_SIZE=0)
    def test_a_scan_taken_by_someone_else_is_skipped(self):
        mine = ScanFactory(status=Status.QUEUED)
        theirs = ScanFactory(status=Status.QUEUED)
        # Another replica claimed it between the snapshot and its turn.
        Scan.objects.filter(pk=theirs.pk).update(status=Status.PROCESSING)

        mock = self._run()

        self.assertEqual([c.args[0] for c in mock.call_args_list], [mine.pk])

    @override_settings(DAEMON_BATCH_SIZE=0)
    def test_a_scan_cancelled_before_its_turn_is_skipped(self):
        scan = ScanFactory(status=Status.QUEUED)
        Scan.objects.filter(pk=scan.pk).update(status=Status.CANCELLED)

        mock = self._run()

        mock.assert_not_called()
