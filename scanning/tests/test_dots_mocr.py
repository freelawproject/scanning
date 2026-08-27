"""Tests for the dots.mocr stage: rows, waves, polls and deadlines.

No HTTP and no S3 -- ``runpod_client`` and the S3 helpers are patched.
Under test is what separates an asynchronous provider from doctor:

- submitting stores a job id and leaves the row in flight
- a poll, not a response, is what finishes a job
- queue time is free, so only the crossing into ``IN_PROGRESS`` starts
  the run budget
- a job nothing will read gets cancelled, because it bills while it runs
- a shard that lost some pages is still a success
"""

from datetime import timedelta
from unittest.mock import patch

from django.test import override_settings
from django.utils import timezone

from scanning import dots_mocr, jobs, runpod_client
from scanning.factories import ScanFactory
from scanning.models import (
    ExternalJob,
    JobEngine,
    JobProvider,
    JobStage,
    JobStatus,
)
from scanning.tests.test_jobs import make_manifest
from scanning.tests.test_views import ScanningTestCase

DOTS = {
    "RUNPOD_ENABLED": True,
    "RUNPOD_API_KEY": "key-1",
    "RUNPOD_PRESIGNED_TTL": 3600,
    "RUNPOD_REQUEST_TIMEOUT": 600,
    "DOTS_MOCR_ENABLED": True,
    "RUNPOD_DOTSMOCR_ENDPOINT_ID": "ep-dots",
    "DOTS_MOCR_MAX_CONCURRENCY": 4,
    "DOTS_MOCR_MAX_ATTEMPTS": 3,
    "DOTS_MOCR_SECONDS_PER_PAGE": 4.0,
    # Doctor off, so a tick exercises the RunPod wave alone.
    "DOCTOR_ENABLED": False,
}


def analyze_jobs(scan):
    """Return a scan's dots.mocr rows in shard order.

    :param scan: The scan to look up.
    :returns: Its rows.
    :rtype: list[ExternalJob]
    """
    return list(
        ExternalJob.objects.filter(
            scan=scan,
            stage=JobStage.ANALYZE,
            engine=JobEngine.DOTS_MOCR,
        ).order_by("shard_index")
    )


def outcome(status, **kwargs):
    """Build a :class:`runpod_client.PollOutcome`.

    :param status: The normalized status, or ``None`` for "no answer".
    :param kwargs: Any other field.
    :returns: The outcome.
    :rtype: runpod_client.PollOutcome
    """
    kwargs.setdefault("provider_status", str(status or ""))
    return runpod_client.PollOutcome(status=status, **kwargs)


# ── switches ────────────────────────────────────────────────────────
class TestEnabled(ScanningTestCase):
    """Both the stage switch and the RunPod credentials are required."""

    @override_settings(**DOTS)
    def test_on_with_everything_set(self):
        self.assertTrue(dots_mocr.enabled())

    @override_settings(**{**DOTS, "DOTS_MOCR_ENABLED": False})
    def test_off_without_the_stage_switch(self):
        # Off by default: every run costs GPU money.
        self.assertFalse(dots_mocr.enabled())

    @override_settings(**{**DOTS, "RUNPOD_DOTSMOCR_ENDPOINT_ID": ""})
    def test_off_without_this_engine_s_endpoint(self):
        self.assertFalse(dots_mocr.enabled())

    @override_settings(**{**DOTS, "RUNPOD_ENABLED": False})
    def test_off_when_runpod_is_off_for_the_environment(self):
        self.assertFalse(dots_mocr.enabled())


