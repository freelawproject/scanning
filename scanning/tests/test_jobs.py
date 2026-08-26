"""Tests for ``scanning.jobs``: the external-job lifecycle.

No HTTP and no S3 -- ``doctor_client`` and the S3 helpers are patched.
Under test is the bookkeeping the daemon's resumability rests on:

- one job per shard, each addressing *its own* shard
- a row marked submitted before the request goes out
- an answered failure retries at once; an unanswered one waits for the
  S3 confirm rather than pay for the shard twice
- every write a compare-and-swap, so a second replica cannot overwrite
  an outcome
"""

from datetime import timedelta
from unittest.mock import patch

from django.test import override_settings
from django.utils import timezone

from scanning import doctor_client, jobs
from scanning.factories import ExternalJobFactory, ScanFactory
from scanning.models import (
    ExternalJob,
    JobEngine,
    JobProvider,
    JobStage,
    JobStatus,
    Status,
)
from scanning.tests.test_views import ScanningTestCase

DOCTOR = {
    "DOCTOR_ENABLED": True,
    "DOCTOR_HOST": "http://doctor:5050",
    "DOCTOR_MAX_CONCURRENCY": 4,
    "DOCTOR_MAX_ATTEMPTS": 3,
    "DOCTOR_JOB_DEADLINE_SECONDS": 900,
    "DOCTOR_PRESIGNED_TTL": 3600,
}


def make_manifest(shard_count=3, pages_per_shard=10):
    """Build a shard manifest of the shape ``sharding`` produces.

    :param shard_count: How many shards to describe.
    :param pages_per_shard: Pages in each shard.
    :returns: A manifest dict.
    :rtype: dict
    """
    shards = []
    for index in range(shard_count):
        start = index * pages_per_shard
        shards.append(
            {
                "name": f"{index + 1:04d}.pdf",
                "index": index,
                "from_page": start,
                "to_page": start + pages_per_shard - 1,
                "page_count": pages_per_shard,
                "size_bytes": 1024,
            }
        )
    return {
        "version": 1,
        "source": {
            "name": "vol.original.pdf",
            "size_bytes": 1024 * shard_count,
            "page_count": shard_count * pages_per_shard,
        },
        "target_bytes": 1024,
        "max_pages": pages_per_shard,
        "shards": shards,
        "timings": {"split_seconds": 0.1},
    }


def convert_jobs(scan):
    """Return a scan's conversion rows ordered by shard index.

    :param scan: The scan to look up.
    :returns: Its CONVERT/BITONAL rows.
    :rtype: list[ExternalJob]
    """
    return list(
        ExternalJob.objects.filter(
            scan=scan, stage=JobStage.CONVERT, engine=JobEngine.BITONAL
        ).order_by("run", "shard_index")
    )


