"""Tests for ``scanning.pipeline`` -- the advance half of the cycle.

The property under test throughout is re-entrancy: running a scan's
action twice must pick up where the jobs got to, not start over and not
double-apply.
"""

from unittest.mock import MagicMock, patch

from django.test import TestCase, override_settings

from scanning import jobs, pipeline
from scanning.factories import ExternalJobFactory, ScanFactory
from scanning.models import (
    ExternalJob,
    JobEngine,
    JobProvider,
    JobStage,
    JobStatus,
    QueuedAction,
    Scan,
    Status,
)


def _batch_on():
    """Patch the three conditions ``batch_enabled`` checks.

    :returns: A context manager enabling the batch path.
    """
    return patch("scanning.jobs.batch_enabled", return_value=True)


class TestBatchEnabled(TestCase):
    """The flag is one of three necessary conditions."""

    @override_settings(DAEMON_BATCH_JOBS=True, RUNPOD_ENABLED=True)
    def test_needs_credentials_to_deliver_a_result(self):
        # A batched result comes back by presigned PUT and is read on a
        # later tick. Without S3 there is nowhere for it to wait.
        with patch("scanning.utils.has_s3_credentials", return_value=False):
            self.assertFalse(jobs.batch_enabled())
        with patch("scanning.utils.has_s3_credentials", return_value=True):
            self.assertTrue(jobs.batch_enabled())

    @override_settings(DAEMON_BATCH_JOBS=False, RUNPOD_ENABLED=True)
    def test_the_flag_is_off_by_default(self):
        with patch("scanning.utils.has_s3_credentials", return_value=True):
            self.assertFalse(jobs.batch_enabled())

    @override_settings(DAEMON_BATCH_JOBS=True, RUNPOD_ENABLED=False)
    def test_needs_remote_mode(self):
        with patch("scanning.utils.has_s3_credentials", return_value=True):
            self.assertFalse(jobs.batch_enabled())


class AwaitStageTestCase(TestCase):
    """Shared plumbing for the stage barrier."""

    def setUp(self):
        self.scan = ScanFactory()

    def await_detect_stage(self, provider=None):
        """Call ``await_stage`` for a detect job on this scan.

        :param provider: Stub provider for result fetching.
        :returns: The payload list.
        :rtype: list[dict]
        """
        provider = provider or MagicMock()
        with patch("scanning.pipeline.get_provider", return_value=provider):
            return pipeline.await_stage(
                self.scan,
                JobStage.DETECT,
                JobEngine.BLACKLETTER,
                JobProvider.RUNPOD,
                input_key="processing/1/a/1/1/bitonal.pdf",
                manifest={"models": ["small"]},
            )


