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
from unittest.mock import MagicMock, patch

import requests
from django.db import IntegrityError
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from scanning import dots_mocr, jobs, runpod_client, sharding
from scanning.factories import ScanFactory
from scanning.models import (
    ExternalJob,
    JobEngine,
    JobProvider,
    JobStage,
    JobStatus,
    Scan,
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

    def _finished_run(self, scan, manifest, holes=(), filtered=()):
        """Complete a run whose shards in ``holes`` left pages unread."""
        rows = dots_mocr.ensure_analyze_jobs(scan, manifest)
        for row in rows:
            output = {"page_count": 10, "failed_pages": []}
            if row.shard_index in holes:
                output["failed_pages"] = [3]
            if row.shard_index in filtered:
                output["filtered_pages"] = [7]
            ExternalJob.objects.filter(pk=row.pk).update(
                status=JobStatus.CONSUMED,
                result_key=f"r{row.run}-s{row.shard_index}-a1.json",
                provider_meta={"output": output},
            )
        return dots_mocr.live_analyze_jobs(scan)

    def test_a_whole_run_is_kept_unless_a_new_one_is_forced(self):
        scan = ScanFactory()
        manifest = make_manifest(shard_count=2)
        first = self._finished_run(scan, manifest, holes=(1,))

        again = dots_mocr.ensure_analyze_jobs(scan, manifest)
        self.assertEqual([job.pk for job in again], [job.pk for job in first])

        with (
            patch("scanning.s3_sync.s3_active", return_value=True),
            patch("scanning.s3_sync.object_exists", return_value=True),
        ):
            forced = dots_mocr.ensure_analyze_jobs(
                scan, manifest, force_new_run=True
            )
        self.assertEqual(forced[0].run, 2)
        self.assertEqual(len(analyze_jobs(scan)), 4)

    def test_a_result_with_unread_pages_is_not_carried(self):
        # The carry is what makes a forced run the backfill of #238:
        # the clean shard rides along for free, the shard with a hole
        # is read again.
        scan = ScanFactory()
        manifest = make_manifest(shard_count=2)
        self._finished_run(scan, manifest, holes=(1,))

        with (
            patch("scanning.s3_sync.s3_active", return_value=True),
            patch("scanning.s3_sync.object_exists", return_value=True),
        ):
            forced = dots_mocr.ensure_analyze_jobs(
                scan, manifest, force_new_run=True
            )

        self.assertEqual(forced[0].status, JobStatus.COMPLETED)
        self.assertEqual(forced[0].result_key, "r1-s0-a1.json")
        self.assertEqual(forced[1].status, JobStatus.PENDING)
        self.assertEqual(forced[1].result_key, "")
        self.assertEqual(
            [
                row.shard_index
                for row in dots_mocr.shards_with_holes(
                    dots_mocr.live_analyze_jobs(scan)
                )
            ],
            [],
        )

    def test_a_result_with_filtered_pages_is_not_carried_either(self):
        scan = ScanFactory()
        manifest = make_manifest(shard_count=2)
        self._finished_run(scan, manifest, filtered=(0,))

        with (
            patch("scanning.s3_sync.s3_active", return_value=True),
            patch("scanning.s3_sync.object_exists", return_value=True),
        ):
            forced = dots_mocr.ensure_analyze_jobs(
                scan, manifest, force_new_run=True
            )

        self.assertEqual(forced[0].status, JobStatus.PENDING)
        self.assertEqual(forced[1].status, JobStatus.COMPLETED)

    def _reread(self, scan, manifest, shard_index, failed_pages):
        """Force a new run and answer its re-read of one shard."""
        with (
            patch("scanning.s3_sync.s3_active", return_value=True),
            patch("scanning.s3_sync.object_exists", return_value=True),
        ):
            rows = dots_mocr.ensure_analyze_jobs(
                scan, manifest, force_new_run=True
            )
        row = rows[shard_index]
        ExternalJob.objects.filter(pk=row.pk).update(
            result_key=f"r{row.run}-s{shard_index}-a1.json",
            provider_meta={
                "output": {"page_count": 10, "failed_pages": failed_pages}
            },
        )
        ExternalJob.objects.filter(scan=scan, run=row.run).update(
            status=JobStatus.CONSUMED
        )
        return dots_mocr.live_analyze_jobs(scan)

    def test_a_hole_the_previous_run_reproduced_is_carried(self):
        # The worker is deterministic: run 2 re-read shard 1 and got the
        # same hole, so run 3 would pay for the same answer. The stable
        # hole rides along, and the command has nothing to start.
        scan = ScanFactory()
        manifest = make_manifest(shard_count=2)
        self._finished_run(scan, manifest, holes=(1,))
        live = self._reread(scan, manifest, 1, failed_pages=[3])
        self.assertTrue(jobs.hole_is_stable(live[1]))
        self.assertEqual(dots_mocr.shards_worth_rereading(live), [])

        with (
            patch("scanning.s3_sync.s3_active", return_value=True),
            patch("scanning.s3_sync.object_exists", return_value=True),
        ):
            third = dots_mocr.ensure_analyze_jobs(
                scan, manifest, force_new_run=True
            )
        self.assertEqual(third[0].run, 3)
        self.assertEqual(
            [row.status for row in third],
            [JobStatus.COMPLETED, JobStatus.COMPLETED],
        )
        self.assertEqual(third[1].result_key, "r2-s1-a1.json")

    def test_a_hole_that_changed_is_not_stable(self):
        scan = ScanFactory()
        manifest = make_manifest(shard_count=2)
        self._finished_run(scan, manifest, holes=(1,))
        live = self._reread(scan, manifest, 1, failed_pages=[5])
        self.assertFalse(jobs.hole_is_stable(live[1]))
        self.assertEqual(dots_mocr.shards_worth_rereading(live), [live[1]])
        # A first hole has no previous run to compare with.
        first = self._finished_run(ScanFactory(), manifest, holes=(0,))
        self.assertFalse(jobs.hole_is_stable(first[0]))
        self.assertEqual(dots_mocr.shards_worth_rereading(first), [first[0]])


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

    def test_the_claim_stamps_the_queue_ceiling(self):
        # The claim hands the row to the provider's queue, which is
        # where the bounded wait starts. Being accepted is not being
        # started: only the IN_PROGRESS crossing starts the run budget.
        self._tick()
        job = analyze_jobs(self.scan)[0]
        self.assertEqual(job.deadline, jobs.queue_deadline(job.submitted_at))

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

    def test_recovered_pages_are_logged_at_info_in_volume_numbering(self):
        # The worker's retry ladder (#238) saved a page; the INFO line
        # beside the WARNING is what makes the recovery rate readable
        # off the logs. Shard 2 page 2 is volume page 13.
        with self.assertLogs("scanning.jobs", level="INFO") as logs:
            jobs._complete(
                self.job,
                {"page_count": 10, "failed_pages": [], "recovered_pages": [2]},
                timezone.now(),
            )
        lines = [line for line in logs.output if "recovered" in line]
        self.assertEqual(len(lines), 1)
        self.assertIn("recovered 1 page(s) on a retry", lines[0])
        self.assertIn("[13]", lines[0])
        self.assertFalse(any("could not read" in line for line in logs.output))
        self.assertFalse(jobs.has_unread_pages(self.job))

    def test_a_row_with_unread_pages_says_so(self):
        jobs._complete(
            self.job,
            {"page_count": 10, "failed_pages": [2]},
            timezone.now(),
        )
        self.job.refresh_from_db()
        self.assertTrue(jobs.has_unread_pages(self.job))

    def test_a_filtered_page_is_a_hole_too_and_is_warned_about(self):
        # An answer that was not layout JSON has no cell, so the
        # page-number reader gets nothing from it either. A WARNING and
        # not an INFO since #242: the repair reaches every measured
        # shape of the fault, so a page that survives it is a new shape
        # somebody has to look at.
        with self.assertLogs("scanning.jobs", level="INFO") as logs:
            jobs._complete(
                self.job,
                {"page_count": 10, "failed_pages": [], "filtered_pages": [5]},
                timezone.now(),
            )
        lines = [line for line in logs.output if "could repair" in line]
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].startswith("WARNING"))
        self.assertIn(
            "answered 1 page(s) with layout JSON nothing could repair",
            lines[0],
        )
        self.assertIn("[16]", lines[0])
        self.job.refresh_from_db()
        self.assertTrue(jobs.has_unread_pages(self.job))

    def test_a_repaired_page_is_logged_at_info_and_is_no_hole(self):
        # #242: the glue or the worker put one character back, so the
        # page has its cells and its number. Worth a line, not a
        # warning, and never a re-read.
        with self.assertLogs("scanning.jobs", level="INFO") as logs:
            jobs._complete(
                self.job,
                {"page_count": 10, "failed_pages": [], "repaired_pages": [5]},
                timezone.now(),
            )
        lines = [line for line in logs.output if "repaired the layout" in line]
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].startswith("INFO"))
        self.assertIn("repaired the layout JSON of 1 page(s)", lines[0])
        self.assertIn("[16]", lines[0])
        self.job.refresh_from_db()
        self.assertFalse(jobs.has_unread_pages(self.job))


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
        # "open" is what the daemon still has to do, so a COMPLETED row
        # is not in it -- otherwise a run holding one failed shard would
        # read as open forever and the button would refuse its re-run.
        self.assertEqual(summary["open"], 2)
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