class TestEnsureConvertJobs(ScanningTestCase):
    """Creating the work, and knowing when it is already created."""

    def test_one_row_per_shard_addressing_its_own_shard(self):
        """The bug worth a test: rows must not share one input key.

        A shard is a separate PDF, so shard 3's job reads
        ``shards/0003.pdf``. One key for every row would convert the
        same pages N times and lose the rest.
        """
        scan = ScanFactory()
        manifest = make_manifest(shard_count=3)

        created = jobs.ensure_convert_jobs(scan, manifest)

        self.assertEqual(len(created), 3)
        self.assertEqual(
            [job.input_key.rsplit("/", 1)[-1] for job in created],
            ["0001.pdf", "0002.pdf", "0003.pdf"],
        )
        self.assertEqual(len({job.input_key for job in created}), 3)
        for index, job in enumerate(created):
            self.assertTrue(
                job.input_key.endswith(f"shards/{index + 1:04d}.pdf")
            )
            self.assertEqual(job.shard_index, index)
            self.assertEqual(job.shard_count, 3)
            self.assertEqual(job.status, JobStatus.PENDING)
            self.assertEqual(job.provider, JobProvider.DOCTOR)
            self.assertEqual(job.run, 1)

    def test_rows_carry_the_shard_identity_they_were_cut_from(self):
        """So the merge checks what ran, not a live manifest.

        A later re-shard can replace the manifest in S3; the row's copy
        cannot be replaced.
        """
        scan = ScanFactory()

        created = jobs.ensure_convert_jobs(
            scan, make_manifest(shard_count=2, pages_per_shard=7)
        )

        self.assertEqual(
            created[1].input_manifest,
            {
                "name": "0002.pdf",
                "from_page": 7,
                "to_page": 13,
                "page_count": 7,
                "source_page_count": 14,
            },
        )

    def test_calling_twice_reuses_the_same_run(self):
        """Idempotent, so a re-queued scan does not pay twice."""
        scan = ScanFactory()
        manifest = make_manifest()

        first = jobs.ensure_convert_jobs(scan, manifest)
        second = jobs.ensure_convert_jobs(scan, manifest)

        self.assertEqual([j.pk for j in first], [j.pk for j in second])
        self.assertEqual(len(convert_jobs(scan)), 3)

    def test_reuse_survives_progress_on_the_rows(self):
        scan = ScanFactory()
        manifest = make_manifest()
        jobs.ensure_convert_jobs(scan, manifest)
        ExternalJob.objects.update(status=JobStatus.COMPLETED)

        again = jobs.ensure_convert_jobs(scan, manifest)

        self.assertEqual(len(again), 3)
        self.assertEqual({j.run for j in again}, {1})

    def test_a_reshard_that_keeps_the_count_still_starts_a_new_run(self):
        """Counting rows is not enough; see also TestReshardDetection.

        Here the key itself moved. The harder case -- same count, same
        keys, different page ranges -- is what the identity comparison
        exists for.
        """
        scan = ScanFactory()
        jobs.ensure_convert_jobs(
            scan, make_manifest(shard_count=2, pages_per_shard=10)
        )

        moved = make_manifest(shard_count=2, pages_per_shard=10)
        moved["shards"][1]["name"] = "0009.pdf"
        second = jobs.ensure_convert_jobs(scan, moved)

        self.assertEqual({j.run for j in second}, {2})
        # The first run is kept as history, not overwritten.
        self.assertEqual(len(convert_jobs(scan)), 4)

    def test_a_reshard_into_more_shards_starts_a_new_run(self):
        scan = ScanFactory()
        jobs.ensure_convert_jobs(scan, make_manifest(shard_count=2))

        second = jobs.ensure_convert_jobs(scan, make_manifest(shard_count=5))

        self.assertEqual(len(second), 5)
        self.assertEqual({j.run for j in second}, {2})

    def test_another_scans_jobs_are_not_reused(self):
        first, second = ScanFactory(), ScanFactory()
        manifest = make_manifest(shard_count=2)

        jobs.ensure_convert_jobs(first, manifest)
        jobs.ensure_convert_jobs(second, manifest)

        self.assertEqual(len(convert_jobs(first)), 2)
        self.assertEqual(len(convert_jobs(second)), 2)
        self.assertNotEqual(
            convert_jobs(first)[0].input_key,
            convert_jobs(second)[0].input_key,
        )

    def test_rows_start_with_a_queue_ceiling(self):
        """Waiting to be submitted is the other way a job waits."""
        scan = ScanFactory()
        with override_settings(DAEMON_JOB_MAX_QUEUE_SECONDS=60):
            created = jobs.ensure_convert_jobs(
                scan, make_manifest(shard_count=1)
            )
        self.assertIsNotNone(created[0].deadline)
        self.assertLess(
            created[0].deadline, timezone.now() + timedelta(seconds=61)
        )


