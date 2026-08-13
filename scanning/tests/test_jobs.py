"""Tests for ``scanning.jobs`` -- the submit and collect halves.

Providers are stubbed: what matters here is what the sweep writes to a
row given an answer, not how any provider obtains one.
"""

from datetime import timedelta
from unittest.mock import MagicMock, patch

from django.test import TestCase, override_settings
from django.utils import timezone

from scanning import jobs
from scanning.factories import ExternalJobFactory, ScanFactory
from scanning.models import (
    ExternalJob,
    JobEngine,
    JobProvider,
    JobStage,
    JobStatus,
)
from scanning.providers import PollOutcome, SubmitReceipt

_KEY = "processing/1/a/1/1/jobs/detect/r1-s0-a1.json"


def _outcome(status, **kwargs):
    """Build a poll outcome with sensible defaults.

    :param status: The normalized status, or None for "no answer".
    :param kwargs: Any other :class:`PollOutcome` field.
    :returns: The outcome.
    :rtype: PollOutcome
    """
    return PollOutcome(status=status, **kwargs)


class CollectTestCase(TestCase):
    """Shared plumbing: an in-flight job and a stubbed provider."""

    def make_job(self, **kwargs):
        """Create an in-flight job with a result key.

        :param kwargs: Overrides for the factory.
        :returns: The job.
        :rtype: ExternalJob
        """
        defaults = {
            "status": JobStatus.IN_PROGRESS,
            "external_id": "job-xyz",
            "result_key": _KEY,
            "submitted_at": timezone.now() - timedelta(minutes=5),
        }
        return ExternalJobFactory(**{**defaults, **kwargs})

    def collect(self, provider=None, now=None):
        """Run one sweep against a stubbed provider.

        :param provider: Stub provider, or None for a bare MagicMock.
        :param now: Tick timestamp.
        :returns: ``(summary, provider)``.
        :rtype: tuple
        """
        provider = provider or MagicMock()
        with patch("scanning.jobs.get_provider", return_value=provider):
            return jobs.collect_once(now=now), provider


class TestSweepsTheRightRows(CollectTestCase):
    """Which jobs a sweep asks about."""

    def test_polls_every_in_flight_job_in_one_tick(self):
        # The whole point of the batch cycle: many jobs, one sweep.
        for _ in range(4):
            self.make_job()
        provider = MagicMock()
        provider.poll.return_value = _outcome(JobStatus.IN_PROGRESS)

        summary, _ = self.collect(provider)

        self.assertEqual(summary.polled, 4)
        self.assertEqual(provider.poll.call_count, 4)

    def test_ignores_jobs_that_are_not_in_flight(self):
        for status in (
            JobStatus.PENDING,
            JobStatus.COMPLETED,
            JobStatus.CONSUMED,
            JobStatus.FAILED,
        ):
            self.make_job(status=status)

        summary, provider = self.collect()

        self.assertEqual(summary.polled, 0)
        provider.poll.assert_not_called()

    def test_one_jobs_failure_does_not_end_the_sweep(self):
        # Failure isolation. A provider that raises on one row must not
        # cost the other jobs their tick.
        self.make_job(external_id="bad")
        good = self.make_job(external_id="good")

        provider = MagicMock()
        provider.poll.side_effect = [
            RuntimeError("provider exploded"),
            _outcome(JobStatus.COMPLETED),
        ]
        with patch("scanning.jobs.get_provider", return_value=provider):
            summary = jobs.collect_once()

        self.assertEqual(summary.errors, 1)
        self.assertEqual(summary.completed, 1)
        good.refresh_from_db()
        self.assertEqual(good.status, JobStatus.COMPLETED)