# ── creating the work ───────────────────────────────────────────────
class TestEnsureAnalyzeJobs(ScanningTestCase):
    """One row per shard of the original, at ANALYZE/DOTS_MOCR/RUNPOD."""

    def test_one_row_per_shard_addressing_its_own_pages(self):
        scan = ScanFactory()
        created = dots_mocr.ensure_analyze_jobs(
            scan, make_manifest(shard_count=3, pages_per_shard=10)
        )

        self.assertEqual(len(created), 3)
        for index, job in enumerate(created):
            self.assertEqual(job.stage, JobStage.ANALYZE)
            self.assertEqual(job.engine, JobEngine.DOTS_MOCR)
            self.assertEqual(job.provider, JobProvider.RUNPOD)
            self.assertEqual(job.status, JobStatus.PENDING)
            self.assertEqual(job.shard_index, index)
            self.assertEqual(job.shard_count, 3)
            self.assertTrue(job.input_key.endswith(f"{index + 1:04d}.pdf"))
            self.assertEqual(job.input_manifest["from_page"], index * 10)

    def test_it_reads_the_original_shards_not_the_bitonal_ones(self):
        # dots.mocr wants the greyscale scan its layout model was
        # trained on. Both stages therefore fan out over the one shard
        # set cut from the original.
        scan = ScanFactory()
        manifest = make_manifest(shard_count=2)
        convert = jobs.ensure_convert_jobs(scan, manifest)
        analyze = dots_mocr.ensure_analyze_jobs(scan, manifest)

        self.assertEqual(
            [job.input_key for job in convert],
            [job.input_key for job in analyze],
        )

    def test_a_second_call_reuses_the_run(self):
        # Which is what makes a second press of the button free rather
        # than a second run over shards already read.
        scan = ScanFactory()
        manifest = make_manifest(shard_count=2)
        first = dots_mocr.ensure_analyze_jobs(scan, manifest)
        second = dots_mocr.ensure_analyze_jobs(scan, manifest)

        self.assertEqual([job.pk for job in first], [job.pk for job in second])
        self.assertEqual(len(analyze_jobs(scan)), 2)

    def test_a_dead_row_forces_a_fresh_run(self):
        scan = ScanFactory()
        manifest = make_manifest(shard_count=2)
        first = dots_mocr.ensure_analyze_jobs(scan, manifest)
        ExternalJob.objects.filter(pk=first[0].pk).update(
            status=JobStatus.CANCELLED
        )

        second = dots_mocr.ensure_analyze_jobs(scan, manifest)

        self.assertEqual(second[0].run, 2)
        self.assertEqual(len(analyze_jobs(scan)), 4)

    def test_a_re_cut_volume_forces_a_fresh_run(self):
        # Shards are named by position, so the same count over different
        # pages gives identical keys. Only the stored ranges catch it.
        scan = ScanFactory()
        dots_mocr.ensure_analyze_jobs(
            scan, make_manifest(shard_count=2, pages_per_shard=10)
        )
        second = dots_mocr.ensure_analyze_jobs(
            scan, make_manifest(shard_count=2, pages_per_shard=25)
        )

        self.assertEqual(second[0].run, 2)
        self.assertEqual(second[0].input_manifest["page_count"], 25)

    def test_the_convert_run_is_not_disturbed(self):
        scan = ScanFactory()
        manifest = make_manifest(shard_count=2)
        convert = jobs.ensure_convert_jobs(scan, manifest)
        dots_mocr.ensure_analyze_jobs(scan, manifest)
        dots_mocr.ensure_analyze_jobs(scan, manifest)

        # Runs are scoped per engine, so re-running one must not
        # renumber another's rows.
        for job in convert:
            job.refresh_from_db()
            self.assertEqual(job.run, 1)
            self.assertEqual(job.status, JobStatus.PENDING)