# ── the start button ────────────────────────────────────────────────
@override_settings(**DOTS)
class TestStartButton(ScanningTestCase):
    """Staff-only, and it writes rows rather than calling RunPod."""

    def setUp(self):
        super().setUp()
        self.staff = self.make_staff_user()
        self.scan = ScanFactory(page_count=30)
        self.url = reverse("start_dots_mocr", kwargs={"pk": self.scan.pk})
        self.manifest = make_manifest(shard_count=3, pages_per_shard=10)

    _UNSET = object()

    def _committed(self, manifest=_UNSET, reason=""):
        """Patch the manifest check to answer without S3."""
        return patch(
            "scanning.sharding.committed_manifest",
            return_value=(
                self.manifest if manifest is self._UNSET else manifest,
                reason,
            ),
        )

    def _press(self, user=None):
        self.client.force_login(user or self.staff)
        return self.client.post(self.url)

    def test_staff_press_creates_one_row_per_shard(self):
        with self._committed():
            response = self._press()

        self.assertRedirects(
            response,
            reverse("scan_process", kwargs={"pk": self.scan.pk}),
            fetch_redirect_response=False,
        )
        rows = analyze_jobs(self.scan)
        self.assertEqual(len(rows), 3)
        self.assertEqual({row.status for row in rows}, {JobStatus.PENDING})

    def test_the_request_never_calls_runpod(self):
        # The web pod writes rows; the daemon sends them. That keeps a
        # request off a slow HTTP call and survives a redeployed pod.
        with (
            self._committed(),
            patch("scanning.runpod_client.submit_job") as submit,
        ):
            self._press()
        submit.assert_not_called()

    def test_a_non_staff_user_is_refused(self):
        with self._committed():
            response = self._press(self.make_user())
        self.assertEqual(analyze_jobs(self.scan), [])
        messages = list(response.wsgi_request._messages)
        self.assertIn("Only staff", str(messages[0]))

    def test_an_anonymous_user_is_redirected_to_login(self):
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response["Location"])
        self.assertEqual(analyze_jobs(self.scan), [])

    def test_a_get_is_rejected(self):
        self.client.force_login(self.staff)
        self.assertEqual(self.client.get(self.url).status_code, 405)

    def test_a_disabled_stage_is_refused(self):
        with override_settings(DOTS_MOCR_ENABLED=False), self._committed():
            self._press()
        self.assertEqual(analyze_jobs(self.scan), [])

    def test_no_committed_shard_set_is_refused(self):
        # Re-cutting is the pipeline's job, not a request's.
        with self._committed(manifest=None, reason="no shard set"):
            response = self._press()
        self.assertEqual(analyze_jobs(self.scan), [])
        messages = list(response.wsgi_request._messages)
        self.assertIn("no shard set", str(messages[0]))

    def test_a_second_press_while_a_run_is_open_is_refused(self):
        with self._committed():
            self._press()
            first = [row.pk for row in analyze_jobs(self.scan)]
            response = self._press()

        self.assertEqual([row.pk for row in analyze_jobs(self.scan)], first)
        messages = list(response.wsgi_request._messages)
        self.assertIn("already going", str(messages[-1]))

    def test_a_press_after_a_finished_run_reuses_it(self):
        # Idempotent rather than refused: it must not pay twice for
        # shards already read.
        with self._committed():
            self._press()
            rows = analyze_jobs(self.scan)
            ExternalJob.objects.filter(pk__in=[row.pk for row in rows]).update(
                status=JobStatus.COMPLETED
            )
            self._press()

        self.assertEqual(len(analyze_jobs(self.scan)), 3)
        self.assertEqual(
            {row.status for row in analyze_jobs(self.scan)},
            {JobStatus.COMPLETED},
        )

    def test_a_press_after_a_dead_run_starts_a_new_one(self):
        # One failed shard beside two finished ones: the run can never
        # complete, and a re-run is exactly what it needs.
        with self._committed():
            self._press()
            rows = analyze_jobs(self.scan)
            ExternalJob.objects.filter(pk=rows[0].pk).update(
                status=JobStatus.FAILED
            )
            ExternalJob.objects.filter(
                pk__in=[row.pk for row in rows[1:]]
            ).update(status=JobStatus.COMPLETED)
            self._press()

        self.assertEqual(len(analyze_jobs(self.scan)), 6)
        self.assertEqual(dots_mocr.run_summary(self.scan)["run"], 2)

    def test_a_dead_shard_does_not_restart_while_siblings_run(self):
        # The daemon may still be working on them, and a second run
        # would read the same shards twice.
        with self._committed():
            self._press()
            rows = analyze_jobs(self.scan)
            ExternalJob.objects.filter(pk=rows[0].pk).update(
                status=JobStatus.FAILED
            )
            self._press()

        self.assertEqual(len(analyze_jobs(self.scan)), 3)

    def test_the_scan_status_is_untouched(self):
        # The stage is tracked on rows alone (#190), so a volume stays
        # browsable and reviewable while it reads.
        before = self.scan.status
        with self._committed():
            self._press()
        self.scan.refresh_from_db()
        self.assertEqual(self.scan.status, before)