class TestAppliesTheOutcome(CollectTestCase):
    """What one answer writes to the row."""

    def test_still_running_records_the_status_and_the_poll(self):
        job = self.make_job(status=JobStatus.SUBMITTED)
        provider = MagicMock()
        provider.poll.return_value = _outcome(
            JobStatus.IN_QUEUE, provider_status="IN_QUEUE"
        )

        summary, _ = self.collect(provider)

        job.refresh_from_db()
        self.assertEqual(job.status, JobStatus.IN_QUEUE)
        self.assertIsNotNone(job.last_polled_at)
        self.assertEqual(summary.unchanged, 1)

    def test_a_failed_poll_leaves_the_job_alone(self):
        # We learned nothing, which is not the same as learning it
        # failed. Only the "we looked" stamp moves.
        job = self.make_job()
        provider = MagicMock()
        provider.poll.return_value = _outcome(None, error_message="timeout")

        summary, _ = self.collect(provider)

        job.refresh_from_db()
        self.assertEqual(job.status, JobStatus.IN_PROGRESS)
        self.assertIsNotNone(job.last_polled_at)
        self.assertEqual(summary.unchanged, 1)

    def test_completed_stops_at_completed(self):
        # Not CONSUMED: the result has not been applied, and calling it
        # done here would lose the output.
        job = self.make_job()
        provider = MagicMock()
        provider.poll.return_value = _outcome(
            JobStatus.COMPLETED, output={"result_key": _KEY}
        )

        summary, _ = self.collect(provider)

        job.refresh_from_db()
        self.assertEqual(job.status, JobStatus.COMPLETED)
        self.assertIsNotNone(job.completed_at)
        self.assertIsNone(job.consumed_at)
        self.assertEqual(summary.completed, 1)

    def test_completed_never_fetches_the_result(self):
        # Applying a result belongs to the advance phase, which reads it
        # back off result_key on its own tick.
        self.make_job()
        provider = MagicMock()
        provider.poll.return_value = _outcome(JobStatus.COMPLETED)

        self.collect(provider)

        provider.fetch_result.assert_not_called()

    def test_completed_without_a_result_key_fails_loudly(self):
        # An inline payload has nowhere to survive the tick. Dropping
        # it silently would lose work we already paid for.
        job = self.make_job(result_key="")
        provider = MagicMock()
        provider.poll.return_value = _outcome(
            JobStatus.COMPLETED, output={"detections": []}
        )

        summary, _ = self.collect(provider)

        job.refresh_from_db()
        self.assertEqual(job.status, JobStatus.FAILED)
        self.assertEqual(job.error_code, "NO_RESULT_KEY")
        self.assertEqual(summary.failed, 1)

    def test_completing_clears_an_earlier_attempts_error(self):
        job = self.make_job(error_code="NO_GPU", error_message="no capacity")
        provider = MagicMock()
        provider.poll.return_value = _outcome(JobStatus.COMPLETED)

        self.collect(provider)

        job.refresh_from_db()
        self.assertEqual(job.error_code, "")
        self.assertEqual(job.error_message, "")


@override_settings(RUNPOD_MAX_TRANSIENT_RETRIES=2)
class TestRetryAndFailure(CollectTestCase):
    """Where the retry budget is spent, and what a retry preserves."""

    def _fail(self, job, retriable=True, status=JobStatus.FAILED):
        """Sweep ``job`` with a failing outcome.

        :param job: The row to fail.
        :param retriable: Whether the failure invites another submit.
        :param status: The normalized terminal status.
        :returns: The sweep summary.
        :rtype: jobs.CollectSummary
        """
        provider = MagicMock()
        provider.poll.return_value = _outcome(
            status,
            error_code="NO_GPU",
            error_message="no capacity",
            retriable=retriable,
        )
        summary, _ = self.collect(provider)
        job.refresh_from_db()
        return summary

    def test_a_retriable_failure_goes_back_to_pending(self):
        job = self.make_job()

        summary = self._fail(job)

        self.assertEqual(job.status, JobStatus.PENDING)
        self.assertEqual(job.retry_count, 1)
        self.assertEqual(job.attempt, 2)
        self.assertEqual(summary.retried, 1)

    def test_a_retry_clears_the_previous_attempts_handles(self):
        # The next submit mints its own. Reusing the key would let an
        # abandoned worker's late upload read as this attempt's output.
        job = self.make_job()

        self._fail(job)

        self.assertEqual(job.external_id, "")
        self.assertEqual(job.result_key, "")
        self.assertIsNone(job.submitted_at)
        self.assertIsNone(job.deadline)

    def test_a_retry_keeps_the_previous_attempt_as_history(self):
        # A retry mutates the row, so this list is the only place the
        # earlier provider id and failure survive.
        job = self.make_job(external_id="job-first")

        self._fail(job)

        attempts = job.provider_meta["attempts"]
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0]["external_id"], "job-first")
        self.assertEqual(attempts[0]["result_key"], _KEY)
        self.assertEqual(attempts[0]["status"], JobStatus.FAILED)

    def test_a_terminal_failure_is_not_retried(self):
        job = self.make_job()

        summary = self._fail(job, retriable=False)

        self.assertEqual(job.status, JobStatus.FAILED)
        self.assertEqual(job.retry_count, 0)
        self.assertEqual(job.error_code, "NO_GPU")
        self.assertEqual(summary.failed, 1)

    def test_the_retry_budget_is_per_job(self):
        # Two jobs failing on the same tick each spend their own budget,
        # rather than one exhausting a shared one.
        first = self.make_job()
        second = self.make_job()

        provider = MagicMock()
        provider.poll.return_value = _outcome(
            JobStatus.FAILED, error_message="boom", retriable=True
        )
        summary, _ = self.collect(provider)

        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(summary.retried, 2)
        self.assertEqual(first.retry_count, 1)
        self.assertEqual(second.retry_count, 1)

    def test_the_budget_runs_out(self):
        job = self.make_job(retry_count=2)

        summary = self._fail(job)

        self.assertEqual(job.status, JobStatus.FAILED)
        self.assertEqual(job.retry_count, 2)
        self.assertEqual(summary.failed, 1)

    def test_an_expired_job_is_worth_another_submit(self):
        # The job record is gone but the inputs are still on S3.
        job = self.make_job()

        self._fail(job, retriable=True, status=JobStatus.EXPIRED)

        self.assertEqual(job.status, JobStatus.PENDING)