@override_settings(**DOCTOR)
class TestSubmitPending(ScanningTestCase):
    """The submit wave."""

    def setUp(self):
        super().setUp()
        self.scan = ScanFactory()
        self.jobs = jobs.ensure_convert_jobs(
            self.scan, make_manifest(shard_count=3)
        )
        # S3 is inert under TESTING, and submitting requires it to be
        # live: doctor reads its input through a presigned GET, so
        # without the sync the shards were never uploaded.
        self.presign = patch.multiple(
            "scanning.s3_sync",
            s3_active=lambda: True,
            presign_get=lambda key, ttl: f"https://s3/{key}?get",
            presign_put=lambda key, ct, ttl: f"https://s3/{key}?put",
        )
        self.presign.start()
        self.addCleanup(self.presign.stop)

    def _summary(self, side_effect=None, return_value=None, **kwargs):
        """Run one submit tick with ``convert_bitonal`` patched."""
        patcher = patch(
            "scanning.doctor_client.convert_bitonal",
            side_effect=side_effect,
            return_value=return_value,
        )
        with patcher as convert:
            summary = jobs.submit_pending(**kwargs)
        return summary, convert

    def test_a_successful_wave_completes_every_row(self):
        summary, convert = self._summary(return_value={"pages": 10})

        self.assertEqual(summary.submitted, 3)
        self.assertEqual(convert.call_count, 3)
        for job in convert_jobs(self.scan):
            self.assertEqual(job.status, JobStatus.COMPLETED)
            self.assertIsNotNone(job.completed_at)
            self.assertIsNotNone(job.submitted_at)
            self.assertEqual(job.provider_meta["output"], {"pages": 10})
            self.assertEqual(job.provider_meta["confirmed_by"], "response")

    def test_each_row_gets_its_own_attempt_scoped_result_key(self):
        self._summary(return_value={"pages": 10})

        keys = [job.result_key for job in convert_jobs(self.scan)]
        self.assertEqual(len(set(keys)), 3)
        for index, key in enumerate(keys):
            self.assertTrue(key.endswith(f"r1-s{index}-a1.pdf"))
            self.assertIn("jobs/convert/bitonal/", key)

    def test_the_input_url_signs_the_row_own_shard(self):
        _, convert = self._summary(return_value={"pages": 10})

        signed = sorted(call.args[0] for call in convert.call_args_list)
        self.assertEqual(len(signed), 3)
        for index, url in enumerate(signed):
            self.assertIn(f"shards/{index + 1:04d}.pdf", url)

    def test_the_wave_is_capped(self):
        """A claimed row must be genuinely in flight, not pool-queued."""
        summary, convert = self._summary(return_value={"pages": 10}, limit=2)

        self.assertEqual(summary.submitted, 2)
        self.assertEqual(convert.call_count, 2)
        statuses = [job.status for job in convert_jobs(self.scan)]
        self.assertEqual(statuses.count(JobStatus.PENDING), 1)

    def test_the_cap_defaults_to_the_concurrency_setting(self):
        with override_settings(DOCTOR_MAX_CONCURRENCY=1):
            summary, convert = self._summary(return_value={"pages": 10})
        self.assertEqual(summary.submitted, 1)
        self.assertEqual(convert.call_count, 1)

    def test_nothing_is_submitted_when_doctor_is_off(self):
        with override_settings(DOCTOR_ENABLED=False):
            summary, convert = self._summary(return_value={"pages": 10})

        self.assertEqual(summary.submitted, 0)
        convert.assert_not_called()
        self.assertEqual(
            {job.status for job in convert_jobs(self.scan)},
            {JobStatus.PENDING},
        )

    def test_a_terminal_error_fails_the_row_without_retrying(self):
        summary, _ = self._summary(
            side_effect=doctor_client.DoctorError(
                "corrupt", error_code="INVALID_PDF"
            )
        )

        self.assertEqual(summary.failed, 3)
        for job in convert_jobs(self.scan):
            self.assertEqual(job.status, JobStatus.FAILED)
            self.assertEqual(job.error_code, "INVALID_PDF")
            self.assertEqual(job.attempt, 1)

    def test_an_answered_transient_error_retries_at_once(self):
        """Doctor answered, so it will never upload: nothing is wasted."""
        summary, _ = self._summary(
            side_effect=doctor_client.DoctorTransientError(
                "s3 rejected the put", error_code="RESULT_UPLOAD_FAILED"
            )
        )

        self.assertEqual(summary.retried, 3)
        for job in convert_jobs(self.scan):
            self.assertEqual(job.status, JobStatus.PENDING)
            self.assertEqual(job.attempt, 2)
            self.assertEqual(job.retry_count, 1)
            # Cleared so the next attempt mints a fresh key and URLs --
            # which is also how an expired signature heals.
            self.assertEqual(job.result_key, "")
            self.assertEqual(job.provider_meta["attempts"][0]["attempt"], 1)

    def test_an_unanswered_request_is_left_in_flight(self):
        """The conversion may still land, so do not pay for it twice."""
        summary, _ = self._summary(
            side_effect=doctor_client.DoctorTransientError(
                "read timeout",
                error_code=doctor_client.UNANSWERED_ERROR_CODE,
            )
        )

        self.assertEqual(summary.unanswered, 3)
        for job in convert_jobs(self.scan):
            self.assertEqual(job.status, JobStatus.SUBMITTED)
            self.assertEqual(job.attempt, 1)
            self.assertTrue(job.result_key)

    def test_an_unexpected_error_fails_the_row_loudly(self):
        with self.assertLogs("scanning.jobs", level="ERROR"):
            summary, _ = self._summary(side_effect=RuntimeError("boom"))

        self.assertEqual(summary.failed, 3)
        self.assertEqual(
            {job.error_code for job in convert_jobs(self.scan)},
            {"SUBMIT_FAILED"},
        )

    def test_the_row_is_marked_before_its_urls_are_even_signed(self):
        """Otherwise a crash mid-request leaves the result unaddressed.

        Observed at presign time, not inside the request: that runs on a
        worker thread, which cannot see this test's open transaction.
        Presigning happens after the claim and before anything is sent,
        so it is the stricter checkpoint anyway.
        """
        seen = {}

        def _capture(key, ttl):
            row = ExternalJob.objects.get(pk=self.jobs[0].pk)
            seen["status"] = row.status
            seen["result_key"] = row.result_key
            seen["deadline"] = row.deadline
            return "https://s3/in?get"

        with patch("scanning.s3_sync.presign_get", side_effect=_capture):
            with patch(
                "scanning.doctor_client.convert_bitonal",
                return_value={"pages": 10},
            ):
                jobs.submit_pending(limit=1)

        self.assertEqual(seen["status"], JobStatus.SUBMITTED)
        self.assertTrue(seen["result_key"].endswith("r1-s0-a1.pdf"))
        self.assertIsNotNone(seen["deadline"])

    def test_a_presign_failure_retries_without_calling_doctor(self):
        from botocore.exceptions import BotoCoreError

        with patch(
            "scanning.s3_sync.presign_get", side_effect=BotoCoreError()
        ):
            summary, convert = self._summary(return_value={"pages": 10})

        convert.assert_not_called()
        self.assertEqual(summary.retried, 3)
        self.assertEqual(
            {job.error_code for job in convert_jobs(self.scan)},
            {"PRESIGN_FAILED"},
        )

    def test_only_doctor_convert_rows_are_picked_up(self):
        """A future provider's rows must not be submitted to doctor."""
        other = ExternalJobFactory(
            stage=JobStage.DETECT,
            engine=JobEngine.BLACKLETTER,
            provider=JobProvider.RUNPOD,
            status=JobStatus.PENDING,
        )

        summary, _ = self._summary(return_value={"pages": 10})

        other.refresh_from_db()
        self.assertEqual(other.status, JobStatus.PENDING)
        self.assertEqual(summary.submitted, 3)