class TestBuildPayload(ScanningTestCase):
    """What one shard is asked for."""

    def _payload(self, **manifest_extra):
        scan = ScanFactory()
        job = dots_mocr.ensure_analyze_jobs(scan, make_manifest(2))[0]
        if manifest_extra:
            job.input_manifest = {**job.input_manifest, **manifest_extra}
        job.result_key = "jobs/analyze/dots_mocr/r1-s0-a1.json"
        return dots_mocr.build_payload(
            job, "https://s3/in?get", "https://s3/out?put"
        ), job

    def test_it_asks_for_layout_and_text(self):
        payload, job = self._payload()
        self.assertEqual(payload["action"], "parse")
        self.assertEqual(payload["scan_pk"], job.scan_id)
        self.assertEqual(payload["pdf_url"], "https://s3/in?get")
        self.assertEqual(payload["result_url"], "https://s3/out?put")
        self.assertEqual(payload["result_key"], job.result_key)
        # Issue #149 needs the cells to find the running head and the
        # text to read the number out of it.
        self.assertEqual(payload["prompt_mode"], "prompt_layout_all_en")

    def test_the_render_resolution_matches_the_rest_of_the_corpus(self):
        payload, _ = self._payload()
        self.assertEqual(payload["dpi"], 200)

    def test_a_row_may_override_its_own_tuning(self):
        # For a one-off experiment, without a deploy.
        payload, _ = self._payload(dpi=400, prompt_mode="prompt_ocr")
        self.assertEqual(payload["dpi"], 400)
        self.assertEqual(payload["prompt_mode"], "prompt_ocr")

    def test_shard_geometry_is_not_mistaken_for_tuning(self):
        payload, _ = self._payload()
        self.assertNotIn("from_page", payload)
        self.assertNotIn("page_count", payload)


