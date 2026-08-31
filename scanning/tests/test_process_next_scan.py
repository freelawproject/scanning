"""Tests for the process_next_scan daemon tick: the transient
DB-connection retry behavior added for issue #116 (SCANNING-20), the
stale-scan recovery, the legacy-action park, and the intake cap that
paces admission against the external queues (issue #218)."""

from datetime import timedelta
from unittest.mock import patch

from django.db import OperationalError
from django.test import TestCase, override_settings
from django.utils import timezone

from scanning import jobs
from scanning.factories import ExternalJobFactory, ScanFactory
from scanning.management.commands import process_next_scan as module
from scanning.management.commands.process_next_scan import (
    MAX_DB_RETRIES,
    Command,
)
from scanning.models import (
    ExternalJob,
    JobEngine,
    JobProvider,
    JobStage,
    JobStatus,
    Scan,
    Status,
)


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

    def test_an_awaiting_scan_is_never_swept(self):
        """Waiting on external jobs is not the same as being stuck.

        AWAITING (#176) means nothing of ours is running and the job
        rows carry their own deadlines. Sweeping it would charge an
        interruption for waiting and re-run work already paid for --
        which is why it is a separate status from PROCESSING.
        """
        from datetime import timedelta

        scan = ScanFactory(status=Status.AWAITING, interruption_count=0)
        Scan.objects.filter(pk=scan.pk).update(
            processed_at=timezone.now() - timedelta(days=1)
        )

        Command()._recover_stale()

        scan.refresh_from_db()
        self.assertEqual(scan.status, Status.AWAITING)
        self.assertEqual(scan.interruption_count, 0)


class TestLegacyQueuedActionsPark(TestCase):
    """Legacy queued actions are parked, not run or errored (issue #173).

    The views that queued validate/detect/reprocess/generate_files now
    refuse with the pipeline-paused message, so only a row from before
    the cutover can still carry one. The daemon must neither dispatch a
    deleted pipeline nor mark the scan ERROR: it parks the scan back in
    PENDING_REVIEW with the unified message.
    """

    def _dispatch(self, action):
        scan = ScanFactory(status=Status.QUEUED, queued_action=action)
        cmd = Command()
        with (
            patch("django.db.connections.close_all"),
            patch("scanning.services.run_full_pipeline") as mock_pipeline,
        ):
            cmd.handle()
        mock_pipeline.assert_not_called()
        scan.refresh_from_db()
        return scan

    def test_each_legacy_action_parks_the_scan(self):
        from scanning.models import QueuedAction
        from scanning.utils import PIPELINE_PAUSED_MESSAGE

        for action in (
            QueuedAction.VALIDATE,
            QueuedAction.DETECT,
            QueuedAction.REPROCESS,
            QueuedAction.GENERATE_FILES,
        ):
            with self.subTest(action=action):
                scan = self._dispatch(action)
                self.assertEqual(scan.status, Status.PENDING_REVIEW)
                self.assertEqual(
                    scan.progress_message, PIPELINE_PAUSED_MESSAGE
                )
                # Park the row for real, or the next tick would claim it
                # again and loop forever.
                self.assertFalse(
                    Scan.objects.filter(status=Status.QUEUED).exists()
                )