class TestCommittedManifest(ScanningTestCase):
    """The fingerprint check a request thread can afford."""

    def setUp(self):
        super().setUp()
        self.scan = ScanFactory(page_count=30)
        self.manifest = make_manifest(shard_count=3, pages_per_shard=10)

    _UNSET = object()

    def _check(self, manifest=_UNSET, size=3072, **kwargs):
        defaults = {
            "s3_active": lambda: True,
            "fetch_shard_manifest": lambda scan: (
                self.manifest if manifest is self._UNSET else manifest
            ),
            "object_size": lambda key: size,
            "s3_original_key": lambda scan: "processing/1/vol.original.pdf",
        }
        with patch.multiple("scanning.s3_sync", **{**defaults, **kwargs}):
            return sharding.committed_manifest(self.scan)

    def test_a_current_set_is_accepted(self):
        found, reason = self._check()
        self.assertEqual(found, self.manifest)
        self.assertEqual(reason, "")

    def test_it_reads_no_shard_object(self):
        # One head_object on the original, and nothing else. A web pod
        # must not download a multi-gigabyte PDF to answer a button.
        calls = []

        def size(key):
            calls.append(key)
            return 3072

        found, _ = self._check(object_size=size)
        self.assertIsNotNone(found)
        self.assertEqual(calls, ["processing/1/vol.original.pdf"])

    def test_a_resized_original_is_refused(self):
        found, reason = self._check(size=9999)
        self.assertIsNone(found)
        self.assertIn("has changed", reason)

    def test_a_page_count_disagreement_is_refused(self):
        self.scan.page_count = 44
        found, reason = self._check()
        self.assertIsNone(found)
        self.assertIn("44", reason)

    def test_a_missing_manifest_is_refused(self):
        found, reason = self._check(
            manifest=None, fetch_shard_manifest=lambda scan: None
        )
        self.assertIsNone(found)
        self.assertIn("no committed shard set", reason)

    def test_a_wrong_version_is_refused(self):
        found, reason = self._check(manifest={**self.manifest, "version": 99})
        self.assertIsNone(found)
        self.assertIn("version", reason)

    def test_a_malformed_manifest_is_refused(self):
        found, reason = self._check(
            manifest={"version": 1, "source": "nope", "shards": []}
        )
        self.assertIsNone(found)
        self.assertIn("malformed", reason)

    def test_an_empty_shard_list_is_refused(self):
        found, reason = self._check(manifest={**self.manifest, "shards": []})
        self.assertIsNone(found)
        self.assertIn("no shards", reason)

    def test_a_missing_original_is_refused(self):
        found, reason = self._check(object_size=lambda key: None)
        self.assertIsNone(found)
        self.assertIn("not in the bucket", reason)

    def test_an_s3_error_is_reported_not_raised(self):
        # fetch_shard_manifest re-raises anything that is not a missing
        # object on purpose. A throttle must not reach the user as a 500.
        def boom(scan):
            raise RuntimeError("throttled")

        with self.assertLogs("scanning.sharding", level="WARNING"):
            found, reason = self._check(fetch_shard_manifest=boom)
        self.assertIsNone(found)
        self.assertIn("throttled", reason)

    def test_inactive_s3_is_refused(self):
        found, reason = self._check(s3_active=lambda: False)
        self.assertIsNone(found)
        self.assertIn("S3 is not active", reason)