@override_settings(**DOCTOR)
class TestRetryCeiling(ScanningTestCase):
    """How many times one shard may be attempted."""

    def test_the_last_attempt_fails_instead_of_retrying(self):
        job = ExternalJobFactory(
            stage=JobStage.CONVERT,
            engine=JobEngine.BITONAL,
            provider=JobProvider.DOCTOR,
            status=JobStatus.SUBMITTED,
            attempt=3,
        )

        with self.assertLogs("scanning.jobs", level="ERROR"):
            result = jobs._retry_or_fail(
                job, "DEADLINE_EXCEEDED", "gone", timezone.now()
            )

        job.refresh_from_db()
        self.assertEqual(result, "failed")
        self.assertEqual(job.status, JobStatus.FAILED)
        self.assertEqual(job.attempt, 3)

    def test_attempts_accumulate_history(self):
        job = ExternalJobFactory(
            stage=JobStage.CONVERT,
            engine=JobEngine.BITONAL,
            provider=JobProvider.DOCTOR,
            status=JobStatus.SUBMITTED,
            result_key="first-key.pdf",
        )
        now = timezone.now()

        jobs._retry_or_fail(job, "CODE_ONE", "one", now)
        job.refresh_from_db()
        job.status = JobStatus.SUBMITTED
        job.save(update_fields=["status"])
        jobs._retry_or_fail(job, "CODE_TWO", "two", now)

        job.refresh_from_db()
        self.assertEqual(job.attempt, 3)
        history = job.provider_meta["attempts"]
        self.assertEqual([entry["attempt"] for entry in history], [1, 2])
        self.assertEqual(history[0]["result_key"], "first-key.pdf")