@override_settings(RUNPOD_MAX_TRANSIENT_RETRIES=2)
class TestDeadlines(CollectTestCase):
    """Timeout and cancellation, now that nothing sits on the job."""

    def test_an_overdue_job_is_cancelled_and_retried(self):
        job = self.make_job(
            deadline=timezone.now() - timedelta(minutes=1),
        )
        provider = MagicMock()
        provider.poll.return_value = _outcome(JobStatus.IN_PROGRESS)

        summary, _ = self.collect(provider)

        provider.cancel.assert_called_once()
        job.refresh_from_db()
        self.assertEqual(job.status, JobStatus.PENDING)
        self.assertEqual(job.error_code, "DEADLINE_EXCEEDED")
        self.assertEqual(summary.cancelled, 1)

    def test_an_overdue_job_out_of_budget_settles_as_cancelled(self):
        job = self.make_job(
            deadline=timezone.now() - timedelta(minutes=1),
            retry_count=2,
        )
        provider = MagicMock()
        provider.poll.return_value = _outcome(JobStatus.IN_PROGRESS)

        self.collect(provider)

        job.refresh_from_db()
        self.assertEqual(job.status, JobStatus.CANCELLED)

    def test_a_job_that_finished_late_is_harvested_not_cancelled(self):
        # Polling runs before the deadline check for exactly this: the
        # work is done and paid for, and cancelling it a second before
        # reading it would throw the result away.
        job = self.make_job(deadline=timezone.now() - timedelta(minutes=1))
        provider = MagicMock()
        provider.poll.return_value = _outcome(JobStatus.COMPLETED)

        summary, _ = self.collect(provider)

        provider.cancel.assert_not_called()
        job.refresh_from_db()
        self.assertEqual(job.status, JobStatus.COMPLETED)
        self.assertEqual(summary.cancelled, 0)

    def test_a_failing_cancel_still_moves_the_row(self):
        # Cancelling costs money, not correctness. The row has to leave
        # flight either way, or it is swept forever.
        job = self.make_job(
            deadline=timezone.now() - timedelta(minutes=1),
            retry_count=2,
        )
        provider = MagicMock()
        provider.poll.return_value = _outcome(JobStatus.IN_PROGRESS)
        provider.cancel.side_effect = RuntimeError("network down")

        self.collect(provider)

        job.refresh_from_db()
        self.assertEqual(job.status, JobStatus.CANCELLED)

    def test_a_job_with_no_deadline_is_never_overdue(self):
        job = self.make_job(deadline=None)
        provider = MagicMock()
        provider.poll.return_value = _outcome(JobStatus.IN_PROGRESS)

        self.collect(provider)

        provider.cancel.assert_not_called()
        job.refresh_from_db()
        self.assertEqual(job.status, JobStatus.IN_PROGRESS)


class TestConcurrentWriters(CollectTestCase):
    """A poll holds no lock, so writes are guarded on what they saw."""

    def test_a_row_moved_mid_poll_is_left_to_whoever_moved_it(self):
        job = self.make_job()

        def move_it_then_answer(polled):
            ExternalJob.objects.filter(pk=polled.pk).update(
                status=JobStatus.CANCELLED
            )
            return _outcome(JobStatus.COMPLETED)

        provider = MagicMock()
        provider.poll.side_effect = move_it_then_answer

        self.collect(provider)

        job.refresh_from_db()
        self.assertEqual(job.status, JobStatus.CANCELLED)
        self.assertIsNone(job.completed_at)