# ── review findings (PR #192) ───────────────────────────────────────
@override_settings(**DOTS)
class TestScanDeletionStopsBilling(ScanningTestCase):
    """A deleted scan must not leave a GPU job running.

    ``ExternalJob.scan`` is CASCADE, so the delete takes the row and
    with it ``external_id`` -- the only handle on a running job. The
    sweep iterates rows that still exist, so an orphan would bill to
    completion with nothing left anywhere to cancel it.
    """

    def setUp(self):
        super().setUp()
        self.scan = ScanFactory()
        self.job = dots_mocr.ensure_analyze_jobs(
            self.scan, make_manifest(shard_count=1)
        )[0]
        ExternalJob.objects.filter(pk=self.job.pk).update(
            status=JobStatus.IN_PROGRESS, external_id="job-1"
        )

    def _delete(self, bulk=False):
        from django.contrib import admin as django_admin

        from scanning.admin import ScanAdmin

        model_admin = ScanAdmin(Scan, django_admin.site)
        with (
            patch("scanning.runpod_client.cancel_job") as cancel,
            patch("scanning.s3_sync.delete_shard_objects"),
            patch("scanning.s3_sync.delete_job_objects"),
        ):
            if bulk:
                model_admin.delete_queryset(
                    None, Scan.objects.filter(pk=self.scan.pk)
                )
            else:
                model_admin.delete_model(None, self.scan)
        return cancel

    def test_delete_model_cancels_the_running_job(self):
        cancel = self._delete()
        cancel.assert_called_once()
        self.assertEqual(cancel.call_args[0][2], "job-1")
        self.assertFalse(Scan.objects.filter(pk=self.scan.pk).exists())

    def test_delete_queryset_cancels_the_running_job(self):
        cancel = self._delete(bulk=True)
        cancel.assert_called_once()

    def test_the_cancel_runs_before_the_s3_sweep(self):
        # A worker still running could PUT after the sweep and
        # re-orphan an object we had just deleted.
        order = []
        from scanning.admin import _release_scan_external_work

        with (
            patch(
                "scanning.jobs.abandon_open",
                side_effect=lambda *a, **k: order.append("cancel"),
            ),
            patch(
                "scanning.s3_sync.delete_shard_objects",
                side_effect=lambda scan: order.append("shards"),
            ),
            patch(
                "scanning.s3_sync.delete_job_objects",
                side_effect=lambda scan: order.append("jobs"),
            ),
        ):
            _release_scan_external_work(self.scan)
        self.assertEqual(order, ["cancel", "shards", "jobs"])

    def test_a_stuck_provider_does_not_block_the_delete(self):
        from scanning.admin import _release_scan_external_work

        with (
            patch(
                "scanning.jobs.abandon_open",
                side_effect=RuntimeError("runpod down"),
            ),
            patch("scanning.s3_sync.delete_shard_objects") as shards,
            patch("scanning.s3_sync.delete_job_objects"),
            self.assertLogs("scanning.admin", level="WARNING"),
        ):
            _release_scan_external_work(self.scan)
        # The sweep still ran, and nothing propagated.
        shards.assert_called_once()


@override_settings(**DOTS)
class TestConfirmedByRecordsHowWeLearned(ScanningTestCase):
    """A job recovered by probing S3 must not read as a normal answer."""

    def setUp(self):
        super().setUp()
        self.scan = ScanFactory()
        self.job = dots_mocr.ensure_analyze_jobs(
            self.scan, make_manifest(shard_count=1)
        )[0]
        ExternalJob.objects.filter(pk=self.job.pk).update(
            status=JobStatus.SUBMITTED,
            external_id="job-1",
            result_key="jobs/analyze/dots_mocr/r1-s0-a1.json",
            submitted_at=timezone.now(),
            deadline=jobs.queue_deadline(timezone.now()),
        )
        self.job.refresh_from_db()

    def _sweep(self, poll_outcome):
        with patch(
            "scanning.runpod_client.poll_once", return_value=poll_outcome
        ):
            jobs.sweep_jobs()
        self.job.refresh_from_db()

    def test_a_provider_answer_reads_as_a_response(self):
        self._sweep(outcome(JobStatus.COMPLETED, output={"page_count": 10}))
        self.assertEqual(self.job.provider_meta["confirmed_by"], "response")

    def test_a_404_recovered_job_reads_as_an_s3_head(self):
        # The poll synthesises a truthy ``output`` for this path, so
        # inferring the label would hide that the job record was gone.
        recovered = runpod_client.PollOutcome(
            status=JobStatus.COMPLETED,
            provider_status="NOT_FOUND",
            output={"result_key": self.job.result_key},
            confirmed_by="s3_head",
        )
        self._sweep(recovered)
        self.assertEqual(self.job.status, JobStatus.COMPLETED)
        self.assertEqual(self.job.provider_meta["confirmed_by"], "s3_head")

    def test_poll_once_labels_the_recovery_itself(self):
        with (
            patch("scanning.runpod_client.requests.get") as get,
            patch("scanning.s3_sync.object_exists", return_value=True),
        ):
            get.return_value = MagicMock(status_code=404, text="gone")
            found = runpod_client.poll_once(
                "https://api.runpod.ai/v2/ep-dots",
                {},
                "job-1",
                result_key="k",
            )
        self.assertEqual(found.status, JobStatus.COMPLETED)
        self.assertEqual(found.confirmed_by, "s3_head")
        self.assertEqual(found.provider_status, "NOT_FOUND")