@override_settings(**DOCTOR)
class TestRetryDead(ScanningTestCase):
    """The next tick picking a failed shard back up."""

    def _row(self, **kwargs):
        """Build one bitonal conversion row on doctor.

        :param kwargs: Field overrides.
        :returns: The saved row.
        """
        fields = {
            "scan": ScanFactory(status=Status.AWAITING),
            "stage": JobStage.CONVERT,
            "engine": JobEngine.BITONAL,
            "provider": JobProvider.DOCTOR,
            "status": JobStatus.FAILED,
            "error_code": "CONVERSION_FAILED",
            "error_message": "pdftoppm timed out after 120s on page 1",
            "result_key": "jobs/convert/bitonal/r1-s0-a1.pdf",
            "deadline": timezone.now() - timedelta(days=2),
        }
        fields.update(kwargs)
        return ExternalJobFactory(**fields)

    def test_a_failed_row_goes_back_to_pending(self):
        job = self._row()

        with self.assertLogs("scanning.jobs", level="INFO"):
            self.assertEqual(jobs.retry_dead(), 1)

        job.refresh_from_db()
        self.assertEqual(job.status, JobStatus.PENDING)
        self.assertEqual(job.attempt, 2)
        self.assertEqual(job.retry_count, 1)
        # Cleared so the next submit mints an attempt-scoped key: a late
        # upload from attempt 1 must never be read as attempt 2's output.
        self.assertEqual(job.result_key, "")
        self.assertEqual(job.provider_meta["attempts"][0]["attempt"], 1)

    def test_the_deadline_is_re_stamped_into_the_future(self):
        """Otherwise the same tick's sweep writes the row straight off."""
        job = self._row()

        jobs.retry_dead()

        job.refresh_from_db()
        self.assertGreater(job.deadline, timezone.now())

    def test_a_cancelled_row_is_left_alone(self):
        """A cancel is a person's decision, not a failure to recover."""
        job = self._row(status=JobStatus.CANCELLED, error_code="ABANDONED")

        self.assertEqual(jobs.retry_dead(), 0)

        job.refresh_from_db()
        self.assertEqual(job.status, JobStatus.CANCELLED)

    def test_an_expired_row_is_picked_up(self):
        """Our own lost answer, not a job doctor rejected."""
        job = self._row(status=JobStatus.EXPIRED)

        with self.assertLogs("scanning.jobs", level="INFO"):
            self.assertEqual(jobs.retry_dead(), 1)

        job.refresh_from_db()
        self.assertEqual(job.status, JobStatus.PENDING)

    def test_a_row_out_of_attempts_stays_failed(self):
        """The bound that stops a defective page converting forever."""
        job = self._row(attempt=3)

        self.assertEqual(jobs.retry_dead(), 0)

        job.refresh_from_db()
        self.assertEqual(job.status, JobStatus.FAILED)
        self.assertEqual(job.attempt, 3)

    def test_a_cancelled_scans_failure_is_left_alone(self):
        """``abandon_open`` cannot cancel a row that already failed."""
        job = self._row(scan=ScanFactory(status=Status.CANCELLED))

        self.assertEqual(jobs.retry_dead(), 0)

        job.refresh_from_db()
        self.assertEqual(job.status, JobStatus.FAILED)

    def test_a_scan_that_already_moved_on_is_left_alone(self):
        job = self._row(scan=ScanFactory(status=Status.AWAITING_VALIDATION))

        self.assertEqual(jobs.retry_dead(), 0)

        job.refresh_from_db()
        self.assertEqual(job.status, JobStatus.FAILED)

    def test_an_errored_scan_is_the_recovery_case(self):
        job = self._row(scan=ScanFactory(status=Status.ERROR))

        with self.assertLogs("scanning.jobs", level="INFO"):
            self.assertEqual(jobs.retry_dead(), 1)

        job.refresh_from_db()
        self.assertEqual(job.status, JobStatus.PENDING)

    def test_a_superseded_runs_failure_is_left_alone(self):
        """Its shard set no longer exists, so its key is unsubmittable."""
        scan = ScanFactory(status=Status.AWAITING)
        old_run = self._row(scan=scan, run=1)
        live = self._row(scan=scan, run=2, shard_index=0)

        with self.assertLogs("scanning.jobs", level="INFO"):
            self.assertEqual(jobs.retry_dead(), 1)

        old_run.refresh_from_db()
        live.refresh_from_db()
        self.assertEqual(old_run.status, JobStatus.FAILED)
        self.assertEqual(live.status, JobStatus.PENDING)

    def test_another_providers_rows_are_not_touched(self):
        job = self._row(
            provider=JobProvider.RUNPOD,
            stage=JobStage.DETECT,
            engine=JobEngine.BLACKLETTER,
        )

        self.assertEqual(jobs.retry_dead(), 0)

        job.refresh_from_db()
        self.assertEqual(job.status, JobStatus.FAILED)

    def test_can_retry_reads_the_status_and_the_ceiling(self):
        self.assertTrue(jobs.can_retry(self._row(attempt=2)))
        self.assertFalse(jobs.can_retry(self._row(attempt=3)))
        self.assertFalse(jobs.can_retry(self._row(status=JobStatus.CANCELLED)))
        self.assertFalse(jobs.can_retry(self._row(status=JobStatus.COMPLETED)))


@override_settings(**DOCTOR)
class TestFailureLocation(ScanningTestCase):
    """Naming the volume pages a failed shard covers."""

    def _failed(self, details=None, manifest=None):
        """Fail one row and return it, with its manifest set.

        :param details: Doctor's per-page fields, if any.
        :param manifest: Override for the row's shard identity.
        :returns: The refreshed row.
        """
        job = ExternalJobFactory(
            stage=JobStage.CONVERT,
            engine=JobEngine.BITONAL,
            provider=JobProvider.DOCTOR,
            status=JobStatus.SUBMITTED,
            input_manifest=(
                {"from_page": 700, "to_page": 799, "page_count": 100}
                if manifest is None
                else manifest
            ),
        )
        with self.assertLogs("scanning.jobs", level="ERROR"):
            jobs._fail(job, "CONVERSION_TIMEOUT", "timed out", details)
        job.refresh_from_db()
        return job

    def test_the_shard_page_range_is_recorded(self):
        """1-based, as a viewer shows it; the manifest is 0-based."""
        job = self._failed()
        self.assertIn("volume pages 701-800", job.error_message)

    def test_doctors_page_number_names_the_volume_page(self):
        job = self._failed({"page_number": 1, "pixels": 8_216_000})
        self.assertIn("failed on volume page 701", job.error_message)
        self.assertIn("8216000 pixel(s)", job.error_message)

    def test_a_row_with_no_manifest_says_nothing_extra(self):
        job = self._failed(manifest={})
        self.assertEqual(job.error_message, "timed out")