class TestEnsureJobs(CollectTestCase):
    """``ensure_jobs`` is how a pipeline step says "this has to happen"."""

    def test_creates_the_stage_pending(self):
        scan = ScanFactory()
        created = jobs.ensure_jobs(
            scan,
            JobStage.DETECT,
            JobEngine.BLACKLETTER,
            JobProvider.RUNPOD,
            input_key="processing/1/a/1/1/bitonal.pdf",
            manifest={"models": ["small"]},
        )

        self.assertEqual(len(created), 1)
        job = created[0]
        self.assertEqual(job.status, JobStatus.PENDING)
        self.assertEqual(job.run, 1)
        self.assertEqual(job.input_manifest, {"models": ["small"]})

    def test_is_idempotent_across_passes(self):
        # The pipeline is re-entered from the top on every resume, so
        # asking a second time must find the same rows, not make more.
        scan = ScanFactory()
        args = (
            scan,
            JobStage.DETECT,
            JobEngine.BLACKLETTER,
            JobProvider.RUNPOD,
        )
        first = jobs.ensure_jobs(*args, input_key="k")
        second = jobs.ensure_jobs(*args, input_key="k")

        self.assertEqual([j.pk for j in first], [j.pk for j in second])
        self.assertEqual(ExternalJob.objects.count(), 1)

    def test_finds_a_finished_stage_rather_than_rerunning_it(self):
        scan = ScanFactory()
        args = (
            scan,
            JobStage.DETECT,
            JobEngine.BLACKLETTER,
            JobProvider.RUNPOD,
        )
        done = jobs.ensure_jobs(*args, input_key="k")[0]
        ExternalJob.objects.filter(pk=done.pk).update(
            status=JobStatus.CONSUMED
        )

        found = jobs.ensure_jobs(*args, input_key="k")

        self.assertEqual([j.pk for j in found], [done.pk])

    def test_fans_out_into_shards(self):
        scan = ScanFactory()
        created = jobs.ensure_jobs(
            scan,
            JobStage.DETECT,
            JobEngine.BLACKLETTER,
            JobProvider.RUNPOD,
            input_key="k",
            shard_count=4,
        )

        self.assertEqual([j.shard_index for j in created], [0, 1, 2, 3])
        self.assertEqual({j.shard_count for j in created}, {4})

    def test_a_second_engine_is_its_own_stage_run(self):
        # A stage holds several engines reading the same document, and
        # neither may hide the other's rows from the barrier.
        scan = ScanFactory()
        jobs.ensure_jobs(
            scan,
            JobStage.DETECT,
            JobEngine.BLACKLETTER,
            JobProvider.RUNPOD,
            input_key="k",
        )
        other = jobs.ensure_jobs(
            scan,
            JobStage.DETECT,
            JobEngine.DOTS_MOCR,
            JobProvider.RUNPOD,
            input_key="k",
        )

        self.assertEqual(other[0].run, 1)
        self.assertEqual(ExternalJob.objects.count(), 2)