# ── the submit wave ─────────────────────────────────────────────────
@override_settings(**DOTS)
class TestSubmitWave(ScanningTestCase):
    """Submitting queues work; it does not finish it."""

    def setUp(self):
        super().setUp()
        self.scan = ScanFactory()
        self.jobs = dots_mocr.ensure_analyze_jobs(
            self.scan, make_manifest(shard_count=3)
        )
        self.presign = patch.multiple(
            "scanning.s3_sync",
            s3_active=lambda: True,
            presign_get=lambda key, ttl: f"https://s3/{key}?get",
            presign_put=lambda key, ct, ttl: f"https://s3/{key}?put",
        )
        self.presign.start()
        self.addCleanup(self.presign.stop)

    def _tick(self, side_effect=None, return_value="job-1", **kwargs):
        """Run one submit tick with ``submit_job`` patched."""
        with patch(
            "scanning.runpod_client.submit_job",
            side_effect=side_effect,
            return_value=return_value,
        ) as submit:
            summary = jobs.submit_pending(**kwargs)
        return summary, submit

    def test_a_successful_wave_leaves_every_row_in_flight(self):
        # The row is SUBMITTED, not COMPLETED: only a poll can finish it.
        summary, submit = self._tick()

        self.assertEqual(summary.submitted, 3)
        self.assertEqual(submit.call_count, 3)
        for job in analyze_jobs(self.scan):
            self.assertEqual(job.status, JobStatus.SUBMITTED)
            self.assertEqual(job.external_id, "job-1")
            self.assertTrue(job.result_key.endswith(".json"))

    def test_a_claimed_row_keeps_the_queue_ceiling(self):
        # It has been accepted but may not have started, and queue time
        # is free. Only the IN_PROGRESS crossing starts the run budget.
        self._tick()
        job = analyze_jobs(self.scan)[0]
        expected = jobs.queue_deadline(job.submitted_at)
        self.assertAlmostEqual(
            job.deadline, expected, delta=timedelta(seconds=2)
        )

    def test_the_result_key_is_scoped_to_run_shard_and_attempt(self):
        self._tick()
        keys = {job.result_key for job in analyze_jobs(self.scan)}
        self.assertEqual(len(keys), 3)
        for job in analyze_jobs(self.scan):
            self.assertIn(
                f"r{job.run}-s{job.shard_index}-a{job.attempt}",
                job.result_key,
            )

    def test_a_busy_endpoint_costs_no_attempt(self):
        # Nothing is wrong with the job, so its retry budget is intact.
        summary, _ = self._tick(
            side_effect=runpod_client.RunpodEndpointBusy(
                "paused", error_code="ENDPOINT_PAUSED"
            )
        )

        self.assertEqual(summary.deferred, 3)
        self.assertEqual(summary.retried, 0)
        for job in analyze_jobs(self.scan):
            self.assertEqual(job.status, JobStatus.PENDING)
            self.assertEqual(job.attempt, 1)
            self.assertEqual(job.retry_count, 0)
            self.assertEqual(job.error_code, "ENDPOINT_PAUSED")

    def test_a_lost_answer_stays_in_flight(self):
        # RunPod may hold the job and its worker may still PUT, so the
        # confirm pass judges it by the result key.
        summary, _ = self._tick(
            side_effect=runpod_client.RunpodTransientError(
                "no answer",
                error_code=runpod_client.UNANSWERED_ERROR_CODE,
            )
        )

        self.assertEqual(summary.unanswered, 3)
        for job in analyze_jobs(self.scan):
            self.assertEqual(job.status, JobStatus.SUBMITTED)
            self.assertEqual(job.external_id, "")
            self.assertTrue(job.result_key)

    def test_an_answered_refusal_retries_at_once(self):
        summary, _ = self._tick(
            side_effect=runpod_client.RunpodTransientError(
                "unavailable", error_code="BAD_GATEWAY"
            )
        )

        self.assertEqual(summary.retried, 3)
        for job in analyze_jobs(self.scan):
            self.assertEqual(job.status, JobStatus.PENDING)
            self.assertEqual(job.attempt, 2)
            # A fresh attempt mints a fresh key.
            self.assertEqual(job.result_key, "")

    def test_a_terminal_rejection_fails_the_row(self):
        summary, _ = self._tick(
            side_effect=runpod_client.RunpodError(
                "bad key", error_code="SUBMIT_REJECTED"
            )
        )

        self.assertEqual(summary.failed, 3)
        for job in analyze_jobs(self.scan):
            self.assertEqual(job.status, JobStatus.FAILED)

    def test_the_wave_is_capped_by_this_engine_s_setting(self):
        with override_settings(DOTS_MOCR_MAX_CONCURRENCY=2):
            summary, submit = self._tick()
        self.assertEqual(submit.call_count, 2)
        self.assertEqual(summary.submitted, 2)

    def test_rows_in_flight_count_against_the_cap(self):
        # An unanswered request is still a job RunPod may be running.
        ExternalJob.objects.filter(pk=self.jobs[0].pk).update(
            status=JobStatus.IN_PROGRESS
        )
        with override_settings(DOTS_MOCR_MAX_CONCURRENCY=2):
            _, submit = self._tick()
        self.assertEqual(submit.call_count, 1)

    def test_the_stage_switch_stops_the_wave(self):
        with override_settings(DOTS_MOCR_ENABLED=False):
            _, submit = self._tick()
        submit.assert_not_called()

    def test_nothing_is_submitted_without_s3(self):
        # The worker fetches its shard through a presigned GET, so
        # without the upload every request would 404 on its input.
        with patch("scanning.s3_sync.s3_active", return_value=False):
            with self.assertLogs("scanning.jobs", level="ERROR"):
                _, submit = self._tick()
        submit.assert_not_called()


@override_settings(**DOTS)
class TestWavesDoNotCompete(ScanningTestCase):
    """Each provider counts its own rows against its own cap."""

    def test_a_saturated_endpoint_does_not_starve_doctor(self):
        scan = ScanFactory()
        manifest = make_manifest(shard_count=2)
        dots_mocr.ensure_analyze_jobs(scan, manifest)
        jobs.ensure_convert_jobs(scan, manifest)

        with (
            patch.multiple(
                "scanning.s3_sync",
                s3_active=lambda: True,
                presign_get=lambda key, ttl: "https://s3/in?get",
                presign_put=lambda key, ct, ttl: "https://s3/out?put",
            ),
            override_settings(
                DOCTOR_ENABLED=True,
                DOCTOR_HOST="http://doctor:5050",
                DOCTOR_MAX_CONCURRENCY=2,
                DOTS_MOCR_MAX_CONCURRENCY=0,
            ),
            patch(
                "scanning.doctor_client.convert_bitonal",
                return_value={"pages": 10},
            ) as convert,
            patch("scanning.runpod_client.submit_job") as submit,
        ):
            summary = jobs.submit_pending()

        submit.assert_not_called()
        self.assertEqual(convert.call_count, 2)
        self.assertEqual(summary.submitted, 2)