class TestConcurrentPressesDoNotCrash(ScanningTestCase):
    """Two staff presses at once must reuse, not raise.

    Until the button landed, the only caller of ``ensure_shard_jobs``
    was the single-threaded daemon. Now two requests can read no rows,
    compute the same run, and both insert -- and the unique constraint
    is what serializes them.
    """

    def test_the_loser_reuses_the_winner_s_rows(self):
        scan = ScanFactory()
        manifest = make_manifest(shard_count=2)
        winner = dots_mocr.ensure_analyze_jobs(scan, manifest)

        # Stand in for the race: this call decides it needs a new run
        # (the shard set moved), and its insert collides with the run
        # another writer committed in between.
        with (
            patch(
                "scanning.jobs.ExternalJob.objects.bulk_create",
                side_effect=IntegrityError("duplicate key"),
            ),
            self.assertLogs("scanning.jobs", level="INFO") as logs,
        ):
            loser = jobs.ensure_shard_jobs(
                scan,
                make_manifest(shard_count=2, pages_per_shard=25),
                stage=JobStage.ANALYZE,
                engine=JobEngine.DOTS_MOCR,
                provider=JobProvider.RUNPOD,
            )

        self.assertEqual([row.pk for row in loser], [row.pk for row in winner])
        self.assertIn("created by another writer", "\n".join(logs.output))

    def test_no_duplicate_rows_survive(self):
        scan = ScanFactory()
        manifest = make_manifest(shard_count=2)
        dots_mocr.ensure_analyze_jobs(scan, manifest)
        dots_mocr.ensure_analyze_jobs(scan, manifest)
        self.assertEqual(len(analyze_jobs(scan)), 2)


@override_settings(**DOTS)
class TestMessageMatchesWhatHappened(ScanningTestCase):
    """The flash must not promise a dispatch that is not coming."""

    def setUp(self):
        super().setUp()
        self.staff = self.make_staff_user()
        self.scan = ScanFactory(page_count=30)
        self.url = reverse("start_dots_mocr", kwargs={"pk": self.scan.pk})
        self.manifest = make_manifest(shard_count=3, pages_per_shard=10)
        self.client.force_login(self.staff)

    def _press(self):
        with patch(
            "scanning.sharding.committed_manifest",
            return_value=(self.manifest, ""),
        ):
            return self.client.post(self.url)

    def test_a_first_press_says_it_queued_the_work(self):
        response = self._press()
        text = str(list(response.wsgi_request._messages)[0])
        self.assertIn("Queued OCR for 3 part(s)", text)

    def test_a_press_on_a_finished_run_says_nothing_was_queued(self):
        self._press()
        ExternalJob.objects.filter(scan=self.scan).update(
            status=JobStatus.COMPLETED
        )
        response = self._press()

        text = str(list(response.wsgi_request._messages)[-1])
        self.assertIn("already read", text)
        self.assertNotIn("Queued OCR", text)


# ── review findings (second pass) ───────────────────────────────────
@override_settings(**DOTS)
class TestQueueCeilingCannotBeReset(ScanningTestCase):
    """A paused endpoint must not hold a scan forever.

    The ceiling is stamped once per attempt, at the attempt's first
    claim -- the moment the row is handed to the provider. A re-claim
    after a defer does not move it and a defer does not, or a row would
    have its wait forgiven on every tick. A row nothing has claimed yet
    carries no ceiling at all: our own queue has no clock, so a long
    backlog drains instead of expiring (issue #218).
    """

    def setUp(self):
        super().setUp()
        self.scan = ScanFactory()
        self.jobs = dots_mocr.ensure_analyze_jobs(
            self.scan, make_manifest(shard_count=1)
        )
        self.presign = patch.multiple(
            "scanning.s3_sync",
            s3_active=lambda: True,
            presign_get=lambda key, ttl: "https://s3/in?get",
            presign_put=lambda key, ct, ttl: "https://s3/out?put",
        )
        self.presign.start()
        self.addCleanup(self.presign.stop)

    def _row(self):
        return analyze_jobs(self.scan)[0]

    def _tick(self, side_effect=None):
        with patch(
            "scanning.runpod_client.submit_job",
            side_effect=side_effect,
            return_value="job-1",
        ):
            jobs.submit_pending()

    def test_a_created_row_carries_no_ceiling(self):
        # Waiting in our own queue is not a fault, however long: a
        # creation-stamped clock expired a whole backlog unsubmitted
        # (issue #218).
        self.assertIsNone(self._row().deadline)

    def test_a_defer_keeps_the_queue_ceiling(self):
        busy = runpod_client.RunpodEndpointBusy(
            "paused", error_code="ENDPOINT_PAUSED"
        )
        self._tick(side_effect=busy)
        stamped = self._row().deadline
        self.assertIsNotNone(stamped)
        for _ in range(3):
            self._tick(side_effect=busy)
        row = self._row()
        self.assertEqual(row.status, JobStatus.PENDING)
        self.assertEqual(row.deadline, stamped)

    def test_a_permanently_paused_endpoint_eventually_times_out(self):
        # The failure this whole ceiling exists to produce. Before the
        # fix the deadline moved on every defer and this never fired.
        busy = runpod_client.RunpodEndpointBusy(
            "paused", error_code="ENDPOINT_PAUSED"
        )
        self._tick(side_effect=busy)
        # Six hours later, still paused.
        ExternalJob.objects.filter(pk=self.jobs[0].pk).update(
            deadline=timezone.now() - timedelta(seconds=1)
        )
        self._tick(side_effect=busy)
        summary = jobs.sweep_jobs()

        row = self._row()
        self.assertEqual(summary.failed, 1)
        self.assertEqual(row.status, JobStatus.FAILED)
        self.assertEqual(row.error_code, "QUEUE_TIMEOUT")

    def test_a_retry_clears_the_ceiling_for_the_next_claim(self):
        # A new attempt is a new wait, and its clock starts when the
        # next claim hands it to the provider.
        self._tick(
            side_effect=runpod_client.RunpodTransientError(
                "refused", error_code="BAD_GATEWAY"
            )
        )
        row = self._row()
        self.assertEqual(row.attempt, 2)
        self.assertIsNone(row.deadline)

    def test_a_doctor_row_still_takes_its_flat_answer_budget(self):
        convert = jobs.ensure_convert_jobs(
            self.scan, make_manifest(shard_count=1)
        )[0]
        with (
            override_settings(
                DOCTOR_ENABLED=True,
                DOCTOR_HOST="http://doctor:5050",
                DOCTOR_JOB_DEADLINE_SECONDS=900,
                DOTS_MOCR_ENABLED=False,
            ),
            patch(
                "scanning.doctor_client.convert_bitonal",
                side_effect=RuntimeError("stop after the claim"),
            ),
        ):
            jobs.submit_pending()
        convert.refresh_from_db()
        # Its response *is* the completion, so its clock starts at the
        # request rather than at the queue.
        self.assertLess(
            convert.deadline, timezone.now() + timedelta(seconds=1000)
        )