class TestIntakeCap(TestCase):
    """The tick admits no scan while DAEMON_MAX_ACTIVE_SCANS are busy.

    Issue #218: uncapped intake queued more work than
    DAEMON_JOB_MAX_QUEUE_SECONDS lets a row wait, so the rows expired
    unsubmitted and sank their volumes to ERROR.
    """

    def _busy_scan(self, status=JobStatus.PENDING, rows=1):
        """Create a scan holding ``rows`` job rows at ``status``.

        :param status: The JobStatus every row gets.
        :param rows: How many rows the scan holds.
        :return: The scan.
        """
        scan = ScanFactory(status=Status.AWAITING)
        for index in range(rows):
            ExternalJobFactory(
                scan=scan,
                stage=JobStage.CONVERT,
                engine=JobEngine.BITONAL,
                provider=JobProvider.DOCTOR,
                status=status,
                shard_index=index,
                shard_count=rows,
            )
        return scan

    def _tick(self):
        """Run one whole tick with the pipeline call patched out.

        :return: The patched ``run_full_pipeline`` mock.
        """
        with (
            patch("django.db.connections.close_all"),
            patch.object(module, "_intake_full", False),
            patch("scanning.services.run_full_pipeline") as pipeline,
        ):
            Command().handle()
        return pipeline

    @override_settings(DAEMON_MAX_ACTIVE_SCANS=2)
    def test_a_scan_is_claimed_under_the_cap(self):
        """One busy scan against a cap of two still admits the next."""
        self._busy_scan()
        queued = ScanFactory(status=Status.QUEUED)

        pipeline = self._tick()

        queued.refresh_from_db()
        self.assertEqual(queued.status, Status.PROCESSING)
        pipeline.assert_called_once_with(queued.pk)

    @override_settings(DAEMON_MAX_ACTIVE_SCANS=2)
    def test_no_scan_is_claimed_at_the_cap(self):
        """A refused scan stays QUEUED, untouched and undispatched."""
        self._busy_scan()
        self._busy_scan()
        queued = ScanFactory(status=Status.QUEUED)

        pipeline = self._tick()

        queued.refresh_from_db()
        self.assertEqual(queued.status, Status.QUEUED)
        # Nothing was written: no PROCESSING transit, no claim stamp.
        self.assertIsNone(queued.processed_at)
        pipeline.assert_not_called()

    @override_settings(DAEMON_MAX_ACTIVE_SCANS=1)
    def test_the_cap_moves_with_the_setting(self):
        """One busy scan is enough at a cap of one."""
        self._busy_scan()
        queued = ScanFactory(status=Status.QUEUED)

        pipeline = self._tick()

        queued.refresh_from_db()
        self.assertEqual(queued.status, Status.QUEUED)
        pipeline.assert_not_called()

    @override_settings(DAEMON_MAX_ACTIVE_SCANS=1)
    def test_in_flight_rows_hold_the_slot(self):
        """A row a provider is working on is what the cap counts."""
        for status in (
            JobStatus.SUBMITTED,
            JobStatus.IN_QUEUE,
            JobStatus.IN_PROGRESS,
        ):
            with self.subTest(status=status):
                ExternalJob.objects.all().delete()
                self._busy_scan(status=status)

                self.assertEqual(jobs.active_scan_count(), 1)

    @override_settings(DAEMON_MAX_ACTIVE_SCANS=1)
    def test_finished_and_dead_rows_free_the_slot(self):
        """Only unfinished provider work holds a slot.

        A COMPLETED row waits on local work, and a failed merge leaves
        one nothing moves again -- so it must not hold a slot for good.
        """
        for status in (
            JobStatus.COMPLETED,
            JobStatus.CONSUMED,
            JobStatus.FAILED,
            JobStatus.CANCELLED,
            JobStatus.EXPIRED,
        ):
            with self.subTest(status=status):
                ExternalJob.objects.all().delete()
                self._busy_scan(status=status)

                self.assertEqual(jobs.active_scan_count(), 0)

    def test_one_scan_with_many_rows_holds_one_slot(self):
        """Scans are the unit, so a shard count cannot inflate the count."""
        self._busy_scan(rows=11)
        self._busy_scan(rows=11)

        self.assertEqual(jobs.active_scan_count(), 2)

    @override_settings(DAEMON_MAX_ACTIVE_SCANS=1)
    def test_recovery_still_runs_while_intake_is_full(self):
        """Returning a dropped scan is not intake, so the cap misses it."""
        self._busy_scan()
        stale = ScanFactory(status=Status.PROCESSING, interruption_count=0)
        Scan.objects.filter(pk=stale.pk).update(
            processed_at=timezone.now() - timedelta(seconds=7200)
        )

        self._tick()

        stale.refresh_from_db()
        self.assertEqual(stale.status, Status.QUEUED)

    @override_settings(DAEMON_MAX_ACTIVE_SCANS=1)
    def test_the_crossing_logs_once_in_each_direction(self):
        """Loud at the edge, quiet in between: one line each way."""
        scan = self._busy_scan()
        cmd = Command()

        with (
            patch.object(module, "_intake_full", False),
            self.assertLogs(module.logger, level="INFO") as logs,
        ):
            self.assertTrue(cmd._intake_is_full())
            # Still full: the second and third ticks say nothing.
            self.assertTrue(cmd._intake_is_full())
            self.assertTrue(cmd._intake_is_full())
            ExternalJob.objects.filter(scan=scan).update(
                status=JobStatus.CONSUMED
            )
            self.assertFalse(cmd._intake_is_full())
            self.assertFalse(cmd._intake_is_full())

        levels = [r.levelname for r in logs.records]
        self.assertEqual(levels, ["WARNING", "INFO"])
        self.assertIn("intake paused", logs.records[0].getMessage())
        self.assertIn("intake resumed", logs.records[1].getMessage())