@override_settings(**DOCTOR)
class TestSweepJobs(ScanningTestCase):
    """The confirm pass: the only path that recovers a lost response."""

    def setUp(self):
        super().setUp()
        self.scan = ScanFactory()
        self.job = jobs.ensure_convert_jobs(
            self.scan, make_manifest(shard_count=1)
        )[0]
        self.job.status = JobStatus.SUBMITTED
        self.job.result_key = "processing/1/jobs/convert/bitonal/r1-s0-a1.pdf"
        self.job.submitted_at = timezone.now()
        self.job.deadline = timezone.now() + timedelta(seconds=900)
        self.job.save()

    def test_a_present_object_completes_a_job_with_no_response(self):
        with patch("scanning.s3_sync.object_exists", return_value=True):
            summary = jobs.sweep_jobs()

        self.job.refresh_from_db()
        self.assertEqual(summary.completed, 1)
        self.assertEqual(self.job.status, JobStatus.COMPLETED)
        self.assertEqual(self.job.provider_meta["confirmed_by"], "s3_head")
        self.assertIsNone(self.job.provider_meta["output"])

    def test_an_absent_object_inside_the_deadline_keeps_waiting(self):
        with patch("scanning.s3_sync.object_exists", return_value=False):
            summary = jobs.sweep_jobs()

        self.job.refresh_from_db()
        self.assertEqual(summary.pending, 1)
        self.assertEqual(self.job.status, JobStatus.SUBMITTED)
        self.assertIsNotNone(self.job.last_polled_at)

    def test_an_absent_object_past_the_deadline_is_retried(self):
        with patch("scanning.s3_sync.object_exists", return_value=False):
            summary = jobs.sweep_jobs(
                now=self.job.deadline + timedelta(seconds=1)
            )

        self.job.refresh_from_db()
        self.assertEqual(summary.retried, 1)
        self.assertEqual(self.job.status, JobStatus.PENDING)
        self.assertEqual(self.job.attempt, 2)
        self.assertEqual(self.job.error_code, "DEADLINE_EXCEEDED")

    def test_an_s3_error_does_not_burn_an_attempt(self):
        """An S3 problem is ours, not evidence the worker produced nothing."""
        from botocore.exceptions import ClientError

        error = ClientError({"Error": {"Code": "AccessDenied"}}, "HeadObject")
        with patch("scanning.s3_sync.object_exists", side_effect=error):
            with self.assertLogs("scanning.jobs", level="WARNING"):
                summary = jobs.sweep_jobs(
                    now=self.job.deadline + timedelta(seconds=1)
                )

        self.job.refresh_from_db()
        self.assertEqual(summary.errors, 1)
        self.assertEqual(self.job.status, JobStatus.SUBMITTED)
        self.assertEqual(self.job.attempt, 1)

    def test_a_never_submitted_row_is_written_off_past_its_queue_ceiling(self):
        """Otherwise its scan waits in AWAITING forever."""
        stranded = jobs.ensure_convert_jobs(
            ScanFactory(), make_manifest(shard_count=1)
        )[0]
        ExternalJob.objects.filter(pk=stranded.pk).update(
            deadline=timezone.now() - timedelta(seconds=1)
        )

        with patch("scanning.s3_sync.object_exists", return_value=False):
            with self.assertLogs("scanning.jobs", level="ERROR"):
                summary = jobs.sweep_jobs()

        stranded.refresh_from_db()
        self.assertEqual(summary.failed, 1)
        self.assertEqual(stranded.status, JobStatus.FAILED)
        self.assertEqual(stranded.error_code, "QUEUE_TIMEOUT")


class TestConcurrentWriters(ScanningTestCase):
    """Two daemon replicas may look at one row; only one may write it."""

    def test_a_write_whose_status_moved_is_dropped(self):
        job = ExternalJobFactory(status=JobStatus.SUBMITTED)
        # Someone else cancelled it while we held this instance.
        ExternalJob.objects.filter(pk=job.pk).update(
            status=JobStatus.CANCELLED
        )

        with self.assertLogs("scanning.jobs", level="INFO"):
            won = jobs._write(job, status=JobStatus.COMPLETED)

        job.refresh_from_db()
        self.assertFalse(won)
        self.assertEqual(job.status, JobStatus.CANCELLED)

    def test_a_won_write_updates_the_instance_too(self):
        """So a caller can chain another compare-and-swap."""
        job = ExternalJobFactory(status=JobStatus.PENDING)

        won = jobs._write(job, status=JobStatus.SUBMITTED)

        self.assertTrue(won)
        self.assertEqual(job.status, JobStatus.SUBMITTED)
        self.assertTrue(jobs._write(job, status=JobStatus.COMPLETED))