# ── the confirm pass ────────────────────────────────────────────────
@override_settings(**DOTS)
class TestSweepRunpodJobs(ScanningTestCase):
    """A poll, not a response, is what finishes a RunPod job."""

    def setUp(self):
        super().setUp()
        self.scan = ScanFactory()
        self.job = dots_mocr.ensure_analyze_jobs(
            self.scan, make_manifest(shard_count=1, pages_per_shard=50)
        )[0]
        submitted = timezone.now() - timedelta(seconds=30)
        ExternalJob.objects.filter(pk=self.job.pk).update(
            status=JobStatus.SUBMITTED,
            external_id="job-1",
            result_key="jobs/analyze/dots_mocr/r1-s0-a1.json",
            submitted_at=submitted,
            deadline=jobs.queue_deadline(submitted),
        )
        self.job.refresh_from_db()

    def _sweep(self, poll_outcome):
        with patch(
            "scanning.runpod_client.poll_once", return_value=poll_outcome
        ) as poll:
            summary = jobs.sweep_jobs()
        self.job.refresh_from_db()
        return summary, poll

    def test_completed_completes_the_row_and_keeps_the_summary(self):
        summary, _ = self._sweep(
            outcome(
                JobStatus.COMPLETED,
                output={"page_count": 50, "duration_ms": 200_000},
            )
        )

        self.assertEqual(summary.completed, 1)
        self.assertEqual(self.job.status, JobStatus.COMPLETED)
        self.assertEqual(self.job.provider_meta["output"]["page_count"], 50)
        self.assertEqual(self.job.provider_meta["confirmed_by"], "response")

    def test_the_run_stops_at_completed(self):
        # Issue #190 applies nothing. CONSUMED is the follow-up's write.
        self._sweep(outcome(JobStatus.COMPLETED, output={"page_count": 50}))
        self.assertNotEqual(self.job.status, JobStatus.CONSUMED)

    def test_in_queue_is_recorded_without_starting_the_run_budget(self):
        before = self.job.deadline
        summary, _ = self._sweep(outcome(JobStatus.IN_QUEUE))

        self.assertEqual(summary.pending, 1)
        self.assertEqual(self.job.status, JobStatus.IN_QUEUE)
        self.assertEqual(self.job.deadline, before)

    def test_the_crossing_into_in_progress_starts_the_run_budget(self):
        summary, _ = self._sweep(outcome(JobStatus.IN_PROGRESS))

        self.assertEqual(summary.pending, 1)
        self.assertEqual(self.job.status, JobStatus.IN_PROGRESS)
        # 600s base + 50 pages x 4.0s.
        expected = timezone.now() + timedelta(seconds=600 + 200)
        self.assertAlmostEqual(
            self.job.deadline, expected, delta=timedelta(seconds=5)
        )

    def test_only_the_crossing_moves_the_deadline(self):
        # Otherwise every tick pushes it out and a wedged job never ends.
        self._sweep(outcome(JobStatus.IN_PROGRESS))
        stamped = self.job.deadline
        self._sweep(outcome(JobStatus.IN_PROGRESS))
        self.assertEqual(self.job.deadline, stamped)

    def test_no_answer_leaves_the_row_alone(self):
        before = self.job.status
        summary, _ = self._sweep(outcome(None, error_message="502"))

        self.assertEqual(summary.pending, 1)
        self.assertEqual(self.job.status, before)
        self.assertIsNotNone(self.job.last_polled_at)

    def test_a_retriable_failure_retries(self):
        summary, _ = self._sweep(
            outcome(
                JobStatus.FAILED,
                error_code="NO_GPU",
                error_message="no gpu",
                retriable=True,
            )
        )

        self.assertEqual(summary.retried, 1)
        self.assertEqual(self.job.status, JobStatus.PENDING)
        self.assertEqual(self.job.attempt, 2)

    def test_a_terminal_failure_fails(self):
        summary, _ = self._sweep(
            outcome(
                JobStatus.FAILED,
                error_code="BAD_INPUT",
                error_message="bad pdf",
                retriable=False,
            )
        )

        self.assertEqual(summary.failed, 1)
        self.assertEqual(self.job.status, JobStatus.FAILED)
        self.assertEqual(self.job.attempt, 1)

    def test_an_expired_job_is_retriable(self):
        # The inputs are still on S3; only the job record is gone.
        summary, _ = self._sweep(
            outcome(
                JobStatus.EXPIRED,
                error_code="JOB_NOT_FOUND",
                error_message="gone",
                retriable=True,
            )
        )
        self.assertEqual(summary.retried, 1)
        self.assertEqual(self.job.status, JobStatus.PENDING)

    def test_a_job_past_its_run_budget_is_written_off(self):
        ExternalJob.objects.filter(pk=self.job.pk).update(
            status=JobStatus.IN_PROGRESS,
            deadline=timezone.now() - timedelta(seconds=1),
        )
        summary, _ = self._sweep(outcome(JobStatus.IN_PROGRESS))

        self.assertEqual(summary.retried, 1)
        self.assertEqual(summary.pending, 0)
        self.assertEqual(self.job.status, JobStatus.PENDING)
        self.assertEqual(self.job.error_code, "DEADLINE_EXCEEDED")

    def test_a_job_is_polled_before_its_deadline_is_judged(self):
        # One tick either way is free; discarding a job that finished
        # just inside its budget is not.
        ExternalJob.objects.filter(pk=self.job.pk).update(
            deadline=timezone.now() - timedelta(seconds=1)
        )
        summary, _ = self._sweep(
            outcome(JobStatus.COMPLETED, output={"page_count": 50})
        )
        self.assertEqual(summary.completed, 1)
        self.assertEqual(self.job.status, JobStatus.COMPLETED)

    def test_a_row_with_no_job_id_is_judged_by_its_result_key(self):
        # Its submit never answered, so there is nothing to poll.
        ExternalJob.objects.filter(pk=self.job.pk).update(external_id="")
        with (
            patch("scanning.runpod_client.poll_once") as poll,
            patch("scanning.s3_sync.object_exists", return_value=True),
        ):
            summary = jobs.sweep_jobs()

        poll.assert_not_called()
        self.job.refresh_from_db()
        self.assertEqual(summary.completed, 1)
        self.assertEqual(self.job.status, JobStatus.COMPLETED)
        self.assertEqual(self.job.provider_meta["confirmed_by"], "s3_head")