class TestAwaitStage(AwaitStageTestCase):
    """Creating, waiting on, and consuming a stage."""

    def test_the_first_pass_creates_the_work_and_unwinds(self):
        with self.assertRaises(pipeline.Awaiting):
            self.await_detect_stage()

        job = ExternalJob.objects.get()
        self.assertEqual(job.stage, JobStage.DETECT)
        self.assertEqual(job.status, JobStatus.PENDING)

    def test_a_later_pass_does_not_create_more_work(self):
        # Re-entry is the whole mechanism; it must be free of side
        # effects beyond noticing what finished.
        for _ in range(3):
            with self.assertRaises(pipeline.Awaiting):
                self.await_detect_stage()

        self.assertEqual(ExternalJob.objects.count(), 1)

    def test_an_in_flight_stage_still_unwinds(self):
        with self.assertRaises(pipeline.Awaiting):
            self.await_detect_stage()
        ExternalJob.objects.update(status=JobStatus.IN_PROGRESS)

        with self.assertRaises(pipeline.Awaiting) as ctx:
            self.await_detect_stage()
        self.assertEqual(ctx.exception.stage, JobStage.DETECT)

    def test_a_completed_stage_returns_its_payload(self):
        with self.assertRaises(pipeline.Awaiting):
            self.await_detect_stage()
        ExternalJob.objects.update(
            status=JobStatus.COMPLETED, result_key="k.json"
        )

        provider = MagicMock()
        provider.fetch_result.return_value = {"detections": [{"page": 0}]}
        payloads = self.await_detect_stage(provider)

        self.assertEqual(payloads, [{"detections": [{"page": 0}]}])

    def test_reading_a_result_consumes_the_job(self):
        with self.assertRaises(pipeline.Awaiting):
            self.await_detect_stage()
        ExternalJob.objects.update(
            status=JobStatus.COMPLETED, result_key="k.json"
        )

        self.await_detect_stage()

        job = ExternalJob.objects.get()
        self.assertEqual(job.status, JobStatus.CONSUMED)
        self.assertIsNotNone(job.consumed_at)

    def test_a_consumed_stage_is_re_read_not_re_run(self):
        # The pipeline may have died after applying a result. Re-entry
        # must find the same answer, not pay for the job again.
        job = ExternalJobFactory(
            scan=self.scan,
            stage=JobStage.DETECT,
            engine=JobEngine.BLACKLETTER,
            status=JobStatus.CONSUMED,
            result_key="k.json",
        )
        provider = MagicMock()
        provider.fetch_result.return_value = {"detections": []}

        payloads = self.await_detect_stage(provider)

        self.assertEqual(payloads, [{"detections": []}])
        provider.fetch_result.assert_called_once()
        self.assertEqual(ExternalJob.objects.count(), 1)
        job.refresh_from_db()
        self.assertEqual(job.status, JobStatus.CONSUMED)

    def test_a_dead_job_fails_the_stage(self):
        ExternalJobFactory(
            scan=self.scan,
            stage=JobStage.DETECT,
            engine=JobEngine.BLACKLETTER,
            status=JobStatus.FAILED,
            error_code="BAD_INPUT",
            error_message="unreadable pdf",
        )

        with self.assertRaises(RuntimeError) as ctx:
            self.await_detect_stage()
        self.assertIn("BAD_INPUT", str(ctx.exception))

    def test_the_stage_waits_for_every_shard(self):
        # A barrier: one finished shard does not release the step.
        jobs.ensure_jobs(
            self.scan,
            JobStage.DETECT,
            JobEngine.BLACKLETTER,
            JobProvider.RUNPOD,
            input_key="k",
            shard_count=3,
        )
        ExternalJob.objects.filter(shard_index=0).update(
            status=JobStatus.COMPLETED, result_key="k0.json"
        )

        with self.assertRaises(pipeline.Awaiting) as ctx:
            self.await_detect_stage()
        self.assertIn("2 of 3", str(ctx.exception))