class TestAbandonOpen(ScanningTestCase):
    """The re-queue path."""

    def test_open_rows_are_cancelled_and_terminal_ones_left_alone(self):
        scan = ScanFactory()
        open_rows = [
            ExternalJobFactory(
                scan=scan, status=status, shard_index=index, shard_count=4
            )
            for index, status in enumerate(
                [
                    JobStatus.PENDING,
                    JobStatus.SUBMITTED,
                    JobStatus.COMPLETED,
                ]
            )
        ]
        consumed = ExternalJobFactory(
            scan=scan,
            status=JobStatus.CONSUMED,
            shard_index=3,
            shard_count=4,
        )

        cancelled = jobs.abandon_open(scan, "Re-queued from the admin")

        self.assertEqual(cancelled, 3)
        for job in open_rows:
            job.refresh_from_db()
            self.assertEqual(job.status, JobStatus.CANCELLED)
            self.assertEqual(job.error_code, "ABANDONED")
        consumed.refresh_from_db()
        self.assertEqual(consumed.status, JobStatus.CONSUMED)

    def test_another_scans_jobs_are_untouched(self):
        mine, theirs = ScanFactory(), ScanFactory()
        ExternalJobFactory(scan=mine, status=JobStatus.SUBMITTED)
        other = ExternalJobFactory(scan=theirs, status=JobStatus.SUBMITTED)

        jobs.abandon_open(mine, "cancelled")

        other.refresh_from_db()
        self.assertEqual(other.status, JobStatus.SUBMITTED)


class TestRunReuse(ScanningTestCase):
    """Which existing runs can be picked back up (issue #176).

    The re-queue path is why this matters: ``abandon_open`` cancels a
    scan's open rows, and reusing that run parks the scan behind work
    that is over.
    """

    def setUp(self):
        super().setUp()
        self.scan = ScanFactory()
        self.manifest = make_manifest(shard_count=2)
        self.first = jobs.ensure_convert_jobs(self.scan, self.manifest)

    def _statuses(self, *statuses):
        for job, status in zip(self.first, statuses, strict=True):
            ExternalJob.objects.filter(pk=job.pk).update(status=status)

    def test_a_cancelled_run_is_replaced_not_reused(self):
        jobs.abandon_open(self.scan, "Re-queued from the admin")

        second = jobs.ensure_convert_jobs(self.scan, self.manifest)

        self.assertEqual({job.run for job in second}, {2})
        self.assertEqual({job.status for job in second}, {JobStatus.PENDING})

    def test_one_dead_shard_replaces_the_whole_run(self):
        """A part-failed run can neither finish nor be merged."""
        self._statuses(JobStatus.COMPLETED, JobStatus.FAILED)

        second = jobs.ensure_convert_jobs(self.scan, self.manifest)

        self.assertEqual({job.run for job in second}, {2})

    def test_a_run_in_flight_is_reused(self):
        """Or a re-queue would pay for a conversion already running."""
        self._statuses(JobStatus.SUBMITTED, JobStatus.PENDING)

        second = jobs.ensure_convert_jobs(self.scan, self.manifest)

        self.assertEqual([j.pk for j in second], [j.pk for j in self.first])

    def test_a_consumed_run_is_reused_so_it_is_not_redone(self):
        """The pipeline reads this as "already converted".

        A new run would re-convert an already merged volume -- and its
        results are deleted, so there is nothing to re-merge either.
        """
        self._statuses(JobStatus.CONSUMED, JobStatus.CONSUMED)

        second = jobs.ensure_convert_jobs(self.scan, self.manifest)

        self.assertEqual([j.pk for j in second], [j.pk for j in self.first])
        self.assertEqual({job.status for job in second}, {JobStatus.CONSUMED})


class TestReshardDetection(ScanningTestCase):
    """A re-cut volume must not be converted with the old page ranges.

    Shard keys are positional (``0001.pdf``), so they say nothing about
    which pages a shard holds: the same count over different ranges
    gives identical keys. Detection compares the stored page ranges.
    """

    def test_same_count_different_ranges_starts_a_new_run(self):
        scan = ScanFactory()
        jobs.ensure_convert_jobs(
            scan, make_manifest(shard_count=3, pages_per_shard=100)
        )

        # A page was deleted, so ensure_shards re-cut 300 pages into
        # 100/100/99 -- same three names, different ranges.
        recut = make_manifest(shard_count=3, pages_per_shard=100)
        recut["shards"][2]["to_page"] = 298
        recut["shards"][2]["page_count"] = 99
        recut["source"]["page_count"] = 299
        second = jobs.ensure_convert_jobs(scan, recut)

        self.assertEqual({job.run for job in second}, {2})
        self.assertEqual(
            [job.input_manifest["page_count"] for job in second],
            [100, 100, 99],
        )

    def test_an_unchanged_shard_set_is_still_reused(self):
        scan = ScanFactory()
        manifest = make_manifest(shard_count=3, pages_per_shard=100)

        first = jobs.ensure_convert_jobs(scan, manifest)
        second = jobs.ensure_convert_jobs(
            scan, make_manifest(shard_count=3, pages_per_shard=100)
        )

        self.assertEqual([j.pk for j in first], [j.pk for j in second])