# ── cancelling ──────────────────────────────────────────────────────
@override_settings(**DOTS)
class TestCancelStopsBilling(ScanningTestCase):
    """A GPU job nothing will read must be told to stop."""

    def setUp(self):
        super().setUp()
        self.scan = ScanFactory()
        self.job = dots_mocr.ensure_analyze_jobs(
            self.scan, make_manifest(shard_count=1)
        )[0]
        ExternalJob.objects.filter(pk=self.job.pk).update(
            status=JobStatus.IN_PROGRESS, external_id="job-1"
        )
        self.job.refresh_from_db()

    def test_abandoning_an_open_row_cancels_its_job(self):
        with patch("scanning.runpod_client.cancel_job") as cancel:
            jobs.abandon_open(self.scan, "stopped", stage=JobStage.ANALYZE)

        cancel.assert_called_once()
        self.assertEqual(cancel.call_args[0][2], "job-1")

    def test_failing_a_row_cancels_its_job(self):
        with patch("scanning.runpod_client.cancel_job") as cancel:
            jobs._fail(self.job, "BAD_INPUT", "bad pdf")
        cancel.assert_called_once()

    def test_retrying_a_row_cancels_the_previous_attempt(self):
        # A deadline write-off is exactly the case where the old attempt
        # may still be running, and two at once is paid twice.
        with patch("scanning.runpod_client.cancel_job") as cancel:
            jobs._retry_or_fail(
                self.job, "DEADLINE_EXCEEDED", "too slow", timezone.now()
            )
        cancel.assert_called_once()

    def test_a_doctor_row_has_nothing_to_cancel(self):
        convert = jobs.ensure_convert_jobs(
            self.scan, make_manifest(shard_count=1)
        )[0]
        ExternalJob.objects.filter(pk=convert.pk).update(
            status=JobStatus.SUBMITTED
        )
        convert.refresh_from_db()
        with patch("scanning.runpod_client.cancel_job") as cancel:
            jobs._fail(convert, "CONVERSION_FAILED", "bad pdf")
        cancel.assert_not_called()

    def test_a_cancel_failure_does_not_stop_the_sweep(self):
        # cancel_job swallows its own errors, and this is the guarantee
        # the sweep relies on.
        with patch(
            "scanning.runpod_client.requests.post",
            side_effect=RuntimeError("network"),
        ):
            jobs.abandon_open(self.scan, "stopped", stage=JobStage.ANALYZE)
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, JobStatus.CANCELLED)


