"""Tests for ``scanning.jobs`` -- the collect half of the batch cycle.

Providers are stubbed: what matters here is what the sweep writes to a
row given an answer, not how any provider obtains one.
"""

from datetime import timedelta
from unittest.mock import MagicMock, patch

from django.test import TestCase, override_settings
from django.utils import timezone

from scanning import jobs
from scanning.factories import ExternalJobFactory
from scanning.models import ExternalJob, JobStatus
from scanning.providers import PollOutcome

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