@override_settings(**DOCTOR)
class TestConcurrencyCap(ScanningTestCase):
    """The cap counts work doctor is already doing, not rows we claim.

    An unanswered request leaves a row SUBMITTED while doctor keeps
    converting, so a fresh full wave every tick would grow our load on
    it without bound exactly when it is slow.
    """

    def setUp(self):
        super().setUp()
        self.scan = ScanFactory()
        self.jobs = jobs.ensure_convert_jobs(
            self.scan, make_manifest(shard_count=4)
        )
        patcher = patch.multiple(
            "scanning.s3_sync",
            s3_active=lambda: True,
            presign_get=lambda key, ttl: "https://s3/in",
            presign_put=lambda key, ct, ttl: "https://s3/out",
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _mark_in_flight(self, count):
        for job in self.jobs[:count]:
            ExternalJob.objects.filter(pk=job.pk).update(
                status=JobStatus.SUBMITTED
            )

    def test_in_flight_rows_take_up_the_cap(self):
        self._mark_in_flight(2)

        with patch(
            "scanning.doctor_client.convert_bitonal",
            return_value={"pages": 10},
        ) as convert:
            summary = jobs.submit_pending(limit=3)

        self.assertEqual(convert.call_count, 1)
        self.assertEqual(summary.submitted, 1)

    def test_a_full_cap_submits_nothing(self):
        self._mark_in_flight(4)
        ExternalJob.objects.filter(pk=self.jobs[3].pk).update(
            status=JobStatus.PENDING
        )

        with patch("scanning.doctor_client.convert_bitonal") as convert:
            with self.assertLogs("scanning.jobs", level="INFO"):
                summary = jobs.submit_pending(limit=3)

        convert.assert_not_called()
        self.assertEqual(summary.submitted, 0)


@override_settings(**DOCTOR)
class TestSubmitNeedsS3(ScanningTestCase):
    """Doctor fetches its input from S3, so the shards must be there."""

    def test_nothing_is_submitted_when_s3_is_inactive(self):
        """Otherwise every request 404s on its input and burns a budget.

        One loud failure beats three attempts per shard reporting a
        download error that points at doctor, not at the missing upload.
        """
        scan = ScanFactory()
        jobs.ensure_convert_jobs(scan, make_manifest(shard_count=2))

        with patch("scanning.s3_sync.s3_active", return_value=False):
            with patch("scanning.doctor_client.convert_bitonal") as convert:
                with self.assertLogs("scanning.jobs", level="ERROR"):
                    summary = jobs.submit_pending()

        convert.assert_not_called()
        self.assertEqual(summary.submitted, 0)
        self.assertEqual(
            {job.status for job in convert_jobs(scan)}, {JobStatus.PENDING}
        )


@override_settings(**DOCTOR)
class TestDurationLogging(ScanningTestCase):
    """Timings readable from the logs, not only from SQL.

    The point of the stage is that it is faster than the in-process pass
    it replaced, so the numbers that prove it should not need a query to
    recover.
    """

    def test_a_completed_shard_logs_its_duration(self):
        job = ExternalJobFactory(
            stage=JobStage.CONVERT,
            engine=JobEngine.BITONAL,
            provider=JobProvider.DOCTOR,
            status=JobStatus.SUBMITTED,
            shard_index=0,
            shard_count=4,
        )
        submitted = timezone.now() - timedelta(seconds=42)
        ExternalJob.objects.filter(pk=job.pk).update(submitted_at=submitted)
        job.submitted_at = submitted

        with self.assertLogs("scanning.jobs", level="INFO") as logs:
            jobs._complete(
                job, {"duration_ms": 31_200, "pages": 100}, timezone.now()
            )

        line = "\n".join(logs.output)
        self.assertIn("shard 1/4", line)
        self.assertIn("completed in 42.0s", line)
        # Ours versus doctor's own clock: the gap is queue and transport.
        self.assertIn("doctor 31.2s", line)
        self.assertIn("100 page(s)", line)
        self.assertIn("confirmed by response", line)

    def test_a_shard_confirmed_by_s3_still_logs_a_duration(self):
        """No response, so no doctor timing -- but ours is still known."""
        job = ExternalJobFactory(
            stage=JobStage.CONVERT,
            engine=JobEngine.BITONAL,
            provider=JobProvider.DOCTOR,
            status=JobStatus.SUBMITTED,
        )
        submitted = timezone.now() - timedelta(seconds=10)
        ExternalJob.objects.filter(pk=job.pk).update(submitted_at=submitted)
        job.submitted_at = submitted

        with self.assertLogs("scanning.jobs", level="INFO") as logs:
            jobs._complete(job, None, timezone.now())

        line = "\n".join(logs.output)
        self.assertIn("completed in 10.0s", line)
        self.assertIn("confirmed by s3_head", line)
        self.assertNotIn("doctor", line)

    def test_nothing_is_logged_when_the_write_loses_the_race(self):
        job = ExternalJobFactory(status=JobStatus.SUBMITTED)
        ExternalJob.objects.filter(pk=job.pk).update(
            status=JobStatus.CANCELLED
        )

        with self.assertLogs("scanning.jobs", level="INFO") as logs:
            self.assertFalse(jobs._complete(job, None, timezone.now()))

        self.assertNotIn("completed in", "\n".join(logs.output))