@override_settings(**DOTS)
class TestAbandonIsScopedByStage(ScanningTestCase):
    """A re-queue must not throw away a finished dots.mocr run."""

    def test_a_convert_restart_spares_a_completed_analyze_run(self):
        scan = ScanFactory()
        manifest = make_manifest(shard_count=1)
        convert = jobs.ensure_convert_jobs(scan, manifest)[0]
        analyze = dots_mocr.ensure_analyze_jobs(scan, manifest)[0]
        ExternalJob.objects.filter(pk=analyze.pk).update(
            status=JobStatus.COMPLETED
        )

        jobs.abandon_open(scan, "Re-queued", stage=JobStage.CONVERT)

        convert.refresh_from_db()
        analyze.refresh_from_db()
        self.assertEqual(convert.status, JobStatus.CANCELLED)
        # COMPLETED counts as open, so an unscoped call would have
        # cancelled this and made the next press pay RunPod again.
        self.assertEqual(analyze.status, JobStatus.COMPLETED)

    def test_an_unscoped_call_still_cancels_everything(self):
        scan = ScanFactory()
        manifest = make_manifest(shard_count=1)
        jobs.ensure_convert_jobs(scan, manifest)
        dots_mocr.ensure_analyze_jobs(scan, manifest)

        self.assertEqual(jobs.abandon_open(scan, "everything"), 2)


# ── partial results ─────────────────────────────────────────────────
@override_settings(**DOTS)
class TestFailedPages(ScanningTestCase):
    """A shard that lost some pages is a success, loudly."""

    def setUp(self):
        super().setUp()
        self.scan = ScanFactory()
        self.job = dots_mocr.ensure_analyze_jobs(
            self.scan, make_manifest(shard_count=3, pages_per_shard=10)
        )[1]
        ExternalJob.objects.filter(pk=self.job.pk).update(
            status=JobStatus.SUBMITTED
        )
        self.job.refresh_from_db()

    def test_the_shard_still_completes(self):
        # Issue #149 reads a missing page as detected=None and
        # interpolates, so re-running 9 good pages to recover 1 is poor
        # value.
        with self.assertLogs("scanning.jobs", level="WARNING"):
            jobs._complete(
                self.job,
                {"page_count": 10, "failed_pages": [2]},
                timezone.now(),
            )
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, JobStatus.COMPLETED)

    def test_the_warning_names_volume_pages_not_shard_pages(self):
        # The worker counts from zero inside the shard it was given;
        # shard 2 page 2 is volume page 13.
        with self.assertLogs("scanning.jobs", level="WARNING") as logs:
            jobs._complete(
                self.job,
                {"page_count": 10, "failed_pages": [2, 7]},
                timezone.now(),
            )
        line = "\n".join(logs.output)
        self.assertIn("[13, 18]", line)

    def test_the_page_numbers_survive_on_the_row(self):
        jobs._complete(
            self.job,
            {"page_count": 10, "failed_pages": [2]},
            timezone.now(),
        )
        self.job.refresh_from_db()
        self.assertEqual(self.job.provider_meta["output"]["failed_pages"], [2])