@override_settings(**DOTS)
class TestSubmitCancelRace(ScanningTestCase):
    """A job whose id has nowhere to live must still be cancelled."""

    def setUp(self):
        super().setUp()
        self.scan = ScanFactory()
        self.job = dots_mocr.ensure_analyze_jobs(
            self.scan, make_manifest(shard_count=1)
        )[0]
        self.presign = patch.multiple(
            "scanning.s3_sync",
            s3_active=lambda: True,
            presign_get=lambda key, ttl: "https://s3/in?get",
            presign_put=lambda key, ct, ttl: "https://s3/out?put",
        )
        self.presign.start()
        self.addCleanup(self.presign.stop)

    def test_a_cancel_mid_submit_still_stops_the_billing(self):
        # The sequence: the claim marks the row SUBMITTED, an admin
        # re-queue abandons it (abandon_open's own cancel is a no-op,
        # because external_id is still blank), then POST /run succeeds
        # and the write of the id loses the compare-and-swap. Nothing
        # else can ever find this job, so the submit path cancels here.
        def submit_then_cancel(*args, **kwargs):
            jobs.abandon_open(self.scan, "Re-queued from the admin")
            return "job-1"

        with (
            patch(
                "scanning.runpod_client.submit_job",
                side_effect=submit_then_cancel,
            ),
            patch("scanning.runpod_client.cancel_job") as cancel,
            self.assertLogs("scanning.jobs", level="WARNING") as logs,
        ):
            summary = jobs.submit_pending()

        self.job.refresh_from_db()
        self.assertEqual(self.job.status, JobStatus.CANCELLED)
        self.assertEqual(self.job.external_id, "")
        self.assertEqual(summary.skipped, 1)
        cancel.assert_called_once()
        self.assertEqual(cancel.call_args[0][2], "job-1")
        self.assertIn("cancelling it", "\n".join(logs.output))

    def test_a_lost_write_with_no_job_id_cancels_nothing(self):
        with patch("scanning.runpod_client.cancel_job") as cancel:
            jobs._apply_runpod_outcome(self.job, None, None, timezone.now())
        cancel.assert_not_called()


class TestSubmitClassification(ScanningTestCase):
    """The two status codes the first pass got wrong."""

    def _submit(self, status_code, body):
        response = MagicMock(status_code=status_code, text=str(body))
        response.json.return_value = body
        response.raise_for_status.side_effect = requests.HTTPError(
            "err", response=response
        )
        with patch(
            "scanning.runpod_client.requests.post", return_value=response
        ):
            try:
                runpod_client.submit_job(
                    "https://api.runpod.ai/v2/ep", {}, {"action": "parse"}
                )
            except runpod_client.RunpodError as exc:
                return exc
        return None

    def test_a_rate_limit_is_the_endpoint_declining_work(self):
        # Not a bad job. Failing a shard for good over one 429 is
        # wildly disproportionate.
        exc = self._submit(429, {"detail": "slow down"})
        self.assertIsInstance(exc, runpod_client.RunpodEndpointBusy)

    def test_a_paused_endpoint_is_still_busy(self):
        exc = self._submit(409, {"code": "ENDPOINT_PAUSED"})
        self.assertIsInstance(exc, runpod_client.RunpodEndpointBusy)

    def test_another_4xx_stays_terminal(self):
        exc = self._submit(401, {"detail": "bad key"})
        self.assertNotIsInstance(exc, runpod_client.RunpodTransientError)

    def test_a_corrupt_input_is_retriable(self):
        # We cut the shard and verified it, so a copy that will not open
        # is a transfer fault, not a bad volume.
        self.assertIn(
            "INPUT_DOWNLOAD_CORRUPT", runpod_client.TRANSIENT_ERROR_CODES
        )


class TestRunSummaryLabel(ScanningTestCase):
    """The template must not print a Python dict."""

    def test_the_label_is_readable_prose(self):
        scan = ScanFactory()
        rows = dots_mocr.ensure_analyze_jobs(scan, make_manifest(3))
        ExternalJob.objects.filter(pk=rows[0].pk).update(
            status=JobStatus.COMPLETED
        )

        label = dots_mocr.run_summary(scan)["label"]
        self.assertNotIn("{", label)
        self.assertNotIn("'", label)
        self.assertIn("1 completed", label)
        self.assertIn("2 pending submit", label)