@override_settings(
    RUNPOD_REQUEST_TIMEOUT=1800,
    DAEMON_JOB_SECONDS_PER_PAGE=1.0,
)
class TestSubmitPending(CollectTestCase):
    """``submit_pending`` hands the batch over without waiting on it."""

    def make_pending(self, **kwargs):
        """Create a submittable pending job.

        :param kwargs: Overrides for the factory.
        :returns: The job.
        :rtype: ExternalJob
        """
        defaults = {
            "status": JobStatus.PENDING,
            "input_key": "processing/1/a/1/1/bitonal.pdf",
            "input_manifest": {"models": ["small"]},
        }
        return ExternalJobFactory(**{**defaults, **kwargs})

    def submit(self, provider=None, **kwargs):
        """Run one submit sweep against a stubbed provider.

        :param provider: Stub provider.
        :param kwargs: Passed to ``submit_pending``.
        :returns: ``(summary, provider)``.
        :rtype: tuple
        """
        provider = provider or MagicMock()
        provider.submit.side_effect = lambda job, payload: SubmitReceipt(
            external_id=f"job-{job.pk}",
            result_key=job.result_key,
            submitted_at=timezone.now(),
        )
        with (
            patch("scanning.jobs.get_provider", return_value=provider),
            patch(
                "scanning.runpod_client.presign_input_get",
                return_value="https://signed/get",
            ),
        ):
            return jobs.submit_pending(**kwargs), provider

    def test_submits_the_whole_batch_in_one_tick(self):
        for _ in range(5):
            self.make_pending()

        summary, provider = self.submit()

        self.assertEqual(summary.submitted, 5)
        self.assertEqual(provider.submit.call_count, 5)

    def test_records_what_a_later_poll_will_need(self):
        job = self.make_pending()

        self.submit()

        job.refresh_from_db()
        self.assertEqual(job.status, JobStatus.SUBMITTED)
        self.assertEqual(job.external_id, f"job-{job.pk}")
        self.assertIsNotNone(job.submitted_at)
        self.assertIsNotNone(job.deadline)
        self.assertTrue(job.result_key)

    def test_each_job_gets_its_own_result_key(self):
        # Two shards presigning one object would leave whichever wrote
        # last as the only surviving output.
        scan = ScanFactory()
        jobs.ensure_jobs(
            scan,
            JobStage.DETECT,
            JobEngine.BLACKLETTER,
            JobProvider.RUNPOD,
            input_key="k",
            shard_count=3,
        )

        self.submit()

        keys = set(ExternalJob.objects.values_list("result_key", flat=True))
        self.assertEqual(len(keys), 3)

    def test_the_key_is_scoped_to_the_attempt(self):
        job = self.make_pending(run=2, shard_index=1, shard_count=2, attempt=3)

        self.submit()

        job.refresh_from_db()
        self.assertIn("r2-s1-a3", job.result_key)

    def test_the_input_is_presigned_at_submit_not_at_creation(self):
        # A job can sit pending behind a narrow worker pool for a long
        # time; a signature minted when the row was written could have
        # expired by the time it goes out.
        job = self.make_pending()

        _, provider = self.submit()

        payload = provider.submit.call_args.args[1]
        self.assertEqual(payload["pdf_url"], "https://signed/get")
        self.assertEqual(payload["models"], ["small"])
        self.assertEqual(job.input_key, "processing/1/a/1/1/bitonal.pdf")

    def test_the_deadline_grows_with_the_pages(self):
        small = self.make_pending(scan=ScanFactory(page_count=10))
        big = self.make_pending(scan=ScanFactory(page_count=1400))

        self.submit()

        small.refresh_from_db()
        big.refresh_from_db()
        self.assertLess(small.deadline, big.deadline)

    def test_a_paused_endpoint_defers_without_spending_the_budget(self):
        # Not a failure of the work: the endpoint is scaled to zero and
        # the next tick should simply try again.
        from scanning.runpod_client import RunpodTransientError

        job = self.make_pending()
        provider = MagicMock()
        provider.submit.side_effect = RunpodTransientError("paused")
        with (
            patch("scanning.jobs.get_provider", return_value=provider),
            patch(
                "scanning.runpod_client.presign_input_get",
                return_value="https://signed/get",
            ),
        ):
            summary = jobs.submit_pending()

        job.refresh_from_db()
        self.assertEqual(summary.deferred, 1)
        self.assertEqual(job.status, JobStatus.PENDING)
        self.assertEqual(job.retry_count, 0)

    def test_a_broken_submit_fails_the_job(self):
        job = self.make_pending()
        provider = MagicMock()
        provider.submit.side_effect = RuntimeError("bad request")
        with (
            patch("scanning.jobs.get_provider", return_value=provider),
            patch(
                "scanning.runpod_client.presign_input_get",
                return_value="https://signed/get",
            ),
        ):
            summary = jobs.submit_pending()

        job.refresh_from_db()
        self.assertEqual(summary.failed, 1)
        self.assertEqual(job.status, JobStatus.FAILED)
        self.assertEqual(job.error_code, "SUBMIT_FAILED")

    def test_a_job_with_no_input_fails_rather_than_being_sent(self):
        job = self.make_pending(input_key="")

        summary, provider = self.submit()

        job.refresh_from_db()
        self.assertEqual(summary.failed, 1)
        self.assertEqual(job.status, JobStatus.FAILED)
        provider.submit.assert_not_called()

    def test_one_broken_job_does_not_stop_the_batch(self):
        self.make_pending(input_key="")
        good = self.make_pending()

        summary, _ = self.submit()

        good.refresh_from_db()
        self.assertEqual(summary.failed, 1)
        self.assertEqual(summary.submitted, 1)
        self.assertEqual(good.status, JobStatus.SUBMITTED)

    def test_limit_caps_the_tick(self):
        for _ in range(4):
            self.make_pending()

        summary, _ = self.submit(limit=2)

        self.assertEqual(summary.submitted, 2)

    def test_only_pending_jobs_go_out(self):
        self.make_pending(status=JobStatus.SUBMITTED)
        self.make_pending(status=JobStatus.CONSUMED)

        summary, provider = self.submit()

        self.assertEqual(summary.submitted, 0)
        provider.submit.assert_not_called()