class TestAdvanceScan(TestCase):
    """What ``advance_scan`` leaves the scan in."""

    def setUp(self):
        self.scan = ScanFactory(
            status=Status.PROCESSING,
            queued_action=QueuedAction.FULL_PIPELINE,
        )

    def advance(self, side_effect=None):
        """Advance the scan with ``run_full_pipeline`` stubbed.

        :param side_effect: What the stubbed runner should do.
        :returns: ``(status, mock)``.
        :rtype: tuple
        """
        with patch(
            "scanning.services.run_full_pipeline", side_effect=side_effect
        ) as mock:
            return pipeline.advance_scan(self.scan.pk), mock

    def test_an_unfinished_step_parks_the_scan(self):
        status, _ = self.advance(
            side_effect=pipeline.Awaiting("detect: 1 of 1 running", "detect")
        )

        self.scan.refresh_from_db()
        self.assertEqual(status, Status.AWAITING)
        self.assertEqual(self.scan.status, Status.AWAITING)
        self.assertIn("detect", self.scan.progress_message)

    def test_a_parked_scan_is_not_held_by_any_process(self):
        # Which is the point of AWAITING: the process-death timeout
        # must not apply to a scan whose work is on a GPU.
        self.advance(side_effect=pipeline.Awaiting("waiting"))

        self.scan.refresh_from_db()
        self.assertNotEqual(self.scan.status, Status.PROCESSING)

    def test_an_unexpected_error_is_a_backstop_not_a_park(self):
        status, _ = self.advance(side_effect=RuntimeError("boom"))

        self.scan.refresh_from_db()
        self.assertEqual(status, Status.ERROR)
        self.assertEqual(self.scan.status, Status.ERROR)

    def test_a_finished_action_keeps_the_status_it_set(self):
        def finish(scan_pk):
            Scan.objects.filter(pk=scan_pk).update(
                status=Status.PENDING_REVIEW
            )

        status, _ = self.advance(side_effect=finish)

        self.assertEqual(status, Status.PENDING_REVIEW)

    def test_an_unknown_action_errors_without_running_anything(self):
        Scan.objects.filter(pk=self.scan.pk).update(queued_action="nonesuch")

        status, mock = self.advance()

        self.assertEqual(status, Status.ERROR)
        mock.assert_not_called()

    def test_a_deleted_scan_is_not_an_error(self):
        pk = self.scan.pk
        self.scan.delete()
        self.assertEqual(pipeline.advance_scan(pk), "")


class TestResumableScans(TestCase):
    """Which parked scans are ready for another pass."""

    def make_awaiting(self, *job_statuses):
        """Create an AWAITING scan with jobs in the given statuses.

        :param job_statuses: One status per job to create.
        :returns: The scan.
        :rtype: Scan
        """
        scan = ScanFactory(status=Status.AWAITING)
        for index, status in enumerate(job_statuses):
            ExternalJobFactory(
                scan=scan,
                status=status,
                shard_index=index,
                shard_count=max(len(job_statuses), 1),
            )
        return scan

    def test_a_scan_with_work_outstanding_is_not_ready(self):
        for status in (
            JobStatus.PENDING,
            JobStatus.SUBMITTED,
            JobStatus.IN_QUEUE,
            JobStatus.IN_PROGRESS,
        ):
            with self.subTest(status=status):
                Scan.objects.all().delete()
                self.make_awaiting(status)
                self.assertEqual(pipeline.resumable_scans().count(), 0)

    def test_a_scan_whose_jobs_all_landed_is_ready(self):
        self.make_awaiting(JobStatus.COMPLETED)
        self.assertEqual(pipeline.resumable_scans().count(), 1)

    def test_one_outstanding_shard_holds_the_scan_back(self):
        self.make_awaiting(JobStatus.COMPLETED, JobStatus.IN_PROGRESS)
        self.assertEqual(pipeline.resumable_scans().count(), 0)

    def test_a_failed_job_still_makes_the_scan_ready(self):
        # Ready to be told it failed: re-entry is what turns a dead job
        # into an ERROR on the scan with a reason attached.
        self.make_awaiting(JobStatus.FAILED)
        self.assertEqual(pipeline.resumable_scans().count(), 1)

    def test_scans_that_are_not_parked_are_left_alone(self):
        ScanFactory(status=Status.PENDING_REVIEW)
        ScanFactory(status=Status.QUEUED)
        self.assertEqual(pipeline.resumable_scans().count(), 0)


class TestAwaitingSurvivesTheErrorHandler(TestCase):
    """``Awaiting`` must not be swallowed as a pipeline failure."""

    def test_the_shared_handler_re_raises_it(self):
        from scanning import services

        scan = ScanFactory(status=Status.PROCESSING)
        exc = pipeline.Awaiting("detect: still running", "detect")

        with self.assertRaises(pipeline.Awaiting):
            services._handle_pipeline_exception(scan.pk, exc)

        scan.refresh_from_db()
        self.assertEqual(scan.status, Status.PROCESSING)