class TestKnownEnqueuePaths(ScanningTestCase):
    """Every path that creates paid GPU work, pinned.

    dots.mocr has two: the pipeline (#207) and the button that remains
    as the re-run and backfill path (#190). YOLO detection has one, its
    staff button (#195); the pipeline deliberately does not enqueue it
    until the stage has been exercised on real volumes (#211).

    Row creation is what costs GPU money, so a new caller of the
    creators must be a deliberate decision that updates this set -- not
    an accident this test lets through.
    """

    def test_the_row_creators_have_exactly_the_known_callers(self):
        import ast
        import pathlib

        root = pathlib.Path("scanning")
        callers = set()
        for path in root.rglob("*.py"):
            if "tests" in path.parts or "migrations" in path.parts:
                continue
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                name = getattr(func, "attr", getattr(func, "id", ""))
                if name in (
                    "ensure_analyze_jobs",
                    "ensure_detect_jobs",
                    "ensure_shard_jobs",
                ):
                    callers.add((str(path), name))

        self.assertEqual(
            callers,
            {
                # dots.mocr: the staff button (#190), the pipeline (#207),
                # and the backfill of runs that left pages unread (#238)
                # -- a command, never a tick, because it spends money.
                ("scanning/views_process.py", "ensure_analyze_jobs"),
                ("scanning/services.py", "ensure_analyze_jobs"),
                (
                    "scanning/management/commands/reread_failed_pages.py",
                    "ensure_analyze_jobs",
                ),
                # Detection: the staff button alone (#195). Nothing in
                # the pipeline may appear here until #211 says so.
                ("scanning/views_process.py", "ensure_detect_jobs"),
                # The generic creator's three wrappers.
                ("scanning/dots_mocr.py", "ensure_shard_jobs"),
                ("scanning/yolo.py", "ensure_shard_jobs"),
                ("scanning/jobs.py", "ensure_shard_jobs"),
            },
            "Something new creates external-job rows. Row creation "
            "starts paid GPU work, so update this set only on purpose.",
        )

    @override_settings(**DOTS)
    def test_convert_rows_alone_submit_no_ocr(self):
        # The convert stage creates no ANALYZE rows, so a tick over a
        # convert-only scan must make no RunPod call.
        scan = ScanFactory()
        jobs.ensure_convert_jobs(scan, make_manifest(shard_count=2))
        self.assertEqual(analyze_jobs(scan), [])
        with (
            patch("scanning.s3_sync.s3_active", return_value=True),
            patch("scanning.runpod_client.submit_job") as submit,
        ):
            jobs.submit_pending()
        submit.assert_not_called()