# ── run completion ──────────────────────────────────────────────────
@override_settings(**DOTS)
class TestRunCompletionLog(ScanningTestCase):
    """The stage total is logged once, by the last shard to land."""

    def setUp(self):
        super().setUp()
        self.scan = ScanFactory()
        self.jobs = dots_mocr.ensure_analyze_jobs(
            self.scan, make_manifest(shard_count=2, pages_per_shard=10)
        )
        ExternalJob.objects.filter(
            pk__in=[job.pk for job in self.jobs]
        ).update(status=JobStatus.SUBMITTED)
        for job in self.jobs:
            job.refresh_from_db()

    def test_no_total_while_a_shard_is_still_open(self):
        with self.assertLogs("scanning.jobs", level="INFO") as logs:
            jobs._complete(self.jobs[0], {"page_count": 10}, timezone.now())
        self.assertNotIn("run 1 done", "\n".join(logs.output))

    def test_the_last_shard_logs_the_total(self):
        jobs._complete(self.jobs[0], {"page_count": 10}, timezone.now())
        with self.assertLogs("scanning.jobs", level="INFO") as logs:
            jobs._complete(self.jobs[1], {"page_count": 10}, timezone.now())

        line = "\n".join(logs.output)
        self.assertIn("analyze/dots_mocr run 1 done", line)
        self.assertIn("2 shard(s), 20 page(s)", line)
        self.assertIn("pages/s", line)


# ── the run summary the viewer reads ────────────────────────────────
class TestRunSummary(ScanningTestCase):
    """The rows are the only place this stage's progress lives."""

    def test_none_before_the_stage_has_ever_run(self):
        self.assertIsNone(dots_mocr.run_summary(ScanFactory()))

    def test_it_counts_the_live_run(self):
        scan = ScanFactory()
        rows = dots_mocr.ensure_analyze_jobs(scan, make_manifest(3))
        ExternalJob.objects.filter(pk=rows[0].pk).update(
            status=JobStatus.COMPLETED
        )

        summary = dots_mocr.run_summary(scan)
        self.assertEqual(summary["run"], 1)
        self.assertEqual(summary["total"], 3)
        self.assertEqual(summary["done"], 1)
        self.assertEqual(summary["open"], 3)
        self.assertEqual(summary["failed"], 0)

    def test_it_reports_the_first_failure(self):
        scan = ScanFactory()
        rows = dots_mocr.ensure_analyze_jobs(scan, make_manifest(2))
        ExternalJob.objects.filter(pk=rows[0].pk).update(
            status=JobStatus.FAILED,
            error_code="BAD_INPUT",
            error_message="bad pdf",
        )

        summary = dots_mocr.run_summary(scan)
        self.assertEqual(summary["failed"], 1)
        self.assertEqual(summary["error_code"], "BAD_INPUT")
        self.assertEqual(summary["error_message"], "bad pdf")

    def test_it_ignores_a_superseded_run(self):
        scan = ScanFactory()
        manifest = make_manifest(2)
        first = dots_mocr.ensure_analyze_jobs(scan, manifest)
        ExternalJob.objects.filter(pk=first[0].pk).update(
            status=JobStatus.CANCELLED
        )
        dots_mocr.ensure_analyze_jobs(scan, manifest)

        summary = dots_mocr.run_summary(scan)
        self.assertEqual(summary["run"], 2)
        self.assertEqual(summary["failed"], 0)