@override_settings(**DOTS)
class TestShardResultCarryOver(ScanningTestCase):
    """A replacement run re-reads only the shards that need it.

    A shard whose identity is unchanged and whose result object is
    still on S3 enters the new run as a COMPLETED row pointing at the
    prior attempt's object (``jobs._reusable_results``). The identity
    includes the shard's byte size, so a re-uploaded original with the
    same page count carries nothing.
    """

    def _dead_run(self, manifest):
        """Build a run with shard 0 COMPLETED and shard 1 FAILED."""
        scan = ScanFactory()
        rows = dots_mocr.ensure_analyze_jobs(scan, manifest)
        ExternalJob.objects.filter(pk=rows[0].pk).update(
            status=JobStatus.COMPLETED,
            result_key="jobs/analyze/dots_mocr/r1-s0-a1.json",
            completed_at=timezone.now(),
        )
        ExternalJob.objects.filter(pk=rows[1].pk).update(
            status=JobStatus.FAILED
        )
        return scan, rows

    def _carry_patches(self, object_exists=True):
        return (
            patch("scanning.s3_sync.s3_active", return_value=True),
            patch(
                "scanning.s3_sync.object_exists",
                return_value=object_exists,
            ),
        )

    def test_an_unchanged_completed_shard_is_carried(self):
        manifest = make_manifest(shard_count=2, pages_per_shard=1)
        scan, old = self._dead_run(manifest)

        active, exists = self._carry_patches()
        with active, exists:
            fresh = dots_mocr.ensure_analyze_jobs(scan, manifest)

        self.assertEqual(len(fresh), 2)
        self.assertEqual(fresh[0].run, 2)
        self.assertEqual(fresh[0].status, JobStatus.COMPLETED)
        self.assertEqual(
            fresh[0].result_key, "jobs/analyze/dots_mocr/r1-s0-a1.json"
        )
        self.assertEqual(
            fresh[0].provider_meta["carried_from"],
            {"run": 1, "job": old[0].pk},
        )
        self.assertEqual(fresh[0].external_id, "")
        self.assertEqual(fresh[1].status, JobStatus.PENDING)

    def test_a_carried_row_is_not_submitted(self):
        manifest = make_manifest(shard_count=2, pages_per_shard=1)
        scan, _ = self._dead_run(manifest)

        active, exists = self._carry_patches()
        with active, exists:
            fresh = dots_mocr.ensure_analyze_jobs(scan, manifest)
        with (
            patch.multiple(
                "scanning.s3_sync",
                s3_active=lambda: True,
                presign_get=lambda key, ttl: "https://s3/in?get",
                presign_put=lambda key, ct, ttl: "https://s3/out?put",
            ),
            patch(
                "scanning.runpod_client.submit_job", return_value="job-1"
            ) as submit,
        ):
            jobs.submit_pending()

        # One call for the still-pending shard; none for the carried one.
        self.assertEqual(submit.call_count, 1)
        carried, pending = [
            ExternalJob.objects.get(pk=row.pk) for row in fresh
        ]
        self.assertEqual(carried.status, JobStatus.COMPLETED)
        self.assertEqual(
            carried.result_key, "jobs/analyze/dots_mocr/r1-s0-a1.json"
        )
        self.assertEqual(pending.status, JobStatus.SUBMITTED)

    def test_an_s3_blip_reads_the_shard_again(self):
        """The carry is an optimization: a throttle or IAM fault must
        cost the shard's price, never the volume (an unhandled S3
        error would mark the scan ERROR in the pipeline, with no
        retry, and 500 the start button)."""
        from botocore.exceptions import ClientError

        manifest = make_manifest(shard_count=2, pages_per_shard=1)
        scan, _ = self._dead_run(manifest)
        blip = ClientError(
            {"Error": {"Code": "SlowDown", "Message": "throttled"}},
            "HeadObject",
        )

        with (
            patch("scanning.s3_sync.s3_active", return_value=True),
            patch("scanning.s3_sync.object_exists", side_effect=blip),
            self.assertLogs("scanning.jobs", level="WARNING"),
        ):
            fresh = dots_mocr.ensure_analyze_jobs(scan, manifest)

        self.assertEqual({row.status for row in fresh}, {JobStatus.PENDING})

    def test_a_missing_result_object_is_not_carried(self):
        manifest = make_manifest(shard_count=2, pages_per_shard=1)
        scan, _ = self._dead_run(manifest)

        active, exists = self._carry_patches(object_exists=False)
        with active, exists:
            fresh = dots_mocr.ensure_analyze_jobs(scan, manifest)

        self.assertEqual({row.status for row in fresh}, {JobStatus.PENDING})

    def test_a_changed_shard_is_not_carried(self):
        """Same pages, different bytes: a re-uploaded original."""
        manifest = make_manifest(shard_count=2, pages_per_shard=1)
        scan, _ = self._dead_run(manifest)
        recut = make_manifest(shard_count=2, pages_per_shard=1)
        for entry in recut["shards"]:
            entry["size_bytes"] = 2048

        active, exists = self._carry_patches()
        with active, exists:
            fresh = dots_mocr.ensure_analyze_jobs(scan, recut)

        self.assertEqual({row.status for row in fresh}, {JobStatus.PENDING})

    def test_a_legacy_row_without_size_bytes_is_not_carried(self):
        """The byte size is the proof, so a row without it re-reads."""
        manifest = make_manifest(shard_count=2, pages_per_shard=1)
        scan, old = self._dead_run(manifest)
        legacy = dict(old[0].input_manifest)
        legacy.pop("size_bytes")
        ExternalJob.objects.filter(pk=old[0].pk).update(input_manifest=legacy)

        active, exists = self._carry_patches()
        with active, exists:
            fresh = dots_mocr.ensure_analyze_jobs(scan, manifest)

        self.assertEqual({row.status for row in fresh}, {JobStatus.PENDING})

    def test_a_legacy_live_run_is_still_reused_whole(self):
        """The lenient identity match: no re-pay on deploy."""
        manifest = make_manifest(shard_count=2, pages_per_shard=1)
        scan = ScanFactory()
        rows = dots_mocr.ensure_analyze_jobs(scan, manifest)
        for row in rows:
            legacy = dict(row.input_manifest)
            legacy.pop("size_bytes")
            ExternalJob.objects.filter(pk=row.pk).update(input_manifest=legacy)

        again = dots_mocr.ensure_analyze_jobs(scan, manifest)

        self.assertEqual([row.pk for row in again], [row.pk for row in rows])

    def test_the_convert_stage_never_carries(self):
        """The bitonal merge deletes its results; there is nothing to
        point a carried row at."""
        manifest = make_manifest(shard_count=2, pages_per_shard=1)
        scan = ScanFactory()
        rows = jobs.ensure_convert_jobs(scan, manifest)
        ExternalJob.objects.filter(pk=rows[0].pk).update(
            status=JobStatus.COMPLETED,
            result_key="jobs/convert/bitonal/r1-s0-a1.pdf",
        )
        ExternalJob.objects.filter(pk=rows[1].pk).update(
            status=JobStatus.FAILED
        )

        active, exists = self._carry_patches()
        with active, exists:
            fresh = jobs.ensure_convert_jobs(scan, manifest)

        self.assertEqual({row.status for row in fresh}, {JobStatus.PENDING})


@override_settings(**DOTS)
class TestWaveOrder(ScanningTestCase):
    """The blocking provider goes last.

    The daemon's scheduler is serial (#156), so whichever wave runs
    first delays the other. A doctor submit holds its socket for the
    whole conversion; a RunPod submit returns as soon as the job is
    queued.
    """

    def test_runpod_is_sent_before_doctor(self):
        scan = ScanFactory()
        manifest = make_manifest(shard_count=1)
        jobs.ensure_convert_jobs(scan, manifest)
        dots_mocr.ensure_analyze_jobs(scan, manifest)

        order = []
        with (
            patch.multiple(
                "scanning.s3_sync",
                s3_active=lambda: True,
                presign_get=lambda key, ttl: "https://s3/in?get",
                presign_put=lambda key, ct, ttl: "https://s3/out?put",
            ),
            override_settings(
                DOCTOR_ENABLED=True, DOCTOR_HOST="http://doctor:5050"
            ),
            patch(
                "scanning.runpod_client.submit_job",
                side_effect=lambda *a, **k: order.append("runpod") or "job-1",
            ),
            patch(
                "scanning.doctor_client.convert_bitonal",
                side_effect=lambda *a, **k: (
                    order.append("doctor") or {"pages": 10}
                ),
            ),
        ):
            jobs.submit_pending()

        self.assertEqual(order, ["runpod", "doctor"])
